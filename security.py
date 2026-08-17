#!/usr/bin/env python3
"""Hardening policy for the local web server. Stdlib only.

The threat this guards against is not a stranger on the internet — the server
binds to loopback — but everything else a machine is exposed to:

* Any web page the user visits can POST to http://127.0.0.1:8000. Without a
  check it would spend their OpenRouter credit and write to their history.
  Cross-site requests always carry an Origin header, so rejecting foreign
  origins stops that.
* DNS rebinding points an attacker-controlled name at 127.0.0.1; the Host
  header still says evil.example, so pinning Host closes it.
* Binding to 0.0.0.0 hands an unauthenticated API key to the whole network,
  which is why that requires a token.
"""

import collections
import hmac
import os
import secrets
import stat
import sys
import threading
import time

UI_TOKEN = os.environ.get("OPENROUTER_UI_TOKEN", "").strip()
COOKIE_NAME = "chatbox_session"

# Nama domain yang dilayani lewat reverse proxy, selain loopback. Browser
# mengirim "Host: mbahgpt.com" sementara server tetap bind ke 127.0.0.1, jadi
# tanpa daftar ini penjaga rebinding menolak setiap permintaan dengan 400.
# Daftar putih eksplisit, bukan "terima semua Host": nama yang tidak ada di sini
# tetap ditolak, dan Origin lintas situs tetap gagal seperti sebelumnya.
PUBLIC_HOSTS = frozenset(
    h.strip().lower() for h in
    os.environ.get("OPENROUTER_PUBLIC_HOST", "").split(",") if h.strip())

# Di belakang proxy, setiap koneksi datang dari 127.0.0.1 — rate limiter per
# klien akan runtuh jadi satu keranjang bersama, dan cookie tidak akan pernah
# ditandai Secure. X-Forwarded-* hanya dipercaya kalau ini dinyalakan DAN peer
# memang loopback; dari klien langsung, header itu dikendalikan penyerang.
TRUST_PROXY = os.environ.get("OPENROUTER_TRUST_PROXY", "").strip().lower() in (
    "1", "true", "yes", "on")

# Bodies are small JSON messages; anything larger is a mistake or an attack.
MAX_BODY_BYTES = int(os.environ.get("OPENROUTER_MAX_BODY", str(256 * 1024)))

# Chat requests per minute per client. Each one can cost real money, so the
# ceiling protects the wallet as much as the process.
RATE_LIMIT = int(os.environ.get("OPENROUTER_RATE_LIMIT", "30"))

LOOPBACK = {"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1"}


def is_loopback(host):
    return host in LOOPBACK


def strip_port(hostport):
    """Host without its port, handling [::1]:8000 as well as host:8000."""
    if not hostport:
        return ""
    if hostport.startswith("["):
        return hostport[1:hostport.find("]")] if "]" in hostport else hostport
    return hostport.rsplit(":", 1)[0] if ":" in hostport else hostport


def host_allowed(host_header, bind_host):
    """Reject Host headers we never bound to (DNS rebinding)."""
    host = strip_port(host_header).lower()
    if not host:
        return False
    return (is_loopback(host) or host == bind_host.lower()
            or host in PUBLIC_HOSTS)


def client_ip(peer, forwarded_for):
    """Alamat klien sesungguhnya, hanya kalau proxy layak dipercaya."""
    if not (TRUST_PROXY and is_loopback(peer)):
        return peer
    # Proxy menambah alamatnya di ujung kanan; yang paling kiri adalah klien.
    first = (forwarded_for or "").split(",")[0].strip()
    return first or peer


def is_secure(peer, forwarded_proto):
    """True kalau permintaan tiba lewat HTTPS di sisi proxy."""
    if not (TRUST_PROXY and is_loopback(peer)):
        return False
    return (forwarded_proto or "").strip().lower() == "https"


def origin_allowed(origin, bind_host):
    """Absent Origin means a non-browser client; a foreign one means CSRF."""
    if not origin:
        return True
    if origin == "null":
        return False
    without_scheme = origin.split("://", 1)[-1]
    return host_allowed(without_scheme, bind_host)


def token_matches(supplied):
    if not UI_TOKEN:
        return True
    return bool(supplied) and hmac.compare_digest(str(supplied), UI_TOKEN)


def cookie_value(cookie_header, name=COOKIE_NAME):
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def new_nonce():
    return secrets.token_urlsafe(16)


def csp(nonce):
    """Everything still comes from this origin only.

    'self' is needed alongside the nonce because Ionic lazy-loads its component
    chunks with dynamic import(), and a nonce does not carry to those. Ionic's
    runtime stamps the same nonce onto the styles it injects (setNonce), so
    'unsafe-inline' stays out of the policy.
    """
    return (
        "default-src 'none'; "
        "script-src 'self' 'nonce-{n}'; "
        "style-src 'self' 'nonce-{n}'; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ).format(n=nonce)


BASE_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
]


class RateLimiter:
    """Sliding window per client address."""

    def __init__(self, limit, window=60):
        self.limit = limit
        self.window = window
        self.hits = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def allow(self, key):
        if self.limit <= 0:
            return True
        now = time.time()
        with self.lock:
            seen = self.hits[key]
            while seen and now - seen[0] > self.window:
                seen.popleft()
            if len(seen) >= self.limit:
                return False
            seen.append(now)
            if len(self.hits) > 1024:            # bound memory
                for stale in [k for k, v in self.hits.items() if not v][:512]:
                    del self.hits[stale]
            return True


def protect_file(path):
    """Make a secrets file owner-only, and say so if it was not."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        try:
            os.chmod(path, 0o600)
            print("note: tightened permissions on %s to 0600" % path,
                  file=sys.stderr)
        except OSError as e:
            print("warning: %s is readable by other users (%s)" % (path, e),
                  file=sys.stderr)


def startup_check(bind_host):
    """Refuse configurations that would expose the API key to a network."""
    # Bind loopback di belakang proxy tetap terbuka ke internet lewat nama
    # domainnya, jadi syarat tokennya sama saja dengan bind non-loopback.
    if PUBLIC_HOSTS and not UI_TOKEN:
        sys.exit(
            "refusing to serve %s without authentication.\n"
            "OPENROUTER_PUBLIC_HOST makes this server reachable by name through "
            "a reverse proxy; anyone who resolves it could spend your "
            "OpenRouter credit and read your chat history.\n"
            "Set OPENROUTER_UI_TOKEN=<secret>, or unset OPENROUTER_PUBLIC_HOST."
            % ", ".join(sorted(PUBLIC_HOSTS)))
    if is_loopback(bind_host):
        return
    if not UI_TOKEN:
        sys.exit(
            "refusing to bind %s without authentication.\n"
            "Anyone who can reach this port could spend your OpenRouter credit "
            "and read your chat history.\n"
            "Set OPENROUTER_UI_TOKEN=<secret> to enable access control, or bind "
            "to 127.0.0.1." % bind_host)
