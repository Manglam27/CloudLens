"""The LLM steps of a check, on NVIDIA's hosted model API (build.nvidia.com):

    extract_claim  step 2: the checkable claim in the OCR text, or why there is none
    judge_sources  step 4: each source's stance toward the claim, and how directly
                   it settles it

The model is set by MODEL_ID (default NVIDIA Nemotron 3 Ultra) and called through
the OpenAI-compatible chat completions endpoint with thinking switched off and
temperature 0, so the same screenshot gets the same answer (consistency NFR).
Each reply must be one JSON object of the documented shape and is checked
before use. The API key comes from SSM through set_api_key().
"""

import json
import os
import re
import urllib.error
import urllib.request

API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
TIMEOUT_SECONDS = 50
STANCES = ("SUPPORTS", "REFUTES", "MIXED", "UNRELATED")

_api_key = None


class LLMError(Exception):
    """The model could not produce a usable answer."""


def set_api_key(key):
    global _api_key
    _api_key = key or None


def _post(body):
    """Sends one chat completion request and returns the decoded reply."""
    request = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"), headers={
        "Authorization": f"Bearer {_api_key}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"NVIDIA API returned HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LLMError(f"NVIDIA API request failed: {type(e).__name__}") from None


def _ask(system, user, max_tokens):
    if not _api_key:
        raise LLMError("No NVIDIA API key is configured")
    reply = _post({
        "model": os.environ.get("MODEL_ID", DEFAULT_MODEL),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
        # Nemotron 3 thinks before answering unless told not to; these short
        # judgements don't need it, and it would cost time and tokens.
        "chat_template_kwargs": {"enable_thinking": False},
    })
    choice = (reply.get("choices") or [{}])[0]
    if choice.get("finish_reason") not in ("stop", None):
        raise LLMError(f"The model stopped with {choice.get('finish_reason')}")
    return _parse_json((choice.get("message") or {}).get("content") or "")


def _parse_json(text):
    """The JSON object in a reply, ignoring any thinking or code fences around it."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LLMError("The model did not return JSON")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError("The model did not return valid JSON") from e


CLAIM_SYSTEM = """You read text taken from a screenshot of a social-media post. The text comes from OCR, so it can \
include usernames, @handles, timestamps, like and share counts, and other app interface text; ignore those.

Find the single main factual claim that a fact-checker could verify, and classify the post:
- claim: it states something checkable as fact.
- opinion: it is a personal view, preference, or prediction.
- satire: it is an obvious joke or parody.
- no_claim: there is no statement to check.

For a claim, rewrite it as one clear, self-contained English sentence: keep names, places and numbers exactly, \
resolve words like "this" or "he" from the surrounding text, and add nothing that the post does not say. Also give \
a short search_query of 3 to 8 keywords from the claim, as a fact-check search engine would match it. For anything \
other than a claim, return an empty claim and an empty search_query.

Reply with only this JSON object and nothing else:
{"kind": "claim" | "opinion" | "satire" | "no_claim", "claim": "...", "search_query": "..."}"""


def extract_claim(lines):
    """Returns {"kind", "claim", "search_query"}."""
    text = "\n".join(lines)
    result = _ask(CLAIM_SYSTEM, f"<ocr_text>\n{text}\n</ocr_text>", max_tokens=512)
    kind = result.get("kind")
    if kind not in ("claim", "opinion", "satire", "no_claim"):
        raise LLMError("The model returned an unknown kind of post")
    if kind == "claim" and not str(result.get("claim", "")).strip():
        raise LLMError("The model classified the text as a claim but returned no claim")
    return {"kind": kind, "claim": str(result.get("claim", "")), "search_query": str(result.get("search_query", ""))}


JUDGE_SYSTEM = """You compare a claim with sources found for it. There are three kinds of source:
- a fact-check, with the claim it reviewed, its title and its rating;
- a news article, with its headline and a short summary;
- an encyclopedia article, with its opening text.
Judge only from these fields, not from your own knowledge.

For each numbered source, decide its stance toward the claim:
- SUPPORTS: a fact-check of the same claim that rates it true, mostly true, or correct; a news article that \
reports the claim as established fact; or an encyclopedia article whose text states the claim as a current fact.
- REFUTES: a fact-check of the same claim that rates it false, mostly false, fabricated, or misleading; a news \
article that reports the claim as false or debunked; or an encyclopedia article whose text plainly contradicts it.
- MIXED: a fact-check of the same claim with an in-between rating, such as half true or missing context.
- UNRELATED: a source about a different claim, or one that only mentions the topic without confirming or denying \
the claim.

Also give each source a strength from 0.0 to 1.0: how directly and clearly it settles this exact claim. 1.0 is a \
fact-check of exactly this claim, or a source that states exactly this as fact; 0.5 is a source that addresses the \
claim only partly or indirectly; 0.2 is a source that barely touches it. Use 0.0 for UNRELATED.

Then write one short, plain sentence for a general audience that summarizes what the sources found, such as \
"Two fact-checkers rated this claim false." Do not give a final verdict or a score.

Reply with only this JSON object and nothing else:
{"sources": [{"index": 1, "stance": "SUPPORTS" | "REFUTES" | "MIXED" | "UNRELATED", "strength": 0.0}], \
"reason": "..."}"""


def judge_sources(claim, sources):
    """Returns ([stance for each source], [strength for each source], reason), in source order.
    Sources the model left out count as UNRELATED with strength 0."""
    def describe(i, s):
        if s.get("kind", "fact_check") == "fact_check":
            # Fact-check articles found in news feeds have a headline and summary but no rating.
            return (f"[{i}] Fact-check by {s['publisher']} ({s['domain']})\n"
                    f"Claim reviewed: {s.get('claim_reviewed') or '(not given)'}\nTitle: {s['title']}\n"
                    f"Rating: {s.get('rating') or '(none; see title and summary)'}\n"
                    f"Summary: {s.get('summary') or '(none)'}")
        if s.get("kind") == "reference":
            return (f"[{i}] Encyclopedia article from {s['publisher']}: {s['title']}\n"
                    f"Opening text: {s.get('summary') or '(none)'}")
        return (f"[{i}] News article from {s['publisher']} ({s['domain']})\n"
                f"Headline: {s['title']}\nSummary: {s.get('summary') or '(none)'}")

    listing = "\n\n".join(describe(i, s) for i, s in enumerate(sources, start=1))
    user = f"<claim>\n{claim}\n</claim>\n\n<sources>\n{listing}\n</sources>"
    result = _ask(JUDGE_SYSTEM, user, max_tokens=1500)
    by_index = {}
    for item in result.get("sources", []):
        if isinstance(item, dict) and item.get("stance") in STANCES and isinstance(item.get("index"), int):
            try:
                strength = max(0.0, min(1.0, float(item.get("strength", 0.0))))
            except (TypeError, ValueError):
                strength = 0.0
            by_index[item["index"]] = (item["stance"], strength)
    judged = [by_index.get(i, ("UNRELATED", 0.0)) for i in range(1, len(sources) + 1)]
    return [s for s, _ in judged], [m for _, m in judged], str(result.get("reason", "")).strip()
