"""CloudLens check worker: runs one credibility check in the background.

Invoked asynchronously by the Check API with {"userId", "checkId"}. Each stage
is written to the Checks table, which drives the app's progress screen:

    QUEUED -> READING_TEXT -> FINDING_CLAIM -> SEARCHING_SOURCES -> SCORING -> DONE
                                                                   (or FAILED)

Pipeline:
    1 read text    Amazon Textract
    2 find claim   Claude finds the claim, or says it is an opinion, satire or
                   nothing to check; the rules in claim.py are the fallback
    3 evidence     Google Fact Check, NewsAPI, The Guardian and GDELT in
                   parallel, trusted sources only (CloudLensTrustedSources table)
    4 judge        Claude labels each source Supports, Refutes or Unrelated;
                   without Claude, fact-check ratings are read by rules
    5 score        Milestone 2 formula and verdict rules (scoring.py)

Configuration (Lambda environment variables):
    CHECKS_TABLE, CACHE_TABLE, SOURCES_TABLE, BUCKET_NAME
    T_TRUE=70, T_FALSE=30, MAX_RESULTS=5   verdict limits and result budget
    EVIDENCE_ENABLED=true                  turns steps 3 and 4 on
    GDELT_ENABLED=true                     the keyless GDELT provider
    MODEL_ID                               Bedrock model, default Claude Haiku 4.5
    FACTCHECK_KEY_PARAM, NEWSAPI_KEY_PARAM, GUARDIAN_KEY_PARAM
                                           SSM SecureStrings holding provider API keys
"""

import os
import time
import traceback
from datetime import datetime, timezone

import claim as rules
import evidence
import llm
from scoring import FALSE, TRUE, score_evidence

MAX_OCR_CHARS = 4000
SOURCES_TTL_SECONDS = 300
KEY_PARAMS = {"google_fact_check": "FACTCHECK_KEY_PARAM", "newsapi": "NEWSAPI_KEY_PARAM",
              "guardian": "GUARDIAN_KEY_PARAM"}

_textract = None
_ssm = None
_keys = {}
_sources = None  # (loaded_at, TrustedSources)
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


def _api_keys():
    """Provider API keys from SSM. A missing key is retried on the next check, so adding one needs no redeploy."""
    global _ssm
    for provider, env_name in KEY_PARAMS.items():
        name = os.environ.get(env_name)
        if _keys.get(provider) or not name:
            continue
        if _ssm is None:
            import boto3
            _ssm = boto3.client("ssm")
        try:
            _keys[provider] = _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        except Exception as e:
            code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code")
            if code != "ParameterNotFound":
                print(f"could not read {name}: {code or type(e).__name__}")
    return dict(_keys)


def _trusted_sources():
    """The CloudLensTrustedSources table, refreshed every 5 minutes; the bundled list if it can't be read."""
    global _sources
    if _sources and time.time() - _sources[0] < SOURCES_TTL_SECONDS:
        return _sources[1]
    by_domain = {}
    try:
        table, kwargs = _table("SOURCES_TABLE"), {}
        while True:
            page = table.scan(**kwargs)
            by_domain.update({item["domain"]: item for item in page.get("Items", [])})
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    except Exception:
        traceback.print_exc()
    if not by_domain:
        by_domain = evidence.load_seed()
    _sources = (time.time(), evidence.TrustedSources(by_domain))
    return _sources[1]


def read_text(bucket, key):
    """Step 1: OCR. Returns the text lines Textract found, top to bottom."""
    result = _textract_client().detect_document_text(Document={"S3Object": {"Bucket": bucket, "Name": key}})
    return [b["Text"] for b in result.get("Blocks", []) if b.get("BlockType") == "LINE" and b.get("Text")]


def find_claim(lines):
    """Step 2. Returns (kind, claim, search_query, used_claude)."""
    try:
        found = llm.extract_claim(lines)
        claim = found["claim"].strip() or None
        return found["kind"], claim, found["search_query"].strip() or (claim or ""), True
    except llm.LLMError:
        traceback.print_exc()
        claim = rules.find_claim(lines)
        return ("claim" if claim else "no_claim"), claim, claim or "", False


def explain(lines, kind, claim, problem, sources, scored, reason):
    if not lines:
        return "No readable text was found in this image."
    if kind == "opinion":
        return "This looks like an opinion, not a statement of fact, so it can't be fact-checked."
    if kind == "satire":
        return "This looks like satire or a joke, so it isn't fact-checked."
    if not claim:
        return "No checkable statement was found in the text."
    if problem:
        return problem
    if not sources:
        return "No trusted fact-checks or news reports were found for this claim yet."
    if scored["verdict"] not in (TRUE, FALSE):
        reason = (reason + " " if reason else "") + (
            "Fewer than 2 independent trusted sources agree, so the claim stays Unverified.")
    return reason


def lambda_handler(event, context):
    key = {"userId": event["userId"], "checkId": event["checkId"]}
    item = _table("CHECKS_TABLE").get_item(Key=key).get("Item")
    if not item:
        print(f"check not found: {key}")
        return
    evidence_enabled = os.environ.get("EVIDENCE_ENABLED", "false").lower() == "true"
    gdelt_enabled = os.environ.get("GDELT_ENABLED", "true").lower() == "true"
    max_results = int(os.environ.get("MAX_RESULTS", "5"))
    try:
        _update(key, status="READING_TEXT")
        lines = read_text(os.environ["BUCKET_NAME"], item["imageKey"])

        _update(key, status="FINDING_CLAIM")
        kind, claim, query, used_claude = find_claim(lines) if lines else ("no_claim", None, "", True)
        # A result is cached only if every step it needed ran without a fallback.
        complete = used_claude

        _update(key, status="SEARCHING_SOURCES")
        sources, problem, report = [], None, {}
        if claim and kind == "claim":
            if not evidence_enabled:
                problem, complete = "Evidence search is not enabled yet, so this claim cannot be verified.", False
            else:
                enabled = [name for name, _, _ in evidence.PROVIDERS if name != "gdelt" or gdelt_enabled]
                sources, report = evidence.find_evidence(query, _api_keys(), _trusted_sources(), max_results, enabled)
                if report.get(evidence.PRIMARY) != "ok":
                    complete = False  # the main fact-check source was missing or failed
                    if not sources and report.get(evidence.PRIMARY) == "skipped":
                        problem = "Fact-check search is not configured yet, so this claim cannot be verified."
                    elif not sources:
                        problem = "The fact-check search could not be completed. Please try again."

        _update(key, status="SCORING")
        stances, reason = [], ""
        if sources:
            try:
                stances, reason = llm.judge_sources(claim, sources)
            except llm.LLMError:
                traceback.print_exc()
                stances = [evidence.rating_stance(s) for s in sources]
                reason, complete = evidence.summarize(sources, stances), False
        scored = score_evidence(
            [{"domain": s["domain"], "weight": s["weight"], "stance": st} for s, st in zip(sources, stances)],
            int(os.environ.get("T_TRUE", "70")), int(os.environ.get("T_FALSE", "30")))
        result = {
            "verdict": scored["verdict"],
            "score": scored["score"],
            "reason": explain(lines, kind, claim, problem, sources, scored, reason),
            "claim": claim,
            "sources": [{"kind": s["kind"], "publisher": s["publisher"], "domain": s["domain"], "url": s["url"],
                         "title": s["title"], "rating": s["rating"], "stance": st}
                        for s, st in zip(sources, stances)],
        }
        _update(key, **result, kind=kind, providers=report, ocrText="\n".join(lines)[:MAX_OCR_CHARS],
                status="DONE", finishedAt=_now())
        if complete:
            _table("CACHE_TABLE").put_item(Item={"imageHash": item["imageHash"], **result})
    except Exception:
        traceback.print_exc()
        _update(key, status="FAILED", error="The check could not be completed.", finishedAt=_now())
