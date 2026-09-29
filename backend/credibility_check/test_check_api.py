"""Tests for the Check API Lambda. Run: python -m unittest discover backend/credibility_check"""

import hashlib
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fakes import FakeLambda, FakeS3, FakeTable  # noqa: E402

spec = importlib.util.spec_from_file_location("check_api", HERE / "check_api" / "lambda_function.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

USER = "44b8a418-d021-70bf-86aa-875bc10aeb8c"
OTHER = "345864a8-60c1-707e-e0c2-1a580265b048"
KEY = f"{USER}_0d75e4b5-a65e-420c-ab95-a59b24fb7d51.jpg"
IMAGE = b"fake jpeg bytes"


def event(route, user=USER, body=None, check_id=None):
    e = {"routeKey": route, "requestContext": {"authorizer": {"jwt": {"claims": {"sub": user}}}}}
    if user is None:
        e["requestContext"] = {}
    if body is not None:
        e["body"] = json.dumps(body)
    if check_id is not None:
        e["pathParameters"] = {"checkId": check_id}
    return e


class CheckApiTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(CHECKS_TABLE="checks", CACHE_TABLE="cache", BUCKET_NAME="bucket",
                          WORKER_FUNCTION="cloudlens-check-worker")
        self.checks = FakeTable(["userId", "checkId"])
        self.cache = FakeTable(["imageHash"])
        self.s3 = FakeS3({KEY: IMAGE})
        self.lam = FakeLambda()
        api._tables = {"checks": self.checks, "cache": self.cache}
        api._s3, api._lambda = self.s3, self.lam

    def call(self, e):
        r = api.lambda_handler(e, None)
        return r["statusCode"], json.loads(r["body"])

    def start(self, key=KEY, user=USER):
        return self.call(event("POST /checks", user=user, body={"imageKey": key}))

    def test_requires_sign_in(self):
        status, _ = self.call(event("POST /checks", user=None, body={"imageKey": KEY}))
        self.assertEqual(status, 401)

    def test_start_queues_a_check_and_invokes_the_worker(self):
        status, body = self.start()
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "QUEUED")
        stored = self.checks.items[(USER, body["checkId"])]
        self.assertEqual(stored["imageHash"], hashlib.sha256(IMAGE).hexdigest())
        self.assertEqual(len(self.lam.calls), 1)
        call = self.lam.calls[0]
        self.assertEqual(call["InvocationType"], "Event")
        self.assertEqual(json.loads(call["Payload"]), {"userId": USER, "checkId": body["checkId"]})
        self.assertNotIn("imageHash", body)

    def test_cannot_check_another_users_photo(self):
        status, _ = self.start(key=f"{OTHER}_abc.jpg")
        self.assertEqual(status, 403)
        self.assertEqual(self.lam.calls, [])

    def test_rejects_invalid_keys(self):
        for key in ["../secret.jpg", f"{USER}_x.exe", "", f"private/{USER}/x.jpg"]:
            with self.subTest(key=key):
                status, _ = self.start(key=key)
                self.assertEqual(status, 400)

    def test_missing_photo_is_404(self):
        status, _ = self.start(key=f"{USER}_missing.jpg")
        self.assertEqual(status, 404)

    def test_cached_result_is_returned_without_running_again(self):
        self.cache.put_item({"imageHash": hashlib.sha256(IMAGE).hexdigest(), "verdict": "FALSE", "score": 12,
                             "reason": "2 sources refute this claim.", "claim": "x", "sources": []})
        status, body = self.start()
        self.assertEqual(status, 200)
        self.assertEqual((body["status"], body["verdict"], body["score"], body["cached"]), ("DONE", "FALSE", 12, True))
        self.assertEqual(self.lam.calls, [])
        self.assertIn((USER, body["checkId"]), self.checks.items)  # still appears in history

    def test_worker_invoke_failure_marks_the_check_failed(self):
        api._lambda = FakeLambda(fail=True)
        status, _ = self.start()
        self.assertEqual(status, 500)
        (item,) = self.checks.items.values()
        self.assertEqual(item["status"], "FAILED")

    def test_get_returns_own_check(self):
        _, started = self.start()
        status, body = self.call(event("GET /checks/{checkId}", check_id=started["checkId"]))
        self.assertEqual(status, 200)
        self.assertEqual(body["checkId"], started["checkId"])

    def test_get_cannot_read_another_users_check(self):
        _, started = self.start()
        status, _ = self.call(event("GET /checks/{checkId}", user=OTHER, check_id=started["checkId"]))
        self.assertEqual(status, 404)

    def test_get_rejects_malformed_ids(self):
        status, _ = self.call(event("GET /checks/{checkId}", check_id="../../x"))
        self.assertEqual(status, 400)

    def test_history_is_newest_first_and_only_own(self):
        ids = [self.start()[1]["checkId"] for _ in range(3)]
        self.s3.objects[f"{OTHER}_z.jpg"] = b"other"
        self.start(key=f"{OTHER}_z.jpg", user=OTHER)
        status, body = self.call(event("GET /checks"))
        self.assertEqual(status, 200)
        self.assertEqual([c["checkId"] for c in body["checks"]], sorted(ids, reverse=True))


if __name__ == "__main__":
    unittest.main()
