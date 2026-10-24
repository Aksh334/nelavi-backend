"""Deterministic, mocked end-to-end tests for the integrated workflow (orchestration/integrated.py).

Every provider here is a local fake: no network, no credential, no remote service. The five approved roles map to
these boundaries (the vendor is the intended one; nothing here contacts it):
  image analysis    -> PromptedFindingsProvider around a fake analyzer (the NVIDIA role)
  research          -> a fake search client (the Tavily role)
  synthesis         -> FakeSynthesisProvider (the OpenRouter role)
  compute summary   -> FakeTaskRunner, or NodusTaskRunner around a mocked handle (the Nodus role)
  specialist worker -> not exercised: recorded as skipped (the Agent37 role has no defined contract)
A mocked result is never evidence about a live provider.
"""

import ast
import copy
import itertools
import json
import socket
import unittest
from pathlib import Path
from unittest import mock

from agri_pipeline.findings import FindingsResult, PromptedFindingsProvider
from demo_app.fake_research import FakeResearchClient
from image_analysis.boundary import error_result, ok_result
from orchestration import integrated, pipeline_steps, synthesis
from orchestration import run as run_module
from orchestration import task_runner as task_runner_module
from orchestration.integrated import check_integrated_result, run_integrated
from orchestration.nodus_adapter import NodusTaskRunner
from orchestration.synthesis import (
    ERROR_CODES as SYNTHESIS_CODES,
    FakeSynthesisProvider,
    SynthesisResult,
    allowed_refs,
    check_synthesis,
    inputs_from_results,
)
from orchestration.task_runner import FakeTaskRunner, PAYLOAD_KEYS, expected_summary

CANARY = "canary-not-a-real-key-1234567890"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image"
QUERY = "how should soil moisture readings be read"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT = object()   # "use the standard valid record" (None must stay available as an invalid record)


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


class FakeAnalyzer:
    """A local stand-in for an ImageAnalyzer. It records every request and never touches a network."""

    def __init__(self, text=None, error_code=None, model="fake-vision-model", raises=None):
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


class CountingResearch(FakeResearchClient):
    """The demo's deterministic fake search client, counting its calls."""

    def __init__(self, error_code=None, result=None, raises=None):
        super().__init__(error_code=error_code)
        self.calls, self._result, self._raises = [], result, raises

    def search(self, query):
        self.calls.append(query)
        if self._raises is not None:
            raise self._raises
        return self._result if self._result is not None else super().search(query)


class MockHandle:
    """A mocked Nodus function handle: records spawn, answers with the expected summary, contacts nothing."""

    def __init__(self):
        self.spawned = []

    def spawn(self, payload):
        self.spawned.append(copy.deepcopy(payload))
        handle = self

        class Call:
            def get(self, timeout=None):
                return expected_summary(handle.spawned[-1])

        return Call()


def clock():
    counter = itertools.count()
    return lambda: "t%02d" % next(counter)


class Rig:
    """One set of fakes with their call counters, and a way to run the workflow."""

    def __init__(self, analyzer=None, research=None, runner=None, provider=None):
        self.analyzer = analyzer if analyzer is not None else FakeAnalyzer()
        self.findings = PromptedFindingsProvider(self.analyzer)
        self.research = research if research is not None else CountingResearch()
        self.runner = runner if runner is not None else FakeTaskRunner()
        self.provider = provider if provider is not None else FakeSynthesisProvider()

    def calls(self):
        return {"image": len(self.analyzer.requests), "research": len(self.research.calls),
                "summary": len(getattr(self.runner, "calls", []) or getattr(self.runner, "spawned", [])),
                "synthesis": len(self.provider.calls)}

    def run(self, record=DEFAULT, **overrides):
        arguments = dict(findings_provider=self.findings, png_bytes=PNG, research_client=self.research, query=QUERY,
                         task_runner=self.runner, synthesis_provider=self.provider, run_id="run-1", clock=clock())
        arguments.update(overrides)
        return run_integrated(make_record() if record is DEFAULT else record, **arguments)


def steps_of(result):
    return {step["name"]: step for step in result["run"]["steps"]}


def stages_of(result):
    return {stage["stage"]: stage for stage in result["integration"]["stages"]}


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


class HappyPathTests(NoNetwork):
    def test_every_role_is_represented_and_the_run_completes(self):
        rig = Rig()
        result = rig.run()
        self.assertEqual(result["run_status"], "completed")
        self.assertEqual([s["name"] for s in result["run"]["steps"]],
                         ["image_findings", "research_search", "prepare_payload", "summarize_record", "synthesis"])
        self.assertTrue(all(s["status"] == "completed" for s in result["run"]["steps"]))
        stages = stages_of(result)
        self.assertEqual(set(stages), {"image_findings", "research", "sensor_summary", "synthesis"})
        self.assertTrue(all(s["status"] == "completed" and s["provider_called"] is True for s in stages.values()))
        self.assertEqual(rig.calls(), {"image": 1, "research": 1, "summary": 1, "synthesis": 1})
        self.assertEqual(check_integrated_result(result), [])

    def test_the_report_is_the_existing_pipeline_report_and_synthesis_does_not_change_it(self):
        rig = Rig()
        result = rig.run()
        plain = pipeline_steps.run_report(make_record(), findings_provider=Rig().findings, png_bytes=PNG,
                                          research_client=CountingResearch(), query=QUERY, run_id="run-1",
                                          clock=clock())
        self.assertEqual(result["report"], plain["report"])
        sections = result["report"]["sections"]
        self.assertEqual(sections["image_inference"]["status"], "ok")
        self.assertEqual(sections["retrieved_sources"]["status"], "ok")
        self.assertEqual(result["report"]["origin"], "unknown")
        self.assertEqual(sections["recommendations"]["supported"], [])

    def test_fake_image_findings_are_validated_and_kept(self):
        result = Rig().run()
        stored = steps_of(result)["image_findings"]["result"]
        self.assertEqual(stored["findings"], good_findings())
        self.assertEqual(stored["model"], "fake-vision-model")

    def test_fake_evidence_keeps_its_provenance_and_stays_visibly_fake(self):
        result = Rig().run()
        entries = result["report"]["sections"]["retrieved_sources"]["entries"]
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertIn("FAKE", entry["title"])
            self.assertTrue(entry["url"].startswith("https://fake-source.invalid/"))
            self.assertEqual((entry["provenance"], entry["review_status"]), ("retrieved_source", "unreviewed"))
        codes = [c["code"] for c in result["report"]["sections"]["retrieved_sources"]["caveats"]]
        self.assertIn("fake_provider_output", codes)

    def test_synthesis_receives_only_validated_findings_evidence_and_the_question(self):
        rig = Rig()
        rig.run()
        inputs = rig.provider.calls[0]
        self.assertEqual(set(inputs), {"findings", "evidence", "question"})
        self.assertEqual(inputs["question"], QUERY)
        self.assertEqual(inputs["findings"], good_findings())
        self.assertEqual([e["id"] for e in inputs["evidence"]], ["E1", "E2"])
        self.assertEqual(set(inputs["evidence"][0]), {"id", "title", "url", "snippet", "published_date"})
        text = json.dumps(inputs)
        for forbidden in ("test-rover", "photo-opaque-1", "leaf-a", "sensor_status", "nitrogen", "voltage_v",
                          "versions", "synthetic bytes", "score", "sim:1"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_synthesis_is_structured_cited_labelled_and_never_treated_as_evidence(self):
        result = Rig().run()
        syn = result["synthesis"]
        self.assertEqual(syn["kind"], "model_generated_unverified")
        self.assertIs(syn["fake"], True)
        self.assertIn("fake", syn["model"].lower())
        allowed = {"F:crop", "F:growth_stage", "F:symptom:0", "E1", "E2"}
        self.assertTrue(syn["statements"])
        for statement in syn["statements"]:
            self.assertTrue(statement["cites"] and set(statement["cites"]) <= allowed)
        self.assertIn("not verified evidence", " ".join(syn["uncertainties"]))
        self.assertEqual(syn, steps_of(result)["synthesis"]["result"])
        self.assertEqual(result["report"]["sections"]["recommendations"]["supported"], [])

    def test_the_summary_step_sends_only_the_four_sections_and_matches_the_expected_counts(self):
        rig = Rig()
        result = rig.run()
        task, payload = rig.runner.calls[0]
        self.assertEqual((task, tuple(payload)), ("summarize_record", PAYLOAD_KEYS))
        text = json.dumps(payload)
        for forbidden in ("test-rover", "photo-opaque-1", "leaf-a", "sim:1", "versions"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(result["sensor_summary"]["summary"], expected_summary(payload))
        self.assertIs(result["sensor_summary"]["fake"], True)

    def test_the_real_nodus_adapter_around_a_mocked_handle_also_completes_without_any_remote_call(self):
        handle = MockHandle()
        rig = Rig(runner=NodusTaskRunner(lambda: handle))
        result = rig.run()
        self.assertEqual(result["run_status"], "completed")
        self.assertEqual(len(handle.spawned), 1)
        self.assertEqual(tuple(handle.spawned[0]), PAYLOAD_KEYS)
        self.assertIs(result["sensor_summary"]["fake"], False)             # the adapter, not the fake runner
        self.assertIn("injected function handle", result["sensor_summary"]["runner"])
        self.assertEqual(check_integrated_result(result), [])

    def test_the_specialist_role_is_skipped_with_a_recorded_reason_and_does_not_change_the_status(self):
        result = Rig().run()
        specialist = result["integration"]["specialist"]
        self.assertEqual(specialist, {"stage": "specialist", "status": "skipped",
                                      "skip_reason": "specialist_role_undefined", "provider_called": False})
        self.assertNotIn("specialist", steps_of(result))
        self.assertEqual(result["run_status"], "completed")

    def test_provenance_records_invocation_and_never_claims_a_remote_request(self):
        result = Rig().run()
        provenance = result["provenance"]
        self.assertEqual(len(provenance["record_sha256"]), 64)
        for stage in ("image_findings", "research_search", "sensor_summary", "synthesis"):
            self.assertIs(provenance[stage]["provider_called"], True, stage)
            self.assertTrue(provenance[stage]["implementation"], stage)
        self.assertEqual(provenance["image_findings"]["model"], "fake-vision-model")
        for key in provenance:
            self.assertFalse("remote" in key or "request" in key, key)

    def test_identical_inputs_give_identical_results(self):
        first, second = Rig().run(), Rig().run()
        self.assertEqual(first, second)
        third = Rig().run(run_id="run-1", clock=clock())
        self.assertEqual(first, third)

    def test_the_record_is_never_changed_by_the_workflow(self):
        record = make_record()
        before = copy.deepcopy(record)
        Rig().run(record)
        self.assertEqual(record, before)

    def test_the_run_total_and_step_limits_are_the_approved_ones(self):
        self.assertEqual(run_module.MAX_STEP_RESULT_BYTES, 4 * 1024 * 1024)
        self.assertEqual(run_module.MAX_RUN_RESULT_BYTES, 8 * 1024 * 1024)


class InvalidInputTests(NoNetwork):
    def assert_no_provider_called(self, rig):
        self.assertEqual(rig.calls(), {"image": 0, "research": 0, "summary": 0, "synthesis": 0})

    def test_an_invalid_record_invokes_no_provider(self):
        rig = Rig()
        for record in (None, [], {}, make_record(soil={}), make_record(extra=1)):
            result = rig.run(record)
            self.assertEqual(result["run_status"], "invalid_record")
            self.assertEqual(result["run"]["steps"], [])
            self.assertEqual(result["report"]["status"], "invalid_record")
            self.assertIsNone(result["synthesis"])
            self.assertIsNone(result["sensor_summary"])
            self.assertTrue(all(s["status"] == "not_run" for s in result["integration"]["stages"]))
            self.assertEqual(check_integrated_result(result), [])
        self.assert_no_provider_called(rig)

    def test_refused_report_inputs_start_nothing(self):
        rig = Rig()
        for overrides in ({"origin": "somewhere"}, {"user": {"crop": "<b>"}}, {"image_ref": "bad\nref"}):
            result = rig.run(**overrides)
            self.assertEqual((result["run_status"], result["run"], result["run_id"]), ("not_started", None, None))
            self.assertEqual(result["report"]["status"], "invalid_input")
            self.assertTrue(all(s["status"] == "not_run" for s in result["integration"]["stages"]))
        self.assert_no_provider_called(rig)

    def test_bad_run_arguments_and_wrong_provider_objects_raise_before_any_provider_is_called(self):
        rig = Rig()
        with self.assertRaises(ValueError):
            rig.run(run_id="bad id!")
        with self.assertRaises(TypeError):
            rig.run(synthesis_provider=object())
        with self.assertRaises(TypeError):
            rig.run(task_runner=object())
        with self.assertRaises(TypeError):
            rig.run(findings_provider=object())
        self.assert_no_provider_called(rig)

    def test_png_bytes_that_are_not_a_png_never_reach_the_image_provider(self):
        rig = Rig()
        result = rig.run(png_bytes=b"not a png")
        steps = steps_of(result)
        self.assertEqual((steps["image_findings"]["status"], steps["image_findings"]["error_code"]),
                         ("failed", "image_unsupported"))
        self.assertEqual(rig.calls()["image"], 0)
        self.assertEqual(steps["research_search"]["status"], "completed")     # independent steps continue
        self.assertEqual(steps["summarize_record"]["status"], "completed")
        self.assertEqual((steps["synthesis"]["status"], steps["synthesis"]["skip_reason"]),
                         ("skipped", "dependency_not_completed"))
        self.assertEqual(result["run_status"], "failed")
        self.assertEqual(rig.calls()["synthesis"], 0)
        self.assertEqual(check_integrated_result(result), [])


class FailureInjectionTests(NoNetwork):
    def check(self, result, **expected):
        steps = steps_of(result)
        for name, (status, code) in expected.items():
            self.assertEqual((steps[name]["status"], steps[name].get("error_code") or steps[name].get("skip_reason")),
                             (status, code), name)
        self.assertEqual(check_integrated_result(result), [])
        self.assertNotIn(CANARY, json.dumps(result))
        return steps

    def test_an_image_provider_failure_skips_synthesis_and_lets_the_others_continue(self):
        rig = Rig(analyzer=FakeAnalyzer(error_code="provider_unavailable"))
        result = rig.run()
        self.check(result, image_findings=("failed", "provider_unavailable"),
                   research_search=("completed", None), summarize_record=("completed", None),
                   synthesis=("skipped", "dependency_not_completed"))
        self.assertEqual(result["run_status"], "failed")
        self.assertEqual(result["report"]["sections"]["image_inference"]["status"], "error")
        self.assertIsNone(result["synthesis"])
        self.assertEqual(rig.calls()["synthesis"], 0)

    def test_malformed_image_findings_fail_the_stage(self):
        rig = Rig(analyzer=FakeAnalyzer(text='{"not": "findings"}'))
        result = rig.run()
        self.check(result, image_findings=("failed", "response_malformed"), synthesis=("skipped", "dependency_not_completed"))

    def test_a_research_failure_skips_synthesis_and_is_not_shown_as_sources(self):
        rig = Rig(research=CountingResearch(error_code="provider_unavailable"))
        result = rig.run()
        self.check(result, research_search=("failed", "provider_unavailable"), image_findings=("completed", None),
                   synthesis=("skipped", "dependency_not_completed"))
        sources = result["report"]["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["entries"]), ("error", []))

    def test_missing_evidence_is_explicit_and_nothing_is_invented(self):
        rig = Rig(research=None)
        rig.research = None
        result = rig.run(research_client=None)
        steps = self.check(result, research_search=("skipped", "no_provider_configured"),
                           synthesis=("skipped", "dependency_not_completed"))
        self.assertEqual(result["run_status"], "partial")
        self.assertEqual(result["report"]["sections"]["retrieved_sources"]["status"], "not_run")
        self.assertIsNone(result["synthesis"])
        self.assertEqual(len(rig.provider.calls), 0)
        self.assertEqual(steps["image_findings"]["status"], "completed")

    def test_missing_image_findings_skip_synthesis_too(self):
        result = Rig().run(findings_provider=None)
        self.check(result, image_findings=("skipped", "no_provider_configured"),
                   synthesis=("skipped", "dependency_not_completed"))
        self.assertEqual(result["run_status"], "partial")

    def test_a_malformed_synthesis_response_fails_that_step_only(self):
        for mode in ("malformed", "empty", "unknown_cite", "not_json"):
            with self.subTest(mode=mode):
                rig = Rig(provider=FakeSynthesisProvider(mode))
                result = rig.run()
                steps = self.check(result, synthesis=("failed", "synthesis_response_malformed"),
                                   image_findings=("completed", None), research_search=("completed", None),
                                   summarize_record=("completed", None))
                self.assertIsNone(steps["synthesis"]["result"])
                self.assertIsNone(result["synthesis"])
                self.assertEqual(result["run_status"], "failed")
                self.assertEqual(result["report"]["sections"]["image_inference"]["status"], "ok")   # source data intact
                self.assertEqual(rig.calls()["synthesis"], 1)

    def test_synthesis_error_results_keep_their_closed_codes(self):
        for mode, code in (("fail", "synthesis_failed"), ("unavailable", "synthesis_unavailable"),
                           ("rejected", "synthesis_rejected")):
            with self.subTest(mode=mode):
                result = Rig(provider=FakeSynthesisProvider(mode)).run()
                self.check(result, synthesis=("failed", code))
                self.assertIn(code, SYNTHESIS_CODES)

    def test_a_synthesis_provider_that_raises_leaks_nothing(self):
        class Raising:
            def synthesize(self, inputs):
                raise RuntimeError("secret detail " + CANARY + " " + json.dumps(inputs)[:50])

        result = Rig(provider=Raising()).run()
        self.check(result, synthesis=("failed", "step_failed"))
        self.assertNotIn("secret detail", json.dumps(result))

    def test_a_result_that_is_not_a_synthesis_result_is_malformed(self):
        class Odd:
            def synthesize(self, inputs):
                return {"status": "ok"}

        self.check(Rig(provider=Odd()).run(), synthesis=("failed", "synthesis_response_malformed"))

    def test_an_unknown_error_code_from_a_synthesis_provider_becomes_the_generic_failure(self):
        class Odd:
            def synthesize(self, inputs):
                return SynthesisResult("error", "made_up " + CANARY, None, "m", False)

        self.check(Rig(provider=Odd()).run(), synthesis=("failed", "synthesis_failed"))

    def test_a_summary_failure_does_not_stop_synthesis_and_is_not_shown_as_a_success(self):
        for mode, code in (("fail", "task_failed"), ("malformed", "task_response_malformed"),
                           ("timeout", "task_timeout"), ("limit", "task_limit_reached")):
            with self.subTest(mode=mode):
                rig = Rig(runner=FakeTaskRunner(mode))
                result = rig.run()
                self.check(result, summarize_record=("failed", code), synthesis=("completed", None),
                           image_findings=("completed", None), research_search=("completed", None))
                self.assertIsNone(result["sensor_summary"])
                self.assertEqual(result["run_status"], "failed")
                self.assertEqual(stages_of(result)["sensor_summary"]["status"], "failed")

    def test_a_failed_payload_step_skips_the_summary_and_the_runner_is_never_called(self):
        rig = Rig()
        with mock.patch.object(task_runner_module, "minimal_payload", side_effect=RuntimeError("path " + CANARY)):
            result = rig.run()
        self.check(result, prepare_payload=("failed", "step_failed"),
                   summarize_record=("skipped", "dependency_not_completed"), synthesis=("completed", None))
        self.assertEqual(rig.calls()["summary"], 0)

    def test_a_nodus_adapter_error_is_mapped_to_a_closed_code_without_text(self):
        class Boom:
            def spawn(self, payload):
                raise RuntimeError("service said " + CANARY)

        result = Rig(runner=NodusTaskRunner(lambda: Boom())).run()
        self.check(result, summarize_record=("failed", "task_failed"))

    def test_a_result_that_cannot_be_serialised_fails_its_step_and_reaches_nothing_downstream(self):
        weird = type("R", (), {})()
        bad = FakeResearchClient().search(QUERY)
        bad_items = (type(bad.items[0])("T", "u", "S", {1, 2}, None),)
        weird = type(bad)("ok", None, QUERY, bad_items, 0, bad.retrieved_at)
        rig = Rig(research=CountingResearch(result=weird))
        result = rig.run()
        self.check(result, research_search=("failed", "result_not_serializable"),
                   synthesis=("skipped", "dependency_not_completed"))
        self.assertEqual(result["report"]["sections"]["retrieved_sources"]["error_code"], "response_malformed")

    def test_a_result_over_the_step_limit_fails_explicitly_and_is_not_stored(self):
        big = FakeResearchClient().search(QUERY)
        item = big.items[0]
        huge = type(item)(item.title, "https://fake-source.invalid/" + "u" * (5 * 1024 * 1024), item.snippet, None, None)
        result = Rig(research=CountingResearch(result=type(big)("ok", None, QUERY, (huge,), 0, big.retrieved_at))).run()
        self.check(result, research_search=("failed", "result_too_large"), synthesis=("skipped", "dependency_not_completed"))
        self.assertEqual(result["report"]["sections"]["retrieved_sources"]["error_code"], "response_too_large")
        self.assertLess(len(json.dumps(result)), 200000)

    def test_the_per_run_total_fails_a_later_step_and_its_dependants_are_skipped(self):
        first = len(json.dumps(steps_of(Rig().run())["image_findings"]["result"]))
        with mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", first):
            result = Rig().run()
        steps = self.check(result, image_findings=("completed", None), research_search=("failed", "result_too_large"),
                           synthesis=("skipped", "dependency_not_completed"))
        self.assertEqual(result["run_status"], "failed")
        self.assertEqual(steps["prepare_payload"]["error_code"], "result_too_large")
        self.assertEqual(steps["summarize_record"]["skip_reason"], "dependency_not_completed")

    def test_secret_like_text_in_every_provider_error_path_never_reaches_the_result(self):
        rig = Rig(analyzer=FakeAnalyzer(raises=RuntimeError(CANARY)), research=CountingResearch(raises=ValueError(CANARY)),
                  runner=FakeTaskRunner("raise"), provider=FakeSynthesisProvider("raise"))
        result = rig.run()
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertNotIn("exploded", json.dumps(result))
        self.assertEqual(result["run_status"], "failed")
        self.assertEqual(check_integrated_result(result), [])

    def test_states_are_distinct_completed_partial_failed_skipped_and_invalid(self):
        statuses = {
            "completed": Rig().run()["run_status"],
            "partial": Rig().run(research_client=None)["run_status"],
            "failed": Rig(provider=FakeSynthesisProvider("fail")).run()["run_status"],
            "skipped": run_integrated(make_record(), run_id="run-1")["run_status"],
            "invalid": Rig().run({})["run_status"],
        }
        self.assertEqual(statuses, {"completed": "completed", "partial": "partial", "failed": "failed",
                                    "skipped": "skipped", "invalid": "invalid_record"})

    def test_no_provider_is_called_more_than_once_and_skipped_providers_are_never_called(self):
        rig = Rig()
        rig.run(research_client=None, task_runner=None)
        self.assertEqual(rig.calls(), {"image": 1, "research": 0, "summary": 0, "synthesis": 0})


class ResultCheckTests(NoNetwork):
    def test_the_check_notices_a_failure_dressed_as_a_success_and_other_inconsistencies(self):
        good = Rig(provider=FakeSynthesisProvider("fail")).run()
        self.assertEqual(check_integrated_result(good), [])
        tampered = copy.deepcopy(good)
        stages_of(tampered)["synthesis"]["status"] = "completed"
        self.assertIn("stage_status_mismatch", check_integrated_result(tampered))
        tampered = copy.deepcopy(good)
        steps_of(tampered)["synthesis"]["result"] = {"statements": []}
        self.assertIn("non_completed_step_has_result", check_integrated_result(tampered))
        tampered = copy.deepcopy(good)
        tampered["synthesis"] = {"kind": "x", "statements": [], "uncertainties": []}
        self.assertIn("synthesis_without_completed_step", check_integrated_result(tampered))
        tampered = copy.deepcopy(good)
        tampered["integration"]["specialist"]["status"] = "completed"
        self.assertIn("specialist_not_skipped", check_integrated_result(tampered))
        tampered = copy.deepcopy(good)
        tampered["run_status"] = "completed"
        self.assertIn("run_status_mismatch", check_integrated_result(tampered))
        del tampered["integration"]
        self.assertEqual(check_integrated_result(tampered), ["missing_keys"])
        self.assertEqual(check_integrated_result(None), ["missing_keys"])

    def test_the_check_notices_an_unknown_citation_and_a_report_that_shows_success_for_an_unfinished_step(self):
        result = Rig().run()
        tampered = copy.deepcopy(result)
        tampered["synthesis"]["statements"][0]["cites"] = ["E99"]
        self.assertIn("synthesis_unknown_citation", check_integrated_result(tampered))
        failed = Rig(analyzer=FakeAnalyzer(error_code="provider_unavailable")).run()
        tampered = copy.deepcopy(failed)
        tampered["report"]["sections"]["image_inference"]["status"] = "ok"
        self.assertIn("report_shows_success_for_unfinished_step", check_integrated_result(tampered))


class SynthesisBoundaryTests(unittest.TestCase):
    def inputs(self):
        return {"findings": good_findings(),
                "evidence": [{"id": "E1", "title": "T", "url": "https://x.invalid/a", "snippet": "S",
                              "published_date": None}], "question": QUERY}

    def good(self):
        return {"statements": [{"text": "A statement.", "cites": ["F:crop", "E1"]}], "uncertainties": ["Unverified."]}

    def test_allowed_references_come_from_the_validated_inputs_only(self):
        self.assertEqual(allowed_refs(self.inputs()), {"F:crop", "F:growth_stage", "F:symptom:0", "E1"})
        empty = {"findings": {"crop": {"candidates": []}, "growth_stage": {"candidate": None, "evidence": [],
                                                                            "uncertainty": "unspecified"},
                              "symptoms": [], "needs_more": []}, "evidence": [], "question": None}
        self.assertEqual(allowed_refs(empty), set())

    def test_a_well_formed_cited_answer_is_accepted_as_a_plain_copy(self):
        checked = check_synthesis(self.good(), self.inputs())
        self.assertEqual(checked, self.good())
        self.assertIsNot(checked, self.good())

    def test_every_malformed_shape_is_refused(self):
        good = self.good()

        def with_(**changes):
            return dict(copy.deepcopy(good), **changes)

        statement = good["statements"][0]
        bad = [None, [], "text", 5, {}, dict(good, extra=1), {"statements": good["statements"]},
               with_(statements=[]), with_(statements="x"), with_(statements=[statement] * 9),
               with_(statements=[{"text": "A."}]), with_(statements=[{"text": "A.", "cites": [], "x": 1}]),
               with_(statements=[{"text": "", "cites": ["E1"]}]), with_(statements=[{"text": "a" * 401, "cites": ["E1"]}]),
               with_(statements=[{"text": "bad\nline", "cites": ["E1"]}]),
               with_(statements=[{"text": "see http://x.invalid", "cites": ["E1"]}]),
               with_(statements=[{"text": "see www.x.invalid", "cites": ["E1"]}]),
               with_(statements=[{"text": 5, "cites": ["E1"]}]),
               with_(statements=[{"text": "A.", "cites": []}]), with_(statements=[{"text": "A.", "cites": "E1"}]),
               with_(statements=[{"text": "A.", "cites": ["E99"]}]), with_(statements=[{"text": "A.", "cites": ["E1", "E1"]}]),
               with_(statements=[{"text": "A.", "cites": [1]}]), with_(statements=[{"text": "A.", "cites": {"E1"}}]),
               with_(statements=[{"text": "A.", "cites": ["E1"] * 7}]),
               with_(uncertainties="x"), with_(uncertainties=[1]), with_(uncertainties=["u"] * 7),
               with_(uncertainties=["x" * 301]), with_(uncertainties=[True])]
        for output in bad:
            with self.subTest(output=str(output)[:50]):
                self.assertIsNone(check_synthesis(output, self.inputs()))

    def test_a_hostile_object_cannot_make_the_checker_raise(self):
        class Hostile(dict):
            def __iter__(self):
                raise RuntimeError("secret " + CANARY)

        self.assertIsNone(check_synthesis(Hostile(self.good()), self.inputs()))

    def test_the_fake_provider_is_deterministic_and_validates_against_its_own_inputs(self):
        provider = FakeSynthesisProvider()
        first, second = provider.synthesize(self.inputs()), provider.synthesize(self.inputs())
        self.assertEqual(first, second)
        self.assertEqual((first.status, first.fake), ("ok", True))
        self.assertIsNotNone(check_synthesis(first.output, self.inputs()))
        self.assertEqual(len(provider.calls), 2)

    def test_the_fake_provider_refuses_to_invent_a_statement_when_there_is_nothing_to_cite(self):
        nothing = {"findings": {"crop": {"candidates": []}, "growth_stage": {"candidate": None, "evidence": [],
                                                                              "uncertainty": "unspecified"},
                                "symptoms": [], "needs_more": []}, "evidence": [], "question": None}
        result = FakeSynthesisProvider().synthesize(nothing)
        self.assertEqual((result.status, result.error_code, result.output), ("error", "synthesis_rejected", None))

    def test_the_fake_provider_rejects_an_unknown_mode(self):
        with self.assertRaises(ValueError):
            FakeSynthesisProvider("explode")

    def test_inputs_are_built_from_stored_step_results_and_leave_out_scores(self):
        findings_result = {"status": "ok", "model": "m", "provenance": "image_analysis", "findings": good_findings()}
        research_result = {"items": [{"title": "T", "url": "https://x.invalid/a", "snippet": "S",
                                      "published_date": None, "score": 0.9}]}
        inputs = inputs_from_results(findings_result, research_result, QUERY)
        self.assertEqual(inputs["evidence"], [{"id": "E1", "title": "T", "url": "https://x.invalid/a", "snippet": "S",
                                               "published_date": None}])
        self.assertEqual(inputs_from_results(findings_result, research_result, 5)["question"], None)


class IsolationTests(unittest.TestCase):
    NEW = ("synthesis.py", "integrated.py")

    def test_the_new_modules_import_only_the_standard_library_and_project_modules(self):
        allowed_projects = {"orchestration", "agri_pipeline", "record"}
        stdlib = {"copy", "dataclasses", "json", "hashlib", "re", "unicodedata"}
        for name in self.NEW:
            tree = ast.parse((ROOT / "orchestration" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                           else [node.module] if isinstance(node, ast.ImportFrom) else [])
                for module in modules:
                    self.assertIn(module.split(".")[0], stdlib | allowed_projects, (name, module))
                    self.assertNotIn(module.split(".")[0], ("nodus", "socket", "urllib", "requests", "os", "subprocess"))

    def test_the_new_modules_open_no_connection_read_no_environment_and_make_no_file_access(self):
        banned = {"environ", "getenv", "open", "system", "popen", "connect", "urlopen"}
        for name in self.NEW:
            tree = ast.parse((ROOT / "orchestration" / name).read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                    {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            self.assertEqual(names & banned, set(), name)

    def test_nothing_on_the_rover_side_or_in_the_providers_depends_on_the_new_modules(self):
        # IW-3: the demo's service now imports these modules on purpose; that is pinned by the demo isolation tests
        # (tests/test_demo_orchestrated.py, tests/test_demo_app.py). Everything below must still not depend on them.
        for package in ("mock_rover", "record", "storage", "agri_pipeline", "research", "image_analysis"):
            directory = ROOT / package
            if not directory.is_dir():
                continue
            for path in directory.glob("*.py"):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("orchestration.integrated", text, str(path))
                self.assertNotIn("orchestration.synthesis", text, str(path))

    def test_the_existing_report_function_is_unchanged_by_the_new_modules(self):
        source = (ROOT / "orchestration" / "pipeline_steps.py").read_text(encoding="utf-8")
        self.assertNotIn("synthesis", source)
        self.assertNotIn("integrated", source)


if __name__ == "__main__":
    unittest.main()
