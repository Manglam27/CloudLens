"""Finds the checkable claim in the text read from a screenshot.

Rule-based first step (M4, step 2). OCR of a social-media screenshot mixes the
post with interface text: names, @handles, timestamps and like counts. These
rules drop that noise and keep the longest full sentence. A later step adds
one LLM call to tell claims apart from opinions and jokes.
"""

import re

MIN_CLAIM_WORDS = 5
MAX_CLAIM_CHARS = 300

_MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_DATE = _MONTHS + r"\s+\d{1,2}(,\s*\d{4})?"
_AGE = r"\d+\s*(s|m|h|d|w|min|mins|hr|hrs)"
_COUNT = r"\d+([.,]\d+)?\s*[kmb]?"
_ACTION = r"(likes?|retweets?|reposts?|repl(y|ies)|comments?|shares?|views?|quotes?|bookmarks?)"
# Separator dots in post headers. OCR often reads "·" as "." or "-".
_SEP = r"\s*[·•|.\-]\s*"
_NOISE = [re.compile(p, re.I) for p in (
    r"@\w+(" + _SEP + "(" + _AGE + "|" + _DATE + "))?",          # "@handle" or "@handle · 2h"
    r".*\B@\w+" + _SEP + "(" + _AGE + "|" + _DATE + ")",          # "Jane Doe @handle · 2h"
    _COUNT,                                                     # bare counts: "1.2K"
    r"(" + _COUNT + r"\s+" + _ACTION + r"\s*)+",                # "1.2K Reposts 8,431 Likes"
    r"(reply|repost|retweet|like|share|follow|following|quote|views?|translate post|show more)",
    r"\d{1,2}:\d{2}\s*(am|pm)?(" + _SEP + ".*)?",               # "3:45 PM · Sep 1, 2026"
    _AGE,                                                       # "2h"
    _DATE,                                                      # "Sep 1, 2026"
    r"https?://\S+|www\.\S+",                                   # bare links
)]


def is_noise(line):
    return any(p.fullmatch(line) for p in _NOISE)


def clean_lines(lines):
    """Drops interface text and short labels such as display names."""
    kept = []
    for line in lines:
        line = line.strip()
        if not line or is_noise(line):
            continue
        # Short lines without sentence punctuation are names, labels or buttons.
        if len(line.split()) < 3 and not re.search(r"[.!?]$", line):
            continue
        kept.append(line)
    return kept


def find_claim(lines):
    """Returns the claim to check, or None when there is no full sentence."""
    text = " ".join(clean_lines(lines))
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    candidates = [s for s in sentences if len(s.split()) >= MIN_CLAIM_WORDS]
    if not candidates:
        return None
    return max(candidates, key=len)[:MAX_CLAIM_CHARS]
