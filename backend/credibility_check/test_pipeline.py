"""Tests for the evidence and Claude steps of the worker, with fakes for Claude, SSM and the Fact Check API."""

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "check_worker"))
from fakes import ClientError, FakeTable, FakeTextract  # noqa: E402
import evidence  # noqa: E402
import lambda_function as worker  # noqa: E402
import llm  # noqa: E402

USER, CHECK = "44b8a418-d021-70bf-86aa-875bc10aeb8c", "1790380000000-abcdef12"
LINES = ["Jane Doe", "@janedoe . 2h", "BREAKING: Drinking two cups of coffee a day cures the flu, doctors say."]
CLAIM = "Drinking two cups of coffee a day cures the flu."


def review(site, rating, title="Does coffee cure the flu?", url=None, claimed=CLAIM):
    return {"text": claimed, "claimReview": [{
        "publisher": {"name": site.split(".")[0].title(), "site": site},
        "url": url or f"https://{site}/coffee-flu", "title": title, "textualRating": rating}]}


class FakeClaude:
    """Answers the claim and judge calls with canned JSON, like the structured-outputs API."""

    def __init__(self, claim=None, stances=None, reason="Two fact-checkers rated this claim false.",
                 claim_stop="end_turn", judge_stop="end_turn"):
        self.claim = claim or {"kind": "claim", "claim": CLAIM, "search_query": "coffee cures flu"}
        self.stances, self.reason = stances, reason
        self.claim_stop, self.judge_stop = claim_stop, judge_stop
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        assert "temperature" not in kwargs  # removed from SDK 1.x signatures
        assert kwargs["extra_body"] == {"temperature": 0}
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        if kwargs["system"] == llm.CLAIM_SYSTEM:
            return self._reply(self.claim, self.claim_stop)
        n = kwargs["messages"][0]["content"].count("\nClaim reviewed:")
        stances = self.stances or ["REFUTES"] * n
        return self._reply({"stances": [{"index": i + 1, "stance": s} for i, s in enumerate(stances)],
                            "reason": self.reason}, self.judge_stop)

    @staticmethod
    def _reply(payload, stop):
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(payload))])


class FakeSSM:
    def __init__(self, value=None):
        self.value = value

    def get_parameter(self, Name, WithDecryption):
        if self.value is None:
            raise ClientError("ParameterNotFound")
        return {"Parameter": {"Value": self.value}}


class EvidenceTest(unittest.TestCase):
    def test_domains_are_normalized_and_subdomains_match(self):
        self.assertEqual(evidence.normalize_domain("https://www.Snopes.com/fact-check/x"), "snopes.com")
        self.assertEqual(evidence.weight_for("factcheck.afp.com"), 1.0)
        self.assertEqual(evidence.weight_for("bbc.co.uk"), 0.6)
        self.assertEqual(evidence.weight_for("randomblog.net"), 0.0)

    def test_only_trusted_reviews_are_kept_fact_checkers_first(self):
        data = {"claims": [review("usatoday.com", "False"), review("randomblog.net", "False"),
                           review("snopes.com", "False"), review("snopes.com", "False")]}
        original = evidence._http_get_json
        evidence._http_get_json = lambda url: data
        try:
            items = evidence.search_fact_checks("coffee flu", "KEY", 5)
        finally:
            evidence._http_get_json = original
        self.assertEqual([i["domain"] for i in items], ["snopes.com", "usatoday.com"])  # duplicate URL dropped
        self.assertEqual([i["weight"] for i in items], [1.0, 0.6])

    def test_result_count_is_capped(self):
        data = {"claims": [review("snopes.com", "False", url=f"https://snopes.com/{i}") for i in range(9)]}
        original = evidence._http_get_json
        evidence._http_get_json = lambda url: data
        try:
            self.assertEqual(len(evidence.search_fact_checks("q", "KEY", 5)), 5)
        finally:
            evidence._http_get_json = original

    def test_errors_never_leak_the_api_key(self):
        import urllib.error
        original = evidence.urllib.request.urlopen

        def boom(url, timeout):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

        evidence.urllib.request.urlopen = boom
        try:
            with self.assertRaises(evidence.EvidenceError) as ctx:
                evidence.search_fact_checks("q", "SECRET-KEY-123", 5)
        finally:
            evidence.urllib.request.urlopen = original
        self.assertNotIn("SECRET-KEY-123", str(ctx.exception))


class JudgeTest(unittest.TestCase):
    def test_missing_indexes_count_as_unrelated(self):
        llm._client = FakeClaude(stances=["REFUTES"])
        sources = [{"publisher": p, "domain": f"{p}.com", "claim_reviewed": CLAIM, "title": "t", "rating": "False"}
                   for p in ("snopes", "politifact")]
        stances, reason = llm.judge_sources(CLAIM, sources)
        self.assertEqual(stances, ["REFUTES", "UNRELATED"])
        self.assertTrue(reason)

    def test_non_end_turn_stop_is_an_error(self):
        llm._client = FakeClaude(claim_stop="refusal")
        with self.assertRaises(llm.LLMError):
            llm.extract_claim(LINES)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(CHECKS_TABLE="checks", CACHE_TABLE="cache", BUCKET_NAME="bucket", EVIDENCE_ENABLED="true",
                          FACTCHECK_KEY_PARAM="/cloudlens/google-factcheck-api-key", MAX_RESULTS="5")
        self.checks = FakeTable(["userId", "checkId"])
        self.cache = FakeTable(["imageHash"])
        self.checks.put_item({"userId": USER, "checkId": CHECK, "imageKey": f"{USER}_a.jpg", "imageHash": "h",
                              "status": "QUEUED"})
        worker._tables = {"checks": self.checks, "cache": self.cache}
        worker._textract = FakeTextract(LINES)
        worker._ssm, worker._api_key = FakeSSM("KEY"), None
        self.searches = []
        self.fact_checks = {"claims": [review("snopes.com", "False"), review("politifact.com", "Pants on Fire"),
                                       review("randomblog.net", "False")]}
        self._original_get = evidence._http_get_json
        evidence._http_get_json = lambda url: (self.searches.append(url), self.fact_checks)[1]

    def tearDown(self):
        evidence._http_get_json = self._original_get

    def run_check(self, claude):
        llm._client = claude
        worker.lambda_handler({"userId": USER, "checkId": CHECK}, None)
        return self.checks.items[(USER, CHECK)]

    def test_two_refuting_fact_checkers_give_false(self):
        claude = FakeClaude()
        item = self.run_check(claude)
        self.assertEqual((item["status"], item["verdict"], item["score"]), ("DONE", "FALSE", 0))
        self.assertEqual(item["claim"], CLAIM)
        self.assertEqual(item["reason"], "Two fact-checkers rated this claim false.")
        self.assertEqual([s["domain"] for s in item["sources"]], ["snopes.com", "politifact.com"])
        self.assertEqual({s["stance"] for s in item["sources"]}, {"REFUTES"})
        self.assertIn("query=coffee+cures+flu", self.searches[0])
        self.assertNotIn("randomblog", claude.calls[1]["messages"][0]["content"])  # untrusted never sent
        self.assertIn(("h",), self.cache.items)  # complete results are cached

    def test_two_supporting_wire_services_give_true(self):
        self.fact_checks = {"claims": [review("apnews.com", "True"), review("reuters.com", "Correct")]}
        item = self.run_check(FakeClaude(stances=["SUPPORTS", "SUPPORTS"], reason="Two sources confirm it."))
        self.assertEqual((item["verdict"], item["score"]), ("TRUE", 100))

    def test_one_source_is_not_enough(self):
        self.fact_checks = {"claims": [review("snopes.com", "False")]}
        item = self.run_check(FakeClaude())
        self.assertEqual((item["verdict"], item["score"]), ("UNVERIFIED", 0))
        self.assertIn("Fewer than 2 independent trusted sources", item["reason"])

    def test_opinions_are_not_searched(self):
        item = self.run_check(FakeClaude(claim={"kind": "opinion", "claim": "", "search_query": ""}))
        self.assertEqual(item["verdict"], "UNVERIFIED")
        self.assertIn("opinion", item["reason"])
        self.assertEqual(self.searches, [])
        self.assertIn(("h",), self.cache.items)

    def test_claude_failure_falls_back_to_rules_and_is_not_cached(self):
        item = self.run_check(FakeClaude(claim_stop="max_tokens"))
        self.assertEqual(item["status"], "DONE")
        self.assertIn("coffee", item["claim"])  # found by the rules instead
        self.assertEqual(self.cache.items, {})

    def test_missing_api_key(self):
        worker._ssm = FakeSSM(None)
        item = self.run_check(FakeClaude())
        self.assertIn("not configured", item["reason"])
        self.assertEqual(self.searches, [])
        self.assertEqual(self.cache.items, {})

    def test_search_failure(self):
        def fail(url):
            raise evidence.EvidenceError("HTTP 429")
        evidence._http_get_json = fail
        item = self.run_check(FakeClaude())
        self.assertIn("could not be completed", item["reason"])
        self.assertEqual(self.cache.items, {})

    def test_judge_failure(self):
        item = self.run_check(FakeClaude(judge_stop="max_tokens"))
        self.assertEqual(item["verdict"], "UNVERIFIED")
        self.assertIn("could not be compared", item["reason"])
        self.assertEqual(self.cache.items, {})

    def test_stages_still_run_in_order(self):
        self.run_check(FakeClaude())
        stages = [h["status"] for h in self.checks.history]
        self.assertEqual(stages, ["READING_TEXT", "FINDING_CLAIM", "SEARCHING_SOURCES", "SCORING", "DONE"])


if __name__ == "__main__":
    unittest.main()
