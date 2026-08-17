#!/usr/bin/env python3
"""MbahGPT — local web UI for the OpenRouter Qwen runner. Stdlib only.

    ./server.py            # then open http://127.0.0.1:8000

Credentials come from the .env file next to this script (see qwen.load_env).
The API key never reaches the browser: the page talks to this server, and this
server talks to OpenRouter.

Conversations live in SQLite (see db.py), so history survives a reload and
several sessions can be kept side by side.
"""

import argparse
import base64
import binascii
import json
import os
import re
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import memory
import security
import tools
import web
from qwen import DEFAULT_MODEL, DEFAULT_TEMPERATURE, HERE, request

# Upstream calls must not pin a worker thread forever if OpenRouter stalls.
CONNECT_TIMEOUT = float(os.environ.get("OPENROUTER_TIMEOUT", "120"))

# Every turn resends the whole conversation, so an old thread would grow more
# expensive and slower without end. The opening message is always kept — it is
# what anchors follow-ups like "final ucl".
MAX_HISTORY = int(os.environ.get("OPENROUTER_MAX_HISTORY", "40"))

RATE = security.RateLimiter(security.RATE_LIMIT)

# One reply per session at a time. The browser disables its composer too, but
# a second tab or a stray script must not interleave two answers into the same
# conversation.
_busy_sessions = set()
_busy_lock = threading.Lock()


class Generation:
    """A reply being produced, independent of any browser connection.

    The model keeps writing here even if the page reloads; clients attach and
    replay what has arrived so far, then follow along live.
    """

    def __init__(self):
        self.chunks = []          # raw SSE lines, replayable in order
        self.done = False
        self.cond = threading.Condition()

    def emit(self, raw):
        with self.cond:
            self.chunks.append(raw)
            self.cond.notify_all()

    def finish(self):
        with self.cond:
            self.done = True
            self.cond.notify_all()


GENERATIONS = {}                  # session_id -> Generation
GEN_LOCK = threading.Lock()


def register_generation(session_id, gen):
    with GEN_LOCK:
        GENERATIONS[session_id] = gen


def drop_generation(session_id):
    with GEN_LOCK:
        GENERATIONS.pop(session_id, None)


def get_generation(session_id):
    with GEN_LOCK:
        return GENERATIONS.get(session_id)


def claim_session(session_id):
    with _busy_lock:
        if session_id in _busy_sessions:
            return False
        _busy_sessions.add(session_id)
        return True


def release_session(session_id):
    with _busy_lock:
        _busy_sessions.discard(session_id)

BIND_HOST = "127.0.0.1"          # set from main() before serving

SESSION_RE = re.compile(r"^/api/sessions/(\d+)$")
STREAM_RE = re.compile(r"^/api/stream/(\d+)$")
MEMORY_RE = re.compile(r"^/api/memories/(\d+)$")
ATTACH_RE = re.compile(r"^/api/attachments/(\d+)$")
INSTRUCTIONS_KEY = "response_instructions"

# Measured: OpenRouter bills a flat ~$0.007 per search whatever the result
# count, so a smaller cap saves nothing but grounding. The lever that controls
# spend is how *often* we search — that is what "auto" mode is for.
WEB_RESULTS = int(os.environ.get("OPENROUTER_WEB_RESULTS", "5"))

# Attachments make a chat request far larger than a plain message, so they get
# their own ceiling instead of loosening the one that guards every endpoint.
MAX_UPLOAD = int(os.environ.get("OPENROUTER_MAX_UPLOAD", str(8 * 1024 * 1024)))
MAX_FILES = int(os.environ.get("OPENROUTER_MAX_FILES", "6"))

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
# Text-ish documents are inlined as text: free, exact, and no plugin involved.
TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json",
              "text/html", "text/xml", "application/xml", "text/x-python",
              "application/javascript", "text/javascript", "text/css"}
TEXT_SUFFIXES = (".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".ts",
                 ".html", ".css", ".yml", ".yaml", ".xml", ".sql", ".sh", ".ini")
PDF_TYPE = "application/pdf"


# A terse follow-up ("final ucl") means the previous turn's subject and
# timeframe still apply. Telling the model the resolved question outright works
# far better than asking it to "use context" — with only a vague hint it keeps
# answering generically and hedging with background.
FOLLOWUP_HINT = (
    "The user's latest message is a short follow-up. In the context of this "
    "conversation it means: \"{resolved}\".\n"
    "Answer exactly that, directly and specifically. Do not broaden it into a "
    "general overview and do not restate background they already have."
)


MIME_BY_SUFFIX = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".pdf": PDF_TYPE,
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".json": "application/json", ".html": "text/html", ".xml": "text/xml",
    ".py": "text/x-python", ".js": "text/javascript", ".css": "text/css",
}


def guess_mime(name):
    return MIME_BY_SUFFIX.get(os.path.splitext(name or "")[1].lower(),
                              "application/octet-stream")


def classify(name, mime):
    """Decide how a file will reach the model, or why it cannot."""
    lower = (name or "").lower()
    if mime in IMAGE_TYPES:
        return "image"
    if mime == PDF_TYPE or lower.endswith(".pdf"):
        return "pdf"
    if mime in TEXT_TYPES or lower.endswith(TEXT_SUFFIXES) or mime.startswith("text/"):
        return "text"
    return None


def decode_upload(item):
    """Turn one posted file into (name, mime, kind, bytes) or raise ValueError."""
    name = str(item.get("name") or "berkas")[:120]
    mime = str(item.get("mime") or "").split(";")[0].strip().lower()
    raw = item.get("data") or ""
    if "," in raw and raw.startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("berkas %s tidak terbaca" % name)
    kind = classify(name, mime)
    if kind is None:
        raise ValueError("jenis berkas %s tidak didukung" % (mime or name))
    if not blob:
        raise ValueError("berkas %s kosong" % name)
    return name, mime, kind, blob


def sse(obj):
    """Our own event, in the same wire shape as OpenRouter's lines."""
    return ("data: " + json.dumps(obj) + "\n\n").encode()


MAX_TEXT_FILE = 80000     # characters of an inlined document


def data_uri(mime, blob):
    return "data:%s;base64,%s" % (mime or "application/octet-stream",
                                  base64.b64encode(blob).decode())


def build_messages(session_id):
    """History shaped for OpenRouter, with attachments folded in.

    Returns (messages, needs_pdf_plugin). Images ride as image_url parts, PDFs
    as file parts, and text documents are inlined — the last of those costs
    nothing extra and keeps the content exact.
    """
    out, needs_pdf = [], False
    for m in db.get_messages(session_id):
        text = m["content"]
        parts = []
        if m["role"] == "user" and m.get("attachments"):
            for att in db.attachments_for_message(m["id"]):
                kind = classify(att["name"], att["mime"])
                if kind == "image":
                    parts.append({"type": "image_url",
                                  "image_url": {"url": data_uri(att["mime"], att["data"])}})
                elif kind == "pdf":
                    needs_pdf = True
                    parts.append({"type": "file",
                                  "file": {"filename": att["name"],
                                           "file_data": data_uri(PDF_TYPE, att["data"])}})
                else:
                    body = att["data"].decode("utf-8", "replace")[:MAX_TEXT_FILE]
                    text += "\n\n--- isi berkas: %s ---\n%s" % (att["name"], body)
        if parts:
            parts.insert(0, {"type": "text", "text": text or "(lihat lampiran)"})
            out.append({"role": m["role"], "content": parts})
        elif text:
            out.append({"role": m["role"], "content": text})
    return out, needs_pdf


def trim_history(messages):
    """Bound what is resent each turn, keeping the opening message as anchor."""
    if MAX_HISTORY <= 0 or len(messages) <= MAX_HISTORY:
        return messages
    return messages[:1] + messages[-(MAX_HISTORY - 1):]


class UpstreamError(Exception):
    """OpenRouter refused the chat request."""

    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers -----------------------------------------------------------
    def base_headers(self, extra=()):
        for name, value in security.BASE_HEADERS:
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)

    def send_bytes(self, body, content_type, status=200, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.base_headers(extra)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json", status)

    def read_json(self, limit=None):
        """Parse the JSON body, refusing anything oversized or malformed."""
        limit = limit or security.MAX_BODY_BYTES
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return None
        if length < 0 or length > limit:
            self.oversized = True
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return None

    # -- request gate ------------------------------------------------------
    def preflight(self):
        """Common checks for every request. False means a reply was sent."""
        self.oversized = False

        if not security.host_allowed(self.headers.get("Host"), BIND_HOST):
            self.send_json({"error": "host not allowed"}, 400)
            return False
        if not security.origin_allowed(self.headers.get("Origin"), BIND_HOST):
            self.send_json({"error": "cross-origin request refused"}, 403)
            return False

        if security.UI_TOKEN and not self.authenticated():
            self.send_json({"error": "authentication required"}, 401)
            return False
        return True

    def authenticated(self):
        cookie = security.cookie_value(self.headers.get("Cookie"))
        if security.token_matches(cookie):
            return True
        if security.token_matches(self.headers.get("X-Auth-Token")):
            return True   # header form, for scripts and curl
        # First visit: ?token=… in the link, which serve_page turns into a cookie.
        query = urllib.parse.urlparse(self.path).query
        supplied = urllib.parse.parse_qs(query).get("token", [""])[0]
        return bool(supplied) and security.token_matches(supplied)

    def safely(self, handler):
        """Never let an unexpected error kill the connection silently."""
        try:
            if self.preflight():
                handler()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"error": "internal server error"}, 500)
            except Exception:
                pass

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        self.safely(self.route_get)

    def do_POST(self):
        self.safely(self.route_post)

    def do_PUT(self):
        self.safely(self.route_put)

    def do_PATCH(self):
        self.safely(self.route_patch)

    def do_DELETE(self):
        self.safely(self.route_delete)

    def serve_page(self):
        """Serve index.html with a per-response CSP nonce on its inline tags."""
        with open(os.path.join(HERE, "index.html"), "r", encoding="utf-8") as f:
            page = f.read()
        nonce = security.new_nonce()
        # Every inline tag needs the nonce: the page has one <style>, Ionic's
        # loader module, and the app script.
        page = page.replace("<style>", '<style nonce="%s">' % nonce)
        page = page.replace("<script>", '<script nonce="%s">' % nonce)
        page = page.replace('<script type="module">',
                            '<script type="module" nonce="%s">' % nonce)
        extra = [("Content-Security-Policy", security.csp(nonce))]

        # A token supplied once in the URL becomes a SameSite cookie, so the
        # secret stops travelling in links and history.
        query = urllib.parse.urlparse(self.path).query
        supplied = urllib.parse.parse_qs(query).get("token", [""])[0]
        if security.UI_TOKEN and supplied and security.token_matches(supplied):
            extra.append(("Set-Cookie",
                          "%s=%s; Path=/; HttpOnly; SameSite=Strict"
                          % (security.COOKIE_NAME, supplied)))
        self.send_bytes(page.encode(), "text/html; charset=utf-8", extra=extra)

    def serve_vendor(self, path):
        """Serve the vendored Ionic assets, and nothing outside that folder."""
        root = os.path.realpath(os.path.join(HERE, "vendor"))
        target = os.path.realpath(os.path.join(HERE, path.lstrip("/")))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            self.send_json({"error": "not found"}, 404)
            return
        kind = {".js": "text/javascript", ".css": "text/css",
                ".woff2": "font/woff2", ".svg": "image/svg+xml",
                ".map": "application/json"}.get(os.path.splitext(target)[1])
        if not kind:
            self.send_json({"error": "not found"}, 404)
            return
        with open(target, "rb") as f:
            body = f.read()
        # Hashed chunk names never change contents, so let the browser keep them.
        self.send_bytes(body, kind + "; charset=utf-8",
                        extra=[("Cache-Control", "public, max-age=604800")])

    def route_get(self):
        path = urllib.parse.urlparse(self.path).path
        match = SESSION_RE.match(path)
        if path in ("/", "/index.html"):
            self.serve_page()
        elif path.startswith("/vendor/"):
            self.serve_vendor(path)
        elif path == "/api/config":
            # Model and sampling live in .env, not in the UI; the page only
            # displays them.
            self.send_json({
                "model": DEFAULT_MODEL,
                "temperature": DEFAULT_TEMPERATURE,
                "search_model": web.SEARCH_MODEL,
                "web_results": WEB_RESULTS,
                "max_files": MAX_FILES,
                "max_upload": MAX_UPLOAD,
                "tools_enabled": tools.ENABLED,
            })
        elif path == "/api/sessions":
            sessions = db.list_sessions()
            for row in sessions:                 # so a reload can re-attach
                row["streaming"] = get_generation(row["id"]) is not None
            self.send_json({"sessions": sessions})
        elif path == "/api/prefs":
            self.send_json({"response_instructions": db.get_pref(INSTRUCTIONS_KEY)})
        elif path == "/api/memories":
            self.send_json({"memories": db.list_memories()})
        elif ATTACH_RE.match(path):
            self.serve_attachment(int(ATTACH_RE.match(path).group(1)))
        elif STREAM_RE.match(path):
            self.attach_stream(int(STREAM_RE.match(path).group(1)))
        elif match:
            self.get_session(int(match.group(1)))
        else:
            self.send_json({"error": "not found"}, 404)

    def route_post(self):
        if self.path == "/api/chat":
            self.post_chat()
        elif self.path == "/api/sessions":
            self.read_json()          # consume the body; keep-alive needs it
            sid = db.create_session(DEFAULT_MODEL)
            self.send_json({"session": db.get_session(sid)}, 201)
        elif self.path == "/api/memories":
            body = self.read_json() or {}
            text = (body.get("text") or "").strip()
            if not text:
                self.send_json({"error": "text is required"}, 400)
                return
            created = db.add_memory(text, pinned=body.get("pinned"))
            self.send_json({"created": created, "memories": db.list_memories()},
                           201 if created else 200)
        else:
            self.send_json({"error": "not found"}, 404)

    def route_put(self):
        if self.path != "/api/prefs":
            self.send_json({"error": "not found"}, 404)
            return
        body = self.read_json()
        if body is None:
            self.send_json({"error": "invalid JSON"}, 400)
            return
        db.set_pref(INSTRUCTIONS_KEY, str(body.get("response_instructions", "")))
        self.send_json({"response_instructions": db.get_pref(INSTRUCTIONS_KEY)})

    def route_patch(self):
        mem = MEMORY_RE.match(self.path)
        if mem:
            body = self.read_json() or {}
            ok = db.update_memory(int(mem.group(1)), body.get("text"),
                                  body.get("pinned"))
            self.send_json({"memories": db.list_memories()} if ok
                           else {"error": "no such memory"}, 200 if ok else 404)
            return

        match = SESSION_RE.match(self.path)
        if not match:
            self.send_json({"error": "not found"}, 404)
            return
        body = self.read_json()
        if body is None or not str(body.get("title", "")).strip():
            self.send_json({"error": "title is required"}, 400)
            return
        sid = int(match.group(1))
        if not db.get_session(sid):
            self.send_json({"error": "no such session"}, 404)
            return
        db.rename_session(sid, str(body["title"]))
        self.send_json({"session": db.get_session(sid)})

    def route_delete(self):
        mem = MEMORY_RE.match(self.path)
        if mem:
            if db.delete_memory(int(mem.group(1))):
                self.send_json({"memories": db.list_memories()})
            else:
                self.send_json({"error": "no such memory"}, 404)
            return

        match = SESSION_RE.match(self.path)
        if not match:
            self.send_json({"error": "not found"}, 404)
            return
        if db.delete_session(int(match.group(1))):
            self.send_json({"deleted": True})
        else:
            self.send_json({"error": "no such session"}, 404)

    def attach_stream(self, session_id):
        """Follow a reply that is already being produced for this session."""
        gen = get_generation(session_id)
        if gen is None:
            self.send_json({"error": "no active generation"}, 404)
            return
        self.begin_stream(session_id)
        self.relay(gen)

    def serve_attachment(self, attachment_id):
        att = db.get_attachment(attachment_id)
        if not att:
            self.send_json({"error": "not found"}, 404)
            return
        # Never let a stored file be interpreted as a document by the browser:
        # images render, everything else downloads.
        inline = att["mime"] in IMAGE_TYPES
        disposition = "inline" if inline else "attachment"
        name = re.sub(r'[^\w.\- ]', '_', att["name"])
        self.send_bytes(
            att["data"],
            att["mime"] if inline else "application/octet-stream",
            extra=[("Content-Disposition", '%s; filename="%s"' % (disposition, name)),
                   ("Cache-Control", "private, max-age=3600")])

    def get_session(self, session_id):
        session = db.get_session(session_id)
        if not session:
            self.send_json({"error": "no such session"}, 404)
            return
        self.send_json({"session": session, "messages": db.get_messages(session_id)})

    def post_chat(self):
        """Append one user message to a session and stream the model's reply.

        The browser sends only the new message; context is rebuilt from SQLite,
        and the reply is written back there when the stream ends.
        """
        if not RATE.allow(self.client_address[0]):
            self.send_json({"error": "too many requests, slow down"}, 429)
            return

        body = self.read_json(MAX_UPLOAD)
        if body is None:
            if getattr(self, "oversized", False):
                self.send_json({"error": "lampiran terlalu besar"}, 413)
            else:
                self.send_json({"error": "invalid JSON"}, 400)
            return

        content = (body.get("content") or "").strip()
        session_id = body.get("session_id")
        model = DEFAULT_MODEL   # configured in .env, never chosen by the client

        # "/web …" forces a search for this message; otherwise the mode decides,
        # with "auto" falling back to keyword detection.
        content, forced = web.strip_prefix(content)
        mode = body.get("web", "auto")

        uploads = body.get("attachments") or []
        if not content and not uploads:
            self.send_json({"error": "content is required"}, 400)
            return
        if len(uploads) > MAX_FILES:
            self.send_json({"error": "maksimal %d berkas per pesan" % MAX_FILES}, 400)
            return
        try:
            files = [decode_upload(u) for u in uploads]
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
            return

        if session_id is None:
            session_id = db.create_session(model)
        elif not db.get_session(session_id):
            self.send_json({"error": "no such session"}, 404)
            return

        if not claim_session(session_id):
            self.send_json(
                {"error": "chat ini sedang menjawab; tunggu sampai selesai"}, 409)
            return
        try:
            self.run_chat(session_id, content, model, body, forced, mode, files)
        finally:
            release_session(session_id)

    def run_chat(self, session_id, content, model, body, forced, mode, files):

        # Earlier turns decide two things: whether a bare follow-up still needs
        # the web, and what the search query should actually say.
        history = db.get_messages(session_id)

        # A retry re-sends a message that already failed. If the failure came
        # after we stored it, storing it again would duplicate the turn — so
        # reuse the stored one and just regenerate the reply.
        already_stored = (body.get("retry") and history
                          and history[-1]["role"] == "user"
                          and history[-1]["content"] == content)
        prior = history[:-1] if already_stored else history

        earlier_user = [m["content"] for m in prior if m["role"] == "user"]
        after_search = any(m["role"] == "assistant" and m.get("sources")
                           for m in prior[-2:])
        searching = forced or (mode == "on") or (
            mode == "auto" and web.should_search(content, after_search))

        if already_stored:
            user_message_id = history[-1]["id"]
        else:
            user_message_id = db.add_message(session_id, "user", content)
            db.autotitle(session_id, content)
            for name, mime, kind, blob in files:
                db.add_attachment(user_message_id, name, mime,
                                  "image" if kind == "image" else "document", blob)

        # "remember this: …" and friends are captured before the model sees the
        # message, so the fact is available from this turn onward.
        captured = [t for t in memory.extract(content)
                    if db.add_memory(t, session_id)]

        instructions = db.get_pref(INSTRUCTIONS_KEY)
        selected = memory.rank(db.list_memories(), content)
        system = memory.system_prompt(instructions, selected)

        resolved = web.expand_query(content, earlier_user)
        if earlier_user and resolved != content:
            hint = FOLLOWUP_HINT.format(resolved=resolved)
            system = (system + "\n\n" + hint) if system else hint

        # The reply is produced by a worker so it survives a page reload; this
        # connection only watches the buffer.
        gen = Generation()
        register_generation(session_id, gen)
        threading.Thread(
            target=self.produce,
            args=(gen, session_id, model, resolved, system, searching,
                  len(captured), user_message_id),
            daemon=True,
        ).start()

        self.begin_stream(session_id)
        self.relay(gen)

    def produce(self, gen, session_id, model, resolved, system, searching,
                captured, user_message_id):
        """Run one reply to completion, whatever the browser is doing."""
        found = []
        try:
            gen.emit(sse({"status": {"searching": searching,
                                     "memories_saved": captured}}))
            if searching:
                digest = ""
                try:
                    digest, found = web.run_search(resolved, WEB_RESULTS)
                except Exception as e:                   # search must not be fatal
                    gen.emit(sse({"web_error": str(e)[:200]}))
                gen.emit(sse({"sources": found, "query": resolved}))
                if digest:
                    block = web.context_block(resolved, digest, found)
                    system = (system + "\n\n" + block) if system else block

            messages, needs_pdf = build_messages(session_id)
            messages = trim_history(messages)
            if system:
                messages.insert(0, {"role": "system", "content": system})

            reply, reasoning, citations = [], [], []
            work, produced = None, []

            # Each round is one model turn. If it asks for tools we run them,
            # append the results, and let it speak again.
            for round_no in range(tools.MAX_ROUNDS + 1):
                offer_tools = tools.ENABLED and round_no < tools.MAX_ROUNDS
                try:
                    upstream = self.open_upstream(model, messages, needs_pdf,
                                                  offer_tools)
                except UpstreamError as e:
                    gen.emit(sse({"error": {"message": e.message}}))
                    return

                calls = {}
                with upstream:
                    for raw in upstream:
                        self.collect(raw, reply, reasoning, citations, calls)
                        gen.emit(raw)

                if not calls:
                    break

                if work is None:
                    work = tools.workspace(session_id, user_message_id)
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": c["id"] or ("call_%d" % i), "type": "function",
                         "function": {"name": c["name"], "arguments": c["args"]}}
                        for i, c in sorted(calls.items())
                    ],
                })
                for i, call in sorted(calls.items()):
                    try:
                        args = json.loads(call["args"] or "{}")
                    except ValueError:
                        args = {}
                    gen.emit(sse({"tool": {"name": call["name"], "args": args}}))
                    result = tools.dispatch(work, call["name"], args)
                    gen.emit(sse({"tool_result": {"name": call["name"],
                                                  "result": result}}))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"] or ("call_%d" % i),
                        "content": json.dumps(result)[:tools.OUTPUT_LIMIT],
                    })

            if work:
                produced = tools.collect(work)
                tools.discard(work)
                if produced:
                    gen.emit(sse({"files": [{"name": n, "size": len(b)}
                                            for n, b in produced]}))
            self.persist(session_id, model, reply, reasoning, citations, found,
                         produced)
        except Exception as e:
            traceback.print_exc()
            gen.emit(sse({"error": {"message": str(e)[:200]}}))
        finally:
            gen.finish()
            drop_generation(session_id)
            release_session(session_id)

    # -- streaming helpers -------------------------------------------------
    def begin_stream(self, session_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Session-Id", str(session_id))
        self.end_headers()

    def relay(self, gen):
        """Replay what the generation already produced, then follow it live.

        A client that reloads mid-answer calls this again and catches up from
        the start of the buffer, so nothing is lost.
        """
        sent = 0
        try:
            while True:
                with gen.cond:
                    while sent >= len(gen.chunks) and not gen.done:
                        gen.cond.wait(timeout=30)
                    batch = gen.chunks[sent:]
                    sent += len(batch)
                    finished = gen.done and sent >= len(gen.chunks)
                for raw in batch:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
                self.wfile.flush()
                if finished:
                    break
            self.end_stream()
        except (BrokenPipeError, ConnectionResetError):
            pass   # the reader left; the generation carries on without them

    def end_stream(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def open_upstream(self, model, messages, needs_pdf=False, use_tools=False):
        payload = {
            "model": model,
            "messages": messages,
            "temperature": DEFAULT_TEMPERATURE,
            "stream": True,
        }
        if use_tools:
            payload["tools"] = tools.DEFINITIONS
        if needs_pdf:
            # pdf-text extracts the embedded text layer; measured as no extra
            # charge beyond the tokens it produces.
            payload["plugins"] = [{"id": "file-parser",
                                   "pdf": {"engine": "pdf-text"}}]
        try:
            return urllib.request.urlopen(request("/chat/completions", payload),
                                          timeout=CONNECT_TIMEOUT)
        except urllib.error.HTTPError as e:
            raise UpstreamError(e.read().decode(errors="replace"), e.code)
        except SystemExit as e:
            raise UpstreamError(str(e), 500)
        except (urllib.error.URLError, OSError) as e:
            raise UpstreamError("could not reach OpenRouter: %s" % e, 502)

    @staticmethod
    def persist(session_id, model, reply, reasoning, citations=None, found=None,
                produced=None):
        text = "".join(reply).lstrip()
        if text or reasoning or produced:
            merged = list(found or [])
            known = {s["url"] for s in merged}
            for s in web.sources(citations):
                if s["url"] not in known:
                    merged.append(s)
            message_id = db.add_message(session_id, "assistant", text,
                                        "".join(reasoning), merged)
            for name, blob in (produced or []):
                kind = "image" if classify(name, "") == "image" else "document"
                db.add_attachment(message_id, name, guess_mime(name), kind, blob)
        db.touch_session(session_id, model)

    @staticmethod
    def collect(raw, reply, reasoning, citations, calls=None):
        """Pull content/reasoning/citation/tool deltas out of one SSE line."""
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data: ") or line[6:] == "[DONE]":
            return
        try:
            delta = json.loads(line[6:])["choices"][0].get("delta", {})
        except (ValueError, KeyError, IndexError):
            return
        if delta.get("content"):
            reply.append(delta["content"])
        if delta.get("reasoning"):
            reasoning.append(delta["reasoning"])
        if delta.get("annotations"):
            citations.extend(delta["annotations"])
        # Tool calls stream in fragments keyed by index; stitch them together.
        if calls is not None and delta.get("tool_calls"):
            for part in delta["tool_calls"]:
                slot = calls.setdefault(part.get("index", 0),
                                        {"id": None, "name": "", "args": ""})
                if part.get("id"):
                    slot["id"] = part["id"]
                fn = part.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]


def main():
    global BIND_HOST
    p = argparse.ArgumentParser(description="Serve the OpenRouter chatbox UI.")
    p.add_argument("-p", "--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; anything but loopback needs "
                        "OPENROUTER_UI_TOKEN (default: %(default)s)")
    args = p.parse_args()

    BIND_HOST = args.host
    security.startup_check(args.host)          # exits if exposed without a token
    security.protect_file(os.path.join(HERE, ".env"))

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("warning: OPENROUTER_API_KEY not found in .env or environment; "
              "requests will fail", file=sys.stderr)

    db.init()
    security.protect_file(db.DB_PATH)   # after init, so the file exists
    print("history: " + db.DB_PATH, file=sys.stderr)

    if not os.path.isfile(os.path.join(HERE, "vendor", "ionic", "ionic.esm.js")):
        print("\nUI assets are missing — the page will not render correctly.\n"
              "Run ./fetch-vendor.sh once to install Ionic into vendor/.\n",
              file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True               # do not hang on open streams
    url = "http://{}:{}".format(args.host, args.port)
    if security.UI_TOKEN:
        print("auth: enabled — open %s/?token=<your token> once" % url,
              file=sys.stderr)
    else:
        print("auth: disabled (loopback only)", file=sys.stderr)
    print("serving on " + url, file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
