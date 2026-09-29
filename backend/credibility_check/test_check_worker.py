"""Tests for the check worker, the claim rules and the scoring formula."""

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "check_worker"))
from fakes import FakeTable, FakeTextract  # noqa: E402
import lambda_function as worker  # noqa: E402  (check_worker/lambda_function.py)
from claim import clean_lines, find_claim  # noqa: E402
from scoring import score_evidence  # noqa: E402

USER, CHECK = "44b8a418-d021-70bf-86aa-875bc10aeb8c", "1790380000000-abcdef12"
TWEET = [
    "Jane Doe",
    "@janedoe · 2h",
    "BREAKING: Drinking two cups of coffee a day cures the flu, doctors say.",
    "Share this with everyone!",
    "3:45 PM · Sep 1, 2026",
    "1.2K Reposts",
    "8,431 Likes",
    "Reply",
]


class ClaimTest(unittest.TestCase):
    def test_interface_text_is_removed(self):
        self.assertEqual(clean_lines(TWEET), [
            "BREAKING: Drinking two cups of coffee a day cures the flu, doctors say.",
            "Share this with everyone!",
        ])

    def test_longest_full_sentence_is_the_claim(self):
        self.assertEqual(find_claim(TWEET),
                         "BREAKING: Drinking two cups of coffee a day cures the flu, doctors say.")

    def test_real_textract_output(self):
        # Exact lines Textract returned for a test screenshot: it read the
        # "·" separators as "." and merged both counts onto one line.
        lines = ["Jane Doe", "@janedoe . 2h", "BREAKING: Drinking two cups of coffee a",
                 "day cures the flu, doctors say.", "3:45 PM . Sep 1, 2026", "1.2K Reposts 8,431 Likes"]
        self.assertEqual(find_claim(lines),
                         "BREAKING: Drinking two cups of coffee a day cures the flu, doctors say.")
        self.assertEqual(clean_lines(lines), lines[2:4])

    def test_header_with_name_and_date(self):
        self.assertEqual(clean_lines(["Jane Doe @janedoe · Sep 1", "Jane Doe @janedoe - 5m"]), [])

    def test_no_claim_without_a_full_sentence(self):
        self.assertIsNone(find_claim(["Jane Doe", "@janedoe", "lol", "1.2K Likes"]))

    def test_claim_is_capped(self):
        self.assertLessEqual(len(find_claim(["word " * 200 + "end."])), 300)


class ScoringTest(unittest.TestCase):
    def ev(self, domain, stance, weight=1.0):
        return {"domain": domain, "stance": stance, "weight": weight}

    def test_no_evidence_is_unverified_without_a_score(self):
        self.assertEqual(score_evidence([])["verdict"], "UNVERIFIED")
        self.assertIsNone(score_evidence([])["score"])

    def test_two_independent_supporters_make_it_true(self):
        r = score_evidence([self.ev("apnews.com", "SUPPORTS"), self.ev("reuters.com", "SUPPORTS")])
        self.assertEqual((r["verdict"], r["score"]), ("TRUE", 100))

    def test_two_independent_refuters_make_it_false(self):
        r = score_evidence([self.ev("snopes.com", "REFUTES"), self.ev("factcheck.org", "REFUTES"),
                            self.ev("bbc.com", "SUPPORTS", 0.6)])
        self.assertEqual((r["verdict"], r["score"]), ("FALSE", 23))

    def test_one_site_counts_once(self):
        r = score_evidence([self.ev("snopes.com", "REFUTES"), self.ev("snopes.com", "REFUTES")])
        self.assertEqual((r["verdict"], r["refuters"]), ("UNVERIFIED", 1))

    def test_unknown_sites_are_ignored(self):
        r = score_evidence([self.ev("random.blog", "SUPPORTS", 0), self.ev("x.example", "SUPPORTS", 0)])
        self.assertEqual((r["verdict"], r["score"]), ("UNVERIFIED", None))

    def test_disagreement_is_unverified(self):
        r = score_evidence([self.ev("apnews.com", "SUPPORTS"), self.ev("reuters.com", "SUPPORTS"),
                            self.ev("snopes.com", "REFUTES"), self.ev("factcheck.org", "REFUTES")])
        self.assertEqual((r["verdict"], r["score"]), ("UNVERIFIED", 50))

    def test_thresholds_are_configurable(self):
        ev = [self.ev("apnews.com", "SUPPORTS"), self.ev("reuters.com", "SUPPORTS"), self.ev("bbc.com", "REFUTES", 0.6)]
        self.assertEqual(score_evidence(ev)["verdict"], "TRUE")                   # 77 >= 70
        self.assertEqual(score_evidence(ev, t_true=80)["verdict"], "UNVERIFIED")  # 77 < 80


class WorkerTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(CHECKS_TABLE="checks", CACHE_TABLE="cache", BUCKET_NAME="bucket",
                          EVIDENCE_ENABLED="false")
        self.checks = FakeTable(["userId", "checkId"])
        self.cache = FakeTable(["imageHash"])
        self.checks.put_item({"userId": USER, "checkId": CHECK, "imageKey": f"{USER}_a.jpg", "imageHash": "h",
                              "status": "QUEUED"})
        worker._tables = {"checks": self.checks, "cache": self.cache}

    def run_with(self, textract):
        worker._textract = textract
        worker.lambda_handler({"userId": USER, "checkId": CHECK}, None)
        return self.checks.items[(USER, CHECK)]

    def test_stages_run_in_order_and_finish_unverified(self):
        item = self.run_with(FakeTextract(TWEET))
        stages = [h["status"] for h in self.checks.history]
        self.assertEqual(stages, ["READING_TEXT", "FINDING_CLAIM", "SEARCHING_SOURCES", "SCORING", "DONE"])
        self.assertEqual(item["verdict"], "UNVERIFIED")
        self.assertIsNone(item["score"])
        self.assertIn("coffee", item["claim"])
        self.assertIn("not enabled", item["reason"])
        self.assertIn("finishedAt", item)

    def test_textract_reads_the_users_photo(self):
        textract = FakeTextract(TWEET)
        self.run_with(textract)
        self.assertEqual(textract.calls, [{"S3Object": {"Bucket": "bucket", "Name": f"{USER}_a.jpg"}}])

    def test_skeleton_results_are_not_cached(self):
        self.run_with(FakeTextract(TWEET))
        self.assertEqual(self.cache.items, {})

    def test_no_text(self):
        item = self.run_with(FakeTextract([]))
        self.assertEqual(item["status"], "DONE")
        self.assertEqual(item["reason"], "No readable text was found in this image.")

    def test_no_claim(self):
        item = self.run_with(FakeTextract(["Jane Doe", "@janedoe", "lol"]))
        self.assertEqual(item["reason"], "No checkable statement was found in the text.")

    def test_ocr_failure_marks_the_check_failed(self):
        item = self.run_with(FakeTextract(fail=True))
        self.assertEqual(item["status"], "FAILED")
        self.assertIn("error", item)


if __name__ == "__main__":
    unittest.main()
