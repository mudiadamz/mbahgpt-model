#!/usr/bin/env python3
"""MbahGPT — minimal OpenRouter runner for Qwen models. Stdlib only.

Reads OPENROUTER_API_KEY (and optionally OPENROUTER_MODEL) from the .env file
next to this script, or from the real environment, which takes precedence.

Usage:
    ./qwen.py "why is the sky blue?"      # one-shot
    echo "explain this" | ./qwen.py       # piped stdin
    ./qwen.py                             # interactive chat
    ./qwen.py --list                      # show available qwen models
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=None):
    """Populate os.environ from a .env file. Existing env vars win.

    Handles `KEY=value`, a leading `export`, comments, and quoted values.
    Silently does nothing if the file is absent.
    """
    try:
        with open(path or os.path.join(HERE, ".env")) as f:
            lines = f.readlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_env()  # before the defaults below read the environment

BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "qwen/qwen3.8-27b")

try:
    DEFAULT_TEMPERATURE = float(os.environ.get("OPENROUTER_TEMPERATURE", "0.7"))
except ValueError:
    DEFAULT_TEMPERATURE = 0.7


def api_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("error: OPENROUTER_API_KEY is not set (add it to .env or export it)")
    return key


def request(path, payload=None):
    headers = {
        "Authorization": "Bearer " + api_key(),
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    return urllib.request.Request(BASE_URL + path, data=data, headers=headers)


def list_models():
    with urllib.request.urlopen(request("/models")) as resp:
        models = json.load(resp)["data"]
    for m in sorted(models, key=lambda m: m["id"]):
        if "qwen" not in m["id"].lower():
            continue
        pricing = m.get("pricing", {})
        print("{:<45} ctx={:<8} in=${} out=${}".format(
            m["id"],
            m.get("context_length", "?"),
            pricing.get("prompt", "?"),
            pricing.get("completion", "?"),
        ))


def stream_chat(messages, model, temperature, show_reasoning=True):
    """POST a chat completion and stream deltas to stdout. Returns answer text.

    Thinking models (qwen3-235b-a22b, the *-thinking-* variants) emit
    `delta.reasoning` for a while before any `delta.content`. Reasoning goes to
    stderr so piping stdout still yields a clean answer, and so the CLI does not
    look frozen while the model thinks.
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    parts = []
    thinking = False
    try:
        resp = urllib.request.urlopen(request("/chat/completions", payload))
    except urllib.error.HTTPError as e:
        sys.exit("error: HTTP {} — {}".format(e.code, e.read().decode(errors="replace")))

    with resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            body = line[6:]
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
            if "error" in chunk:
                sys.exit("\nerror: " + json.dumps(chunk["error"]))
            delta = chunk["choices"][0].get("delta", {})

            reasoning = delta.get("reasoning")
            if reasoning and show_reasoning:
                if not thinking:
                    thinking = True
                    sys.stderr.write("\033[2m[thinking] ")
                sys.stderr.write(reasoning)
                sys.stderr.flush()

            content = delta.get("content")
            if content:
                if not parts:
                    # Some models open with blank lines after the think phase.
                    content = content.lstrip()
                    if not content:
                        continue
                if thinking:
                    thinking = False
                    sys.stderr.write("\033[0m\n\n")
                    sys.stderr.flush()
                parts.append(content)
                sys.stdout.write(content)
                sys.stdout.flush()

    if thinking:
        sys.stderr.write("\033[0m\n")
    print()
    return "".join(parts)


def main():
    p = argparse.ArgumentParser(description="Run a Qwen model via OpenRouter.")
    p.add_argument("prompt", nargs="*", help="prompt text; omit to read stdin or chat")
    p.add_argument("-m", "--model", default=DEFAULT_MODEL, help="model slug (default: %(default)s)")
    p.add_argument("-s", "--system", help="system prompt")
    p.add_argument("-t", "--temperature", type=float, default=DEFAULT_TEMPERATURE,
                   help="sampling temperature (default: %(default)s)")
    p.add_argument("--no-think", action="store_true",
                   help="hide reasoning output from thinking models")
    p.add_argument("--list", action="store_true", help="list qwen models and exit")
    args = p.parse_args()

    if args.list:
        list_models()
        return

    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})

    prompt = " ".join(args.prompt)
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()

    if prompt:
        messages.append({"role": "user", "content": prompt})
        stream_chat(messages, args.model, args.temperature, not args.no_think)
        return

    # Interactive mode: keeps history across turns, Ctrl-D or Ctrl-C to quit.
    print("{} — Ctrl-D to exit".format(args.model), file=sys.stderr)
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        messages.append({"role": "user", "content": line})
        reply = stream_chat(messages, args.model, args.temperature, not args.no_think)
        messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
