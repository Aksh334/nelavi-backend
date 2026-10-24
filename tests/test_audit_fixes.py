"""Regression tests for two audit fixes: the Tavily published-date bound (F1) and the stale error-code lists (F4).

Offline only: fake transports, with sockets and DNS patched to fail. They show how the code treats stand-in data; they
say nothing about what the live services return.
"""

import io
import json
import unittest
from unittest import mock

from demo_app.app import DemoApp
from demo_app.fake_research import _ERROR_CODES as FAKE_CODES
from demo_app.fake_research import FakeResearchClient
from demo_app.service import DemoService
from image_analysis.boundary import ERROR_CODES as IMAGE_ERROR_CODES
from orchestration import synthesis
from research import tavily
from research.tavily import MAX_DATE_CHARS, TavilyResearch
from scripts.nvidia_smoke_test import STAGE_ONE_MEANING
from storage.record_store import RecordStore

KEY = "tvly-canary-not-a-real-key-0009"
HOST = "127.0.0.1:8765"
MARKER = "OVERSIZED-DATE-MARKER"
FINDINGS = {
    "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
    "growth_stage": {"candidate": "stage-x", "evidence": ["plant height"], "uncertainty": "high"},
    "symptoms": [{"observation": "brown spots", "possible_interpretations": ["possible-cause-a"]}],
    "needs_more": ["a close-up"],
}


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


def hit(date="MISSING", url="https://fao.org/a", **extra):
    entry = {"title": "A title", "url": url, "content": "A snippet."}
    if date != "MISSING":
        entry["published_date"] = date
    entry.update(extra)
    return entry


def search(results):
    transport = lambda u, h, b, t: (200, json.dumps({"results": results}).encode("utf-8"))   # noqa: E731
    return TavilyResearch(["fao.org"], transport=transport, environ={"TAVILY_API_KEY": KEY},
                          clock=lambda: "2026-01-01T00:00:00+00:00").search("a question")


class TestPublishedDate(NoNetwork):
    def test_normal_dates_are_kept_exactly_as_reported(self):
        for date in ("2025-01-02", "2025-01-02T10:11:12Z", "Thu, 02 Jan 2025 10:00:00 GMT", "2025", "d" * MAX_DATE_CHARS):
            with self.subTest(date[:30]):
                (item,) = search([hit(date)]).items
                self.assertEqual(item.published_date, date)

    def test_a_missing_or_unusable_date_becomes_none_and_the_result_is_kept(self):
        cases = {"missing": "MISSING", "null": None, "empty": "", "blank": "   ", "number": 20250102, "list": ["2025"],
                 "dict": {"a": 1}, "bool": True, "newline": "2025-01-02\nInjected", "tab": "2025\t01",
                 "nul": "2025\x00", "escape": "\x1b[31m2025"}
        for name, date in cases.items():
            with self.subTest(name):
                outcome = search([hit(date)])
                self.assertEqual((outcome.status, outcome.dropped, len(outcome.items)), ("ok", 0, 1))
                self.assertIsNone(outcome.items[0].published_date)

    def test_an_oversized_date_is_dropped_not_cut_and_never_stored(self):
        for date in ("x" * (MAX_DATE_CHARS + 1), MARKER + "x" * 100000, "2025-01-02 " + "y" * 500):
            with self.subTest(len(date)):
                outcome = search([hit(date)])
                self.assertEqual((outcome.status, outcome.dropped), ("ok", 0))
                self.assertIsNone(outcome.items[0].published_date)
                self.assertNotIn(MARKER, repr(outcome))

    def test_the_other_fields_the_url_filter_and_the_counts_are_unchanged(self):
        outcome = search([hit("2025-01-02", score=0.5), hit("2025", url="http://fao.org/b"),
                          hit("2025", url="https://evil.example/c")])
        self.assertEqual(len(outcome.items), 1)
        self.assertEqual(outcome.dropped, 2)
        item = outcome.items[0]
        self.assertEqual((item.title, item.url, item.snippet, item.score), ("A title", "https://fao.org/a", "A snippet.", 0.5))

    def test_an_oversized_date_does_not_reach_the_integrated_response_or_the_report(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        client = TavilyResearch(["fao.org"], environ={"TAVILY_API_KEY": KEY}, clock=lambda: "2026-01-01T00:00:00+00:00",
                                transport=lambda u, h, b, t: (200, json.dumps({"results": [
                                    hit(MARKER + "z" * 5000), hit("2025-01-02", url="https://fao.org/b")]}).encode()))
        app = DemoApp(DemoService(store, research_client=client, research_domains=["fao.org"]), {HOST})
        payload = {"scenario": "normal_operation", "question": "how should soil moisture be read",
                   "providers": {"image": "skip", "research": "real", "compute": "fake", "synthesis": "fake"}}
        raw = json.dumps(payload).encode("utf-8")
        headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": str(len(raw)),
                   "content-type": "application/json"}
        response = app.handle_request("POST", "/api/run-integrated", headers, io.BytesIO(raw).read)
        self.assertEqual(response.status, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertNotIn(MARKER, response.body.decode("utf-8"))
        step = {s["name"]: s for s in body["integrated"]["run"]["steps"]}["research_search"]
        self.assertEqual([i["published_date"] for i in step["result"]["items"]], [None, "2025-01-02"])
        entries = body["integrated"]["report"]["sections"]["retrieved_sources"]["entries"]
        self.assertEqual([(e["publication_date"], e["date_source"]) for e in entries],
                         [(None, None), ("2025-01-02", "provider_estimate")])
        self.assertEqual([e["review_status"] for e in entries], ["unreviewed", "unreviewed"])

    def test_the_synthesis_input_still_bounds_a_date_from_any_other_source(self):
        research = {"items": [{"title": "t", "url": "https://fao.org/a", "snippet": "s", "published_date": "d" * 500,
                               "score": None}]}
        built = synthesis.inputs_from_results({"findings": FINDINGS}, research, "q q")
        self.assertIsNone(built["evidence"][0]["published_date"])

    def test_the_documented_limit_is_the_one_in_the_code(self):
        self.assertEqual(MAX_DATE_CHARS, synthesis.MAX_EVIDENCE_DATE_CHARS)
        self.assertIn("MAX_DATE_CHARS", tavily.__doc__)


class TestStaleCodeLists(NoNetwork):
    def test_the_smoke_script_explains_every_closed_image_error_code(self):
        for code in IMAGE_ERROR_CODES:
            self.assertIn(code, STAGE_ONE_MEANING, code)
            self.assertGreater(len(STAGE_ONE_MEANING[code]), 15, code)
        self.assertEqual(set(STAGE_ONE_MEANING) - set(IMAGE_ERROR_CODES), set())

    def test_the_new_transport_codes_have_their_own_wording_that_names_no_provider_detail(self):
        for code in ("provider_timeout", "provider_connection_failed", "transport_failed"):
            self.assertIn(code, STAGE_ONE_MEANING)
            self.assertNotIn(KEY, STAGE_ONE_MEANING[code])

    def test_the_fake_research_codes_are_all_codes_the_real_adapter_can_produce(self):
        self.assertNotIn("provider_unreachable", FAKE_CODES)
        self.assertTrue(set(FAKE_CODES) <= set(tavily.ERROR_CODES), set(FAKE_CODES) - set(tavily.ERROR_CODES))
        for code in ("provider_timeout", "provider_connection_failed", "transport_failed"):
            self.assertIn(code, FAKE_CODES)

    def test_the_fake_client_accepts_each_listed_code_and_refuses_others(self):
        for code in FAKE_CODES:
            with self.subTest(code):
                result = FakeResearchClient(error_code=code).search("a question")
                self.assertEqual((result.status, result.error_code, result.items), ("error", code, ()))
        for code in ("provider_unreachable", "no_results", "not_a_code", ""):
            with self.assertRaises(ValueError):
                FakeResearchClient(error_code=code)

    def test_the_adapter_docstring_no_longer_says_it_is_unwired(self):
        self.assertNotIn("not wired into the server", tavily.__doc__)
        self.assertIn("--research-provider tavily", tavily.__doc__)


if __name__ == "__main__":
    unittest.main()
