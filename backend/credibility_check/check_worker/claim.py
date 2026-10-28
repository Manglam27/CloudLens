"""Finds the checkable claim in the text read from a screenshot.

Rule-based first step (M4, step 2). OCR of a social-media screenshot mixes the
post with interface text: names, @handles, timestamps and like counts. These
rules drop that noise and keep the first full sentence of the post. A later
step adds one LLM call to tell claims apart from opinions and jokes.
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
_SEP = r"(\s*[·•|.\-]\s*|\s+)"  # or dropped altogether: "@handle 2h"
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
    # Above the first "@handle" header there is only app or browser chrome and the
    # poster's name, unless a full sentence shows up first.
    for i, line in enumerate(lines):
        if re.search(r"[.!?]$", line):
            break
        if line.startswith("@") and is_noise(line):
            lines = lines[i + 1:]
            break
    kept = []
    for line in lines:
        if not line or is_noise(line):
            continue
        # A lowercase line after an unfinished one continues the sentence ("...outbreaking of" / "hantavirus").
        continues = bool(kept) and not re.search(r"[.!?]$", kept[-1]) and (
            line[0].islower() or (line.isupper() and kept[-1].isupper()))  # all-caps posts
        # Other short lines without sentence punctuation are names, labels or buttons.
        if len(line.split()) < 3 and not re.search(r"[.!?]$", line) and not continues:
            continue
        kept.append(line)
    return kept


# Page footers and captions: "Published on Instagram 19 Aug 2026 at 5:00 AM", "8 hours ago".
_TIMESTAMP = re.compile(r"\b(" + _DATE + r"|\d{1,2}\s+" + _MONTHS + r"|\d{1,2}:\d{2}|published|posted|updated|ago)\b", re.I)


def find_claim(lines):
    """Returns the claim to check, or None when there is no full sentence.

    A post leads with its claim, before commentary, replies and page footers,
    so this takes the first full sentence that is not a caption or a fragment.
    """
    text = " ".join(clean_lines(lines))
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    candidates = [s for s in sentences if len(s.split()) >= MIN_CLAIM_WORDS]
    if not candidates:
        return None
    clean = [s for s in candidates if not _TIMESTAMP.search(s) and not _looks_like_menu(s)]
    return (clean or candidates)[0][:MAX_CLAIM_CHARS]


def _looks_like_menu(sentence):
    """A search box run into menu labels: "colorado weather sn AI Mode All News Images Videos".
    Posts written all in lowercase are fine; it's the lowercase start plus many capitalized labels."""
    words = sentence.split()
    labels = sum(1 for w in words[1:] if w[:1].isupper())
    return sentence[0].islower() and labels >= 0.3 * len(words)


MAX_QUERY_WORDS = 10
_STOPWORDS = frozenset("""a an the is are was were be been being am of to in on for with and or but that this
    these those it its as at by from has have had do does did will would can could should may might just
    very so than then there their they them he she his her we our you your i me my
    all any every over into onto out up about after before now new officially reportedly allegedly
    apparently actually literally says said according""".split())
_LEAD = re.compile(r"^(breaking( news)?|update|just in|urgent|alert)\s*[:!\-]\s*", re.I)
_ATTRIBUTION = re.compile(
    r",?\s*\b(doctors|scientists|experts|researchers|officials|sources|reports?|studies|a study)\s+"
    r"(say|says|said|confirm|confirms|confirmed|warn|warns|show|shows|find|finds|found|claim|claims)\b", re.I)


def search_query(claim):
    """Keywords for the fact-check search, used when the LLM can't write the query.

    Fact-check search matches a few keywords far better than a whole sentence,
    so this drops "BREAKING:", "doctors say" and common words.
    """
    text = _ATTRIBUTION.sub("", _LEAD.sub("", claim))
    words = [w for w in re.findall(r"[\w'%$]+(?:-[\w']+)*", text) if w.lower() not in _STOPWORDS]
    return " ".join(words[:MAX_QUERY_WORDS])
