"""Tests for the photo editing Lambda. S3 is replaced with an in-memory fake.

Run with:  python -m unittest backend/photo_edits/test_lambda_function.py
Requires Pillow locally (pip install Pillow).
"""

import base64
import io
import json
import os
import sys
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lambda_function  # noqa: E402


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = (Body, ContentType)

    def generate_presigned_url(self, method, Params, ExpiresIn):
        return f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}?expires={ExpiresIn}"


def _sample_jpeg_b64():
    image = Image.new("RGB", (64, 48))
    for x in range(64):
        for y in range(48):
            image.putpixel((x, y), (x * 4, y * 5, (x + y) * 2))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _api_event(payload, sub=None):
    event = {"body": json.dumps(payload), "isBase64Encoded": False}
    if sub:
        event["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": sub}}}}
    return event


class PhotoEditsTest(unittest.TestCase):
    def setUp(self):
        os.environ["BUCKET_NAME"] = "test-bucket"
        self.s3 = FakeS3()
        lambda_function._s3_client = self.s3
        self.image_b64 = _sample_jpeg_b64()

    def _call(self, payload, sub=None):
        result = lambda_function.lambda_handler(_api_event(payload, sub), None)
        return result["statusCode"], json.loads(result["body"])

    def test_every_operation_produces_a_valid_jpeg(self):
        for operation in lambda_function.EFFECTS:
            with self.subTest(operation=operation):
                status, body = self._call(
                    {"file_name": f"{operation}_1.jpg", "body": self.image_b64, "operation": operation}
                )
                self.assertEqual(status, 200, body)
                data, content_type = self.s3.objects[("test-bucket", body["key"])]
                self.assertEqual(content_type, "image/jpeg")
                with Image.open(io.BytesIO(data)) as saved:
                    self.assertEqual(saved.format, "JPEG")
                    self.assertEqual(saved.size, (64, 48))
                self.assertIn(body["key"], body["url"])

    def test_flip_mirrors_horizontally(self):
        status, body = self._call({"file_name": "f.jpg", "body": self.image_b64, "operation": "flip"})
        self.assertEqual(status, 200)
        data, _ = self.s3.objects[("test-bucket", body["key"])]
        with Image.open(io.BytesIO(data)) as saved:
            left, right = saved.getpixel((2, 20)), saved.getpixel((61, 20))
        # The original gets redder to the right; mirrored, the left is redder.
        self.assertGreater(left[0], right[0])

    def test_unauthenticated_results_go_under_edited_prefix(self):
        status, body = self._call({"file_name": "blur_1.jpg", "body": self.image_b64, "operation": "blur"})
        self.assertEqual(status, 200)
        self.assertEqual(body["key"], "edited/blur_1.jpg")

    def test_verified_user_results_stay_in_the_preview_folder(self):
        # Previews must never land in the library prefix, or they would skip
        # both the app's discard logic and the edited/ lifecycle rule.
        sub = "44b8a418-d021-70bf-86aa-875bc10aeb8c"
        status, body = self._call(
            {"file_name": "blur_1.jpg", "body": self.image_b64, "operation": "blur"}, sub=sub
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["key"], f"edited/{sub}/blur_1.jpg")

    def test_path_traversal_in_file_name_is_neutralised(self):
        status, body = self._call(
            {"file_name": "../../other-user/evil.jpg", "body": self.image_b64, "operation": "blur"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["key"], "edited/evil.jpg")

    def test_unsafe_file_name_is_replaced(self):
        status, body = self._call(
            {"file_name": "bad name?.jpg", "body": self.image_b64, "operation": "sepia"}
        )
        self.assertEqual(status, 200)
        self.assertRegex(body["key"], r"^edited/sepia_[0-9a-f]{32}\.jpg$")

    def test_unknown_operation_is_rejected(self):
        status, body = self._call({"file_name": "x.jpg", "body": self.image_b64, "operation": "cartoon"})
        self.assertEqual(status, 400)
        self.assertIn("supported", body)
        self.assertEqual(self.s3.objects, {})

    def test_invalid_base64_is_rejected(self):
        status, _ = self._call({"file_name": "x.jpg", "body": "not base64!!", "operation": "blur"})
        self.assertEqual(status, 400)

    def test_non_image_is_rejected(self):
        junk = base64.b64encode(b"definitely not an image").decode()
        status, _ = self._call({"file_name": "x.jpg", "body": junk, "operation": "blur"})
        self.assertEqual(status, 400)

    def test_invalid_json_is_rejected(self):
        result = lambda_function.lambda_handler({"body": "{not json"}, None)
        self.assertEqual(result["statusCode"], 400)

    def test_direct_invoke_event_is_supported(self):
        result = lambda_function.lambda_handler(
            {"file_name": "e.jpg", "body": self.image_b64, "operation": "emboss"}, None
        )
        self.assertEqual(result["statusCode"], 200)

    def test_missing_bucket_configuration_fails_cleanly(self):
        del os.environ["BUCKET_NAME"]
        status, _ = self._call({"file_name": "x.jpg", "body": self.image_b64, "operation": "blur"})
        self.assertEqual(status, 500)


if __name__ == "__main__":
    unittest.main()
