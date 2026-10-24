import ast
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from image_analysis.nvidia import NvidiaImageAnalyzer, TransportTimeout
from scripts import nvidia_smoke_test as smoke

CANARY_KEY = "canary-not-a-real-key-1234567890"
SCRIPT = Path(smoke.__file__).resolve()


def chat_body(text):
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode("utf-8")


class Transport:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.error = status, error
        self.body = chat_body("Keep the leaves dry. Confirm with your agriculture officer.") if body is None else body
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body


class NoSocketCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_script(self, argv, transport=None, key_present=True):
        out, err = io.StringIO(), io.StringIO()
        analyzer = None
        if transport is not None:
            analyzer = NvidiaImageAnalyzer(transport=transport, environ={"NVIDIA_API_KEY": CANARY_KEY})
        code = smoke.run(argv, stdout=out, stderr=err, analyzer=analyzer,
                         key_present=lambda: key_present)
        return code, out.getvalue(), err.getvalue()


class TestSmokeScript(NoSocketCase):
    def test_a_dry_run_sends_nothing_and_does_not_print_the_prompt_by_default(self):
        transport = Transport()
        code, out, err = self.run_script([], transport)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(transport.calls, [])
        self.assertIn("DRY RUN", out)
        self.assertIn("synthetic", out)
        self.assertNotIn("crop:", out)
        self.assertNotIn("RULES", out)

    def test_show_prompt_prints_only_the_synthetic_prompt(self):
        code, out, err = self.run_script(["--show-prompt"], Transport())
        self.assertEqual((code, err), (0, ""))
        self.assertIn("crop:", out)
        self.assertNotIn("base64", out.lower())
        self.assertNotIn("Bearer", out)

    def test_a_dry_run_needs_no_key_reads_no_environment_and_never_builds_the_adapter(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(smoke, "NvidiaImageAnalyzer",
                                  side_effect=AssertionError("adapter built")) as adapter, \
                mock.patch.object(smoke, "api_key_present",
                                  side_effect=AssertionError("key checked")):
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(smoke.run([], stdout=out, stderr=err), 0)
            self.assertEqual(smoke.run(["--show-prompt"], stdout=out, stderr=err), 0)
        adapter.assert_not_called()
        self.assertEqual(err.getvalue(), "")

    def test_a_second_provider_call_is_refused(self):
        transport = Transport()
        recorder = smoke.RecordingAnalyzer(NvidiaImageAnalyzer(
            transport=transport, environ={"NVIDIA_API_KEY": CANARY_KEY}))
        from image_analysis.boundary import AnalysisRequest, ImageInput
        request = AnalysisRequest(ImageInput(smoke.synthetic_png(), "image/png"), "prompt")
        recorder.analyze(request)
        with self.assertRaises(smoke.OneRequestOnly):
            recorder.analyze(request)
        self.assertEqual(len(transport.calls), 1)

    def test_no_model_text_headers_or_payload_is_ever_printed(self):
        marker = "ZZMARKER"
        answers = {
            "short": marker + " keep the leaves dry",
            "long": (marker + " ") * 150,
        }
        for name, text in answers.items():
            with self.subTest(answer=name):
                transport = Transport(body=chat_body(text))
                code, out, err = self.run_script(["--send"], transport)
                combined = out + err
                for forbidden in (marker, CANARY_KEY, "Bearer", "Authorization", "base64",
                                  "data:image", "image_url", "chat/completions\"", "RULES"):
                    self.assertNotIn(forbidden, combined, forbidden)

    def test_the_stage_two_rule_matches_the_production_rule_exactly(self):
        from second_opinion.second_opinion import TOKEN_LIMIT
        self.assertEqual(TOKEN_LIMIT, 80)
        # (text, accepted by production). Whitespace-separated tokens, counted with split().
        boundary_cases = (
            (" ".join(["t"] * (TOKEN_LIMIT - 1)), True),
            (" ".join(["t"] * TOKEN_LIMIT), False),
            (" ".join(["t"] * (TOKEN_LIMIT + 1)), False),
            ("\n".join(["t"] * (TOKEN_LIMIT - 1)), True),   # newlines are separators too
            ("t " * (TOKEN_LIMIT - 1) + "   ", True),        # extra spaces add no tokens
        )
        for text, accepted in boundary_cases:
            with self.subTest(tokens=len(text.split()), accepted=accepted):
                code, out, _ = self.run_script(["--send"], Transport(body=chat_body(text)))
                self.assertEqual(code, 0 if accepted else 3)
                self.assertIn("%d whitespace-separated tokens" % len(text.split()), out)
                self.assertIn("fewer than %d whitespace-separated tokens" % TOKEN_LIMIT, out)
                self.assertNotIn("words", out.lower().replace("whitespace-separated", ""))

    def test_send_without_a_key_stops_before_any_request(self):
        transport = Transport()
        code, out, err = self.run_script(["--send"], transport, key_present=False)
        self.assertEqual(code, 2)
        self.assertEqual(transport.calls, [])
        self.assertIn("NVIDIA_API_KEY", err)

    def test_both_stages_pass_with_a_short_answer_and_one_request_is_made(self):
        transport = Transport()
        code, out, _ = self.run_script(["--send"], transport)
        self.assertEqual(code, 0)
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("STAGE 1", out)
        self.assertIn("PASS: NVIDIA answered", out)
        self.assertIn("PASS: the answer was accepted", out)
        self.assertIn("both stages passed", out)
        self.assertNotIn(CANARY_KEY, out)
        self.assertNotIn("Keep the leaves dry", out)

    def test_a_connection_that_works_but_a_long_answer_fails_only_stage_two(self):
        transport = Transport(body=chat_body("word " * 120))
        code, out, _ = self.run_script(["--send"], transport)
        self.assertEqual(code, 3)
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("STAGE 1", out)
        self.assertIn("PASS: NVIDIA answered", out)
        self.assertIn("FAIL: the app would NOT show this answer (code: response_malformed)", out)
        self.assertIn("the connection works but the answer fails the app's rule", out)
        self.assertIn("120 tokens against a limit of fewer than 80", out)
        self.assertNotIn("word word", out)
        self.assertLess(len(out), 1500)

    def test_a_provider_failure_is_stage_one_and_does_not_retry(self):
        for transport, expected in ((Transport(status=503, body=b"busy"), "provider_unavailable"),
                                    (Transport(status=401, body=b"no"), "provider_rejected"),
                                    (Transport(error=TransportTimeout()), "provider_timeout"),
                                    (Transport(body=b"not json"), "response_malformed"),
                                    (Transport(body=chat_body("  ")), "response_empty")):
            with self.subTest(expected=expected):
                code, out, _ = self.run_script(["--send"], transport)
                self.assertEqual(code, 1)
                self.assertEqual(len(transport.calls), 1)
                self.assertIn("FAIL: " + expected, out)
                self.assertIn("stage 2 not applicable", out)
                self.assertNotIn(CANARY_KEY, out)

    def test_the_request_has_the_defined_shape_and_uses_synthetic_data_only(self):
        transport = Transport()
        self.run_script(["--send"], transport)
        url, headers, body, timeout = transport.calls[0]
        self.assertEqual(timeout, 60.0)
        content = json.loads(body)["messages"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["text", "image_url"])
        self.assertIn("Tomato", content[0]["text"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_the_synthetic_image_is_a_valid_small_png(self):
        from image_analysis.boundary import ImageInput, is_supported_png
        data = smoke.synthetic_png()
        self.assertTrue(is_supported_png(ImageInput(data, "image/png")))
        self.assertLess(len(data), 20000)

    def test_an_unknown_option_is_rejected(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                smoke.run(["--bogus"], stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(raised.exception.code, 2)


class TestSmokeScriptSource(unittest.TestCase):
    SOURCE = SCRIPT.read_text(encoding="utf-8")

    def test_it_never_reads_the_environment_or_the_key_directly(self):
        for banned in ("environ", "getenv", "open(", "subprocess", "urlopen", "socket", "write_text",
                       "write_bytes"):
            self.assertNotIn(banned, self.SOURCE, banned)

    def test_it_adds_no_retry_loop_fallback_or_simulated_provider(self):
        lowered = self.SOURCE.lower()
        for banned in ("while ", "retry", "simulatedsecondopinionprovider", "simulated_provider"):
            self.assertNotIn(banned, lowered, banned)

    def test_imports_are_standard_library_or_existing_project_modules(self):
        allowed = {"argparse", "base64", "struct", "sys", "time", "zlib",
                   "image_analysis", "second_opinion"}
        for node in ast.walk(ast.parse(self.SOURCE)):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module]
            else:
                continue
            for name in names:
                self.assertIn(name.split(".")[0], allowed, name)


if __name__ == "__main__":
    unittest.main()
