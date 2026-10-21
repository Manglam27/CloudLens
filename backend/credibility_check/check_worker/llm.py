"""Claude on Amazon Bedrock for the two judgement steps of a check:

    extract_claim  step 2: the checkable claim in the OCR text, or why there is none
    judge_sources  step 4: whether each fact-check supports or refutes the claim

Both use structured outputs, so the reply is JSON that matches the schema, and
temperature 0 (sent through extra_body, since SDK 1.x dropped the argument), so
the same screenshot gets the same answer (consistency NFR).
The model is set by MODEL_ID (default Claude Haiku 4.5) and authenticated with
the Lambda role's AWS credentials; there is no API key.
"""

import json
import os

try:
    import anthropic

    _API_ERRORS = (anthropic.APIConnectionError, anthropic.APIStatusError)
except ImportError:  # unit tests run without the SDK and inject a fake client
    anthropic = None
    _API_ERRORS = ()

DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
STANCES = ("SUPPORTS", "REFUTES", "UNRELATED")

_client = None


class LLMError(Exception):
    """Claude could not produce a usable answer."""


def _claude():
    global _client
    if _client is None:
        if anthropic is None:
            raise LLMError("The anthropic SDK is not installed")
        _client = anthropic.AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-east-1"))
    return _client


def _ask(system, user, schema, max_tokens):
    try:
        response = _claude().messages.create(
            model=os.environ.get("MODEL_ID", DEFAULT_MODEL),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            # SDK 1.x has no temperature argument; Haiku 4.5 still honours it, and
            # temperature 0 keeps verdicts repeatable (consistency NFR).
            extra_body={"temperature": 0},
        )
    except _API_ERRORS as e:
        raise LLMError(f"Claude request failed: {type(e).__name__}") from e
    if response.stop_reason != "end_turn":
        raise LLMError(f"Claude stopped with {response.stop_reason}")
    text = next((b.text for b in response.content if b.type == "text"), None)
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as e:
        raise LLMError("Claude did not return valid JSON") from e


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
other than a claim, return an empty claim and an empty search_query."""

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["claim", "opinion", "satire", "no_claim"]},
        "claim": {"type": "string"},
        "search_query": {"type": "string"},
    },
    "required": ["kind", "claim", "search_query"],
    "additionalProperties": False,
}


def extract_claim(lines):
    """Returns {"kind", "claim", "search_query"}."""
    text = "\n".join(lines)
    result = _ask(CLAIM_SYSTEM, f"<ocr_text>\n{text}\n</ocr_text>", CLAIM_SCHEMA, max_tokens=512)
    if result.get("kind") == "claim" and not result.get("claim", "").strip():
        raise LLMError("Claude classified the text as a claim but returned no claim")
    return result


JUDGE_SYSTEM = """You compare a claim with published fact-checks. Each fact-check has the claim it reviewed, its \
title and its rating. Judge only from these fields, not from your own knowledge.

For each numbered fact-check, decide its stance toward the claim:
- SUPPORTS: it reviews the same claim and rates it true, mostly true, or correct.
- REFUTES: it reviews the same claim and rates it false, mostly false, fabricated, or misleading.
- UNRELATED: it reviews a different claim, or its rating is mixed or unclear (for example half true, unproven, \
or missing context).

Then write one short, plain sentence for a general audience that summarizes what the fact-checks found, such as \
"Two fact-checkers rated this claim false." Do not give a final verdict or a score."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "stances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "stance": {"type": "string", "enum": list(STANCES)},
                },
                "required": ["index", "stance"],
                "additionalProperties": False,
            },
        },
        "reason": {"type": "string"},
    },
    "required": ["stances", "reason"],
    "additionalProperties": False,
}


def judge_sources(claim, sources):
    """Returns ([stance for each source, in order], reason). Missing indexes count as UNRELATED."""
    listing = "\n\n".join(
        f"[{i}] {s['publisher']} ({s['domain']})\n"
        f"Claim reviewed: {s['claim_reviewed']}\nTitle: {s['title']}\nRating: {s['rating']}"
        for i, s in enumerate(sources, start=1)
    )
    user = f"<claim>\n{claim}\n</claim>\n\n<fact_checks>\n{listing}\n</fact_checks>"
    result = _ask(JUDGE_SYSTEM, user, JUDGE_SCHEMA, max_tokens=1024)
    by_index = {}
    for item in result.get("stances", []):
        if item.get("stance") in STANCES and isinstance(item.get("index"), int):
            by_index[item["index"]] = item["stance"]
    stances = [by_index.get(i, "UNRELATED") for i in range(1, len(sources) + 1)]
    return stances, result.get("reason", "").strip()
