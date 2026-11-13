"""The LLM steps of a check, on NVIDIA's hosted model API (build.nvidia.com):

    extract_claim  step 2: the checkable claim in the OCR text, or why there is none
    judge_sources  step 4: each source's stance toward the claim, and how directly
                   it settles it

The model is set by MODEL_ID (default NVIDIA Nemotron 3 Ultra) and called through
the OpenAI-compatible chat completions endpoint with thinking switched off and
temperature 0, so the same screenshot gets the same answer (consistency NFR).
Each reply must be one JSON object of the documented shape and is checked
before use. The API key comes from SSM through set_api_key().

The AI must not be the bottleneck of a fact-checking service, so requests go
through a chain of models, each behind its own gate (gate.py):

    1 MODEL_ID (Nemotron 3 Ultra). Its free tier allows a short burst and then
      answers "busy" (HTTP 429) for a while, so it gets a few seconds to find a
      slot, and on a 429 every check skips it for a minute.
    2 FALLBACK_MODEL_ID, a comma-separated list tried in order (Gemma 4 31B,
      then Nemotron 3.5 Lightning), each with its own budget. The last one waits
      for a slot and retries a 429 while the deadline allows.
    3 Only if every model fails does the check fall back to the rules.

A stored answer to an identical request, from either model, is used first.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

from gate import Gate

API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
TIMEOUT_SECONDS = 50  # the main model
FALLBACK_TIMEOUT_SECONDS = 20  # a backup model that takes longer is overloaded: move on
TIMEOUT_COOL_DOWN_SECONDS = 120  # after a backup model times out, every check skips it this long
RETRIES = 1
RETRY_DELAY_SECONDS = 1.0
BUSY_WAITS = (3, 6, 10)  # seconds to wait after each "busy" (HTTP 429) before trying again
DEFAULT_DEADLINE_SECONDS = 60
PRIMARY_PATIENCE_SECONDS = 8  # how long to wait for a slot on the main model before using the fallback
COOL_DOWN_SECONDS = 60  # after a 429, how long every check skips the main model
STANCES = ("SUPPORTS", "REFUTES", "MIXED", "UNRELATED")
# The judge answers "is the claim true, according to this source?", which models get
# right far more often than "does it support or refute"; the answers map to stances.
VERDICT_STANCES = {"TRUE": "SUPPORTS", "FALSE": "REFUTES", "PARTLY": "MIXED", "NOT_ADDRESSED": "UNRELATED"}

_api_key = None
_deadline = None  # time.time() after which no new request is started
gates = {}  # model -> Gate; the worker sets ones backed by DynamoDB
last_model = None  # the model that gave the last answer


class LLMError(Exception):
    """The model could not produce a usable answer."""


class LLMTimeout(LLMError):
    """The request took too long; not retried, since a retry would outlast the app's wait."""


class LLMBusy(LLMError):
    """No request could be made in time: NVIDIA kept answering HTTP 429, or no slot under
    the shared rate limit freed up before the check's deadline."""


class LLMRateLimited(LLMBusy):
    """NVIDIA answered HTTP 429 (too many requests)."""


def set_api_key(key):
    global _api_key
    _api_key = key or None


def set_deadline(when):
    """No LLM request starts after `when` (a time.time() value); the check then uses the rules."""
    global _deadline
    _deadline = when


def _post(body, timeout=None):
    """Sends one chat completion request and returns the decoded reply."""
    request = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"), headers={
        "Authorization": f"Bearer {_api_key}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout or TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise LLMRateLimited("NVIDIA API returned HTTP 429") from None
        raise LLMError(f"NVIDIA API returned HTTP {e.code}") from None
    except TimeoutError:
        raise LLMTimeout("NVIDIA API request timed out") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise LLMTimeout("NVIDIA API request timed out") from None
        raise LLMError(f"NVIDIA API request failed: {type(e).__name__}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LLMError(f"NVIDIA API request failed: {type(e).__name__}") from None


def models():
    """The models to try, in order: MODEL_ID, then each of FALLBACK_MODEL_ID (comma-separated)."""
    chain = [os.environ.get("MODEL_ID", DEFAULT_MODEL)]
    for model in os.environ.get("FALLBACK_MODEL_ID", "").split(","):
        if model.strip() and model.strip() not in chain:
            chain.append(model.strip())
    return chain


def _gate(model):
    if model not in gates:
        gates[model] = Gate()
    return gates[model]


def _ask(system, user, max_tokens, check=lambda result: result):
    """The JSON answer of the first model in the chain that gives a usable one,
    after check(answer) accepts it. Sets last_model."""
    global last_model
    if not _api_key:
        raise LLMError("No NVIDIA API key is configured")
    chain = models()
    # Answers of the last-resort model are neither stored nor reused: the next check
    # should get a better model's answer once one is free.
    for model in chain[:-1] or chain:
        stored = _gate(model).cached(Gate.key(model, system, user, max_tokens))
        if stored is not None:
            try:
                result = check(stored)
            except LLMError:
                continue  # stored by an older version in another shape: ask again
            last_model = model
            return result
    deadline = _deadline or time.time() + DEFAULT_DEADLINE_SECONDS
    for i, model in enumerate(chain):
        gate, last = _gate(model), i == len(chain) - 1
        if not last and gate.cooling():
            continue  # rate-limited a moment ago: go straight to the next model
        patience = deadline if last else min(deadline, time.time() + PRIMARY_PATIENCE_SECONDS)
        try:
            result = _ask_model(model, gate, system, user, max_tokens, check, patience, deadline, keep_trying=last,
                                store=not last or len(chain) == 1)
        except LLMRateLimited:
            if last:
                raise
            gate.cool_down(COOL_DOWN_SECONDS)
            print(f"{model} is rate-limited; skipping it for {COOL_DOWN_SECONDS} s, using {chain[i + 1]}")
            continue
        except LLMTimeout:
            if last:
                raise
            if i > 0:  # a free backup endpoint that hangs is usually overloaded for a while
                gate.cool_down(TIMEOUT_COOL_DOWN_SECONDS)
            print(f"{model} timed out; using {chain[i + 1]}")
            continue
        except LLMError as e:
            if last:
                raise
            print(f"{model} gave no usable answer ({e}); using {chain[i + 1]}")
            continue
        last_model = model
        return result
    raise LLMBusy("No model was available")


def _ask_model(model, gate, system, user, max_tokens, check, patience, deadline, keep_trying, store=True):
    """One model's answer. Waits until `patience` for a slot. A reply that is not usable
    (no JSON, cut off, wrong shape) is asked again once: the hosted model sometimes
    returns an empty or broken reply under load. With keep_trying, a 429 is retried
    after a short wait while the deadline allows; a timeout is never retried."""
    unusable = busy = 0
    while True:
        if not gate.acquire(patience):
            raise LLMBusy(f"No free {model} slot in time")
        try:
            result = check(_ask_once(system, user, max_tokens, model))
        except LLMTimeout:
            raise
        except LLMRateLimited:
            wait = BUSY_WAITS[min(busy, len(BUSY_WAITS) - 1)]
            busy += 1
            if not keep_trying or busy > len(BUSY_WAITS) or time.time() + wait > deadline:
                raise
            print(f"{model} busy (HTTP 429); trying again in {wait} s")
            time.sleep(wait)
            continue
        except LLMError as e:
            unusable += 1
            if unusable > RETRIES:
                raise
            print(f"LLM reply not usable ({e}); asking again")
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if store:
            gate.remember(Gate.key(model, system, user, max_tokens), result)
        return result


def _ask_once(system, user, max_tokens, model=None):
    model = model or os.environ.get("MODEL_ID", DEFAULT_MODEL)
    timeout = TIMEOUT_SECONDS if model == os.environ.get("MODEL_ID", DEFAULT_MODEL) else FALLBACK_TIMEOUT_SECONDS
    reply = _post({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
        # Nemotron thinks before answering unless told not to; these short judgements
        # don't need it, and with it a judgement takes longer than the app waits.
        "chat_template_kwargs": {"enable_thinking": False},
    }, timeout)
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
A news headline or press-release title reports that something happened, so it is a claim even when it is worded \
as a celebration or welcome (for example "NASA welcomes the Artemis II crew back to Earth" claims the crew returned).

For a claim, rewrite it as one clear, self-contained English sentence: keep names, places and numbers exactly, \
resolve words like "this" or "he" from the surrounding text, and add nothing that the post does not say. Keep \
words about timing such as "just", "today", "now", "breaking" or "new", and any dates, because a real event \
presented as new is a common false claim. Also give \
a short search_query of 3 to 8 keywords from the claim, as a fact-check search engine would match it, and a \
topic_query of 2 to 4 words naming the story the way fact-checkers and news headlines name it (for example \
"moon landing hoax", "Biden gas stove ban", "Key Bridge collapse"). For anything other than a claim, return an \
empty claim, search_query and topic_query.

Reply with only this JSON object and nothing else:
{"kind": "claim" | "opinion" | "satire" | "no_claim", "claim": "...", "search_query": "...", "topic_query": "..."}"""


def _claim_answer(result):
    kind = result.get("kind")
    if kind not in ("claim", "opinion", "satire", "no_claim"):
        raise LLMError("The model returned an unknown kind of post")
    if kind == "claim" and not str(result.get("claim", "")).strip():
        raise LLMError("The model classified the text as a claim but returned no claim")
    return {"kind": kind, "claim": str(result.get("claim", "")), "search_query": str(result.get("search_query", "")),
            "topic_query": str(result.get("topic_query", ""))}


def extract_claim(lines):
    """Returns {"kind", "claim", "search_query", "topic_query"}."""
    text = "\n".join(lines)
    return _ask(CLAIM_SYSTEM, f"<ocr_text>\n{text}\n</ocr_text>", max_tokens=512, check=_claim_answer)


JUDGE_SYSTEM = """You check a claim against sources found for it. There are three kinds of source:
- a fact-check, with the claim it reviewed, its title and its rating;
- a news article, with its headline and a short summary;
- an encyclopedia article, with its opening text.
Judge only from these fields, not from your own knowledge.

For each numbered source, answer one question: according to this source, is the claim, exactly as written in \
<claim>, true?
- TRUE: the source shows the claim is true: a news article or encyclopedia text that reports what the claim says \
as fact, or a fact-check that rated this same claim true.
- FALSE: the source shows the claim is false: a fact-check that rated this same claim false, misleading or \
fabricated, or whose title says it is not true as stated (for example "did NOT", "fake", or an old event presented \
as new); a news article that debunks it; or encyclopedia text that states something contradicting it.
- PARTLY: a fact-check of this same claim with an in-between rating, such as half true or missing context.
- NOT_ADDRESSED: the source is about something else, or only mentions the topic without settling the claim.

Be careful with direction. A fact-check's rating applies to the claim it reviewed; if that is the opposite of our \
claim, its finding flips. A fact-check rating "Kamala Harris is not eligible to be president" false shows that the \
claim "Kamala Harris cannot be president" is FALSE. A news report that someone died shows the claim "He died" is \
TRUE. Encyclopedia text saying the statue was a gift from France shows "It was a gift from Britain" is FALSE.

A fact-check that a photo, video or post about an event is fake, edited or AI-generated is about that picture or \
post, not about whether the event happened: NOT_ADDRESSED for a claim that the event happened, unless it says the \
event itself did not happen. Sources show their publication date when it is known. A fact-check saying an event \
had not happened (for example an earlier false report that someone died) is outdated when trusted news published \
after it reports that the event did happen: NOT_ADDRESSED.

Also give each source a strength from 0.0 to 1.0: how directly and clearly it settles this exact claim. 1.0 is a \
fact-check of exactly this claim, or a source that states exactly this as fact; a fact-check of the same hoax, \
offer or rumour worded differently is still 0.8 or more; 0.5 is a source that addresses the claim only partly or \
indirectly; 0.2 is a source that barely touches it. Use 0.0 for NOT_ADDRESSED.

Then write one short, plain sentence for a general audience that summarizes what the sources found, such as \
"Two fact-checkers rated this claim false." Do not give a final verdict or a score.

Reply with only this JSON object and nothing else:
{"sources": [{"index": 1, "verdict": "TRUE" | "FALSE" | "PARTLY" | "NOT_ADDRESSED", "strength": 0.0}], \
"reason": "..."}"""


def judge_sources(claim, sources):
    """Returns ([stance for each source], [strength for each source], reason), in source order.
    Sources the model left out count as UNRELATED with strength 0."""
    def published(s):
        return f", published {s['date']}" if s.get("date") else ""

    def describe(i, s):
        if s.get("kind", "fact_check") == "fact_check":
            # Fact-check articles found in news feeds have a headline and summary but no rating.
            return (f"[{i}] Fact-check by {s['publisher']} ({s['domain']}){published(s)}\n"
                    f"Claim reviewed: {s.get('claim_reviewed') or '(not given)'}\nTitle: {s['title']}\n"
                    f"Rating: {s.get('rating') or '(none; see title and summary)'}\n"
                    f"Summary: {s.get('summary') or '(none)'}")
        if s.get("kind") == "reference":
            return (f"[{i}] Encyclopedia article from {s['publisher']}: {s['title']}\n"
                    f"Opening text: {s.get('summary') or '(none)'}")
        return (f"[{i}] News article from {s['publisher']} ({s['domain']}){published(s)}\n"
                f"Headline: {s['title']}\nSummary: {s.get('summary') or '(none)'}")

    listing = "\n\n".join(describe(i, s) for i, s in enumerate(sources, start=1))
    user = f"<claim>\n{claim}\n</claim>\n\n<sources>\n{listing}\n</sources>"
    def check(result):
        if not isinstance(result.get("sources"), list):
            raise LLMError("The model's judgement has no list of sources")
        return result

    result = _ask(JUDGE_SYSTEM, user, max_tokens=2000, check=check)
    by_index = {}
    for item in result.get("sources", []):
        if isinstance(item, dict) and "verdict" in item:
            item["stance"] = VERDICT_STANCES.get(item["verdict"])
        if isinstance(item, dict) and item.get("stance") in STANCES and isinstance(item.get("index"), int):
            try:
                strength = max(0.0, min(1.0, float(item.get("strength", 0.0))))
            except (TypeError, ValueError):
                strength = 0.0
            by_index[item["index"]] = (item["stance"], strength)
    judged = [by_index.get(i, ("UNRELATED", 0.0)) for i in range(1, len(sources) + 1)]
    return [s for s, _ in judged], [m for _, m in judged], str(result.get("reason", "")).strip()
