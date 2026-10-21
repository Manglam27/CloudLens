"""Tests for the evidence and Claude steps of the worker, with fakes for Claude, SSM and every search API."""

import json
import os
import re
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
GOOGLE_PARAM, NEWSAPI_PARAM = "/cloudlens/google-factcheck-api-key", "/cloudlens/newsapi-key"


def review(site, rating, url=None, claimed=CLAIM):
    return {"text": claimed, "claimReview": [{
        "publisher": {"name": site.split(".")[0].title(), "site": site},
        "url": url or f"https://{site}/coffee-flu", "title": "Does coffee cure the flu?", "textualRating": rating}]}


def seed_sources():
    return evidence.TrustedSources(evidence.load_seed())


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
        n = len(re.findall(r"^\[\d+\] ", kwargs["messages"][0]["content"], re.M))
        stances = self.stances or ["REFUTES"] * n
        return self._reply({"stances": [{"index": i + 1, "stance": s} for i, s in enumerate(stances)],
                            "reason": self.reason}, self.judge_stop)

    @staticmethod
    def _reply(payload, stop):
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(payload))])


class FakeSSM:
    def __init__(self, values):
        self.values = values

    def get_parameter(self, Name, WithDecryption):
        if Name not in self.values:
            raise ClientError("ParameterNotFound")
        return {"Parameter": {"Value": self.values[Name]}}


class FakeWeb:
    """Routes evidence requests by host; anything without canned data fails like a rate limit."""

    def __init__(self):
        self.responses, self.calls = {}, []

    def __call__(self, url, timeout=evidence.TIMEOUT_SECONDS, headers=None):
        self.calls.append((url, headers or {}))
        for host, data in self.responses.items():
            if host in url:
                if isinstance(data, Exception):
                    raise data
                return data
        raise evidence.EvidenceError("HTTP 429")


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.web = FakeWeb()
        self._original = evidence._http_get_json
        evidence._http_get_json = self.web

    def tearDown(self):
        evidence._http_get_json = self._original

    def test_trust_lookup_matches_subdomains_and_respects_disabled_entries(self):
        sources = seed_sources()
        self.assertEqual(evidence.normalize_domain("https://www.Snopes.com/fact-check/x"), "snopes.com")
        self.assertEqual(sources.weight("factcheck.afp.com"), 1.0)
        self.assertEqual(sources.weight("bbc.co.uk"), 0.6)
        self.assertEqual(sources.weight("randomblog.net"), 0.0)
        by_domain = evidence.load_seed()
        by_domain["snopes.com"]["enabled"] = False
        self.assertEqual(evidence.TrustedSources(by_domain).weight("snopes.com"), 0.0)

    def test_providers_are_merged_fact_checks_first_untrusted_dropped(self):
        self.web.responses = {
            "factchecktools": {"claims": [review("usatoday.com", "False"), review("randomblog.net", "False"),
                                          review("snopes.com", "False"), review("snopes.com", "False")]},
            "newsapi.org": {"status": "ok", "articles": [
                {"source": {"name": "Reuters"}, "url": "https://www.reuters.com/a", "title": "Coffee and flu",
                 "description": "Doctors say there is no evidence."}]},
        }
        items, report = evidence.find_evidence("coffee flu", {"google_fact_check": "G", "newsapi": "N"},
                                               seed_sources(), 5)
        self.assertEqual([i["domain"] for i in items], ["snopes.com", "usatoday.com", "reuters.com"])
        self.assertEqual([i["kind"] for i in items], ["fact_check", "fact_check", "news"])
        self.assertEqual(report, {"google_fact_check": "ok", "newsapi": "ok", "guardian": "skipped",
                                  "gdelt": "failed"})

    def test_newsapi_key_travels_in_a_header_and_domains_are_trusted_only(self):
        self.web.responses = {"newsapi.org": {"status": "ok", "articles": []}}
        evidence.find_evidence("coffee flu", {"newsapi": "NEWS-KEY"}, seed_sources(), 5, enabled=["newsapi"])
        url, headers = self.web.calls[0]
        self.assertNotIn("NEWS-KEY", url)
        self.assertEqual(headers["X-Api-Key"], "NEWS-KEY")
        self.assertIn("reuters.com", url)
        self.assertNotIn("snopes.com", url)  # fact-checkers are not news domains

    def test_result_count_is_capped(self):
        self.web.responses = {"factchecktools": {"claims": [
            review("snopes.com", "False", url=f"https://snopes.com/{i}") for i in range(9)]}}
        items, _ = evidence.find_evidence("q", {"google_fact_check": "G"}, seed_sources(), 5)
        self.assertEqual(len(items), 5)

    def test_errors_never_leak_api_keys(self):
        import urllib.error
        evidence._http_get_json = self._original
        original_urlopen = evidence.urllib.request.urlopen

        def boom(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        evidence.urllib.request.urlopen = boom
        try:
            with self.assertRaises(evidence.EvidenceError) as ctx:
                evidence.google_fact_check("q", "SECRET-KEY-123", seed_sources(), 5)
        finally:
            evidence.urllib.request.urlopen = original_urlopen
        self.assertNotIn("SECRET-KEY-123", str(ctx.exception))

    def test_rating_rules(self):
        cases = {"False": "REFUTES", "Pants on Fire!": "REFUTES", "Mostly False": "REFUTES", "Incorrect": "REFUTES",
                 "Not true": "REFUTES", "True": "SUPPORTS", "Mostly True": "SUPPORTS", "Correct": "SUPPORTS",
                 "Didn't Happen": "REFUTES", "Labeled Satire": "REFUTES", "Missing context": "REFUTES",
                 "Unfounded": "REFUTES", "Miscaptioned": "REFUTES",
                 "Half True": "MIXED", "Unproven": "MIXED", "Exaggerated": "MIXED", "": "RELATED"}
        for rating, expected in cases.items():
            with self.subTest(rating=rating):
                self.assertEqual(evidence.fallback_stance({"kind": "fact_check", "rating": rating}), expected)
        # Without a clear rating the headline decides.
        self.assertEqual(evidence.fallback_stance({"kind": "fact_check", "rating": "See article",
                                                   "title": "Fact Check: Spotify Did NOT Remove Ed Sheeran's Songs"}),
                         "REFUTES")
        self.assertEqual(evidence.fallback_stance({"kind": "fact_check", "rating": "See article",
                                                   "title": "Did Spotify remove Ed Sheeran's music?"}), "RELATED")
        off_topic = {"kind": "fact_check", "rating": "False", "relevance": 0.1}
        self.assertEqual(evidence.fallback_stance(off_topic), "UNRELATED")

    def test_news_stance_from_how_closely_it_matches(self):
        def news(title, summary=""):
            item = {"kind": "news", "title": title, "summary": summary, "claim_reviewed": "", "rating": ""}
            item["relevance"] = evidence.relevance("Colorado first winter weather advisory snow", item)
            return evidence.fallback_stance(item)

        self.assertEqual(news("Colorado's first winter weather advisory brings snow"), "SUPPORTS")
        self.assertEqual(news("Viral claim about Colorado's first winter weather advisory and snow"), "RELATED")
        self.assertEqual(news("Colorado weather: snow on the way"), "RELATED")
        self.assertEqual(news("Sydney airport incidents investigated"), "UNRELATED")

    def test_relevance_ignores_case_plurals_and_possessives(self):
        item = {"title": "California's ballot count", "summary": "", "claim_reviewed": ""}
        self.assertEqual(evidence.relevance("California ballots", item), 1.0)
        self.assertEqual(evidence.relevance("Florida ballots", item), 0.5)

    def test_relevance_matches_word_forms_short_names_and_synonyms(self):
        item = {"title": "No, 5G does not spread COVID-19", "summary": "",
                "claim_reviewed": "China banned American citizens"}
        self.assertEqual(evidence.relevance("5G towers spreading coronavirus", item), 0.75)
        self.assertEqual(evidence.relevance("China bans US citizens", item), 1.0)

    def test_google_searches_several_phrasings_and_merges_them(self):
        claim = "California Took Over a Month To Count 95% of its Ballots."
        afp = review("factcheck.afp.com", "Missing context", claimed="California took over a month to count ballots")
        politifact = review("politifact.com", "False", claimed="California ballots took a month to count")
        found = {claim: [afp], "California Month Count Ballots": [afp, politifact]}
        queries = []

        def web(url, timeout=None, headers=None):
            parse = evidence.urllib.parse
            q = parse.parse_qs(parse.urlparse(url).query)["query"][0]
            queries.append(q)
            return {"claims": found.get(q, [])}

        evidence._http_get_json = web
        items, _ = evidence.find_evidence("California Took Month Count 95% Ballots", {"google_fact_check": "G"},
                                          seed_sources(), 5, enabled=["google_fact_check"], also_try=[claim])
        self.assertEqual(sorted(queries), sorted(["California Took Month Count 95% Ballots", claim,
                                                  "California Month Count Ballots"]))
        self.assertEqual(sorted(i["domain"] for i in items), ["factcheck.afp.com", "politifact.com"])  # no duplicates

    def test_one_failed_phrasing_does_not_fail_the_search(self):
        def web(url, timeout=None, headers=None):
            if "hantavirus" not in url:
                raise evidence.EvidenceError("HTTP 500")
            return {"claims": [review("fullfact.org", "False")]}

        evidence._http_get_json = web
        items, report = evidence.find_evidence("China bans US citizens enter country outbreaking hantavirus",
                                               {"google_fact_check": "G"}, seed_sources(), 5,
                                               enabled=["google_fact_check"], also_try=["China bans US citizens"])
        self.assertEqual((len(items), report["google_fact_check"]), (1, "ok"))

    def test_off_topic_news_is_dropped(self):
        self.web.responses = {"content.guardianapis.com": {"response": {"status": "ok", "results": [
            {"webUrl": "https://www.theguardian.com/a", "webTitle": "Sydney airport incidents investigated"},
            {"webUrl": "https://www.theguardian.com/b", "webTitle": "California ballot count takes a month"}]}}}
        items, _ = evidence.find_evidence("California ballot count month", {"guardian": "K"}, seed_sources(), 5,
                                          enabled=["guardian"])
        self.assertEqual([i["url"] for i in items], ["https://www.theguardian.com/b"])


class JudgeTest(unittest.TestCase):
    def test_missing_indexes_count_as_unrelated_and_news_is_described(self):
        claude = FakeClaude(stances=["REFUTES"])
        llm._client = claude
        sources = [{"kind": "fact_check", "publisher": "Snopes", "domain": "snopes.com", "claim_reviewed": CLAIM,
                    "title": "t", "rating": "False"},
                   {"kind": "news", "publisher": "Reuters", "domain": "reuters.com", "title": "Coffee and flu",
                    "summary": "Doctors disagree."}]
        stances, reason = llm.judge_sources(CLAIM, sources)
        self.assertEqual(stances, ["REFUTES", "UNRELATED"])
        prompt = claude.calls[0]["messages"][0]["content"]
        self.assertIn("News article from Reuters", prompt)
        self.assertIn("Summary: Doctors disagree.", prompt)

    def test_non_end_turn_stop_is_an_error(self):
        llm._client = FakeClaude(claim_stop="refusal")
        with self.assertRaises(llm.LLMError):
            llm.extract_claim(LINES)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(CHECKS_TABLE="checks", CACHE_TABLE="cache", SOURCES_TABLE="sources", BUCKET_NAME="bucket",
                          EVIDENCE_ENABLED="true", GDELT_ENABLED="true", MAX_RESULTS="5",
                          FACTCHECK_KEY_PARAM=GOOGLE_PARAM, NEWSAPI_KEY_PARAM=NEWSAPI_PARAM,
                          GUARDIAN_KEY_PARAM="/cloudlens/guardian-api-key")
        self.checks = FakeTable(["userId", "checkId"])
        self.cache = FakeTable(["imageHash"])
        self.sources = FakeTable(["domain"])
        for entry in evidence.load_seed().values():
            self.sources.put_item(entry)
        self.checks.put_item({"userId": USER, "checkId": CHECK, "imageKey": f"{USER}_a.jpg", "imageHash": "h",
                              "status": "QUEUED"})
        worker._tables = {"checks": self.checks, "cache": self.cache, "sources": self.sources}
        worker._textract = FakeTextract(LINES)
        worker._ssm, worker._keys, worker._sources = FakeSSM({GOOGLE_PARAM: "GKEY"}), {}, None
        self.web = FakeWeb()
        self.web.responses = {"factchecktools": {"claims": [
            review("snopes.com", "False"), review("politifact.com", "Pants on Fire"), review("randomblog.net", "False")]}}
        self._original = evidence._http_get_json
        evidence._http_get_json = self.web

    def tearDown(self):
        evidence._http_get_json = self._original

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
        self.assertNotIn("randomblog", claude.calls[1]["messages"][0]["content"])  # untrusted never sent
        self.assertEqual(item["providers"]["gdelt"], "failed")  # rate-limited, but not needed
        self.assertIn(("h",), self.cache.items)  # the main source worked, so the result is cached

    def test_two_supporting_wire_services_give_true(self):
        self.web.responses = {"factchecktools": {"claims": [review("apnews.com", "True"),
                                                            review("reuters.com", "Correct")]}}
        item = self.run_check(FakeClaude(stances=["SUPPORTS", "SUPPORTS"], reason="Two sources confirm it."))
        self.assertEqual((item["verdict"], item["score"]), ("TRUE", 100))

    def test_one_source_is_not_enough(self):
        self.web.responses = {"factchecktools": {"claims": [review("snopes.com", "False")]}}
        item = self.run_check(FakeClaude())
        self.assertEqual((item["verdict"], item["score"]), ("UNVERIFIED", 0))
        self.assertIn("Fewer than 2 independent trusted sources", item["reason"])

    def test_without_claude_ratings_still_give_a_verdict(self):
        item = self.run_check(FakeClaude(claim_stop="max_tokens", judge_stop="max_tokens"))
        self.assertEqual((item["verdict"], item["score"]), ("FALSE", 0))
        self.assertIn("coffee", item["claim"])  # found by the rules
        self.assertEqual(item["reason"], "2 trusted fact-checks rated this claim false or misleading.")
        self.assertEqual(self.cache.items, {})  # fallbacks are never cached

    def test_opinions_are_not_searched(self):
        item = self.run_check(FakeClaude(claim={"kind": "opinion", "claim": "", "search_query": ""}))
        self.assertIn("opinion", item["reason"])
        self.assertIsNone(item["score"])  # nothing to score
        self.assertEqual(self.web.calls, [])
        self.assertIn(("h",), self.cache.items)

    def test_missing_google_key_with_no_other_sources(self):
        worker._ssm = FakeSSM({})
        item = self.run_check(FakeClaude())
        self.assertIn("not configured", item["reason"])
        self.assertEqual(self.cache.items, {})

    def test_news_provider_works_without_the_google_key(self):
        worker._ssm = FakeSSM({NEWSAPI_PARAM: "NKEY"})
        self.web.responses = {"newsapi.org": {"status": "ok", "articles": [
            {"source": {"name": "AP"}, "url": "https://apnews.com/a", "title": "No, coffee does not cure flu",
             "description": "Health experts say the viral claim is false."},
            {"source": {"name": "Reuters"}, "url": "https://www.reuters.com/b", "title": "Fact check: coffee flu",
             "description": "The claim is false."}]}}
        item = self.run_check(FakeClaude())
        self.assertEqual([s["kind"] for s in item["sources"]], ["news", "news"])
        self.assertEqual(item["verdict"], "FALSE")
        self.assertEqual(self.cache.items, {})  # the main source was missing

    def test_google_failure_with_no_other_sources(self):
        self.web.responses = {"factchecktools": evidence.EvidenceError("HTTP 500")}
        item = self.run_check(FakeClaude())
        self.assertIn("could not be completed", item["reason"])
        self.assertEqual(self.cache.items, {})

    def test_disabled_source_in_the_table_is_ignored(self):
        self.sources.items[("snopes.com",)]["enabled"] = False
        item = self.run_check(FakeClaude())
        self.assertEqual([s["domain"] for s in item["sources"]], ["politifact.com"])
        self.assertEqual(item["verdict"], "UNVERIFIED")

    def test_source_added_to_the_table_is_trusted(self):
        self.sources.put_item({"domain": "randomblog.net", "name": "Random Blog", "category": "fact_checker",
                               "weight": 1.0, "enabled": True})
        item = self.run_check(FakeClaude())
        self.assertIn("randomblog.net", [s["domain"] for s in item["sources"]])

    def test_no_evidence_gives_a_neutral_score(self):
        self.web.responses = {"factchecktools": {}}
        item = self.run_check(FakeClaude())
        self.assertEqual((item["verdict"], item["score"], item["sources"]), ("UNVERIFIED", 50, []))
        self.assertIn("neutral", item["reason"])

    def test_without_claude_matching_news_counts_and_off_topic_is_hidden(self):
        worker._ssm = FakeSSM({GOOGLE_PARAM: "GKEY", NEWSAPI_PARAM: "NKEY"})
        self.web.responses = {"factchecktools": {}, "newsapi.org": {"status": "ok", "articles": [
            {"source": {"name": "AP"}, "url": "https://apnews.com/a",
             "title": "Two cups of coffee a day cures the flu, study finds"},
            {"source": {"name": "Reuters"}, "url": "https://www.reuters.com/b",
             "title": "Drinking coffee cures flu, doctors report", "description": "Two cups a day."},
            {"source": {"name": "BBC"}, "url": "https://www.bbc.com/c", "title": "Flu season starts early"}]}}
        item = self.run_check(FakeClaude(claim_stop="max_tokens", judge_stop="max_tokens"))
        self.assertEqual([s["stance"] for s in item["sources"]], ["SUPPORTS", "SUPPORTS"])
        self.assertEqual((item["verdict"], item["score"]), ("TRUE", 100))
        self.assertEqual(item["reason"], "2 trusted sources back up this claim.")

    def test_stages_still_run_in_order(self):
        self.run_check(FakeClaude())
        stages = [h["status"] for h in self.checks.history]
        self.assertEqual(stages, ["READING_TEXT", "FINDING_CLAIM", "SEARCHING_SOURCES", "SCORING", "DONE"])


if __name__ == "__main__":
    unittest.main()
