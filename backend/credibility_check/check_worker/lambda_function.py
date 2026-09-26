"""CloudLens check worker: runs one credibility check in the background.

Invoked asynchronously by the Check API with {"userId", "checkId"}. Each stage
is written to the Checks table, which drives the app's progress screen:

    QUEUED -> READING_TEXT -> FINDING_CLAIM -> SEARCHING_SOURCES -> SCORING -> DONE
                                                                   (or FAILED)

Skeleton version: OCR (Amazon Textract), rule-based claim finding and the
scoring formula run for real. Evidence search is switched off with
EVIDENCE_ENABLED=false, so every verdict is Unverified until it is added.

Configuration (Lambda environment variables):
    CHECKS_TABLE, CACHE_TABLE, BUCKET_NAME
    T_TRUE=70, T_FALSE=30, MAX_RESULTS=5   verdict limits and result budget
    EVIDENCE_ENABLED=false                 turns the evidence step on
    MODEL_ID                               Bedrock model for the upcoming LLM steps
"""

import os
import traceback
from datetime import datetime, timezone

from claim import find_claim
from scoring import FALSE, TRUE, score_evidence

MAX_OCR_CHARS = 4000

_textract = None
_tables = {}


def _textract_client():
    global _textract
    if _textract is None:
        import boto3
        _textract = boto3.client("textract")
    return _textract


def _table(env_name):
    name = os.environ[env_name]
    if name not in _tables:
        import boto3
        _tables[name] = boto3.resource("dynamodb").Table(name)
    return _tables[name]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _update(key, **fields):
    names = {f"#f{i}": name for i, name in enumerate(fields)}
    values = {f":v{i}": value for i, value in enumerate(fields.values())}
    _table("CHECKS_TABLE").update_item(
        Key=key,
        UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields))),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def read_text(bucket, key):
    """Step 1: OCR. Returns the text lines Textract found, top to bottom."""
    result = _textract_client().detect_document_text(Document={"S3Object": {"Bucket": bucket, "Name": key}})
    return [b["Text"] for b in result.get("Blocks", []) if b.get("BlockType") == "LINE" and b.get("Text")]


def find_evidence(claim, max_results):
    """Step 3: evidence search. Not part of the skeleton yet."""
    return []


def explain(lines, claim, evidence_enabled, scored):
    if not lines:
        return "No readable text was found in this image."
    if not claim:
        return "No checkable statement was found in the text."
    if not evidence_enabled:
        return "Evidence search is not enabled yet, so this claim cannot be verified."
    if scored["verdict"] == TRUE:
        return f"{scored['supporters']} independent trusted sources support this claim."
    if scored["verdict"] == FALSE:
        return f"{scored['refuters']} independent trusted sources refute this claim."
    if scored["supporters"] + scored["refuters"] == 0:
        return "No trusted sources were found for this claim."
    return "Trusted sources disagree or there are too few of them to decide."


def lambda_handler(event, context):
    key = {"userId": event["userId"], "checkId": event["checkId"]}
    item = _table("CHECKS_TABLE").get_item(Key=key).get("Item")
    if not item:
        print(f"check not found: {key}")
        return
    evidence_enabled = os.environ.get("EVIDENCE_ENABLED", "false").lower() == "true"
    try:
        _update(key, status="READING_TEXT")
        lines = read_text(os.environ["BUCKET_NAME"], item["imageKey"])

        _update(key, status="FINDING_CLAIM")
        claim = find_claim(lines) if lines else None

        _update(key, status="SEARCHING_SOURCES")
        evidence = []
        if claim and evidence_enabled:
            evidence = find_evidence(claim, int(os.environ.get("MAX_RESULTS", "5")))

        _update(key, status="SCORING")
        scored = score_evidence(evidence, int(os.environ.get("T_TRUE", "70")), int(os.environ.get("T_FALSE", "30")))
        result = {
            "verdict": scored["verdict"],
            "score": scored["score"],
            "reason": explain(lines, claim, evidence_enabled, scored),
            "claim": claim,
            "sources": [],
        }
        _update(key, **result, ocrText="\n".join(lines)[:MAX_OCR_CHARS], status="DONE", finishedAt=_now())
        # Only cache real verdicts. Skeleton results would otherwise stay
        # Unverified after evidence search is switched on.
        if evidence_enabled:
            _table("CACHE_TABLE").put_item(Item={"imageHash": item["imageHash"], **result})
    except Exception:
        traceback.print_exc()
        _update(key, status="FAILED", error="The check could not be completed.", finishedAt=_now())
