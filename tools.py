#!/usr/bin/env python3
"""Let the model run short scripts and produce files. Stdlib only.

**This is off by default.** Set OPENROUTER_TOOLS=1 to enable it, and read the
limits below before you do — a model that can run code on your machine is a
different risk class from one that only writes text.

What is enforced:
  * a fresh working directory per message, and the process starts there
  * wall-clock timeout, CPU time, address space, file size and process count
  * a stripped environment (no API keys reach the script)
  * captured output, truncated

What is NOT enforced: the script still runs as you. It can read files in your
home directory and reach the network. This bounds accidents and runaway loops;
it is not a jail. The sharpest edge is that web search results are untrusted
text that reaches the same model deciding what to run, so a crafted page can
try to steer it — keep search off for sensitive work, or leave tools disabled.
"""

import os
import shutil
import subprocess
import sys
import tempfile

try:
    import resource
except ImportError:            # not on this platform
    resource = None

ENABLED = os.environ.get("OPENROUTER_TOOLS", "").strip().lower() in (
    "1", "true", "yes", "on")

TIMEOUT = int(os.environ.get("OPENROUTER_TOOL_TIMEOUT", "30"))
MEMORY_MB = int(os.environ.get("OPENROUTER_TOOL_MEMORY_MB", "512"))
MAX_FILE_MB = int(os.environ.get("OPENROUTER_TOOL_FILE_MB", "16"))
MAX_ROUNDS = int(os.environ.get("OPENROUTER_TOOL_ROUNDS", "4"))
OUTPUT_LIMIT = 20000           # characters of stdout/stderr handed back

WORKSPACES = os.path.join(tempfile.gettempdir(), "mbahgpt-work")

LANGUAGES = {
    "python": [sys.executable, "-I", "-"],
    "bash": ["/bin/bash", "-s"],
    "sh": ["/bin/sh", "-s"],
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": (
                "Jalankan skrip pendek di direktori kerja sementara dan "
                "kembalikan keluarannya. Berkas apa pun yang ditulis skrip ke "
                "direktori kerja akan diberikan ke pengguna sebagai unduhan. "
                "Gunakan untuk perhitungan, pengolahan data, atau membuat berkas."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {"type": "string", "enum": ["python", "bash", "sh"]},
                    "code": {"type": "string", "description": "isi skrip"},
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Tulis berkas teks di direktori kerja supaya bisa diunduh "
                "pengguna. Untuk berkas biner, pakai run_script."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "nama berkas, tanpa path"},
                    "content": {"type": "string"},
                },
                "required": ["name", "content"],
            },
        },
    },
]


def workspace(session_id, message_id):
    path = os.path.join(WORKSPACES, "s%s-m%s" % (session_id, message_id))
    os.makedirs(path, exist_ok=True)
    return path


def discard(path):
    shutil.rmtree(path, ignore_errors=True)


def safe_name(name):
    """One path segment, no traversal, no hidden files."""
    base = os.path.basename(str(name or "berkas")).strip().lstrip(".")
    keep = "".join(c for c in base if c.isalnum() or c in "._- ")
    return (keep or "berkas")[:80]


def _limits():
    """Applied in the child between fork and exec."""
    if resource is None:
        return
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT, TIMEOUT))
    resource.setrlimit(resource.RLIMIT_FSIZE,
                       (MAX_FILE_MB << 20, MAX_FILE_MB << 20))
    try:
        resource.setrlimit(resource.RLIMIT_AS,
                           (MEMORY_MB << 20, MEMORY_MB << 20))
    except (ValueError, OSError):
        pass       # some platforms refuse an address-space cap
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except (ValueError, OSError):
        pass
    os.setsid()    # so a timeout kills the whole group, not just the shell


def run_script(cwd, language, code):
    language = (language or "python").lower()
    if language not in LANGUAGES:
        return {"error": "bahasa %s tidak didukung" % language}
    if not (code or "").strip():
        return {"error": "skrip kosong"}

    before = set(os.listdir(cwd))
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": cwd,
           "LANG": "en_US.UTF-8", "TMPDIR": cwd}
    try:
        proc = subprocess.run(
            LANGUAGES[language],
            input=code,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=TIMEOUT,
            preexec_fn=_limits if os.name == "posix" else None,
        )
        out, err, status = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        out, err, status = "", "dihentikan setelah %d detik" % TIMEOUT, -1
    except OSError as e:
        return {"error": "tidak bisa menjalankan: %s" % e}

    created = sorted(set(os.listdir(cwd)) - before)
    return {
        "exit_code": status,
        "stdout": out[:OUTPUT_LIMIT],
        "stderr": err[:OUTPUT_LIMIT],
        "files_created": created,
    }


def write_file(cwd, name, content):
    name = safe_name(name)
    data = (content or "").encode("utf-8")
    if len(data) > MAX_FILE_MB << 20:
        return {"error": "berkas melebihi %d MB" % MAX_FILE_MB}
    with open(os.path.join(cwd, name), "wb") as f:
        f.write(data)
    return {"written": name, "bytes": len(data)}


def collect(cwd):
    """Files the tools produced, as (name, bytes) — newest scan wins."""
    out = []
    for entry in sorted(os.listdir(cwd)):
        path = os.path.join(cwd, entry)
        if not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        if size == 0 or size > MAX_FILE_MB << 20:
            continue
        with open(path, "rb") as f:
            out.append((entry, f.read()))
    return out


def dispatch(cwd, name, args):
    if name == "run_script":
        return run_script(cwd, args.get("language"), args.get("code"))
    if name == "write_file":
        return write_file(cwd, args.get("name"), args.get("content"))
    return {"error": "tool %s tidak dikenal" % name}
