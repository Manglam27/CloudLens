"""CloudLens check worker: runs one credibility check in the background.

Invoked asynchronously by the Check API with {"userId", "checkId"}. Each stage
is written to the Checks table, which drives the app's progress screen:

    QUEUED -> READING_TEXT -> FINDING_CLAIM -> SEARCHING_SOURCES -> SCORING -> DONE
                                                                   (or FAILED)

Pipeline:
    1 read text    Amazon Textract, on the photo and on a light-text version of it
                   (for white meme captions); the reading with more words is used
    2 find claim   the LLM (NVIDIA Nemotron) finds the claim, or says it is an
                   opinion, satire or nothing to check; the rules in claim.py are the fallback
    3 evidence     Google Fact Check, Google News, Bing News, Wikipedia, NewsAPI and
                   The Guardian in parallel, trusted sources only (CloudLensTrustedSources table)
    4 judge        the LLM labels each source Supports, Refutes, Mixed or Unrelated
                   and rates how directly it settles the claim; without the LLM,
                   fact-check ratings, headlines and keyword matches are read by rules
    5 score        direction and certainty of the evidence (scoring.py)

Configuration (Lambda environment variables):
    CHECKS_TABLE, CACHE_TABLE, SOURCES_TABLE, BUCKET_NAME
    LLM_TABLE=CloudLensLLM, LLM_RPM=4, FALLBACK_MODEL_ID, FALLBACK_LLM_RPM=24, LLM_DEADLINE_SECONDS=75
                                           per-model NVIDIA rate limits, answer cache and
                                           fallback model (gate.py, llm.py)
    T_TRUE=70, T_FALSE=30, MIN_SOURCES=2, MAX_RESULTS=10  verdict limits and result budget
    EVIDENCE_ENABLED=true                  turns steps 3 and 4 on
    MODEL_ID                               NVIDIA model, default Nemotron 3 Ultra
    NVIDIA_KEY_PARAM, FACTCHECK_KEY_PARAM, NEWSAPI_KEY_PARAM, GUARDIAN_KEY_PARAM
                                           SSM SecureStrings holding the API keys
"""

import concurrent.futures
import io
import os
import time
import traceback
from datetime import datetime, timezone

import claim as rules
import evidence
import llm
from gate import Gate
from scoring import FALSE, TRUE, score_evidence

MAX_OCR_CHARS = 4000
RULES_NOTE = "The AI judge was busy, so only fact-check ratings were read; check again in a minute for a full result."
BACKUP_NOTE = "Checked by a backup AI model while the main one was busy; check again later for its answer."
LIGHT_LEVEL = 200  # gray level (0-255) from which a pixel counts as light text
MIN_LINE_CONFIDENCE = 80  # Textract confidence (%) for a line to count as read
MAX_PIXELS = 40_000_000  # larger photos skip the light-text reading
MAX_TEXTRACT_BYTES = 10 * 1024 * 1024  # Textract's limit for an image sent as bytes
SOURCES_TTL_SECONDS = 300
KEY_PARAMS = {"nvidia": "NVIDIA_KEY_PARAM", "google_fact_check": "FACTCHECK_KEY_PARAM",
              "newsapi": "NEWSAPI_KEY_PARAM", "guardian": "GUARDIAN_KEY_PARAM"}

_textract = None
_s3 = None
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


def _s3_client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    return _s3


def _ocr(document):
    """Textract's LINE blocks for one image, top to bottom."""
    blocks = _textract_client().detect_document_text(Document=document).get("Blocks", [])
    return [b for b in blocks if b.get("BlockType") == "LINE" and b.get("Text")]


def light_text_image(data):
    """The photo as black text on white, keeping only its near-white pixels, as PNG
    bytes; None if it can't be made. Meme captions are white letters with a black
    outline over a picture, which Textract reads poorly or not at all; turned into
    plain dark text they read like a document."""
    from PIL import Image, ImageOps  # bundled from requirements.txt
    image = Image.open(io.BytesIO(data))
    if image.width * image.height > MAX_PIXELS:
        return None
    mask = ImageOps.exif_transpose(image).convert("L").point([0 if v >= LIGHT_LEVEL else 255 for v in range(256)])
    out = io.BytesIO()
    mask.save(out, "PNG", optimize=True)
    return out.getvalue() if out.tell() <= MAX_TEXTRACT_BYTES else None


def _ocr_light_text(bucket, key):
    data = _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    png = light_text_image(data)
    return _ocr({"Bytes": png}) if png else []


def readable_words(lines):
    """Words on the lines Textract is fairly sure of."""
    return sum(len(b["Text"].split()) for b in lines if b.get("Confidence", 100) >= MIN_LINE_CONFIDENCE)


def read_text(bucket, key):
    """Step 1: OCR. Returns the text lines Textract found, top to bottom.

    Two readings run in parallel: the photo as it is, and its light-text version
    (light_text_image). The one with more confidently read words is used; the
    photo as it is wins a tie. If the second reading fails, the first is used."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(_ocr, {"S3Object": {"Bucket": bucket, "Name": key}})
        light = pool.submit(_ocr_light_text, bucket, key)
    lines = original.result()
    try:
        alternative = light.result()
    except Exception:
        traceback.print_exc()
        alternative = []
    if readable_words(alternative) > readable_words(lines):
        print(f"OCR: light-text reading used ({readable_words(alternative)} words, not {readable_words(lines)})")
        lines = alternative
    return [b["Text"] for b in lines]


def find_claim(lines):
    """Step 2. Returns (kind, claim, search_query, topic_query, used_llm)."""
    try:
        found = llm.extract_claim(lines)
        claim = found["claim"].strip() or None
        return (found["kind"], claim, found["search_query"].strip() or (claim or ""), found["topic_query"].strip(),
                True)
    except llm.LLMError:
        traceback.print_exc()
        claim = rules.find_claim(lines)
        query = rules.search_query(claim) if claim else ""
        return ("claim" if claim else "no_claim"), claim, query, "", False


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
        return "No trusted fact-checks, news reports or reference articles about this claim were found yet."
    if scored["verdict"] not in (TRUE, FALSE) and scored["for"] + scored["against"]:
        if scored["score"] == 50:
            tail = "The sources disagree, so the claim stays Unverified."
        else:
            lean = "true" if scored["score"] > 50 else "false"
            tail = f"The evidence leans {lean}, but it is not strong enough for a verdict yet."
        reason = (reason + " " if reason else "") + tail
    return reason


def lambda_handler(event, context):
    key = {"userId": event["userId"], "checkId": event["checkId"]}
    item = _table("CHECKS_TABLE").get_item(Key=key).get("Item")
    if not item:
        print(f"check not found: {key}")
        return
    evidence_enabled = os.environ.get("EVIDENCE_ENABLED", "false").lower() == "true"
    max_results = int(os.environ.get("MAX_RESULTS", "10"))
    # Every check shares NVIDIA's limits through the CloudLensLLM table (gate.py), one
    # budget per model; no LLM request starts after the deadline, so the result still
    # arrives while the app waits.
    started = time.time()
    llm_table = _table("LLM_TABLE") if os.environ.get("LLM_TABLE") else None
    primary, *fallbacks = llm.models()
    llm.gates = {primary: Gate(llm_table, rpm=os.environ.get("LLM_RPM", "4"), name="main")}
    for i, model in enumerate(fallbacks, start=1):
        llm.gates[model] = Gate(llm_table, rpm=os.environ.get("FALLBACK_LLM_RPM", "24"), name=f"fallback{i}")
    llm.set_deadline(started + float(os.environ.get("LLM_DEADLINE_SECONDS", "75")))
    used_models = {}  # step -> model that answered it
    timings, mark = {}, [time.time()]

    def lap(stage):
        now = time.time()
        timings[stage] = round((now - mark[0]) * 1000)
        mark[0] = now

    try:
        _update(key, status="READING_TEXT")
        lines = read_text(os.environ["BUCKET_NAME"], item["imageKey"])
        keys = _api_keys()
        llm.set_api_key(keys.get("nvidia"))
        lap("ocrMs")

        _update(key, status="FINDING_CLAIM")
        llm.last_model = None
        kind, claim, query, topic, used_llm = find_claim(lines) if lines else ("no_claim", None, "", "", True)
        if used_llm and llm.last_model:
            used_models["claim"] = llm.last_model
        lap("claimMs")
        # A result is cached only if every step it needed ran without a fallback.
        complete = used_llm

        _update(key, status="SEARCHING_SOURCES")
        sources, problem, report = [], None, {}
        if claim and kind == "claim":
            if not evidence_enabled:
                problem, complete = "Evidence search is not enabled yet, so this claim cannot be verified.", False
            else:
                sources, report = evidence.find_evidence(query, keys, _trusted_sources(), max_results,
                                                         also_try=[claim], topic=topic)
                if report.get(evidence.PRIMARY) != "ok":
                    complete = False  # the main fact-check source was missing or failed
                    if not sources and report.get(evidence.PRIMARY) == "skipped":
                        problem = "Fact-check search is not configured yet, so this claim cannot be verified."
                    elif not sources:
                        problem = "The fact-check search could not be completed. Please try again."
        lap("searchMs")

        _update(key, status="SCORING")
        stances, strengths, reason = [], [], ""
        judged_by = used_models.get("claim", primary) if used_llm else "rules"
        if sources:
            try:
                llm.last_model = None
                stances, strengths, reason = llm.judge_sources(claim, sources)
                judged_by = used_models["judge"] = llm.last_model or primary
                for i in evidence.outdated_fact_checks(sources, stances, strengths):
                    stances[i], sources[i]["outdated"] = "RELATED", True
            except llm.LLMError:
                traceback.print_exc()
                stances = evidence.fallback_stances(sources)
                strengths = [evidence.match_strength(s) for s in sources]
                for i in evidence.outdated_fact_checks(sources, stances, strengths):
                    stances[i], sources[i]["outdated"] = "RELATED", True
                # Keyword rules can't tell a story that confirms the claim from one that only
                # shares its words ("Vaccines do not cause autism"), so without the model they
                # never back a claim up: only fact-check ratings against it count. The result
                # is not cached, so checking again later gets the model's judgement.
                stances = ["RELATED" if st == "SUPPORTS" else st for st in stances]
                reason, complete, judged_by = evidence.summarize(sources, stances), False, "rules"
                reason = (reason + " " if reason else "") + RULES_NOTE
        # Only answers from the main model are kept for the next check of this image;
        # a fallback model's answer is shown but the main model gets to try again.
        if any(m != primary for m in used_models.values()):
            complete = False
            if used_models.get("judge", primary) != primary:
                reason = (reason + " " if reason else "") + BACKUP_NOTE
        lap("judgeMs")
        scored = score_evidence(
            [{"domain": s["domain"], "weight": s["weight"], "stance": st, "kind": s["kind"], "strength": m,
              "about_media": s.get("about_media", False),
              # A fact-check outranks the news only if its own rating or headline agrees with the
              # AI's reading of it (scoring.py), so one misread fact-check can't set the news aside.
              "confirmed": s["kind"] != "fact_check" or evidence.fallback_stance(s) == st}
             for s, st, m in zip(sources, stances, strengths)],
            int(os.environ.get("T_TRUE", "70")), int(os.environ.get("T_FALSE", "30")),
            int(os.environ.get("MIN_SOURCES", "2")))
        # Off-topic search results are not shown.
        shown = [(s, st, m) for s, st, m in zip(sources, stances, strengths) if st != "UNRELATED"]
        checked = bool(claim) and kind == "claim" and not problem
        result = {
            "verdict": scored["verdict"],
            "score": scored["score"] if checked else None,
            "reason": explain(lines, kind, claim, problem, [s for s, _, _ in shown], scored, reason),
            "claim": claim,
            # Weights, strengths and certainty as percentages: DynamoDB does not store floats.
            "sources": [{"kind": s["kind"], "publisher": s["publisher"], "domain": s["domain"], "url": s["url"],
                         "title": s["title"], "rating": s["rating"], "stance": st,
                         "weight": round(s["weight"] * 100), "strength": round(m * 100)}
                        for s, st, m in shown],
            "certainty": round(scored["certainty"] * 100),
            "judgedBy": judged_by,
        }
        timings["llmWaitMs"] = round(sum(g.waited for g in llm.gates.values()) * 1000)
        timings["totalMs"] = round((time.time() - started) * 1000)
        # For review and testing only (the app shows `sources`): what the model was asked
        # to search for, and every source judged, off-topic ones included.
        details = {"searchQuery": query or "", "topicQuery": topic or "", "timings": timings, "models": used_models,
                   "judged": [{"domain": s["domain"], "kind": s["kind"], "stance": st, "strength": round(m * 100),
                               "title": s["title"][:200], "date": s.get("date", ""),
                               "outdated": bool(s.get("outdated"))}
                              for s, st, m in zip(sources, stances, strengths)]}
        _update(key, **result, **details, kind=kind, providers=report,
                ocrText="\n".join(lines)[:MAX_OCR_CHARS], status="DONE", finishedAt=_now())
        if complete:
            _table("CACHE_TABLE").put_item(Item={"imageHash": item["imageHash"], **result,
                                                 "resultsVersion": os.environ.get("RESULTS_VERSION", "1")})
    except Exception:
        traceback.print_exc()
        _update(key, status="FAILED", error="The check could not be completed.", finishedAt=_now())
