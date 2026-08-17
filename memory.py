#!/usr/bin/env python3
"""Memory capture and selection. Stdlib only, no extra model calls.

Two jobs:

1. `extract()` spots "remember this: …" style phrasing in a user message and
   returns the facts worth keeping.
2. `rank()` picks which stored memories are worth spending context on for the
   message at hand, so a large store does not get injected wholesale.
"""

import re

# A trigger has to open the line, optionally after a short lead-in clause
# ("By the way, remember …") or a politeness word ("Please remember …").
# Requiring that keeps incidental uses — "I want to remember this trip" — out
# of the store.
LEAD = r"^(?:[^,\n]{0,40},\s*)?(?:please\s+|also\s+|just\s+|and\s+|oh\s+)*"

# Phrasings that mean "store this". Each captures the rest of the line.
TRIGGERS = [
    re.compile(LEAD + r"remember\b\s*(?:that|this|to)?\s*[:,\-–]?\s*(.+)", re.I),
    re.compile(LEAD + r"note to self\b\s*[:,\-–]?\s*(.+)", re.I),
    re.compile(LEAD + r"keep in mind\b\s*(?:that)?\s*[:,\-–]?\s*(.+)", re.I),
    re.compile(LEAD + r"don'?t forget\b\s*(?:that|to)?\s*[:,\-–]?\s*(.+)", re.I),
    re.compile(LEAD + r"for future reference\b\s*[:,\-–]?\s*(.+)", re.I),
]

# "do you remember what I said?" is a question, not an instruction to store.
QUESTION_LEAD = re.compile(
    r"^\s*(do|does|did|can|could|would|will|are|is|was|were|have|has|any)\b", re.I)

MAX_LEN = 300
MIN_LEN = 3


def extract(message):
    """Return facts the user explicitly asked to remember, in order."""
    found = []
    for line in message.splitlines():
        line = line.strip()
        if not line or QUESTION_LEAD.match(line):
            continue
        for trigger in TRIGGERS:
            match = trigger.search(line)
            if not match:
                continue
            text = clean(match.group(1))
            if text and text not in found:
                found.append(text)
            break   # one memory per line
    return found


def clean(text):
    text = " ".join(text.split()).strip(" ,;:-–")
    if text.endswith("?"):        # still a question after the trigger word
        return ""
    if len(text) < MIN_LEN:
        return ""
    return text[:MAX_LEN].rstrip()


# -- selection --------------------------------------------------------------
STOPWORDS = {
    "the", "and", "for", "you", "your", "that", "this", "with", "have", "has",
    "was", "were", "are", "but", "not", "all", "any", "can", "will", "would",
    "what", "when", "how", "why", "who",
    "about", "from", "into", "than", "then", "them", "they", "there", "here",
    "some", "more", "most", "much", "very", "just", "like", "also", "one",
    "two", "get", "got", "use", "used", "using", "make", "made", "please",
}

WORD_RE = re.compile(r"[a-z0-9']+")


def stem(word):
    """Crude suffix trim so 'commits'/'commit' and 'units'/'unit' match.

    This is lexical only — it will not connect 'shell' to 'zsh'. Synonyms need
    an embedding model, which this stays deliberately free of.
    """
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def words(text):
    return {stem(w) for w in WORD_RE.findall(text.lower())
            if len(w) > 2 and w not in STOPWORDS}


def rank(memories, query, limit=12):
    """Order memories by relevance to `query`, most useful first.

    Pinned memories always survive. Below the limit everything is included —
    ranking only starts discarding once the store outgrows the budget.
    """
    query_words = words(query or "")
    scored = []
    for m in memories:
        overlap = len(query_words & words(m["text"]))
        # Pinned outranks everything; then keyword overlap; then recency.
        score = (100 if m.get("pinned") else 0) + overlap * 10
        scored.append((score, m["id"], m))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [m for _, _, m in scored[:limit]]


def system_prompt(instructions, memories):
    """Assemble the system message from preferences plus selected memories."""
    parts = []
    if instructions and instructions.strip():
        parts.append(instructions.strip())
    if memories:
        lines = "\n".join("- " + m["text"] for m in memories)
        parts.append("Things to remember about this user:\n" + lines)
    return "\n\n".join(parts)
