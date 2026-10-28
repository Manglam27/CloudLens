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
from claim import clean_lines, find_claim, search_query  # noqa: E402
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

    def test_first_full_sentence_is_the_claim(self):
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
        self.assertEqual(clean_lines(["Jane Doe @janedoe · Sep 1", "Jane Doe @janedoe - 5m", "@janedoe 2h"]), [])

    def test_long_display_name_is_removed(self):
        lines = ["Daily News Now", "@dailynewsnow · 2h", "5G towers are spreading the coronavirus,",
                 "scientists confirm."]
        self.assertEqual(find_claim(lines), "5G towers are spreading the coronavirus, scientists confirm.")

    def test_line_before_a_mention_in_the_post_is_kept(self):
        lines = ["Huge congratulations to the whole team and", "@janedoe for winning the award."]
        self.assertEqual(clean_lines(lines), lines)

    def test_search_query_keeps_the_keywords(self):
        self.assertEqual(search_query("5G towers are spreading the coronavirus, scientists confirm."),
                         "5G towers spreading coronavirus")
        self.assertEqual(search_query("BREAKING: Drinking two cups of coffee a day cures the flu, doctors say."),
                         "Drinking two cups coffee day cures flu")

    def test_page_chrome_and_footer_are_skipped(self):
        # Shape of a real screenshot of an archived post: browser chrome above the
        # header, the post, then the archive page's caption with a date.
        lines = ["Archive - MediaVault", "X", "mvau.lt", "ICE Headquarters", "@iceheadquarters", "0 followers",
                 "ICE Headquarters", "@iceheadquarters", "California Took Over a Month", "To Count 95% of its Ballots.",
                 "Florida Did It in an Hour and a Half.", "The contrast could not be starker, critics of the state say,",
                 "and the posts kept coming for weeks after the election ended.",
                 "California struggles for over a month to tally its ballots, published on Instagram 19 Aug 2026."]
        self.assertEqual(find_claim(lines), "California Took Over a Month To Count 95% of its Ballots.")

    def test_one_word_continuation_line_is_kept(self):
        lines = ["Post", "Mega-Politics", "...", "@_MegaPolitics", "Breaking:",
                 "China officially bans all the US citizens to enter the country over outbreaking of", "hantavirus"]
        claim = find_claim(lines)
        self.assertTrue(claim.endswith("outbreaking of hantavirus"))
        self.assertEqual(search_query(claim), "China bans US citizens enter country outbreaking hantavirus")
        caps = ["SPOTIFY HAS REMOVED ED SHEERANS SONGS FROM THE", "PLATFORM"]
        self.assertEqual(find_claim(caps), "SPOTIFY HAS REMOVED ED SHEERANS SONGS FROM THE PLATFORM")

    def test_lowercase_posts_are_claims_but_menus_are_not(self):
        lines = ["Gir Chris", "Subscribe", "@eagleseyeinc",
                 "sleeping with a fan on is extremely dangerous and most people do it", "every single night.",
                 "Hit me with the harshest reality truth."]
        self.assertTrue(find_claim(lines).startswith("sleeping with a fan on"))
        menu = ["colorado winter weather advisory sn AI Mode All News Images Videos Forums Shopp Colorado.",
                "The National Weather Service has issued an advisory."]
        self.assertEqual(find_claim(menu), "The National Weather Service has issued an advisory.")

    def test_breaking_news_label_is_not_searched(self):
        self.assertEqual(search_query("BREAKING NEWS: Adam Sandler has donated $5 million to homeless centers."),
                         "Adam Sandler donated $5 million homeless centers")

    def test_no_claim_without_a_full_sentence(self):
        self.assertIsNone(find_claim(["Jane Doe", "@janedoe", "lol", "1.2K Likes"]))

    def test_claim_is_capped(self):
        self.assertLessEqual(len(find_claim(["word " * 200 + "end."])), 300)


class ScoringTest(unittest.TestCase):
    def ev(self, domain, stance, weight=1.0, strength=1.0, kind="news"):
        return {"domain": domain, "stance": stance, "weight": weight, "strength": strength, "kind": kind}

    def check(self, domain, stance, strength=0.9):
        return self.ev(domain, stance, strength=strength, kind="fact_check")

    def test_no_evidence_is_unverified_with_a_neutral_score(self):
        r = score_evidence([])
        self.assertEqual((r["verdict"], r["score"], r["certainty"]), ("UNVERIFIED", 50, 0))

    def test_more_evidence_moves_the_score_further(self):
        # Direction -1 every time; certainty grows with each fact-check: 0.59, 0.83, 0.93.
        scores = [score_evidence([self.check(d, "REFUTES") for d in domains])["score"]
                  for domains in (["snopes.com"], ["snopes.com", "politifact.com"],
                                  ["snopes.com", "politifact.com", "fullfact.org"])]
        self.assertEqual(scores, [20, 8, 3])

    def test_two_independent_supporters_make_it_true(self):
        r = score_evidence([self.ev("apnews.com", "SUPPORTS"), self.ev("reuters.com", "SUPPORTS")])
        self.assertEqual((r["verdict"], r["score"]), ("TRUE", 93))

    def test_a_weak_supporter_pulls_a_false_claim_toward_the_middle(self):
        r = score_evidence([self.ev("snopes.com", "REFUTES"), self.ev("factcheck.org", "REFUTES"),
                            self.ev("bbc.com", "SUPPORTS", 0.6)])
        self.assertEqual((r["verdict"], r["score"]), ("FALSE", 25))

    def test_one_fact_check_of_the_same_claim_decides(self):
        self.assertEqual(score_evidence([self.check("fullfact.org", "REFUTES")])["verdict"], "FALSE")
        loose = score_evidence([self.check("fullfact.org", "REFUTES", strength=0.4)])
        self.assertEqual((loose["verdict"], loose["score"]), ("UNVERIFIED", 34))
        news = score_evidence([self.ev("cnn.com", "SUPPORTS", 0.6, strength=0.9)])
        self.assertEqual((news["verdict"], news["score"]), ("UNVERIFIED", 69))  # news needs 2 sites

    def test_score_always_matches_the_verdict(self):
        cases = [[self.ev("cnn.com", "SUPPORTS", 0.6)],
                 [self.check("snopes.com", "REFUTES"), self.ev("bbc.com", "SUPPORTS", 0.6)],
                 [self.ev("apnews.com", "SUPPORTS"), self.ev("reuters.com", "SUPPORTS")],
                 [self.check("snopes.com", "REFUTES"), self.check("factcheck.org", "REFUTES")],
                 [self.ev("snopes.com", "REFUTES"), self.ev("snopes.com", "REFUTES")], []]
        for evidence in cases:
            r = score_evidence(evidence)
            band = {"TRUE": (70, 100), "FALSE": (0, 30), "UNVERIFIED": (31, 69)}[r["verdict"]]
            self.assertTrue(band[0] <= r["score"] <= band[1], (evidence, r))

    def test_an_encyclopedia_entry_can_confirm_but_not_refute(self):
        wiki = self.ev("wikipedia.org", "SUPPORTS", 0.6, kind="reference")
        self.assertEqual(score_evidence([wiki])["verdict"], "TRUE")
        self.assertEqual(score_evidence([{**wiki, "stance": "REFUTES"}])["verdict"], "UNVERIFIED")

    def test_one_site_counts_once(self):
        one = score_evidence([self.ev("snopes.com", "REFUTES")])
        two = score_evidence([self.ev("snopes.com", "REFUTES"), self.ev("snopes.com", "REFUTES")])
        self.assertEqual((two["score"], two["refuters"]), (one["score"], 1))

    def test_mixed_ratings_pull_toward_the_middle(self):
        r = score_evidence([self.ev("politifact.com", "MIXED"), self.ev("snopes.com", "REFUTES")])
        self.assertEqual((r["for"], r["against"], r["verdict"]), (0.5, 1.5, "UNVERIFIED"))
        self.assertEqual(score_evidence([self.ev("politifact.com", "MIXED")])["score"], 50)

    def test_weaker_matches_count_for_less(self):
        strong = score_evidence([self.ev("apnews.com", "SUPPORTS", strength=1.0)])["score"]
        weak = score_evidence([self.ev("apnews.com", "SUPPORTS", strength=0.3)])["score"]
        self.assertGreater(strong, weak)
        self.assertGreater(weak, 50)

    def test_unknown_sites_are_ignored(self):
        r = score_evidence([self.ev("random.blog", "SUPPORTS", 0), self.ev("x.example", "SUPPORTS", 0)])
        self.assertEqual((r["verdict"], r["score"]), ("UNVERIFIED", 50))

    def test_disagreement_is_unverified(self):
        r = score_evidence([self.ev("apnews.com", "SUPPORTS"), self.ev("reuters.com", "SUPPORTS"),
                            self.ev("snopes.com", "REFUTES"), self.ev("factcheck.org", "REFUTES")])
        self.assertEqual((r["verdict"], r["score"]), ("UNVERIFIED", 50))

    def test_thresholds_are_configurable(self):
        ev = [self.ev("apnews.com", "SUPPORTS"), self.ev("reuters.com", "SUPPORTS"), self.ev("bbc.com", "REFUTES", 0.6)]
        self.assertEqual(score_evidence(ev)["verdict"], "TRUE")                   # 75 >= 70
        self.assertEqual(score_evidence(ev, t_true=80)["verdict"], "UNVERIFIED")  # 75 < 80


class WorkerTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(CHECKS_TABLE="checks", CACHE_TABLE="cache", BUCKET_NAME="bucket",
                          EVIDENCE_ENABLED="false")
        self.checks = FakeTable(["userId", "checkId"])
        self.cache = FakeTable(["imageHash"])
        self.checks.put_item({"userId": USER, "checkId": CHECK, "imageKey": f"{USER}_a.jpg", "imageHash": "h",
                              "status": "QUEUED"})
        worker._tables = {"checks": self.checks, "cache": self.cache}
        worker._keys = {}  # no LLM key here: these tests cover the rule-based fallback
        worker.llm.set_api_key(None)

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
