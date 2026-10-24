"""Offline tests for orchestration/pipeline_steps.py (the bridge from execute_run to the report pipeline).

Everything here is SYNTHETIC: the Record, the "PNG" bytes, the model replies and the search results. The image
analyzer is a local fake and the Tavily Search adapter runs against a fake transport. No provider is called and
no credential is used; the key below is a made-up canary string.

These tests show the bridge behaves as designed. They do not show that any real model or search service works,
and the socket/urlopen patches are tripwires, not proof of network isolation.
"""

import ast
import copy
import dataclasses
import hashlib
import itertools
import json
import unittest
from pathlib import Path
from unittest import mock

from agri_pipeline import pipeline
from agri_pipeline.findings import PromptedFindingsProvider, UnavailableFindingsProvider, FindingsResult
from image_analysis.boundary import error_result, ok_result
from orchestration import pipeline_steps as ps
from orchestration import run as run_module
from orchestration.run import Step, execute_run
from research.tavily import (
    MAX_QUERY_CHARS,
    MAX_RESULTS_LIMIT,
    MAX_SNIPPET_CHARS,
    MAX_TITLE_CHARS,
    ResearchItem,
    ResearchResult,
    TavilyResearch,
)

KEY = "canary-tavily-key-1234567890"
NOW = "2026-01-02T03:04:05+00:00"
DOMAINS = ("agri.example.org", "research.example.net")
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image"
QUERY = "soil testing guidance"


def make_record(**overrides):
    record = {
        "time": "sim:1", "sequence_number": 3, "rover_id": "test-rover",
        "battery": {"voltage_v": 12.0},
        "soil": {"nitrogen": 10.0, "phosphorus": 20.0, "potassium": 30.0, "moisture": 40.0, "temperature": 20.0},
        "air": {"temperature": 25.0, "humidity": 50.0, "pressure": 1000.0},
        "sensor_status": {"npk": "ok", "moisture": "ok", "soil_temperature": "ok", "air": "ok", "battery": "ok"},
        "leaf_label": "leaf-a", "confidence": 80, "photo_id": "photo-opaque-1",
        "versions": {"firmware": "f", "model": "m", "rules": "r", "calibration": "c"},
    }
    record.update(overrides)
    return record


def good_findings():
    return {
        "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
        "growth_stage": {"candidate": "stage-x", "evidence": ["plant height"], "uncertainty": "high"},
        "symptoms": [{"observation": "brown spots on lower leaves", "possible_interpretations": ["possible-cause-a"]}],
        "needs_more": ["a close-up of the underside of a leaf"],
    }


def make_clock():
    counter = itertools.count()
    return lambda: "t%d" % next(counter)


class FakeAnalyzer:
    """A local stand-in for an ImageAnalyzer. It records every request and never touches a network."""

    def __init__(self, text=None, error_code=None, model="fake-model", raises=None):
        self.text = json.dumps(good_findings()) if text is None else text
        self.error_code, self.model, self.raises = error_code, model, raises
        self.requests = []

    def analyze(self, request):
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if self.error_code is not None:
            return error_result(self.error_code, self.model)
        return ok_result(self.text, self.model)


class Transport:
    """A fake Tavily transport: records each call and returns a scripted reply."""

    def __init__(self, status=200, body=None, error=None):
        self.status, self.error, self.calls = status, error, []
        self.body = self.reply(
            {"title": "First", "url": "https://agri.example.org/a", "content": "First excerpt.", "score": 0.9,
             "published_date": "2025-05-06"},
            {"title": "Off list", "url": "https://evil.example.com/x", "content": "Not allowed.", "score": 0.5},
        ) if body is None else body

    @staticmethod
    def reply(*results):
        return json.dumps({"results": list(results), "request_id": "r"}).encode()

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), json.loads(body), timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body


def search_client(transport, environ=None):
    return TavilyResearch(DOMAINS, transport=transport,
                          environ={"TAVILY_API_KEY": KEY} if environ is None else environ, clock=lambda: NOW)


def findings_provider(analyzer=None):
    return PromptedFindingsProvider(analyzer if analyzer is not None else FakeAnalyzer())


def by_name(run):
    return {s["name"]: s for s in run["steps"]}


class NoSockets(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestImageStep(NoSockets):
    def run_step(self, provider, png=PNG, **kwargs):
        return execute_run(make_record(), [ps.image_findings_step(provider, png, **kwargs)], run_id="run-1",
                           clock=make_clock())

    def test_a_successful_step_returns_json_with_findings_model_and_provenance(self):
        analyzer = FakeAnalyzer()
        run = self.run_step(findings_provider(analyzer))
        step = run["steps"][0]
        self.assertEqual((run["status"], step["status"], step["provider"]), ("completed", "completed", "image"))
        self.assertEqual(step["result"], {"status": "ok", "model": "fake-model", "provenance": "image_analysis",
                                          "findings": good_findings()})
        json.dumps(run)
        self.assertEqual(len(analyzer.requests), 1)
        self.assertEqual(analyzer.requests[0].image.data, PNG)
        self.assertEqual(analyzer.requests[0].image.media_type, "image/png")

    def test_hints_are_forwarded_to_the_provider_unchanged(self):
        seen = []

        class Provider:
            def analyze_image(self, png_bytes, hints=None):
                seen.append((png_bytes, hints))
                return FindingsResult("ok", None, None, good_findings(), "m")

        self.run_step(Provider(), hints={"crop": "crop-a"})
        self.assertEqual(seen, [(PNG, {"crop": "crop-a"})])

    def test_invalid_png_bytes_fail_with_image_unsupported_and_never_reach_the_provider(self):
        for bad in (b"not a png", b"", b"\x89PNG", bytearray(PNG), "text", 5, [PNG]):
            with self.subTest(bad=repr(bad)[:30]):
                analyzer = FakeAnalyzer()
                run = self.run_step(findings_provider(analyzer), png=bad)
                step = run["steps"][0]
                self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", "image_unsupported", None))
                self.assertEqual(run["status"], "failed")
                self.assertEqual(analyzer.requests, [])

    def test_missing_bytes_or_provider_skip_the_step_and_never_call_anything(self):
        analyzer = FakeAnalyzer()
        run = self.run_step(findings_provider(analyzer), png=None)
        self.assertEqual((run["steps"][0]["status"], run["steps"][0]["skip_reason"], run["status"]),
                         ("skipped", "no_image_supplied", "skipped"))
        run = self.run_step(None)
        self.assertEqual((run["steps"][0]["status"], run["steps"][0]["skip_reason"]), ("skipped", "no_provider_configured"))
        self.assertEqual(analyzer.requests, [])

    def test_provider_failures_are_failed_steps_with_the_providers_closed_code(self):
        for code in ("provider_unavailable", "provider_rejected", "credential_missing", "response_too_large"):
            with self.subTest(code=code):
                run = self.run_step(findings_provider(FakeAnalyzer(error_code=code)))
                step = run["steps"][0]
                self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", code, None))

    def test_an_unavailable_provider_and_a_malformed_reply_are_failures_not_findings(self):
        run = self.run_step(UnavailableFindingsProvider())
        self.assertEqual((run["steps"][0]["status"], run["steps"][0]["error_code"]), ("failed", "no_model_configured"))
        for text in ("not json", "[]", json.dumps({"crop": {}}), ""):
            with self.subTest(text=text):
                run = self.run_step(findings_provider(FakeAnalyzer(text=text)))
                self.assertEqual((run["steps"][0]["status"], run["steps"][0]["error_code"]),
                                 ("failed", "response_malformed"))

    def test_findings_from_a_provider_that_skipped_its_own_validation_are_rechecked(self):
        class Careless:
            def analyze_image(self, png_bytes, hints=None):
                return FindingsResult("ok", None, None, {"crop": "anything"}, "m")

        run = self.run_step(Careless())
        self.assertEqual((run["steps"][0]["status"], run["steps"][0]["error_code"]), ("failed", "response_malformed"))

    def test_an_exception_message_never_reaches_the_run(self):
        run = self.run_step(findings_provider(FakeAnalyzer(raises=RuntimeError("provider said " + KEY))))
        self.assertEqual((run["steps"][0]["status"], run["steps"][0]["error_code"]), ("failed", "step_failed"))
        self.assertNotIn(KEY, json.dumps(run))
        self.assertNotIn("RuntimeError", json.dumps(run))

    def test_a_provider_without_analyze_image_is_a_programming_error(self):
        with self.assertRaises(TypeError):
            ps.image_findings_step(object(), PNG)


class TestResearchStep(NoSockets):
    def run_step(self, client, query=QUERY, **kwargs):
        return execute_run(make_record(), [ps.research_step(client, query, **kwargs)], run_id="run-1",
                           clock=make_clock())

    def test_a_successful_search_keeps_citations_dates_scores_and_caveats(self):
        transport = Transport()
        run = self.run_step(search_client(transport))
        step = run["steps"][0]
        self.assertEqual((run["status"], step["status"], step["provider"]), ("completed", "completed", "research"))
        result = step["result"]
        self.assertEqual((result["status"], result["query"], result["retrieved_at"], result["dropped"]),
                         ("ok", QUERY, NOW, 1))
        self.assertEqual(result["items"], [{"title": "First", "url": "https://agri.example.org/a",
                                            "snippet": "First excerpt.", "published_date": "2025-05-06", "score": 0.9}])
        self.assertEqual(result["kind"], "research_evidence_only")
        self.assertEqual({c[0] for c in result["caveats"]},
                         {"not_a_recommendation", "domains_are_owner_configured", "published_date_unreliable"})
        json.dumps(run)
        self.assertEqual(len(transport.calls), 1)  # one search, never a retry
        self.assertEqual(transport.calls[0][2]["query"], QUERY)

    def test_failures_are_failed_steps_with_the_search_adapters_closed_code(self):
        cases = ((Transport(status=429), "rate_limited"), (Transport(status=432), "usage_limit_exceeded"),
                 (Transport(status=401), "provider_rejected"), (Transport(status=503), "provider_unavailable"),
                 (Transport(body=b"{not json"), "response_malformed"), (Transport(body=Transport.reply()), "no_results"))
        for transport, code in cases:
            with self.subTest(code=code):
                run = self.run_step(search_client(transport))
                step = run["steps"][0]
                self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", code, None))
                self.assertLessEqual(len(transport.calls), 1)
        run = self.run_step(search_client(Transport()), query="   ")
        self.assertEqual(run["steps"][0]["error_code"], "query_invalid")
        run = self.run_step(search_client(Transport(), environ={}))
        self.assertEqual(run["steps"][0]["error_code"], "credential_missing")

    def test_an_explicit_skip_is_skipped_and_the_client_is_never_called(self):
        transport = Transport()
        run = self.run_step(search_client(transport), skip_reason="owner_skipped")
        self.assertEqual((run["steps"][0]["status"], run["steps"][0]["skip_reason"], run["status"]),
                         ("skipped", "owner_skipped", "skipped"))
        self.assertEqual(transport.calls, [])

    def test_no_client_or_no_query_is_a_skip(self):
        transport = Transport()
        self.assertEqual(self.run_step(None)["steps"][0]["skip_reason"], "no_provider_configured")
        self.assertEqual(self.run_step(search_client(transport), query=None)["steps"][0]["skip_reason"],
                         "no_query_supplied")
        self.assertEqual(transport.calls, [])

    def test_an_unusable_result_object_is_a_malformed_failure(self):
        class Odd:
            def search(self, query):
                return mock.Mock(status="ok", items=5)

        class Silent:
            def search(self, query):
                return None

        for client in (Odd(), Silent()):
            run = self.run_step(client)
            self.assertEqual((run["steps"][0]["status"], run["steps"][0]["error_code"]), ("failed", "response_malformed"))

    def test_a_result_that_json_cannot_hold_follows_the_runners_own_error_behaviour(self):
        class NanClient:
            def search(self, query):
                item = ResearchItem("t", "https://agri.example.org/x", "s", None, float("nan"))
                return ResearchResult("ok", None, query, (item,), 0, NOW)

        run = self.run_step(NanClient())
        self.assertEqual((run["steps"][0]["status"], run["steps"][0]["error_code"]),
                         ("failed", "result_not_serializable"))

    def test_a_client_without_search_is_a_programming_error(self):
        with self.assertRaises(TypeError):
            ps.research_step(object(), QUERY)


class TestAssembly(NoSockets):
    def direct(self, findings_result=None, research_result=None, **kwargs):
        return pipeline.run(make_record(), origin="simulated", findings_result=findings_result,
                            research_result=research_result, **kwargs)

    def test_a_full_run_assembles_a_report_with_findings_and_sources(self):
        out = ps.run_report(make_record(), findings_provider=findings_provider(), png_bytes=PNG,
                            research_client=search_client(Transport()), query=QUERY, origin="simulated",
                            record_id=7, run_id="run-1", clock=make_clock())
        self.assertEqual((out["run_status"], out["run_id"], out["record_id"], out["record_id_scope"]),
                         ("completed", "run-1", 7, "local_store"))
        report = out["report"]
        self.assertEqual(report["status"], "ok")
        sections = report["sections"]
        self.assertEqual(sections["image_inference"]["status"], "ok")
        self.assertEqual(sections["image_inference"]["model"], "fake-model")
        self.assertEqual(sections["retrieved_sources"]["status"], "ok")
        entry = sections["retrieved_sources"]["entries"][0]
        self.assertEqual((entry["url"], entry["title"], entry["retrieved_at"], entry["review_status"]),
                         ("https://agri.example.org/a", "First", NOW, "unreviewed"))
        self.assertEqual(sections["retrieved_sources"]["dropped"], 1)
        self.assertEqual(sections["recommendations"]["supported"], [])  # nothing becomes a recommendation
        json.dumps(out)

    def test_the_report_equals_a_direct_pipeline_call_with_the_original_objects(self):
        analyzer_text = json.dumps(good_findings())
        findings_result = PromptedFindingsProvider(FakeAnalyzer(text=analyzer_text)).analyze_image(PNG)
        research_result = search_client(Transport()).search(QUERY)
        direct = self.direct(findings_result, research_result, image_ref="photo-ref")
        out = ps.run_report(make_record(), findings_provider=findings_provider(FakeAnalyzer(text=analyzer_text)),
                            png_bytes=PNG, research_client=search_client(Transport()), query=QUERY,
                            origin="simulated", image_ref="photo-ref", run_id="run-1")
        self.assertEqual(out["report"], direct)

    def test_rebuilt_objects_equal_the_originals(self):
        findings_result = PromptedFindingsProvider(FakeAnalyzer()).analyze_image(PNG)
        research_result = search_client(Transport()).search(QUERY)
        run = execute_run(make_record(), [ps.image_findings_step(findings_provider(), PNG),
                                          ps.research_step(search_client(Transport()), QUERY)], run_id="run-1")
        self.assertEqual(ps._findings_result(by_name(run)["image_findings"], None), findings_result)
        self.assertEqual(ps._research_result(by_name(run)["research_search"], None, QUERY), research_result)

    def test_parity_holds_when_a_stage_failed_or_was_skipped(self):
        # The direct objects come from the real provider and client, so what a failure still knows (the model
        # name, the query and the retrieval time) is part of the comparison.
        failed_findings = PromptedFindingsProvider(FakeAnalyzer(error_code="provider_unavailable")).analyze_image(PNG)
        failed_research = search_client(Transport(status=429)).search(QUERY)
        unavailable = UnavailableFindingsProvider().analyze_image(PNG)
        cases = (
            ("image error + research ok",
             dict(findings_provider=findings_provider(FakeAnalyzer(error_code="provider_unavailable")), png_bytes=PNG,
                  research_client=search_client(Transport()), query=QUERY),
             failed_findings, search_client(Transport()).search(QUERY)),
            ("image ok + research error",
             dict(findings_provider=findings_provider(), png_bytes=PNG,
                  research_client=search_client(Transport(status=429)), query=QUERY),
             PromptedFindingsProvider(FakeAnalyzer()).analyze_image(PNG), failed_research),
            ("image unavailable + research skipped",
             dict(findings_provider=UnavailableFindingsProvider(), png_bytes=PNG,
                  research_client=search_client(Transport()), query=QUERY, research_skip_reason="owner_skipped"),
             unavailable, None),
            ("image skipped + research skipped", dict(), None, None),
        )
        for label, kwargs, findings_result, research_result in cases:
            with self.subTest(label):
                out = ps.run_report(make_record(), origin="simulated", run_id="run-1", **kwargs)
                self.assertEqual(out["report"], self.direct(findings_result, research_result))

    def test_failures_are_visible_in_the_report_and_never_become_findings_or_sources(self):
        out = ps.run_report(make_record(), findings_provider=findings_provider(FakeAnalyzer(error_code="provider_unavailable")),
                            png_bytes=PNG, research_client=search_client(Transport(status=429)), query=QUERY,
                            origin="simulated", run_id="run-1")
        self.assertEqual(out["run_status"], "failed")
        sections = out["report"]["sections"]
        image = sections["image_inference"]
        self.assertEqual((image["status"], image["error_code"], image["findings"]), ("error", "provider_unavailable", None))
        self.assertEqual(out["report"]["context"]["crop"]["candidates"], [])
        self.assertEqual(out["report"]["context"]["symptoms"], [])
        sources = sections["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["entries"]), ("error", "rate_limited", []))
        self.assertEqual(sections["recommendations"]["supported"], [])
        codes = {u["code"] for u in sections["uncertain_or_unavailable"]}
        self.assertIn("research_failed", codes)

    def test_a_skipped_stage_is_reported_as_not_run_not_as_complete(self):
        transport = Transport()
        out = ps.run_report(make_record(), findings_provider=findings_provider(), png_bytes=None,
                            research_client=search_client(transport), query=QUERY,
                            research_skip_reason="owner_skipped", origin="simulated", run_id="run-1")
        self.assertEqual(out["run_status"], "skipped")
        self.assertEqual(out["report"]["sections"]["image_inference"]["status"], "not_run")
        self.assertEqual(out["report"]["sections"]["retrieved_sources"]["status"], "not_run")
        self.assertEqual(transport.calls, [])
        skipped = {s["name"]: s["skip_reason"] for s in out["run"]["steps"]}
        self.assertEqual(skipped, {"image_findings": "no_image_supplied", "research_search": "owner_skipped"})

    def test_completed_research_and_skipped_research_are_distinguishable(self):
        done = ps.run_report(make_record(), research_client=search_client(Transport()), query=QUERY,
                             origin="simulated", run_id="run-1")
        skipped = ps.run_report(make_record(), research_client=search_client(Transport()), query=QUERY,
                                research_skip_reason="owner_skipped", origin="simulated", run_id="run-1")
        self.assertEqual(done["report"]["sections"]["retrieved_sources"]["status"], "ok")
        self.assertEqual(skipped["report"]["sections"]["retrieved_sources"]["status"], "not_run")
        self.assertEqual(done["run_status"], "partial")  # the image step was skipped
        self.assertEqual(skipped["run_status"], "skipped")

    def test_an_invalid_record_runs_no_provider_and_gives_an_invalid_record_report(self):
        analyzer, transport = FakeAnalyzer(), Transport()
        out = ps.run_report(make_record(rover_id=""), findings_provider=findings_provider(analyzer), png_bytes=PNG,
                            research_client=search_client(transport), query=QUERY, origin="simulated",
                            record_id=4, run_id="run-1")
        self.assertEqual((out["run_status"], out["run"]["steps"]), ("invalid_record", []))
        self.assertEqual(out["report"]["status"], "invalid_record")
        self.assertEqual(analyzer.requests, [])
        self.assertEqual(transport.calls, [])
        self.assertEqual((out["record_id"], out["record_id_scope"]), (4, "local_store"))
        json.dumps(out)

    def test_record_id_and_run_id_stay_beside_the_record_and_the_report(self):
        record = make_record()
        before = copy.deepcopy(record)
        out = ps.run_report(record, findings_provider=findings_provider(), png_bytes=PNG, origin="simulated",
                            record_id=12, run_id="run-xyz")
        self.assertEqual(record, before)  # the Record was not modified
        self.assertNotIn("run_id", record)
        self.assertNotIn("record_id", record)
        self.assertEqual((out["run_id"], out["record_id"], out["record_id_scope"]), ("run-xyz", 12, "local_store"))
        self.assertEqual(out["run"]["run_id"], "run-xyz")
        text = json.dumps(out["report"])
        self.assertNotIn("run-xyz", text)
        self.assertNotIn("record_id", text)

    def test_without_a_record_id_the_scope_is_unavailable_and_nothing_is_inferred(self):
        out = ps.run_report(make_record(sequence_number=99), origin="simulated", run_id="run-1")
        self.assertEqual((out["record_id"], out["record_id_scope"]), (None, "unavailable"))

    def test_run_ids_are_generated_independently_and_a_factory_is_honoured(self):
        first = ps.run_report(make_record(), origin="simulated")["run_id"]
        second = ps.run_report(make_record(), origin="simulated")["run_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(ps.run_report(make_record(), origin="simulated", run_id_factory=lambda: "made-here")["run_id"],
                         "made-here")

    def test_photo_id_is_never_used_as_an_image_reference_or_a_path(self):
        record = make_record(photo_id="C:/secret/place.png")
        analyzer = FakeAnalyzer()
        out = ps.run_report(record, findings_provider=findings_provider(analyzer), png_bytes=PNG, origin="simulated",
                            run_id="run-1")
        self.assertEqual(out["report"]["context"]["symptoms"][0]["image_ref"], None)
        self.assertEqual(analyzer.requests[0].image.data, PNG)
        self.assertNotIn("C:/secret", json.dumps(analyzer.requests[0].prompt))

    def test_dependent_steps_follow_the_runners_rules(self):
        ran = []

        def annotate(context):
            ran.append(context["results"]["image_findings"]["status"])
            return "annotated"

        def steps(provider):
            return [ps.image_findings_step(provider, PNG), ps.research_step(search_client(Transport()), QUERY),
                    Step("annotate", "local", annotate, depends_on=("image_findings",))]

        good = execute_run(make_record(), steps(findings_provider()), run_id="run-1")
        self.assertEqual((by_name(good)["annotate"]["status"], ran), ("completed", ["ok"]))
        ran.clear()
        bad = execute_run(make_record(), steps(findings_provider(FakeAnalyzer(error_code="provider_unavailable"))),
                          run_id="run-1")
        self.assertEqual(by_name(bad)["annotate"]["status"], "skipped")
        self.assertEqual(by_name(bad)["annotate"]["skip_reason"], "dependency_not_completed")
        self.assertEqual(by_name(bad)["research_search"]["status"], "completed")  # independent: still runs
        self.assertEqual(ran, [])
        out = ps.assemble_report(make_record(), bad, origin="simulated")
        self.assertEqual(out["report"]["sections"]["image_inference"]["status"], "error")
        self.assertEqual(out["report"]["sections"]["retrieved_sources"]["status"], "ok")

    def test_non_closed_codes_are_shown_in_the_report_as_response_malformed_with_the_exact_code_in_the_run(self):
        out = ps.run_report(make_record(), findings_provider=findings_provider(FakeAnalyzer(raises=ValueError("x"))),
                            png_bytes=PNG, origin="simulated", run_id="run-1")
        self.assertEqual(by_name(out["run"])["image_findings"]["error_code"], "step_failed")
        self.assertEqual(out["report"]["sections"]["image_inference"]["error_code"], "response_malformed")

    def test_a_json_failure_in_research_is_shown_as_a_failed_research_stage(self):
        class NanClient:
            def search(self, query):
                item = ResearchItem("t", "https://agri.example.org/x", "s", None, float("nan"))
                return ResearchResult("ok", None, query, (item,), 0, NOW)

        out = ps.run_report(make_record(), research_client=NanClient(), query=QUERY, origin="simulated", run_id="run-1")
        self.assertEqual(by_name(out["run"])["research_search"]["error_code"], "result_not_serializable")
        sources = out["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["entries"]), ("error", "response_malformed", []))

    def test_bad_arguments_are_rejected_clearly(self):
        with self.assertRaises(TypeError):
            ps.assemble_report(make_record(), {"no": "steps"})
        with self.assertRaises(TypeError):
            ps.assemble_report(make_record(), "run")
        with self.assertRaises(ValueError):
            ps.run_report(make_record(), research_client=search_client(Transport()), query=QUERY,
                          research_skip_reason="Not A Token", run_id="run-1")
        with self.assertRaises(ValueError):
            ps.run_report(make_record(), run_id="has space")
        self.assertEqual(ps.run_report(make_record(), origin="mars", run_id="run-1")["report"]["status"], "invalid_input")

    def test_no_secret_value_reaches_the_result(self):
        out = ps.run_report(make_record(), findings_provider=findings_provider(), png_bytes=PNG,
                            research_client=search_client(Transport()), query=QUERY, origin="simulated",
                            run_id="run-1")
        self.assertNotIn(KEY, json.dumps(out))


class TestNoNetworkOrProviderSdk(NoSockets):
    def test_a_full_run_uses_no_network_entry_point(self):
        """A tripwire only: it fails if code in this process opens a socket or calls urllib's opener. It does not
        cover other libraries, other processes or the operating system."""
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network")):
            out = ps.run_report(make_record(), findings_provider=findings_provider(), png_bytes=PNG,
                                research_client=search_client(Transport()), query=QUERY, origin="simulated",
                                run_id="run-1")
        self.assertEqual(out["run_status"], "completed")

    def test_the_module_imports_only_the_reviewed_project_modules_and_the_standard_library(self):
        allowed = {"copy", "hashlib", "json", "re", "unicodedata", "agri_pipeline", "agri_pipeline.findings",
                   "image_analysis.boundary", "orchestration.run", "record.validation", "research.tavily"}
        tree = ast.parse(Path(ps.__file__).read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add(node.module or "")
        self.assertEqual(found - allowed, set())
        for forbidden in ("nodus", "requests", "urllib", "socket", "http", "openai", "image_analysis.nvidia",
                          "research.tavily_research", "demo_app", "storage"):
            self.assertNotIn(forbidden, found)

    def test_the_module_never_reads_the_environment_files_or_a_clock(self):
        tree = ast.parse(Path(ps.__file__).read_text(encoding="utf-8"))
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("environ", "getenv", "open", "subprocess", "sqlite3", "datetime", "time", "sleep", "urlopen"):
            self.assertNotIn(banned, used, banned)


class StubProvider:
    """A findings provider defined at module level so its class name is stable."""

    def __init__(self, result):
        self.result, self.calls = result, 0

    def analyze_image(self, png_bytes, hints=None):
        self.calls += 1
        return self.result


class RaisingSearch:
    def __init__(self):
        self.calls = 0

    def search(self, query):
        self.calls += 1
        raise RuntimeError("provider said " + KEY)


class FixedSearch:
    def __init__(self, result):
        self.result, self.queries = result, []

    def search(self, query):
        self.queries.append(query)
        return self.result


def full_run(**overrides):
    """A complete, successful run_report result made only from fakes."""
    kwargs = dict(findings_provider=findings_provider(), png_bytes=PNG, research_client=search_client(Transport()),
                  query=QUERY, origin="simulated", record_id=7, run_id="run-1", clock=make_clock())
    kwargs.update(overrides)
    return ps.run_report(make_record(), **kwargs)


class TestInputsCheckedBeforeProviders(NoSockets):
    """Item A: an invalid origin, user or image_ref must be refused before either provider is called."""

    def spied(self):
        analyzer, transport, made = FakeAnalyzer(), Transport(), []
        kwargs = dict(findings_provider=findings_provider(analyzer), png_bytes=PNG,
                      research_client=search_client(transport), query=QUERY,
                      run_id_factory=lambda: made.append(1) or "run-made")
        return analyzer, transport, made, kwargs

    def assert_refused(self, **inputs):
        analyzer, transport, made, kwargs = self.spied()
        out = ps.run_report(make_record(), **kwargs, **inputs)
        self.assertEqual(out["run_status"], "not_started")
        self.assertEqual((out["run"], out["run_id"], out["record_id"], out["record_id_scope"], out["provenance"]),
                         (None, None, None, "unavailable", {}))
        report = out["report"]
        self.assertEqual(report["status"], "invalid_input")
        self.assertTrue(report["errors"])
        self.assertTrue(all(set(e) == {"path", "code"} for e in report["errors"]))
        self.assertIsNone(report["sections"])
        self.assertEqual(analyzer.requests, [], inputs)   # the image provider was never called
        self.assertEqual(transport.calls, [], inputs)     # the research client was never called
        self.assertEqual(made, [])                        # and no run id was generated
        json.dumps(out)

    def test_an_invalid_origin_is_refused_with_zero_provider_calls(self):
        for origin in ("mars", "", None, 5, "SIMULATED"):
            with self.subTest(origin=origin):
                self.assert_refused(origin=origin)

    def test_an_invalid_user_is_refused_with_zero_provider_calls(self):
        for user in ({"nope": 1}, {"crop": "<b>"}, {"crop": ["Tomato"]}, "text", {"region": "x" * 65},
                     {"growth_stage": "line\nbreak"},
                     {"region": "India", "coordinates": {"latitude": 1.0, "longitude": 2.0}}):
            with self.subTest(user=repr(user)):
                self.assert_refused(origin="simulated", user=user)

    def test_an_invalid_image_ref_is_refused_with_zero_provider_calls(self):
        for image_ref in ("", "   ", "x" * 65, 5, "line\nbreak", ["a"]):
            with self.subTest(image_ref=repr(image_ref)):
                self.assert_refused(origin="simulated", image_ref=image_ref)

    def test_the_refusal_is_the_pipelines_own_invalid_input_report(self):
        out = ps.run_report(make_record(), origin="mars")
        self.assertEqual(out["report"], pipeline.run(make_record(), origin="mars"))
        out = ps.run_report(make_record(), origin="simulated", user={"nope": 1})
        self.assertEqual(out["report"], pipeline.run(make_record(), origin="simulated", user={"nope": 1}))

    def test_valid_values_still_run_every_stage_once(self):
        users = (None, {}, {"crop": "Tomato", "region": "India", "growth_stage": "flowering"},
                 {"coordinates": {"latitude": 12.5, "longitude": 77.5}})
        for origin in ("simulated", "hardware", "unknown"):
            for user in users:
                for image_ref in (None, "photo-1"):
                    with self.subTest(origin=origin, user=repr(user), image_ref=image_ref):
                        analyzer, transport, _, kwargs = self.spied()
                        out = ps.run_report(make_record(), origin=origin, user=user, image_ref=image_ref, **kwargs)
                        self.assertEqual((out["run_status"], out["report"]["status"]), ("completed", "ok"))
                        self.assertEqual((len(analyzer.requests), len(transport.calls)), (1, 1))
                        direct = pipeline.run(
                            make_record(), origin=origin, user=user, image_ref=image_ref,
                            findings_result=PromptedFindingsProvider(FakeAnalyzer()).analyze_image(PNG),
                            research_result=search_client(Transport()).search(QUERY))
                        self.assertEqual(out["report"], direct)

    def test_an_invalid_record_still_runs_no_provider_and_wins_over_a_bad_user(self):
        analyzer, transport, _, kwargs = self.spied()
        out = ps.run_report(make_record(rover_id=""), origin="simulated", user={"nope": 1}, **kwargs)
        self.assertEqual((out["run_status"], out["report"]["status"]), ("invalid_record", "invalid_record"))
        self.assertEqual((analyzer.requests, transport.calls), ([], []))

    def test_check_report_inputs_can_be_used_on_its_own(self):
        self.assertIsNone(ps.check_report_inputs(make_record(), origin="simulated", user={"crop": "Tomato"},
                                                 image_ref="photo-1"))
        self.assertIsNone(ps.check_report_inputs(make_record(rover_id=""), origin="simulated"))  # the Record is another status
        for bad in (dict(origin="mars"), dict(user={"nope": 1}), dict(image_ref=5)):
            with self.subTest(bad=repr(bad)):
                report = ps.check_report_inputs(make_record(), **bad)
                self.assertEqual(report["status"], "invalid_input")

    def test_bad_run_arguments_are_rejected_even_when_the_report_inputs_are_refused(self):
        """F-07 (decided): the run arguments are checked BEFORE the report-input refusal, so a bad one is never
        silently ignored. The exception type and fixed message are the same as for a call that is not refused."""
        bad_arguments = (dict(run_id="has space"), dict(run_id=""), dict(run_id="x" * 65), dict(run_id=5),
                         dict(record_id="x"), dict(record_id=0), dict(record_id=True),
                         dict(clock="not callable"), dict(run_id="a", run_id_factory=lambda: "b"),
                         dict(run_id_factory="not callable"))
        refusals = (dict(origin="mars"), dict(origin="simulated", user={"nope": 1}),
                    dict(origin="simulated", image_ref=5))
        for bad in bad_arguments:
            with self.subTest(bad=repr(bad)[:40]):
                with self.assertRaises((ValueError, TypeError)) as plain:
                    ps.run_report(make_record(), origin="simulated", **bad)
                for refusal in refusals:
                    with self.assertRaises(type(plain.exception)) as refused:
                        ps.run_report(make_record(), **refusal, **bad)
                    self.assertEqual(str(refused.exception), str(plain.exception))

    def test_valid_run_arguments_do_not_change_the_refusal_and_are_never_invoked(self):
        calls = []
        clock = lambda: calls.append("clock") or "t"
        factory = lambda: calls.append("factory") or "run-made"
        with_arguments = ps.run_report(make_record(), origin="mars", run_id=None, run_id_factory=factory,
                                       record_id=5, clock=clock)
        explicit = ps.run_report(make_record(), origin="mars", run_id="run-1", record_id=5, clock=clock)
        plain = ps.run_report(make_record(), origin="mars")
        self.assertEqual(with_arguments, plain)
        self.assertEqual(explicit, plain)
        self.assertEqual((plain["run_status"], plain["run"], plain["run_id"], plain["record_id"]),
                         ("not_started", None, None, None))
        self.assertEqual(calls, [])   # neither the clock nor the run-id factory was called

    def test_the_validation_order_is_providers_then_run_arguments_then_report_inputs(self):
        # run arguments before report inputs: a bad run_id wins over a bad origin
        with self.assertRaises(ValueError) as raised:
            ps.run_report(make_record(), origin="mars", run_id="has space")
        self.assertIn("run_id", str(raised.exception))
        # among the run arguments: record_id, then run_id, then clock (the runner's own order)
        with self.assertRaises(ValueError) as raised:
            ps.run_report(make_record(), origin="mars", record_id=0, run_id="has space", clock=5)
        self.assertIn("record_id", str(raised.exception))
        with self.assertRaises(ValueError):
            ps.run_report(make_record(), origin="mars", run_id="has space", clock=5)
        # a wrong provider object is reported before any of the above
        with self.assertRaises(TypeError) as raised:
            ps.run_report(make_record(), findings_provider=object(), png_bytes=PNG, origin="mars",
                          run_id="has space")
        self.assertIn("analyze_image", str(raised.exception))
        # the messages never contain the submitted values
        with self.assertRaises(ValueError) as raised:
            ps.run_report(make_record(), origin="mars", run_id="secret " + KEY)
        self.assertNotIn(KEY, str(raised.exception))

    def test_query_is_not_checked_here_the_research_client_checks_it(self):
        analyzer, transport = FakeAnalyzer(), Transport()
        out = ps.run_report(make_record(), findings_provider=findings_provider(analyzer), png_bytes=PNG,
                            research_client=search_client(transport), query="   ", origin="simulated")
        self.assertEqual(out["run_status"], "failed")
        self.assertEqual(by_name(out["run"])["research_search"]["error_code"], "query_invalid")
        self.assertEqual(len(analyzer.requests), 1)


class TestKeywordReconstruction(NoSockets):
    """Item B: dataclasses are rebuilt by field name, never by position."""

    def test_every_construction_in_the_module_uses_only_known_keywords(self):
        fields = {cls.__name__: {f.name for f in dataclasses.fields(cls)}
                  for cls in (FindingsResult, ResearchResult, ResearchItem)}
        tree = ast.parse(Path(ps.__file__).read_text(encoding="utf-8"))
        seen = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in fields:
                name = node.func.id
                self.assertEqual(node.args, [], name)                      # no positional arguments
                names = {k.arg for k in node.keywords}
                self.assertNotIn(None, names, name)                        # no **mapping either
                self.assertLessEqual(names, fields[name], name)            # only real field names
                seen[name] = seen.get(name, 0) + 1
        self.assertEqual(set(seen), {"FindingsResult", "ResearchResult", "ResearchItem"})

    def test_a_non_default_provenance_kind_and_caveats_survive_the_round_trip(self):
        findings = FindingsResult(status="ok", error_code=None, reason=None, findings=copy.deepcopy(good_findings()),
                                  model="m-1", provenance="custom_source")
        research = ResearchResult(
            status="ok", error_code=None, query=QUERY,
            items=(ResearchItem(title="T", url="https://agri.example.org/a", snippet="S", published_date=None, score=0.25),
                   ResearchItem(title="U", url="https://agri.example.org/b", snippet="V", published_date="2024-01-01",
                                score=None)),
            dropped=3, retrieved_at=NOW, kind="custom_kind", caveats=(("c1", "first"), ("c2", "second")))
        run = execute_run(make_record(), [ps.image_findings_step(StubProvider(findings), PNG),
                                          ps.research_step(FixedSearch(research), QUERY)], run_id="run-1")
        self.assertEqual(ps._findings_result(by_name(run)["image_findings"], None), findings)
        self.assertEqual(ps._research_result(by_name(run)["research_search"], None, QUERY), research)
        # item order is preserved
        rebuilt = ps._research_result(by_name(run)["research_search"], None, QUERY)
        self.assertEqual([i.title for i in rebuilt.items], ["T", "U"])


class TestFailureProvenance(NoSockets):
    """Item C: a failed stage keeps the safe facts it genuinely has, and nothing it does not."""

    def test_an_image_failure_keeps_the_model_and_the_configured_implementation(self):
        out = full_run(findings_provider=findings_provider(FakeAnalyzer(error_code="provider_unavailable")))
        stage = out["provenance"]["image_findings"]
        self.assertEqual(stage, {"implementation": "agri_pipeline.findings.PromptedFindingsProvider",
                                 "provider_called": True, "model": "fake-model"})
        step = by_name(out["run"])["image_findings"]
        self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", "provider_unavailable", None))
        image = out["report"]["sections"]["image_inference"]
        self.assertEqual((image["status"], image["error_code"], image["model"], image["findings"]),
                         ("error", "provider_unavailable", "fake-model", None))
        direct = pipeline.run(
            make_record(), origin="simulated",
            findings_result=PromptedFindingsProvider(FakeAnalyzer(error_code="provider_unavailable")).analyze_image(PNG),
            research_result=search_client(Transport()).search(QUERY))
        self.assertEqual(out["report"], direct)
        json.dumps(out)

    def test_an_unsafe_model_text_is_not_kept(self):
        for model in ("bad\nmodel", "x" * 129, "", "   ", "tab\there", "\x00"):
            with self.subTest(model=repr(model)[:20]):
                out = full_run(findings_provider=findings_provider(FakeAnalyzer(error_code="provider_unavailable",
                                                                                model=model)))
                self.assertNotIn("model", out["provenance"]["image_findings"])
                self.assertIsNone(out["report"]["sections"]["image_inference"]["model"])

    def test_a_refused_image_records_that_the_provider_was_not_called(self):
        analyzer = FakeAnalyzer()
        out = full_run(findings_provider=findings_provider(analyzer), png_bytes=b"not a png")
        self.assertEqual(out["provenance"]["image_findings"],
                         {"implementation": "agri_pipeline.findings.PromptedFindingsProvider", "provider_called": False})
        self.assertEqual(by_name(out["run"])["image_findings"]["error_code"], "image_unsupported")
        self.assertEqual(analyzer.requests, [])
        self.assertIsNone(out["report"]["sections"]["image_inference"]["model"])

    def test_the_implementation_is_the_class_that_was_configured_not_a_vendor_claim(self):
        findings = FindingsResult(status="ok", error_code=None, reason=None, findings=good_findings(), model="m")
        out = full_run(findings_provider=StubProvider(findings), research_client=FixedSearch(
            search_client(Transport()).search(QUERY)))
        self.assertEqual(out["provenance"]["image_findings"]["implementation"], StubProvider.__module__ + ".StubProvider")
        self.assertEqual(out["provenance"]["research_search"]["implementation"],
                         FixedSearch.__module__ + ".FixedSearch")
        for stage in out["provenance"].values():
            if isinstance(stage, dict):
                for value in stage.values():
                    self.assertNotIn(str(value).lower(), ("nvidia", "tavily"))

    def test_a_research_failure_keeps_the_query_the_retrieval_time_and_the_dropped_count(self):
        out = full_run(research_client=search_client(Transport(status=429)))
        self.assertEqual(out["provenance"]["research_search"],
                         {"implementation": "research.tavily.TavilyResearch", "provider_called": True,
                          "query": QUERY, "retrieved_at": NOW, "dropped": 0})
        sources = out["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["retrieved_at"], sources["entries"]),
                         ("error", "rate_limited", NOW, []))
        direct = pipeline.run(make_record(), origin="simulated",
                              findings_result=PromptedFindingsProvider(FakeAnalyzer()).analyze_image(PNG),
                              research_result=search_client(Transport(status=429)).search(QUERY))
        self.assertEqual(out["report"], direct)

    def test_dropped_results_are_kept_only_when_the_client_reported_them(self):
        only_off_list = Transport(body=Transport.reply({"title": "x", "url": "https://evil.example.com/x",
                                                        "content": "y", "score": 0.1}))
        out = full_run(research_client=search_client(only_off_list))
        self.assertEqual(out["provenance"]["research_search"]["dropped"], 1)
        self.assertEqual(out["report"]["sections"]["retrieved_sources"]["dropped"], 1)

    def test_a_query_the_client_refused_is_not_echoed_back(self):
        out = full_run(research_client=search_client(Transport()), query="   ")
        self.assertNotIn("query", out["provenance"]["research_search"])
        self.assertNotIn("   ", json.dumps(out["report"]["sections"]["retrieved_sources"]))
        direct = pipeline.run(make_record(), origin="simulated",
                              findings_result=PromptedFindingsProvider(FakeAnalyzer()).analyze_image(PNG),
                              research_result=search_client(Transport()).search("   "))
        self.assertEqual(out["report"], direct)

    def test_no_exception_text_reaches_the_provenance_or_the_run(self):
        client = RaisingSearch()
        out = full_run(research_client=client)
        self.assertEqual(client.calls, 1)
        self.assertEqual(out["provenance"]["research_search"],
                         {"implementation": RaisingSearch.__module__ + ".RaisingSearch", "provider_called": True})
        self.assertEqual(by_name(out["run"])["research_search"]["error_code"], "step_failed")
        self.assertNotIn(KEY, json.dumps(out))
        self.assertNotIn("RuntimeError", json.dumps(out))

    def test_error_codes_stay_in_the_closed_sets(self):
        out = full_run(findings_provider=findings_provider(FakeAnalyzer(raises=ValueError("x"))),
                       research_client=RaisingSearch())
        self.assertEqual(out["report"]["sections"]["image_inference"]["error_code"], "response_malformed")
        self.assertEqual(out["report"]["sections"]["retrieved_sources"]["error_code"], "response_malformed")
        self.assertEqual(by_name(out["run"])["image_findings"]["error_code"], "step_failed")

    def test_a_skipped_stage_has_no_provenance_and_is_not_a_failure(self):
        out = full_run(research_skip_reason="owner_skipped", png_bytes=None)
        self.assertEqual(out["provenance"].get("image_findings"), None)
        self.assertEqual(out["provenance"].get("research_search"), None)
        self.assertEqual({s["status"] for s in out["run"]["steps"]}, {"skipped"})
        failed = full_run(research_client=search_client(Transport(status=503)))
        self.assertEqual(by_name(failed["run"])["research_search"]["status"], "failed")
        self.assertNotEqual(by_name(failed["run"])["research_search"]["status"], "skipped")

    def test_a_successful_run_keeps_its_report_and_adds_only_the_implementation_facts(self):
        out = full_run()
        self.assertEqual(out["provenance"]["image_findings"],
                         {"implementation": "agri_pipeline.findings.PromptedFindingsProvider",
                          "provider_called": True, "model": "fake-model"})
        research = out["provenance"]["research_search"]
        self.assertEqual((research["provider_called"], research["query"], research["retrieved_at"]), (True, QUERY, NOW))
        direct = pipeline.run(make_record(), origin="simulated",
                              findings_result=PromptedFindingsProvider(FakeAnalyzer()).analyze_image(PNG),
                              research_result=search_client(Transport()).search(QUERY))
        self.assertEqual(out["report"], direct)

    def test_provider_called_means_invoked_not_that_a_vendor_was_contacted(self):
        """The real search adapter refuses before sending anything when the key is missing or the query is
        unusable. The adapter was still INVOKED, so `provider_called` is True while no request was made."""
        for environ, query, code in (({}, QUERY, "credential_missing"), (None, "   ", "query_invalid")):
            with self.subTest(code=code):
                transport = Transport()
                out = full_run(research_client=search_client(transport, environ=environ), query=query)
                self.assertEqual(by_name(out["run"])["research_search"]["error_code"], code)
                self.assertIs(out["provenance"]["research_search"]["provider_called"], True)
                self.assertEqual(transport.calls, [])      # no request reached the transport

    def test_assembling_a_failed_research_stage_without_provenance_uses_only_the_given_query(self):
        """The fallback branch of the rebuild: a run made WITHOUT a provenance dict. Nothing the search did not
        report (retrieval time, counts) is invented; the caller's query is used only if it is usable text."""
        transport = Transport(status=429)
        run = execute_run(make_record(), [ps.research_step(search_client(transport), QUERY)], run_id="run-1")
        self.assertEqual(by_name(run)["research_search"]["error_code"], "rate_limited")
        out = ps.assemble_report(make_record(), run, origin="simulated", query=QUERY)
        sources = out["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["retrieved_at"], sources["dropped"],
                          sources["entries"]), ("error", "rate_limited", None, 0, []))
        self.assertEqual(out["provenance"], {})
        direct = ResearchResult(status="error", error_code="rate_limited", query=QUERY, items=(), dropped=0,
                                retrieved_at=None)
        self.assertEqual(out["report"], pipeline.run(make_record(), origin="simulated", research_result=direct))
        # an unusable or missing query is not echoed
        for bad in (None, "   ", "x" * 500, "line\nbreak", 5):
            with self.subTest(query=repr(bad)[:12]):
                again = ps.assemble_report(make_record(), run, origin="simulated", query=bad)
                expected = ResearchResult(status="error", error_code="rate_limited", query=None, items=(),
                                          dropped=0, retrieved_at=None)
                self.assertEqual(again["report"], pipeline.run(make_record(), origin="simulated",
                                                               research_result=expected))

    def test_provenance_given_to_assemble_report_is_sanitised(self):
        run = full_run()["run"]
        hostile = {
            "image_findings": {"model": "bad\nx", "extra": "evil", "provider_called": "yes", "implementation": "x" * 200},
            "research_search": {"query": "x" * 500, "dropped": -1, "retrieved_at": 5},
            "other_stage": {"a": 1}, "record_sha256": None,
        }
        out = ps.assemble_report(make_record(), run, origin="simulated", provenance=hostile)
        self.assertEqual(out["provenance"], {"image_findings": {}, "research_search": {}})
        for bad in ({"record_sha256": "nothex"}, {"record_sha256": "A" * 64}, {"record_sha256": 5}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ps.assemble_report(make_record(), run, provenance=bad)
        with self.assertRaises(TypeError):
            ps.assemble_report(make_record(), run, provenance="text")
        with self.assertRaises(TypeError):
            ps.image_findings_step(findings_provider(), PNG, provenance=[])
        with self.assertRaises(TypeError):
            ps.research_step(search_client(Transport()), QUERY, provenance="x")


class TestRecordRunConsistency(NoSockets):
    """Item D: only provable mismatches are refused, and the limits of the check are stated."""

    def test_a_matching_record_and_run_assemble_to_the_same_result(self):
        out = full_run()
        again = ps.assemble_report(make_record(), out["run"], origin="simulated", query=QUERY,
                                   provenance=out["provenance"])
        self.assertEqual(again, out)

    def test_a_different_record_is_refused_when_the_run_carries_a_fingerprint(self):
        out = full_run()
        for other in (make_record(sequence_number=4), make_record(rover_id="other-rover"),
                      make_record(soil={"nitrogen": 11.0, "phosphorus": 20.0, "potassium": 30.0, "moisture": 40.0,
                                        "temperature": 20.0})):
            with self.subTest(other=repr(other)[:40]):
                with self.assertRaises(ValueError) as raised:
                    ps.assemble_report(other, out["run"], origin="simulated", provenance=out["provenance"])
                self.assertEqual(str(raised.exception), ps.MISMATCH)
                self.assertNotIn("other-rover", str(raised.exception))

    def test_without_a_fingerprint_two_valid_records_cannot_be_told_apart_and_that_is_not_claimed(self):
        out = full_run()
        accepted = ps.assemble_report(make_record(sequence_number=4), out["run"], origin="simulated")
        self.assertEqual(accepted["run_id"], "run-1")   # accepted: nothing in the run identifies its Record
        self.assertIn("cannot be told apart", Path(ps.__file__).read_text(encoding="utf-8"))

    def test_the_fingerprint_is_a_content_hash_of_the_validated_record_only(self):
        record = make_record()
        out = full_run()
        expected = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(out["provenance"]["record_sha256"], expected)
        self.assertEqual(full_run()["provenance"]["record_sha256"], expected)   # equal content, equal hash
        # the hash is about the Record's content only: run id, record id and the other run arguments do not enter it
        other_run = full_run(run_id="run-other", record_id=3, query="a different question")
        self.assertEqual(other_run["provenance"]["record_sha256"], expected)
        changed = ps.run_report(make_record(time="sim:2"), run_id="run-2")["provenance"]["record_sha256"]
        self.assertNotEqual(changed, expected)

    def test_a_record_that_fails_validation_cannot_belong_to_a_run_that_executed_steps(self):
        out = full_run()
        with self.assertRaises(ValueError) as raised:
            ps.assemble_report(make_record(rover_id=""), out["run"], origin="simulated")
        self.assertEqual(str(raised.exception), ps.MISMATCH)

    def test_a_valid_record_cannot_belong_to_an_invalid_record_run(self):
        refused = ps.run_report(make_record(rover_id=""), origin="simulated", run_id="run-1")
        self.assertEqual(refused["run_status"], "invalid_record")
        with self.assertRaises(ValueError) as raised:
            ps.assemble_report(make_record(), refused["run"], origin="simulated")
        self.assertEqual(str(raised.exception), ps.MISMATCH)
        again = ps.assemble_report(make_record(rover_id=""), refused["run"], origin="simulated")
        self.assertEqual(again["report"]["status"], "invalid_record")
        self.assertEqual(again["run_status"], "invalid_record")

    def test_run_identity_fields_are_checked_and_ids_stay_separate(self):
        local = full_run(record_id=9)
        self.assertEqual((local["record_id"], local["record_id_scope"], local["run_id"]), (9, "local_store", "run-1"))
        none = full_run(record_id=None)
        self.assertEqual((none["record_id"], none["record_id_scope"]), (None, "unavailable"))
        for out in (local, none):
            again = ps.assemble_report(make_record(), out["run"], origin="simulated", query=QUERY,
                                       provenance=out["provenance"])
            self.assertEqual((again["record_id"], again["record_id_scope"]), (out["record_id"], out["record_id_scope"]))

    def test_malformed_or_incomplete_runs_are_rejected_deterministically(self):
        base = full_run()["run"]

        def changed(**changes):
            run = copy.deepcopy(base)
            run.update(changes)
            return run

        def with_steps(edit):
            run = copy.deepcopy(base)
            edit(run["steps"])
            return run

        cases = {
            "run_id empty": changed(run_id=""), "run_id number": changed(run_id=5),
            "run_id too long": changed(run_id="x" * 65), "run_id control": changed(run_id="a\nb"),
            "status unknown": changed(status="weird"), "status none": changed(status=None),
            "record_id zero": changed(record_id=0, record_id_scope="local_store"),
            "record_id bool": changed(record_id=True, record_id_scope="local_store"),
            "record_id text": changed(record_id="7", record_id_scope="local_store"),
            "id without local scope": changed(record_id=5, record_id_scope="unavailable"),
            "scope local without id": changed(record_id=None, record_id_scope="local_store"),
            "scope unknown": changed(record_id_scope="somewhere"),
            "completed with no steps": changed(steps=[]),
            "invalid_record with steps": changed(status="invalid_record"),
            "step not a dict": with_steps(lambda s: s.__setitem__(0, "x")),
            "step without name": with_steps(lambda s: s[0].pop("name")),
            "step name number": with_steps(lambda s: s[0].__setitem__("name", 5)),
            "step status pending": with_steps(lambda s: s[0].__setitem__("status", "pending")),
            "step status missing": with_steps(lambda s: s[0].pop("status")),
            "duplicate step names": with_steps(lambda s: s[1].__setitem__("name", s[0]["name"])),
            "image result missing": with_steps(lambda s: s[0].__setitem__("result", None)),
            "image findings missing": with_steps(lambda s: s[0]["result"].pop("findings")),
            "research items missing": with_steps(lambda s: s[1]["result"].pop("items")),
            "research caveat shape": with_steps(lambda s: s[1]["result"].__setitem__("caveats", [5])),
        }
        for label, run in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    ps.assemble_report(make_record(), run, origin="simulated")
        for key in ("version", "run_id", "record_id", "record_id_scope", "status", "started_at", "completed_at",
                    "errors"):
            with self.subTest(missing=key):
                run = copy.deepcopy(base)
                del run[key]
                with self.assertRaises(ValueError):
                    ps.assemble_report(make_record(), run, origin="simulated")
        for not_a_run in (None, "run", 5, [], {}, {"steps": "x"}, {"steps": None}):
            with self.subTest(not_a_run=repr(not_a_run)):
                with self.assertRaises(TypeError):
                    ps.assemble_report(make_record(), not_a_run)
        run = copy.deepcopy(base)
        del run["steps"]
        with self.assertRaises(TypeError):
            ps.assemble_report(make_record(), run)

    def test_error_messages_never_contain_the_submitted_values(self):
        base = full_run()["run"]
        run = copy.deepcopy(base)
        run["run_id"] = "secret-" + KEY + "\n"
        with self.assertRaises(ValueError) as raised:
            ps.assemble_report(make_record(), run)
        self.assertNotIn(KEY, str(raised.exception))
        self.assertEqual(str(raised.exception), ps.MALFORMED_RUN)

    def test_a_large_result_below_the_step_limit_is_stored_in_full_and_never_truncated(self):
        """F-09 / OR-8: the runner caps one step's serialised result (provisionally 4 MiB; the tests for it are in
        test_orchestration.py). Below that cap a large result is stored in full: nothing is truncated. The
        per-run total (8 MiB, OR-9) is tested in test_orchestration.py too."""
        big = {"text": "x" * 3_000_000}
        run = execute_run(make_record(), [Step("big", "local", lambda context: big)], run_id="run-1")
        self.assertEqual((run["status"], len(run["steps"][0]["result"]["text"])), ("completed", 3_000_000))


def bounded_item(**changes):
    fields = dict(title="T", url="https://agri.example.org/a", snippet="S", published_date=None, score=0.5)
    fields.update(changes)
    return ResearchItem(**fields)


def research_result_of(items=None, query=QUERY):
    items = (bounded_item(),) if items is None else tuple(items)
    return ResearchResult(status="ok", error_code=None, query=query, items=items, dropped=0, retrieved_at=NOW)


class TestInjectedResultBounds(NoSockets):
    """F-09, the part the repository settles: the search adapter's OWN constants (at most MAX_RESULTS_LIMIT items,
    titles up to MAX_TITLE_CHARS, snippets up to MAX_SNIPPET_CHARS, a query up to MAX_QUERY_CHARS) bound what a
    real search can return. An injected client that exceeds them fails the stage explicitly with the existing
    `response_too_large` code. Nothing is truncated and nothing oversized is stored or reported."""

    def assert_too_large(self, result):
        out = full_run(research_client=FixedSearch(result))
        step = by_name(out["run"])["research_search"]
        self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", "response_too_large", None))
        sources = out["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["entries"]),
                         ("error", "response_too_large", []))
        self.assertEqual(out["run_status"], "failed")
        return out

    def test_a_result_over_the_search_adapters_own_bounds_fails_explicitly_and_nothing_oversized_is_kept(self):
        many = [bounded_item(url="https://agri.example.org/%d" % n) for n in range(MAX_RESULTS_LIMIT + 1)]
        cases = (("too many items", research_result_of(many)),
                 ("long title", research_result_of([bounded_item(title="t" * (MAX_TITLE_CHARS + 1))])),
                 ("long snippet", research_result_of([bounded_item(snippet="s" * (MAX_SNIPPET_CHARS + 1))])),
                 ("long query", research_result_of(query="q" * (MAX_QUERY_CHARS + 1))),
                 ("huge snippet", research_result_of([bounded_item(snippet="x" * 3_000_000)])))
        for label, result in cases:
            with self.subTest(label):
                out = self.assert_too_large(result)
                text = json.dumps(out)
                self.assertNotIn("x" * 2000, text)           # no oversized value anywhere in the result
                self.assertNotIn("s" * (MAX_SNIPPET_CHARS + 1), text)
                self.assertNotIn("t" * (MAX_TITLE_CHARS + 1), text)
                self.assertNotIn("q" * (MAX_QUERY_CHARS + 1), text)

    def test_values_exactly_at_the_bounds_are_accepted_and_stored_unchanged(self):
        items = [bounded_item(url="https://agri.example.org/%d" % n, title="t" * MAX_TITLE_CHARS,
                              snippet="s" * MAX_SNIPPET_CHARS) for n in range(MAX_RESULTS_LIMIT)]
        out = full_run(research_client=FixedSearch(research_result_of(items, query="q" * MAX_QUERY_CHARS)))
        stored = by_name(out["run"])["research_search"]["result"]
        self.assertEqual(out["run_status"], "completed")
        self.assertEqual(len(stored["items"]), MAX_RESULTS_LIMIT)
        self.assertEqual({len(i["title"]) for i in stored["items"]}, {MAX_TITLE_CHARS})
        self.assertEqual({len(i["snippet"]) for i in stored["items"]}, {MAX_SNIPPET_CHARS})
        self.assertEqual(len(stored["query"]), MAX_QUERY_CHARS)
        self.assertEqual(len(out["report"]["sections"]["retrieved_sources"]["entries"]), MAX_RESULTS_LIMIT)

    def test_the_real_search_adapter_never_trips_the_bounds(self):
        reply = json.dumps({"results": [{"title": "t" * 5000, "url": "https://agri.example.org/%d" % n,
                                         "content": "c" * 50000, "score": 0.5} for n in range(30)]}).encode()
        client = search_client(Transport(body=reply))
        result = client.search(QUERY)
        self.assertEqual(result.status, "ok")                       # the adapter itself cuts and caps
        out = full_run(research_client=client)
        self.assertEqual(out["run_status"], "completed")

    def test_the_generic_step_limit_closes_the_unbounded_fields_in_the_report_vocabulary(self):
        """OR-8: a Search url or the image stage's provenance label has no field-level bound, but the whole stage
        result is capped by the runner. The runner's `result_too_large` is shown in the report as the existing
        closed `response_too_large` (never as `response_malformed`), and nothing oversized is kept anywhere."""
        huge = 5 * 1024 * 1024
        out = full_run(research_client=FixedSearch(research_result_of(
            [bounded_item(url="https://agri.example.org/" + "u" * huge)])))
        step = by_name(out["run"])["research_search"]
        self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", "result_too_large", None))
        sources = out["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["entries"]),
                         ("error", "response_too_large", []))
        self.assertNotIn("u" * 2000, json.dumps(out))
        findings = FindingsResult(status="ok", error_code=None, reason=None, findings=good_findings(), model="m",
                                  provenance="p" * huge)
        out = full_run(findings_provider=StubProvider(findings))
        step = by_name(out["run"])["image_findings"]
        self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", "result_too_large", None))
        image = out["report"]["sections"]["image_inference"]
        self.assertEqual((image["status"], image["error_code"], image["findings"]),
                         ("error", "response_too_large", None))
        self.assertNotIn("p" * 2000, json.dumps(out))
        self.assertEqual(out["run_status"], "failed")

    def test_the_per_run_total_is_shown_in_the_report_as_response_too_large_and_the_shape_is_unchanged(self):
        """OR-9: when the run total is used up by the first stage, the second stage fails with the runner's
        `result_too_large`, which the report shows as `response_too_large` (not `response_malformed`)."""
        normal = full_run()
        first = by_name(normal["run"])["image_findings"]["result"]
        with mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", len(json.dumps(first))):
            out = full_run()
        steps = by_name(out["run"])
        self.assertEqual((steps["image_findings"]["status"], steps["research_search"]["status"],
                          steps["research_search"]["error_code"], steps["research_search"]["result"]),
                         ("completed", "failed", "result_too_large", None))
        sources = out["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["entries"]),
                         ("error", "response_too_large", []))
        self.assertEqual(set(out), set(normal))                       # same top-level shape as a normal run
        self.assertEqual(set(out["report"]), set(normal["report"]))
        self.assertEqual(out["run_status"], "failed")

    def test_other_non_closed_codes_still_show_as_response_malformed(self):
        out = full_run(findings_provider=findings_provider(FakeAnalyzer(raises=ValueError("x"))))
        self.assertEqual(by_name(out["run"])["image_findings"]["error_code"], "step_failed")
        self.assertEqual(out["report"]["sections"]["image_inference"]["error_code"], "response_malformed")

    def test_fields_without_a_field_level_bound_below_the_step_limit_are_stored_in_full(self):
        """No field-level bound for a Search result's url, published date, kind, caveats or the image stage's
        provenance label exists in the repository; only the whole-result cap of the runner applies (OR-8). A
        value below that cap is stored unchanged. Pins current behaviour."""
        long_url = "https://agri.example.org/" + "a" * 100_000
        out = full_run(research_client=FixedSearch(research_result_of([bounded_item(url=long_url)])))
        self.assertEqual(out["run_status"], "completed")
        self.assertEqual(len(by_name(out["run"])["research_search"]["result"]["items"][0]["url"]), len(long_url))


if __name__ == "__main__":
    unittest.main()
