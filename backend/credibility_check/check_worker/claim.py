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
    lines = [line.strip() for line in lines]
    kept = []
    for i, line in enumerate(lines):
        if not line or is_noise(line):
            continue
        # The line above an "@handle · 2h" header is the poster's display name.
        after = lines[i + 1] if i + 1 < len(lines) else ""
        if after.startswith("@") and is_noise(after) and not re.search(r"[.!?]$", line):
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


MAX_QUERY_WORDS = 8
_STOPWORDS = frozenset("""a an the is are was were be been being am of to in on for with and or but that this
    these those it its as at by from has have had do does did will would can could should may might just
    very so than then there their they them he she his her we our you your i me my""".split())
_LEAD = re.compile(r"^(breaking|update|just in|urgent|alert)\s*[:!\-]\s*", re.I)
_ATTRIBUTION = re.compile(
    r",?\s*\b(doctors|scientists|experts|researchers|officials|sources|reports?|studies|a study)\s+"
    r"(say|says|said|confirm|confirms|confirmed|warn|warns|show|shows|find|finds|found|claim|claims)\b", re.I)


def search_query(claim):
    """Keywords for the fact-check search, used when Claude can't write the query.

    Fact-check search matches a few keywords far better than a whole sentence,
    so this drops "BREAKING:", "doctors say" and common words.
    """
    text = _ATTRIBUTION.sub("", _LEAD.sub("", claim))
    words = [w for w in re.findall(r"[\w'%$]+(?:-[\w']+)*", text) if w.lower() not in _STOPWORDS]
    return " ".join(words[:MAX_QUERY_WORDS])
