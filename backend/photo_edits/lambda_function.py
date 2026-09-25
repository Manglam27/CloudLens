"""CloudLens photo editing Lambda.

Receives an image from the app, applies one of the supported effects, saves the
result to the CloudLens S3 bucket and returns a temporary link to it.

Request body (JSON), as sent by lib/Pages/editing_page.dart:
    {"file_name": "blur_1727000000000.jpg",
     "body": "<base64-encoded JPEG>",
     "operation": "blur"}

Response body (JSON):
    {"url": "<presigned GET URL for the edited image>",
     "key": "<S3 object key of the edited image>"}

Configuration (Lambda environment variables):
    BUCKET_NAME           S3 bucket to write edited images to (required)
    URL_EXPIRES_SECONDS   Lifetime of the returned link, default 3600
"""

import base64
import binascii
import io
import json
import os
import re
import traceback
import uuid

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError

# The app compresses images to about 1024 px before sending, so anything much
# larger than this is not a real request. API Gateway caps payloads anyway.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
_SAFE_USER_ID = re.compile(r"[A-Za-z0-9-]{1,64}")

_s3_client = None


def _s3():
    """Create the S3 client lazily so the module imports without AWS access."""
    global _s3_client
    if _s3_client is None:
        import boto3
        from botocore.config import Config

        _s3_client = boto3.client(
            "s3",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            config=Config(signature_version="s3v4"),
        )
    return _s3_client


# --- Effects ----------------------------------------------------------------
# Every effect takes and returns a PIL image. Inputs are always RGB.


def _invert(img):
    return ImageOps.invert(img)


def _grayscale(img):
    return ImageOps.grayscale(img)


def _blur(img):
    return img.filter(ImageFilter.GaussianBlur(radius=4))


def _edge(img):
    return ImageOps.grayscale(img).filter(ImageFilter.FIND_EDGES)


def _flip(img):
    # Horizontal mirror, which is what users expect from a "flip" button.
    return ImageOps.mirror(img)


def _brightness(img):
    return ImageEnhance.Brightness(img).enhance(1.4)


def _contrast(img):
    return ImageEnhance.Contrast(img).enhance(1.5)


def _sharpen(img):
    return ImageEnhance.Sharpness(img).enhance(2.5)


def _sepia(img):
    return ImageOps.colorize(
        ImageOps.grayscale(img), black="#2b1d0e", mid="#a67c52", white="#f7ecd7"
    )


def _pencil(img):
    # Classic pencil sketch: colour-dodge the grayscale image with a blurred,
    # inverted copy of itself. Pillow has no dodge blend, so do it per pixel.
    gray = ImageOps.grayscale(img)
    blurred = ImageOps.invert(gray).filter(ImageFilter.GaussianBlur(radius=12))
    dodged = bytes(
        min(255, (g * 255) // (256 - b))
        for g, b in zip(gray.tobytes(), blurred.tobytes())
    )
    return Image.frombytes("L", gray.size, dodged)


def _threshold(img):
    return ImageOps.grayscale(img).point(lambda p: 255 if p >= 128 else 0)


def _emboss(img):
    return img.filter(ImageFilter.EMBOSS)


EFFECTS = {
    "invert": _invert,
    "grayscale": _grayscale,
    "blur": _blur,
    "edge": _edge,
    "flip": _flip,
    "brightness": _brightness,
    "contrast": _contrast,
    "sharpen": _sharpen,
    "sepia": _sepia,
    "pencil": _pencil,
    "threshold": _threshold,
    "emboss": _emboss,
}


# --- Request handling -------------------------------------------------------


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _parse_request(event):
    """Return the request dict from an API Gateway event or a direct invoke."""
    if "operation" in event:
        return event

    raw = event.get("body")
    if raw is None:
        raise ValueError("Request has no body.")
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        request = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Request body is not valid JSON.")
    if not isinstance(request, dict):
        raise ValueError("Request body must be a JSON object.")
    return request


def _user_id(event):
    """Cognito user id (sub) when API Gateway has verified the caller's token."""
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims") or authorizer.get("claims") or {}
    sub = claims.get("sub")
    if isinstance(sub, str) and _SAFE_USER_ID.fullmatch(sub):
        return sub
    return None


def _object_key(event, file_name, operation):
    name = os.path.basename(file_name) if isinstance(file_name, str) else ""
    if not _SAFE_NAME.fullmatch(name):
        name = f"{operation}_{uuid.uuid4().hex}.jpg"
    if not name.lower().endswith((".jpg", ".jpeg")):
        name += ".jpg"

    # With a verified user, use the same "<userId>_" prefix as the app's own
    # uploads so the edited image shows up in that user's cloud library.
    user_id = _user_id(event)
    return f"{user_id}_{name}" if user_id else f"edited/{name}"


def lambda_handler(event, context):
    bucket = os.environ.get("BUCKET_NAME")
    if not bucket:
        print("BUCKET_NAME environment variable is not set")
        return _response(500, {"error": "Server is not configured."})

    try:
        request = _parse_request(event)
    except ValueError as e:
        return _response(400, {"error": str(e)})

    operation = request.get("operation")
    effect = EFFECTS.get(operation)
    if effect is None:
        return _response(
            400,
            {"error": f"Unsupported operation: {operation!r}.", "supported": sorted(EFFECTS)},
        )

    try:
        image_bytes = base64.b64decode(request.get("body") or "", validate=True)
    except (binascii.Error, ValueError):
        return _response(400, {"error": "Image body is not valid base64."})
    if not image_bytes:
        return _response(400, {"error": "Image body is empty."})
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return _response(413, {"error": "Image is too large."})

    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError):
        return _response(400, {"error": "Body is not a readable image."})

    try:
        edited = effect(image).convert("RGB")
        output = io.BytesIO()
        edited.save(output, format="JPEG", quality=90)

        key = _object_key(event, request.get("file_name"), operation)
        s3 = _s3()
        s3.put_object(
            Bucket=bucket, Key=key, Body=output.getvalue(), ContentType="image/jpeg"
        )
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=int(os.environ.get("URL_EXPIRES_SECONDS", "3600")),
        )
    except Exception:
        traceback.print_exc()
        return _response(500, {"error": "Could not process the image."})

    return _response(200, {"url": url, "key": key})
