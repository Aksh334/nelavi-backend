"""Tests for the additive, orchestration-based demo path (IM-2): POST /api/run-orchestrated.

Everything here is offline. A socket tripwire and an urllib tripwire fail any test that tries to reach out,
and the only "providers" are deterministic fakes. The legacy routes (/api/run, /api/advisory) are checked to be
unchanged and not to use orchestration at all.
"""

import hashlib
import io
import json
import os
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import demo_app
from demo_app import fake_research
from demo_app.app import MAX_JSON_BYTES, DemoApp
from demo_app.fake_research import FakeResearchClient
from demo_app.service import ORCHESTRATED_LABEL, RESEARCH_MODES, DemoService
from orchestration import pipeline_steps
from orchestration import run as run_module
from record.validation import validate_record
from research.tavily import ERROR_CODES, MAX_QUERY_CHARS, ResearchItem, ResearchResult
from storage.record_store import RecordStore

HOST = "127.0.0.1:8765"          # only a Host-header string for the in-process app; no port is opened
ROUTE = "/api/run-orchestrated"
QUESTION = "how should soil moisture readings be read"
LEGACY_RUN_KEYS = {"scenario", "description", "steps", "record", "readings", "photo", "validation", "stored_id",
                   "advice", "report"}
FOUR_MIB = 4 * 1024 * 1024
EIGHT_MIB = 8 * 1024 * 1024
CANARY = "canary-not-a-real-key-1234567890"


class NoNetwork(unittest.TestCase):
    """Any socket or urllib use during a test fails it."""

    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


def call(app, method, target, body=b"", headers=None, csrf=True):
    sent = {"host": HOST}
    if method == "POST":
        if csrf:
            sent["x-nelavi-demo"] = "1"
        sent["content-length"] = str(len(body))
    sent.update(headers or {})
    return app.handle_request(method, target, sent, io.BytesIO(body).read)


def post(app, payload, route=ROUTE):
    return call(app, "POST", route, json.dumps(payload).encode("utf-8"), {"content-type": "application/json"})


def body_of(response):
    return json.loads(response.body.decode("utf-8"))


def steps_of(body):
    return {entry["name"]: entry for entry in body["orchestration"]["run"]["steps"]}


def make(testcase, **kwargs):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    service = DemoService(store, **kwargs)
    return DemoApp(service, {HOST}), store, service


def research_result(url="https://agri.example.org/a", items=None, query=QUESTION):
    if items is None:
        items = (ResearchItem(title="T", url=url, snippet="S", published_date=None, score=None),)
    return ResearchResult(status="ok", error_code=None, query=query, items=tuple(items), dropped=0,
                          retrieved_at="2026-01-01T00:00:00+00:00")


class Scripted:
    """A research client returning (or raising) whatever the test says. It never touches a network."""

    def __init__(self, result=None, error=None):
        self.result, self.error, self.queries = result, error, []

    def search(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.result


class OrchestratedSuccessTests(NoNetwork):
    def setUp(self):
        super().setUp()
        self.app, self.store, self.service = make(self)

    def test_the_default_request_skips_both_stages_and_labels_itself_as_fake(self):
        response = post(self.app, {"scenario": "normal_operation"})
        self.assertEqual(response.status, 200)
        body = body_of(response)
        self.assertEqual((body["workflow"], body["fake_providers"], body["label"]),
                         ("orchestrated_fake", True, ORCHESTRATED_LABEL))
        self.assertIn("FAKE", ORCHESTRATED_LABEL)
        steps = steps_of(body)
        self.assertEqual([(s["status"], s["skip_reason"]) for s in steps.values()],
                         [("skipped", "no_provider_configured"), ("skipped", "research_not_requested")])
        self.assertEqual(body["orchestration"]["run_status"], "skipped")
        self.assertEqual(body["research_stage"], {"mode": "skip", "fake": False,
                                                  "note": body["research_stage"]["note"]})

    def test_the_image_stage_is_explicitly_skipped_and_the_reason_names_the_open_contract(self):
        body = body_of(post(self.app, {"scenario": "normal_operation"}))
        image = body["image_stage"]
        self.assertEqual(image["status"], "skipped")
        for item in ("R-12", "W-12", "A-12", "photo_id"):
            self.assertIn(item, image["note"])
        self.assertEqual(body["orchestration"]["report"]["sections"]["image_inference"]["status"], "not_run")
        self.assertIsNone(body["orchestration"]["report"]["sections"]["image_inference"]["findings"])
        # the placeholder photo id is still shown as the rover's field, and nothing was read for it
        self.assertEqual(body["photo"]["photo_id"], "mock-photo-0-0001")

    def test_run_report_is_called_with_no_provider_no_bytes_unknown_origin_and_the_stored_id(self):
        with mock.patch.object(pipeline_steps, "run_report", wraps=pipeline_steps.run_report) as spy:
            body = body_of(post(self.app, {"scenario": "normal_operation", "research": "fake",
                                           "question": QUESTION}))
        kwargs = spy.call_args.kwargs
        self.assertIsNone(kwargs["findings_provider"])
        self.assertIsNone(kwargs["png_bytes"])
        self.assertEqual(kwargs["origin"], "unknown")
        self.assertEqual(kwargs["record_id"], body["stored_id"])
        self.assertIsInstance(kwargs["research_client"], FakeResearchClient)

    def test_the_origin_is_unknown_and_the_response_says_the_data_come_from_the_simulator(self):
        body = body_of(post(self.app, {"scenario": "sensor_fault"}))
        report = body["orchestration"]["report"]
        self.assertEqual(report["origin"], "unknown")
        self.assertEqual(report["origin_note"], "The origin of these observations was not stated.")
        self.assertIn("SIMULATOR", body["data_source"])
        self.assertIn("unknown", body["data_source"])
        self.assertNotIn("hardware measured", json.dumps(body).lower())

    def test_the_record_is_stored_exactly_once_and_its_id_is_the_run_record_id(self):
        with mock.patch.object(self.service, "process_record", wraps=self.service.process_record) as spy:
            body = body_of(post(self.app, {"scenario": "normal_operation", "research": "fake",
                                           "question": QUESTION}))
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(len(self.store.list_records()), 1)
        run = body["orchestration"]
        self.assertEqual((run["record_id"], run["record_id_scope"]), (body["stored_id"], "local_store"))
        self.assertEqual(body["stored_id"], self.store.list_records()[0].id)
        self.assertEqual(run["run"]["record_id"], body["stored_id"])

    def test_the_fingerprint_is_the_content_hash_of_the_stored_record_and_not_an_identity(self):
        body = body_of(post(self.app, {"scenario": "probe_measurement"}))
        valid = validate_record(body["record"]).record
        text = json.dumps(valid, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.assertEqual(body["orchestration"]["provenance"]["record_sha256"],
                         hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertNotEqual(body["orchestration"]["run_id"], body["orchestration"]["provenance"]["record_sha256"])

    def test_fake_research_succeeds_with_visibly_fake_sources_and_provenance(self):
        body = body_of(post(self.app, {"scenario": "normal_operation", "research": "fake", "question": QUESTION}))
        steps = steps_of(body)
        self.assertEqual((steps["image_findings"]["status"], steps["research_search"]["status"]),
                         ("skipped", "completed"))
        self.assertEqual(body["orchestration"]["run_status"], "partial")
        sources = body["orchestration"]["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], len(sources["entries"])), ("ok", 2))
        for entry in sources["entries"]:
            self.assertIn("FAKE", entry["title"])
            self.assertTrue(entry["url"].startswith("https://fake-source.invalid/"))
            self.assertIn("status", entry["applicability"])      # the page reads this field
        self.assertEqual(sources["caveats"][0]["code"], "fake_provider_output")
        note = body["orchestration"]["provenance"]["research_search"]
        self.assertEqual(note["implementation"], "demo_app.fake_research.FakeResearchClient")
        self.assertEqual((note["provider_called"], note["query"]), (True, QUESTION))
        self.assertNotIn("image_findings", body["orchestration"]["provenance"])   # the image stage never ran
        self.assertEqual(body["research_stage"]["fake"], True)
        self.assertEqual(body["orchestration"]["report"]["sections"]["recommendations"]["supported"], [])

    def test_fake_research_is_deterministic(self):
        first = body_of(post(self.app, {"scenario": "probe_measurement", "research": "fake", "question": QUESTION}))
        second = body_of(post(self.app, {"scenario": "probe_measurement", "research": "fake", "question": QUESTION}))
        self.assertEqual(steps_of(first)["research_search"]["result"], steps_of(second)["research_search"]["result"])
        self.assertEqual(len(self.store.list_records()), 2)       # two requests, two records, one each

    def test_the_users_own_statements_reach_the_report_and_are_validated(self):
        body = body_of(post(self.app, {"scenario": "normal_operation", "crop": "Rice", "growth_stage": "tillering"}))
        context = body["orchestration"]["report"]["context"]
        self.assertEqual(context["crop"]["candidates"][0]["name"], "Rice")
        self.assertEqual(context["growth_stage"]["status"], "user_provided")

    def test_fake_research_failure_is_shown_as_a_failed_stage_with_no_sources(self):
        response = post(self.app, {"scenario": "normal_operation", "research": "fake_error", "question": QUESTION})
        self.assertEqual(response.status, 200)                   # the workflow ran; the failure is in the body
        body = body_of(response)
        step = steps_of(body)["research_search"]
        self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", "provider_unavailable", None))
        self.assertEqual(body["orchestration"]["run_status"], "failed")
        sources = body["orchestration"]["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["entries"]),
                         ("error", "provider_unavailable", []))
        codes = [u["code"] for u in body["orchestration"]["report"]["sections"]["uncertain_or_unavailable"]]
        self.assertIn("research_failed", codes)
        self.assertEqual(len(self.store.list_records()), 1)      # the Record is still stored once

    def test_state_announces_the_path_and_its_limits(self):
        state = body_of(call(self.app, "GET", "/api/state"))
        self.assertEqual(state["orchestrated"], {"available": True, "fake_providers": True,
                                                 "research_modes": list(RESEARCH_MODES),
                                                 "query_max_chars": MAX_QUERY_CHARS})
        self.assertEqual(RESEARCH_MODES, ("skip", "fake", "fake_error"))


class InvalidInputTests(NoNetwork):
    def setUp(self):
        super().setUp()
        self.app, self.store, self.service = make(self)

    def refused(self, response, status=400):
        self.assertEqual(response.status, status, response.body)
        self.assertEqual(self.store.list_records(), [])          # nothing was simulated or stored
        self.assertNotIn(b"Traceback", response.body)

    def test_bad_payloads_are_refused_before_anything_runs(self):
        cases = [
            {}, {"scenario": 5}, {"scenario": "nope"}, {"scenario": "normal_operation", "extra": 1},
            {"scenario": "normal_operation", "research": "real"},
            {"scenario": "normal_operation", "research": 3},
            {"scenario": "normal_operation", "research": "fake"},                       # question required
            {"scenario": "normal_operation", "research": "fake", "question": 5},
            {"scenario": "normal_operation", "research": "fake", "question": "   "},
            {"scenario": "normal_operation", "research": "fake", "question": "q" * (MAX_QUERY_CHARS + 1)},
            {"scenario": "normal_operation", "research": "fake", "question": "bad\nline"},
            {"scenario": "normal_operation", "research": "fake_error", "question": None},
            {"scenario": "normal_operation", "crop": 5},
            {"scenario": "normal_operation", "crop": "<b>"},
            {"scenario": "normal_operation", "region": "x" * 65},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:60]):
                self.refused(post(self.app, payload))

    def test_a_question_at_the_limit_is_accepted(self):
        response = post(self.app, {"scenario": "probe_measurement", "research": "fake",
                                   "question": "q" * MAX_QUERY_CHARS})
        self.assertEqual(response.status, 200)

    def test_a_question_given_with_skip_is_ignored_not_sent_anywhere(self):
        body = body_of(post(self.app, {"scenario": "probe_measurement", "question": "anything at all"}))
        self.assertEqual(steps_of(body)["research_search"]["status"], "skipped")
        self.assertNotIn("anything at all", json.dumps(body))

    def test_route_level_checks_match_the_other_json_routes(self):
        good = json.dumps({"scenario": "normal_operation"}).encode()
        self.refused(call(self.app, "POST", ROUTE, good, {"content-type": "text/plain"}), 415)
        self.refused(call(self.app, "POST", ROUTE, good, {"content-type": "application/json"}, csrf=False), 403)
        self.refused(call(self.app, "POST", ROUTE, b"{bad", {"content-type": "application/json"}))
        self.refused(call(self.app, "POST", ROUTE, b"[1]", {"content-type": "application/json"}))
        self.refused(call(self.app, "POST", ROUTE, b"\xff", {"content-type": "application/json"}))
        deep = b"[" * 100000 + b"]" * 100000                      # far over the body limit: refused unread
        self.refused(call(self.app, "POST", ROUTE, deep, {"content-type": "application/json"}), 413)
        big = b" " * (MAX_JSON_BYTES + 1)
        self.refused(call(self.app, "POST", ROUTE, big, {"content-type": "application/json"}), 413)
        self.refused(call(self.app, "POST", ROUTE, good, {"content-type": "application/json",
                                                           "content-length": "abc"}))
        self.refused(call(self.app, "GET", ROUTE), 404)

    def test_a_nested_body_is_a_400_even_if_the_parser_hits_its_depth_limit(self):
        with mock.patch.object(json, "loads", side_effect=RecursionError):
            self.refused(post(self.app, {"scenario": "normal_operation"}))

    def test_the_host_header_check_applies(self):
        response = self.app.handle_request("POST", ROUTE, {"host": "evil.example", "x-nelavi-demo": "1",
                                                           "content-type": "application/json",
                                                           "content-length": "2"}, io.BytesIO(b"{}").read)
        self.refused(response, 403)


class ProviderFailureTests(NoNetwork):
    """Fake clients that misbehave. The report must show a failure, never a success, and leak nothing."""

    def run_with(self, client, **extra):
        app, store, _ = make(self, fake_research_client=client)
        payload = {"scenario": "normal_operation", "research": "fake", "question": QUESTION}
        payload.update(extra)
        response = post(app, payload)
        return response, store

    def check_failed(self, response, step_code, report_code):
        self.assertEqual(response.status, 200)
        body = body_of(response)
        step = steps_of(body)["research_search"]
        self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", step_code, None))
        sources = body["orchestration"]["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["entries"]), ("error", report_code, []))
        self.assertEqual(body["orchestration"]["run_status"], "failed")
        return body

    def test_a_client_that_raises_leaks_no_exception_text(self):
        response, _ = self.run_with(Scripted(error=RuntimeError("secret detail " + CANARY)))
        self.check_failed(response, "step_failed", "response_malformed")
        self.assertNotIn(b"secret detail", response.body)
        self.assertNotIn(CANARY.encode(), response.body)

    def test_a_malformed_result_object_fails_the_stage(self):
        response, _ = self.run_with(Scripted(result=42))
        self.check_failed(response, "response_malformed", "response_malformed")
        response, _ = self.run_with(Scripted(result=ResearchResult("ok", None, QUESTION, 5, 0, None)))
        self.check_failed(response, "response_malformed", "response_malformed")

    def test_an_error_result_keeps_its_closed_code_and_unknown_codes_become_malformed(self):
        error = ResearchResult("error", "rate_limited", QUESTION, (), 0, None)
        self.check_failed(self.run_with(Scripted(result=error))[0], "rate_limited", "rate_limited")
        odd = ResearchResult("error", "totally_made_up", QUESTION, (), 0, None)
        self.check_failed(self.run_with(Scripted(result=odd))[0], "response_malformed", "response_malformed")

    def test_a_result_that_cannot_be_serialised_fails_without_echoing_it(self):
        # a set is not JSON; the published_date slot is passed through unchecked
        weird = ResearchResult("ok", None, QUESTION, (ResearchItem("T", "u", "S", {1, 2}, None),), 0, None)
        response, _ = self.run_with(Scripted(result=weird))
        self.check_failed(response, "result_not_serializable", "response_malformed")

    def test_more_results_than_the_search_adapter_allows_fails_explicitly(self):
        items = [ResearchItem("T%d" % n, "https://agri.example.org/%d" % n, "S", None, None) for n in range(11)]
        response, _ = self.run_with(Scripted(result=research_result(items=items)))
        self.check_failed(response, "response_too_large", "response_too_large")

    def test_a_service_given_something_that_is_not_a_search_client_refuses_at_construction(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(TypeError):
            DemoService(store, fake_research_client=object())

    def test_an_unexpected_error_in_the_orchestration_call_is_a_generic_500(self):
        app, store, _ = make(self)
        with mock.patch.object(pipeline_steps, "run_report", side_effect=RuntimeError("secret detail " + CANARY)):
            response = post(app, {"scenario": "normal_operation"})
        self.assertEqual(response.status, 500)
        self.assertEqual(body_of(response), {"error": "Internal error in the demo application."})
        self.assertNotIn(b"secret detail", response.body)

    def test_a_canary_in_the_environment_never_reaches_a_response(self):
        app, _, _ = make(self)
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": CANARY, "TAVILY_API_KEY": CANARY}):
            responses = [post(app, {"scenario": "normal_operation", "research": mode, "question": QUESTION})
                         for mode in RESEARCH_MODES]
            responses.append(call(app, "GET", "/api/state"))
        for response in responses:
            self.assertNotIn(CANARY.encode(), response.body)


class SizeLimitTests(NoNetwork):
    """The runner's 4 MiB per-step and 8 MiB per-run limits, reached through the new path."""

    def run_with(self, client):
        app, _, _ = make(self, fake_research_client=client)
        return post(app, {"scenario": "probe_measurement", "research": "fake", "question": QUESTION})

    def url_client(self, extra):
        return Scripted(result=research_result(url="https://agri.example.org/" + "u" * extra))

    def baseline_size(self):
        body = body_of(self.run_with(self.url_client(0)))
        return len(json.dumps(steps_of(body)["research_search"]["result"]))

    def test_the_policy_values_are_the_approved_provisional_ones(self):
        self.assertEqual(run_module.MAX_STEP_RESULT_BYTES, FOUR_MIB)
        self.assertEqual(run_module.MAX_RUN_RESULT_BYTES, EIGHT_MIB)

    def test_a_result_of_exactly_four_mib_is_stored_and_one_byte_more_is_refused(self):
        base = self.baseline_size()
        fits = body_of(self.run_with(self.url_client(FOUR_MIB - base)))
        step = steps_of(fits)["research_search"]
        self.assertEqual(step["status"], "completed")
        self.assertEqual(len(json.dumps(step["result"])), FOUR_MIB)
        over = self.run_with(self.url_client(FOUR_MIB - base + 1))
        self.assertEqual(over.status, 200)
        body = body_of(over)
        step = steps_of(body)["research_search"]
        self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", "result_too_large", None))
        sources = body["orchestration"]["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["entries"]),
                         ("error", "response_too_large", []))
        self.assertLess(len(over.body), 20000)                   # nothing oversized is returned
        self.assertNotIn(b"u" * 1000, over.body)

    def test_a_far_larger_result_is_refused_the_same_way(self):
        response = self.run_with(self.url_client(5 * 1024 * 1024))
        body = body_of(response)
        self.assertEqual(steps_of(body)["research_search"]["error_code"], "result_too_large")
        self.assertNotIn(b"u" * 1000, response.body)

    def test_the_run_total_applies_through_the_new_path_exactly_at_the_limit(self):
        base = self.baseline_size()
        with mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", base):
            fits = body_of(self.run_with(self.url_client(0)))
        self.assertEqual(steps_of(fits)["research_search"]["status"], "completed")
        with mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", base - 1):
            over = body_of(self.run_with(self.url_client(0)))
        step = steps_of(over)["research_search"]
        self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", "result_too_large", None))
        self.assertEqual(over["orchestration"]["report"]["sections"]["retrieved_sources"]["error_code"],
                         "response_too_large")

    def test_the_step_limit_can_also_be_checked_cheaply_with_a_patched_value(self):
        base = self.baseline_size()
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", base):
            self.assertEqual(steps_of(body_of(self.run_with(self.url_client(0))))["research_search"]["status"],
                             "completed")
            self.assertEqual(steps_of(body_of(self.run_with(self.url_client(1))))["research_search"]["error_code"],
                             "result_too_large")


class LegacyUnchangedTests(NoNetwork):
    """The old routes keep their behaviour and never use orchestration."""

    def test_run_keeps_its_exact_response_shape_and_simulated_origin_and_does_not_use_orchestration(self):
        app, store, _ = make(self)
        with mock.patch.object(pipeline_steps, "run_report", side_effect=AssertionError("orchestration used")):
            response = post(app, {"scenario": "normal_operation", "crop": "Rice"}, route="/api/run")
        self.assertEqual(response.status, 200)
        body = body_of(response)
        self.assertEqual(set(body), LEGACY_RUN_KEYS)
        self.assertEqual(body["report"]["origin"], "simulated")
        self.assertEqual(body["report"]["sections"]["image_inference"]["status"], "not_run")
        self.assertNotIn("fake_providers", body)
        self.assertEqual(len(store.list_records()), 1)

    def test_advisory_keeps_its_behaviour_and_does_not_use_orchestration(self):
        client = Scripted(result=research_result())
        client.max_query_chars = 200
        app, _, _ = make(self, research_client=client, research_domains=["agri.example.org"])
        with mock.patch.object(pipeline_steps, "run_report", side_effect=AssertionError("orchestration used")):
            response = post(app, {"question": QUESTION}, route="/api/advisory")
        self.assertEqual(response.status, 200)
        body = body_of(response)
        self.assertEqual((body["status"], body["kind"], len(body["sources"])), ("ok", "research_evidence_only", 1))
        self.assertEqual(client.queries, [QUESTION])
        self.assertNotIn("fake_providers", body)

    def test_advisory_is_still_off_when_no_real_client_is_configured_even_though_the_fake_path_exists(self):
        app, _, _ = make(self)
        response = post(app, {"question": QUESTION}, route="/api/advisory")
        self.assertEqual(response.status, 400)
        self.assertEqual(body_of(response), {"error": "Research is not configured on this server."})

    def test_the_new_route_does_not_change_the_state_of_the_old_sections(self):
        app, _, _ = make(self)
        state = body_of(call(app, "GET", "/api/state"))
        self.assertEqual(state["research"], {"configured": False, "domains": [], "query_max_chars": None})
        self.assertEqual(state["research_pro"]["configured"], False)
        self.assertTrue(state["second_opinion"]["simulated"])


class FakeResearchClientTests(unittest.TestCase):
    def test_it_is_deterministic_and_clearly_fake(self):
        client = FakeResearchClient()
        first, second = client.search(QUESTION), client.search(QUESTION)
        self.assertEqual(first, second)
        self.assertEqual((first.status, first.error_code, first.query, first.dropped), ("ok", None, QUESTION, 0))
        self.assertEqual(len(first.items), 2)
        for item in first.items:
            self.assertIn("FAKE", item.title)
            self.assertTrue(item.url.endswith(".invalid/" + item.url.rsplit("/", 1)[1]) or ".invalid/" in item.url)
            self.assertIsNone(item.score)
        self.assertIn("fake_provider_output", [code for code, _ in first.caveats])
        self.assertEqual(first.kind, "research_evidence_only")

    def test_an_error_client_returns_a_closed_code_and_no_items(self):
        result = FakeResearchClient(error_code="provider_unavailable").search(QUESTION)
        self.assertEqual((result.status, result.error_code, result.items), ("error", "provider_unavailable", ()))
        self.assertIn(result.error_code, ERROR_CODES)
        with self.assertRaises(ValueError):
            FakeResearchClient(error_code="not_a_code")

    def test_a_bad_query_is_a_closed_error_not_an_exception(self):
        for query in (None, 5, "", "  ", "x" * (MAX_QUERY_CHARS + 1), "a\nb"):
            with self.subTest(query=repr(query)[:20]):
                result = FakeResearchClient().search(query)
                self.assertEqual((result.status, result.error_code, result.items), ("error", "query_invalid", ()))

    def test_the_query_limit_matches_the_real_search_adapter(self):
        self.assertEqual(fake_research.QUERY_MAX_CHARS, MAX_QUERY_CHARS)

    def test_it_makes_no_network_call(self):
        with mock.patch("socket.socket", side_effect=AssertionError("socket used")):
            FakeResearchClient().search(QUESTION)


class PageTests(NoNetwork):
    def setUp(self):
        super().setUp()
        app, _, _ = make(self)
        self.html = call(app, "GET", "/").body.decode("utf-8")
        self.script = call(app, "GET", "/static/app.js").body.decode("utf-8")

    def test_the_new_section_exists_after_research_and_starts_disabled_and_labelled_fake(self):
        self.assertLess(self.html.index('<section id="research">'), self.html.index('<section id="orchestrated">'))
        self.assertIn("<h2>7. Orchestrated report (fake providers)</h2>", self.html)
        for element in ("om-research", "om-question", "om-run", "om-status", "om-result"):
            self.assertIn('id="%s"' % element, self.html)
        self.assertRegex(self.html, r'<button id="om-run" type="button" disabled>')
        section = self.html[self.html.index('<section id="orchestrated">'):]
        self.assertIn("FAKE", section)

    def test_the_script_uses_the_shared_helper_and_the_demo_header_and_stays_text_only(self):
        self.assertIn('api("/api/run-orchestrated"', self.script)
        self.assertEqual(self.script.count("fetch("), 1)
        body = self.script[self.script.index("async function runOrchestrated"):]
        body = body[:body.index("\n}\n")]
        for expected in ("SEND", "omBusy", "JSON.stringify", "finally"):
            self.assertIn(expected, body)
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write"):
            self.assertNotIn(banned, self.script)

    def test_the_limits_and_modes_come_from_the_server_state(self):
        self.assertIn("state.orchestrated", self.script)
        self.assertIn("query_max_chars", self.script)


class IsolationTests(unittest.TestCase):
    PACKAGE = Path(demo_app.__file__).resolve().parent

    def test_only_the_service_imports_orchestration_and_only_its_adapter(self):
        import ast
        found = set()
        for path in self.PACKAGE.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    found |= {(path.name, a.name, None) for a in node.names if a.name.split(".")[0] == "orchestration"}
                elif isinstance(node, ast.ImportFrom) and node.module.split(".")[0] == "orchestration":
                    found |= {(path.name, node.module, a.name) for a in node.names}
        # IW-3: the integrated path adds exactly these three modules to the report adapter; nothing else in the demo
        # package may import orchestration.
        self.assertEqual(found, {("service.py", "orchestration", name)
                                 for name in ("integrated", "pipeline_steps", "synthesis", "task_runner")})

    def test_the_fake_client_module_imports_no_provider_and_reads_no_environment(self):
        import ast
        tree = ast.parse((self.PACKAGE / "fake_research.py").read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module)
        self.assertEqual(modules, ["dataclasses"])               # the standard library's dataclasses, nothing else
        source = (self.PACKAGE / "fake_research.py").read_text(encoding="utf-8").lower()
        for banned in ("environ", "getenv", "socket", "urlopen", "subprocess", "open("):
            self.assertFalse(banned in source, banned)


if __name__ == "__main__":
    unittest.main()
