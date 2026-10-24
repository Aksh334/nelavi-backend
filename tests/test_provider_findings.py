"""The NVIDIA image adapter through the findings contract, with fake transports.

A valid reply becomes findings; every kind of failure becomes a closed error and NEVER findings, a crop,
a symptom or a diagnosis. No request is made to any provider. These tests show the wiring and the failure
handling; they do not show that the live service returns findings in this shape. (The Nebius adapter that
these tests once also covered was retired; see CHANGELOG.md. The provider loops are kept so another
provider could be added to PROVIDERS later.)
"""

import json
import unittest
from unittest import mock

from agri_pipeline import findings as fd
from agri_pipeline import pipeline
from image_analysis.nvidia import NvidiaImageAnalyzer, ResponseTooLarge, TransportError, TransportTimeout
from scripts.nvidia_findings_smoke_test import diagnose_reply
from scripts.nvidia_smoke_test import synthetic_png

KEY = "canary-provider-key-1234567890"
MODEL = "example-org/example-vision-model"
MARKER = "ZZMODELTEXT"
PNG = synthetic_png()


def valid_findings():
    return {
        "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
        "growth_stage": {"candidate": None, "evidence": [], "uncertainty": "unspecified"},
        "symptoms": [{"observation": "brown spots on older leaves", "possible_interpretations": ["fungal leaf spot"]}],
        "needs_more": ["a close-up of a leaf underside"],
    }


def chat(text):
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


class Transport:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.error = status, error
        self.body = chat(json.dumps(valid_findings())) if body is None else body
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), json.loads(body), timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body


def nvidia(transport, environ=None):
    return NvidiaImageAnalyzer(transport=transport, environ={"NVIDIA_API_KEY": KEY} if environ is None else environ)


PROVIDERS = {"nvidia": nvidia}
ENDPOINTS = {"nvidia": "https://integrate.api.nvidia.com/v1/chat/completions"}
NO_KEY = {"nvidia": {}}


class NoSockets(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def analyze(self, name, transport, environ=None, hints=None):
        return fd.PromptedFindingsProvider(PROVIDERS[name](transport, environ)).analyze_image(PNG, hints)

    def record(self):
        from mock_rover.config import RoverConfig
        from mock_rover.scenarios import get_scenario, run_scenario
        from mock_rover.simulator import MockRover
        results = run_scenario(MockRover(RoverConfig()), get_scenario("probe_measurement"))
        return next(v for step, v in results if step == "build_record")


class TestValidReplies(NoSockets):
    def test_a_valid_reply_becomes_findings_through_each_provider_with_one_request(self):
        for name in PROVIDERS:
            with self.subTest(provider=name):
                transport = Transport()
                result = self.analyze(name, transport)
                self.assertEqual((result.status, result.error_code, result.findings), ("ok", None, valid_findings()))
                self.assertEqual(len(transport.calls), 1)
                url, headers, body, timeout = transport.calls[0]
                self.assertEqual((url, timeout), (ENDPOINTS[name], 60.0))
                self.assertEqual(headers["Authorization"], "Bearer " + KEY)
                content = body["messages"][0]["content"]
                self.assertEqual(content[0], {"type": "text", "text": fd.PROMPT})
                self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_a_single_code_fence_and_empty_findings_are_accepted(self):
        empty = fd.EMPTY_ANSWER
        for name in PROVIDERS:
            for text in ("```json\n" + json.dumps(valid_findings()) + "\n```", json.dumps(empty)):
                result = self.analyze(name, Transport(body=chat(text)))
                self.assertEqual(result.status, "ok", (name, text[:20]))

    def test_hints_are_appended_after_the_prompt_for_both_providers(self):
        for name in PROVIDERS:
            transport = Transport()
            self.analyze(name, transport, hints={"region": "India"})
            sent = transport.calls[0][2]["messages"][0]["content"][0]["text"]
            self.assertTrue(sent.startswith(fd.PROMPT))
            self.assertIn('region: "India"', sent)


class TestRepliesThatBreakTheContract(NoSockets):
    BAD = {
        "prose": "Probably a plant with spots.",
        "reasoning first": "Let me think. " + json.dumps(valid_findings()),
        "trailing text": json.dumps(valid_findings()) + " Hope that helps.",
        "not an object": "[]",
        "malformed json": "{\"crop\": ",
        "extra key": json.dumps(dict(valid_findings(), confidence=0.9)),
        "confirmed diagnosis field": json.dumps(dict(valid_findings(), diagnosis="late blight")),
        "missing key": json.dumps({k: v for k, v in valid_findings().items() if k != "needs_more"}),
        "wrong uncertainty": json.dumps(dict(valid_findings(), crop={"candidates": [
            {"name": "crop-a", "evidence": [], "uncertainty": "95%"}]})),
        "punctuation in a label": json.dumps(dict(valid_findings(), symptoms=[
            {"observation": "spots", "possible_interpretations": ["leaf spot (fungal)"]}])),
        "null where not allowed": json.dumps(dict(valid_findings(), needs_more=None)),
        "evidence without candidate": json.dumps(dict(valid_findings(), growth_stage={
            "candidate": None, "evidence": ["seen"], "uncertainty": "low"})),
        "too many candidates": json.dumps(dict(valid_findings(), crop={"candidates": [
            {"name": "crop-a", "evidence": [], "uncertainty": "low"}] * 6})),
    }

    def test_each_is_a_closed_malformed_error_with_no_findings_and_a_diagnosis(self):
        for name in PROVIDERS:
            for label, text in self.BAD.items():
                with self.subTest(provider=name, reply=label):
                    transport = Transport(body=chat(text))
                    result = self.analyze(name, transport)
                    self.assertEqual((result.status, result.error_code, result.findings),
                                     ("error", "response_malformed", None))
                    self.assertEqual(len(transport.calls), 1)  # never a second attempt to get a better reply
                    self.assertTrue(diagnose_reply(text), label)  # the diagnostics can say why

    def test_a_blank_reply_is_a_provider_level_empty_error(self):
        for name in PROVIDERS:
            result = self.analyze(name, Transport(body=chat("   ")))
            self.assertEqual((result.status, result.error_code, result.findings), ("error", "response_empty", None))

    def test_the_replys_text_never_appears_in_the_failed_result(self):
        for name in PROVIDERS:
            result = self.analyze(name, Transport(body=chat(MARKER + " " + KEY)))
            self.assertEqual(result.status, "error")
            self.assertNotIn(MARKER, repr(result))
            self.assertNotIn(KEY, repr(result))


class TestProviderFailures(NoSockets):
    def test_each_failure_is_a_closed_error_with_no_findings_and_exactly_one_request(self):
        cases = ((400, None, "provider_rejected"), (401, None, "provider_rejected"),
                 (403, None, "provider_rejected"), (404, None, "provider_rejected"),
                 (429, None, "provider_rejected"), (500, None, "provider_unavailable"),
                 (502, None, "provider_unavailable"), (503, None, "provider_unavailable"),
                 (302, None, "response_malformed"))
        errors = ((TransportTimeout(), "provider_timeout"), (TransportError(), "provider_connection_failed"),
                  (ResponseTooLarge(), "response_too_large"), (OSError(KEY), "provider_connection_failed"))
        bodies = ((b"not json", "response_malformed"),
                  (json.dumps({"choices": [{"message": {"content": 5}}]}).encode(), "response_malformed"),
                  (json.dumps({"choices": [{"message": {"reasoning_content": "x"}}]}).encode(), "response_malformed"))
        for name in PROVIDERS:
            transports = ([(Transport(status=s), c) for s, _, c in cases] + [(Transport(error=e), c) for e, c in errors]
                          + [(Transport(body=b), c) for b, c in bodies])
            for transport, code in transports:
                with self.subTest(provider=name, code=code):
                    result = self.analyze(name, transport)
                    self.assertEqual((result.status, result.error_code, result.findings), ("error", code, None))
                    self.assertEqual(len(transport.calls), 1)
                    self.assertNotIn(KEY, repr(result))

    def test_missing_configuration_is_refused_before_any_request(self):
        for name in PROVIDERS:
            transport = Transport()
            result = self.analyze(name, transport, environ=NO_KEY[name])
            self.assertEqual((result.status, result.error_code, result.findings), ("error", "credential_missing", None))
            self.assertEqual(transport.calls, [])

    def test_a_key_set_under_another_variable_name_is_not_accepted(self):
        transport = Transport()
        self.assertEqual(self.analyze("nvidia", transport, environ={"OTHER_PROVIDER_API_KEY": KEY}).error_code,
                         "credential_missing")
        self.assertEqual(transport.calls, [])

    def test_an_unusable_image_is_refused_locally_for_both_providers(self):
        for name in PROVIDERS:
            transport = Transport()
            provider = fd.PromptedFindingsProvider(PROVIDERS[name](transport))
            for bad in (b"not a png", b"", b"\x89PNG", b"GIF89a" + PNG[6:]):
                result = provider.analyze_image(bad)
                self.assertEqual((result.status, result.findings), ("error", None))
            self.assertEqual(transport.calls, [])

    def test_documented_limit_only_the_png_signature_is_checked_not_the_whole_file(self):
        # By design (decision IA-2) the adapters check the media type and the 8-byte PNG signature and never
        # decode the image, so a truncated file that still starts with the signature IS sent to the provider.
        # The findings smoke-test script is stricter: it also checks the chunk structure before sending.
        from scripts.nvidia_findings_smoke_test import inspect_png
        truncated = PNG[:20]
        self.assertIsNone(inspect_png(truncated))
        for name in PROVIDERS:
            transport = Transport()
            fd.PromptedFindingsProvider(PROVIDERS[name](transport)).analyze_image(truncated)
            self.assertEqual(len(transport.calls), 1)


class TestFailuresNeverBecomeFindings(NoSockets):
    def test_a_failed_request_leaves_the_report_without_crop_symptoms_or_recommendations(self):
        for name in PROVIDERS:
            for transport in (Transport(status=503), Transport(error=TransportTimeout()),
                              Transport(body=chat("Probably a plant.")), Transport(body=b"{")):
                result = self.analyze(name, transport)
                report = pipeline.run(self.record(), origin="simulated", findings_result=result)
                image = report["sections"]["image_inference"]
                self.assertEqual((image["status"], image["findings"]), ("error", None))
                self.assertEqual(report["context"]["crop"]["status"], "unknown")
                self.assertEqual((report["context"]["symptoms"], report["context"]["growth_stage"]["status"]),
                                 ([], "unknown"))
                self.assertEqual(report["sections"]["recommendations"]["supported"], [])
                text = json.dumps(report["sections"]).lower()
                for claim in ("confirmed disease", "diagnosed", "is infected", "blight", "rust"):
                    self.assertNotIn(claim, text)

    def test_a_valid_reply_becomes_candidates_and_possibilities_only_never_a_confirmed_result(self):
        for name in PROVIDERS:
            result = self.analyze(name, Transport())
            report = pipeline.run(self.record(), origin="simulated", findings_result=result)
            context = report["context"]
            self.assertEqual(context["crop"]["status"], "candidate")
            self.assertEqual(context["crop"]["candidates"][0]["provenance"], "image_analysis")
            symptom = context["symptoms"][0]
            self.assertEqual([i["status"] for i in symptom["possible_interpretations"]], ["possible"])
            self.assertIsNone(symptom["confirmation"])
            self.assertEqual(report["sections"]["recommendations"]["supported"], [])
            self.assertIn("not verified facts", report["sections"]["image_inference"]["note"])

    def test_a_model_guess_never_selects_a_crop_specific_rule(self):
        ruleset = {"version": "synthetic", "rules": [{
            "id": "R", "message": "SYNTHETIC", "priority": 1, "source": "SYNTHETIC", "crop": ["crop-a"],
            "when": [{"reading": "air.temperature", "op": "gt", "value": -100}]}]}
        for name in PROVIDERS:
            result = self.analyze(name, Transport())  # the model's candidate is "crop-a"
            report = pipeline.run(self.record(), origin="simulated", findings_result=result, ruleset=ruleset)
            self.assertEqual(report["sections"]["recommendations"]["supported"], [])
            self.assertEqual(report["advice"]["not_assessed"][0]["code"], "crop_not_established")


if __name__ == "__main__":
    unittest.main()
