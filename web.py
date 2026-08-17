#!/usr/bin/env python3
"""Decide when a question needs live web results, and go fetch them.

The model's training data has a cutoff, so anything about recent events, prices,
scores or schedules has to come from a search. Searching every message would be
wasteful — OpenRouter bills per search on top of tokens — so this module spots
the questions that actually need it.

The search runs as its own single-message request rather than as a plugin on the
main chat call. Measured behaviour: the web plugin appends its results as a
trailing system message, and every provider serving qwen3.8-27b (Chutes, Io Net,
AkashML) rejects that with "System message must be at the beginning" as soon as
the conversation has any history or a system prompt. Running it separately keeps
search working for multi-turn chats, and lets the query carry context from
earlier turns — "final ucl" after "highlight ucl 2005" has to search for the
2005 final, not the newest one.
"""

import datetime
import json
import os
import re
import urllib.request

from qwen import request

# A small, fast, non-thinking model is enough to digest search results.
SEARCH_MODEL = os.environ.get(
    "OPENROUTER_SEARCH_MODEL", "qwen/qwen3-30b-a3b-instruct-2507")

SEARCH_PROMPT = (
    "Search the web and summarise the facts needed to answer this request:\n"
    "{query}\n\n"
    "Give concrete details — names, dates, numbers, scores. Stay factual and "
    "brief. Reply in the same language as the request."
)

# Phrases that ask for a search outright, in English and Indonesian.
EXPLICIT = [
    "search the web", "search online", "look it up", "look this up", "google",
    "browse", "on the internet", "cari di internet", "cari online",
    "carikan di internet", "browsing", "telusuri",
]

# Words implying "as of now", which training data cannot answer reliably.
RECENCY = [
    # English
    "latest", "newest", "recent", "recently", "current", "currently", "today",
    "tonight", "yesterday", "this week", "this month", "this year", "right now",
    "so far", "upcoming", "news", "headline", "score", "result", "results",
    "standings", "schedule", "fixture", "price", "stock", "weather", "forecast",
    "released", "release date", "who won", "winner", "live",
    # Indonesian
    "terbaru", "terkini", "terakhir", "sekarang", "saat ini", "hari ini",
    "kemarin", "minggu ini", "bulan ini", "tahun ini", "berita", "kabar",
    "hasil", "skor", "jadwal", "klasemen", "harga", "cuaca", "rilis",
    "siapa yang menang", "pemenang", "langsung",
]

# A year at or after this is beyond what the model can be trusted to know.
RECENT_YEAR = datetime.date.today().year - 2
YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Lets the user force a search for one message: "/web who won ..."
PREFIX_RE = re.compile(r"^\s*/(?:web|search|cari)\b\s*", re.I)


def strip_prefix(text):
    """Return (text_without_prefix, prefix_was_present)."""
    stripped = PREFIX_RE.sub("", text, count=1)
    return stripped, stripped != text


# A message this short is treated as leaning on the previous turn. Kept tight:
# anything longer usually carries its own subject, and widening it pulls in
# unrelated questions ("tulis fungsi python untuk sorting list").
FOLLOWUP_MAX_WORDS = 6


def should_search(text, after_search=False):
    """True when the message looks like it needs live information.

    `after_search` means the previous reply in this session came from a search;
    a short follow-up to that ("final ucl") is almost always about the same
    topic and needs the web too, even though it carries no keyword of its own.
    """
    if not text:
        return False
    lowered = text.lower()

    if any(phrase in lowered for phrase in EXPLICIT):
        return True

    for year in YEAR_RE.findall(lowered):
        if int(year) >= RECENT_YEAR:
            return True

    if any(re.search(r"\b" + re.escape(word) + r"\b", lowered)
           for word in RECENCY):
        return True

    return after_search and len(text.split()) <= FOLLOWUP_MAX_WORDS


def expand_query(current, previous):
    """Fold earlier turns into a short follow-up so the search stays on topic.

    `previous` is the session's earlier user messages, oldest first. A message
    long enough to stand on its own is left alone.
    """
    current = " ".join(current.split())
    if not previous or len(current.split()) > FOLLOWUP_MAX_WORDS:
        return current
    if YEAR_RE.search(current):      # already pins its own timeframe
        return current

    # Anchor on the message that set the topic rather than folding in every
    # earlier turn: accumulating them ("… siapa pencetak golnya di stadion mana
    # berapa penontonnya") produces a run-on query that finds nothing.
    anchor = " ".join(previous[0].split())
    seen = {w.lower() for w in WORD_RE_Q.findall(current.lower())}
    extra = [w for w in anchor.split()
             if w.lower() not in seen and WORD_RE_Q.search(w)]
    if not extra:
        return current
    return (" ".join(extra) + " " + current).strip()[:300]


WORD_RE_Q = re.compile(r"[\w']+", re.UNICODE)


def run_search(query, max_results=5, timeout=90):
    """Single-message search request. Returns (digest_text, sources)."""
    payload = {
        "model": SEARCH_MODEL,
        "messages": [{"role": "user", "content": SEARCH_PROMPT.format(query=query)}],
        "temperature": 0,
        "max_tokens": 800,
        "plugins": plugin(max_results),
    }
    resp = urllib.request.urlopen(request("/chat/completions", payload),
                                  timeout=timeout)
    with resp:
        data = json.load(resp)
    message = data["choices"][0]["message"]
    return (message.get("content") or "").strip(), sources(message.get("annotations"))


def context_block(query, digest, found):
    """Format search findings as a system-message section."""
    today = datetime.date.today().isoformat()
    lines = ["Web search results for \"%s\" (retrieved %s):" % (query, today),
             digest]
    if found:
        lines.append("Sources:")
        lines.extend("%d. %s — %s" % (i, s["title"], s["url"])
                     for i, s in enumerate(found, 1))
    lines.append("Base your answer on these results and say so if they are "
                 "insufficient.")
    return "\n".join(lines)


def plugin(max_results):
    return [{"id": "web", "max_results": max_results}]


def sources(annotations):
    """Flatten OpenRouter url_citation annotations into {title, url} records."""
    out, seen = [], set()
    for a in annotations or []:
        cite = a.get("url_citation") or {}
        url = cite.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"title": cite.get("title") or url, "url": url})
    return out
