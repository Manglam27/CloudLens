"""CloudLens Check API: starts credibility checks and returns their results.

Routes on the cloudlens-api HTTP API. Every route sits behind a JWT authorizer
for the Cognito user pool, so the caller's user id is the token's `sub` claim.

    POST /checks             body {"imageKey": "<userId>_<id>.jpg"}
                             -> 202 {check} queued, or 200 {check} from the cache
    GET  /checks             -> 200 {"checks": [...]} newest first (FR 1.6)
    GET  /checks/{checkId}   -> 200 {check} for polling and results (FR 1.4, 1.5)

Configuration (Lambda environment variables):
    CHECKS_TABLE      DynamoDB table, PK userId, SK checkId
    CACHE_TABLE       DynamoDB table, PK imageHash
    BUCKET_NAME       S3 bucket holding the users' photos
    WORKER_FUNCTION   name of the check worker Lambda, invoked asynchronously
"""

import hashlib
import json
import os
import re
import time
import traceback
import uuid
from datetime import datetime, timezone
from decimal import Decimal

# Textract's synchronous API accepts images up to 10 MB.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
HISTORY_LIMIT = 50
RESULT_FIELDS = ("verdict", "score", "reason", "claim", "sources")
PUBLIC_FIELDS = ("checkId", "status", "imageKey", "createdAt", "finishedAt", "cached", "error") + RESULT_FIELDS
_CHECK_ID = re.compile(r"\d{13}-[0-9a-f]{8}")

_s3 = None
_lambda = None
_tables = {}


def _s3_client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    return _s3


def _lambda_client():
    global _lambda
    if _lambda is None:
        import boto3
        _lambda = boto3.client("lambda")
    return _lambda


def _table(env_name):
    name = os.environ[env_name]
    if name not in _tables:
        import boto3
        _tables[name] = boto3.resource("dynamodb").Table(name)
    return _tables[name]


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=_json_default),
    }


def _public(item):
    return {k: item[k] for k in PUBLIC_FIELDS if k in item}


def _user_id(event):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims") or {}
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", sub) else None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_check_id():
    # Millisecond timestamp first, so sorting by checkId sorts by time.
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


def _error_code(exc):
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")


def _start_check(user_id, event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Request body is not valid JSON."})
    key = body.get("imageKey") if isinstance(body, dict) else None
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,140}\.(jpg|jpeg|png)", key, re.I):
        return _response(400, {"error": "imageKey is missing or invalid."})
    # Uploads are named <userId>_<id>.jpg; a user may only check their own photos.
    if not key.startswith(f"{user_id}_"):
        return _response(403, {"error": "You can only check your own photos."})

    try:
        obj = _s3_client().get_object(Bucket=os.environ["BUCKET_NAME"], Key=key)
    except Exception as exc:
        if _error_code(exc) in ("NoSuchKey", "404", "NotFound"):
            return _response(404, {"error": "Photo not found."})
        raise
    if obj.get("ContentLength", 0) > MAX_IMAGE_BYTES:
        return _response(413, {"error": "Photo is larger than 10 MB."})
    image_hash = hashlib.sha256(obj["Body"].read()).hexdigest()

    now = _now()
    item = {"userId": user_id, "checkId": _new_check_id(), "imageKey": key, "imageHash": image_hash,
            "createdAt": now}

    # Consistency NFR: a screenshot that was checked before gets the same result.
    cached = _table("CACHE_TABLE").get_item(Key={"imageHash": image_hash}).get("Item")
    if cached:
        item.update({k: cached[k] for k in RESULT_FIELDS if k in cached})
        item.update(status="DONE", cached=True, finishedAt=now)
        _table("CHECKS_TABLE").put_item(Item=item)
        return _response(200, _public(item))

    item["status"] = "QUEUED"
    checks = _table("CHECKS_TABLE")
    checks.put_item(Item=item)
    try:
        _lambda_client().invoke(
            FunctionName=os.environ["WORKER_FUNCTION"],
            InvocationType="Event",
            Payload=json.dumps({"userId": user_id, "checkId": item["checkId"]}).encode(),
        )
    except Exception:
        traceback.print_exc()
        checks.update_item(
            Key={"userId": user_id, "checkId": item["checkId"]},
            UpdateExpression="SET #s = :s, #e = :e",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={":s": "FAILED", ":e": "The check could not be started."},
        )
        return _response(500, {"error": "The check could not be started."})
    return _response(202, _public(item))


def _get_check(user_id, check_id):
    if not isinstance(check_id, str) or not _CHECK_ID.fullmatch(check_id):
        return _response(400, {"error": "checkId is invalid."})
    # The key includes the caller's user id, so other users' checks are never found.
    item = _table("CHECKS_TABLE").get_item(Key={"userId": user_id, "checkId": check_id}).get("Item")
    if not item:
        return _response(404, {"error": "Check not found."})
    return _response(200, _public(item))


def _list_checks(user_id):
    result = _table("CHECKS_TABLE").query(
        KeyConditionExpression="userId = :u",
        ExpressionAttributeValues={":u": user_id},
        ScanIndexForward=False,
        Limit=HISTORY_LIMIT,
    )
    return _response(200, {"checks": [_public(i) for i in result.get("Items", [])]})


def lambda_handler(event, context):
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Sign in required."})
    route = event.get("routeKey", "")
    try:
        if route == "POST /checks":
            return _start_check(user_id, event)
        if route == "GET /checks":
            return _list_checks(user_id)
        if route == "GET /checks/{checkId}":
            return _get_check(user_id, (event.get("pathParameters") or {}).get("checkId"))
        return _response(404, {"error": f"Unknown route: {route}"})
    except Exception:
        traceback.print_exc()
        return _response(500, {"error": "Something went wrong. Please try again."})
