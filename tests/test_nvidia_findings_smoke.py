"""Offline tests for the NVIDIA findings smoke-test script. Every request goes to a fake transport.

No request was made to NVIDIA. These tests show the script's safety properties and its handling of
fake replies; they do not show that the real service accepts the request or that a real model's reply
fits the findings contract.
"""

import ast
import io
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from agri_pipeline.findings import PROMPT
from image_analysis import nvidia
from image_analysis.nvidia import NvidiaConfig, TransportError, TransportTimeout
from scripts import nvidia_findings_smoke_test as smoke
from scripts.nvidia_smoke_test import OneRequestOnly, synthetic_png

KEY = "canary-not-a-real-key-1234567890"
MARKER = "ZZMODELTEXT"
SCRIPT = Path(smoke.__file__).resolve()


def chunk(kind, payload):
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png_with(*extra_chunks):
    """A valid PNG with extra chunks inserted after IHDR."""
    data = synthetic_png()
    header_end = 8 + 12 + 13
    return data[:header_end] + b"".join(extra_chunks) + data[header_end:]


def good_findings(**changes):
    findings = {
        "crop": {"candidates": [{"name": MARKER + "crop", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
        "growth_stage": {"candidate": None, "evidence": [], "uncertainty": "unspecified"},
        "symptoms": [{"observation": MARKER + " spots", "possible_interpretations": ["possible-cause-a"]}],
        "needs_more": [MARKER + " another photo"],
    }
    findings.update(changes)
    return findings


def reply(text):
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode("utf-8")


class Transport:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.error = status, error
        self.body = reply(json.dumps(good_findings())) if body is None else body
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body


class Case(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def image(self, data=None, name="leaf.png"):
        path = self.directory / name
        path.write_bytes(synthetic_png() if data is None else data)
        return str(path)

    def run_script(self, argv, transport=None, key_present=True):
        out, err = io.StringIO(), io.StringIO()
        code = smoke.run(argv, stdout=out, stderr=err, transport=transport,
                         key_present=lambda: key_present, environ={"NVIDIA_API_KEY": KEY})
        return code, out.getvalue(), err.getvalue()

    def send(self, transport, *extra, image=None, **kwargs):
        argv = ["--image", image or self.image(), "--send", "--image-is-non-sensitive"] + list(extra)
        return self.run_script(argv, transport, **kwargs)


class TestDryRunIsTheDefault(Case):
    def test_no_arguments_is_a_dry_run_that_needs_nothing_and_sends_nothing(self):
        transport = Transport()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(smoke, "NvidiaImageAnalyzer", side_effect=AssertionError("adapter built")), \
                mock.patch.object(smoke, "api_key_present", side_effect=AssertionError("key checked")):
            code = smoke.run([], stdout=out, stderr=err, transport=transport)
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(transport.calls, [])
        self.assertIn("DRY RUN: nothing was sent and no key was read", out.getvalue())
        self.assertIn("Image: none given", out.getvalue())
        self.assertIn("A live run needs --send, --image PATH and --image-is-non-sensitive", out.getvalue())

    def test_a_dry_run_with_an_image_checks_the_file_locally_and_reports_safe_facts(self):
        transport = Transport()
        path = self.image()
        code, out, err = self.run_script(["--image", path], transport, key_present=False)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(transport.calls, [])
        self.assertIn("Image: leaf.png, %d bytes, 64x64, sha256" % len(synthetic_png()), out)
        self.assertIn("metadata chunks: none", out)
        self.assertNotIn(str(self.directory), out)  # only the file name is shown, not the folder

    def test_every_option_combination_without_send_makes_no_request(self):
        transport = Transport()
        for extra in ([], ["--thinking-off"], ["--show-request"], ["--image-is-non-sensitive"],
                      ["--allow-metadata", "--thinking-off", "--show-request", "--image-is-non-sensitive"]):
            with self.subTest(extra=extra):
                code, out, _ = self.run_script(["--image", self.image()] + extra, transport)
                self.assertEqual(code, 0)
                self.assertIn("DRY RUN", out)
        self.assertEqual(transport.calls, [])

    def test_the_request_outline_shows_the_body_with_a_placeholder_instead_of_the_image(self):
        _, plain, _ = self.run_script(["--show-request"])
        outline = json.loads(plain[plain.index("{"):plain.rindex("}") + 1])
        self.assertEqual(outline["model"], "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
        self.assertEqual(outline["max_tokens"], 1024)
        self.assertEqual(set(outline), {"model", "max_tokens", "messages"})
        content = outline["messages"][0]["content"]
        self.assertEqual([p["type"] for p in content], ["text", "image_url"])
        self.assertEqual(content[0]["text"], PROMPT)
        self.assertEqual(content[1]["image_url"]["url"], smoke.IMAGE_PLACEHOLDER)
        self.assertNotIn("base64", plain)
        _, off, _ = self.run_script(["--show-request", "--thinking-off"])
        outline_off = json.loads(off[off.index("{"):off.rindex("}") + 1])
        self.assertEqual(outline_off["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual({k: v for k, v in outline_off.items() if k != "chat_template_kwargs"}, outline)


class TestLiveRunRefusals(Case):
    def refused(self, argv, key_present=True, expect=None):
        transport = Transport()
        code, out, err = self.run_script(argv, transport, key_present=key_present)
        self.assertEqual(code, 2, (argv, err))
        self.assertEqual(transport.calls, [])
        self.assertIn("Nothing was sent", err)
        self.assertNotIn("DRY RUN", out)
        if expect:
            self.assertIn(expect, err)

    def test_send_alone_is_refused(self):
        self.refused(["--send"], expect="--send needs --image PATH")

    def test_send_without_the_non_sensitive_confirmation_is_refused(self):
        self.refused(["--image", self.image(), "--send"], expect="--image-is-non-sensitive")

    def test_send_without_a_key_is_refused_before_any_request(self):
        self.refused(["--image", self.image(), "--send", "--image-is-non-sensitive"], key_present=False,
                     expect="NVIDIA_API_KEY")

    def test_bad_images_are_refused_with_a_fixed_reason(self):
        big = png_with(chunk(b"tEXt", b"k\x00" + b"x" * nvidia.DEFAULT_MAX_IMAGE_BYTES))
        cases = {
            "image_not_found": str(self.directory / "missing.png"),
            "image_empty": self.image(b"", "empty.png"),
            "image_too_large": self.image(big, "big.png"),
            "image_not_a_valid_png": self.image(b"GIF89a not a png at all", "other.png"),
        }
        truncated = synthetic_png()[:40]
        cases["image_not_a_valid_png "] = self.image(truncated, "cut.png")
        for reason, path in cases.items():
            for flags in ([], ["--send", "--image-is-non-sensitive"]):
                with self.subTest(reason=reason.strip(), send=bool(flags)):
                    self.refused(["--image", path] + flags, expect=reason.strip())

    def test_a_directory_or_odd_path_is_refused_not_crashed_on(self):
        self.refused(["--image", str(self.directory)], expect="image_")
        self.refused(["--image", ""], expect="image_")

    def test_metadata_is_refused_unless_explicitly_allowed(self):
        for kind in (b"eXIf", b"tEXt", b"iTXt", b"zTXt"):
            path = self.image(png_with(chunk(kind, b"k\x00v")), "meta.png")
            self.refused(["--image", path, "--send", "--image-is-non-sensitive"], expect=kind.decode())
        path = self.image(png_with(chunk(b"tEXt", b"Software\x00editor")), "meta.png")
        transport = Transport()
        code, out, _ = self.run_script(["--image", path, "--send", "--image-is-non-sensitive", "--allow-metadata"],
                                       transport)
        self.assertEqual(code, 0)
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("metadata chunks: tEXt", out)

    def test_nothing_is_ever_sent_when_a_refusal_applies_even_with_a_working_transport(self):
        transport = Transport()
        for argv in (["--send"], ["--image", self.image(), "--send"], ["--image", "nope.png", "--send"]):
            self.run_script(argv, transport)
        self.run_script(["--image", self.image(), "--send", "--image-is-non-sensitive"], transport, key_present=False)
        self.assertEqual(transport.calls, [])


class TestOneRequestOnly(Case):
    def test_a_successful_live_run_makes_exactly_one_request_to_the_official_endpoint(self):
        transport = Transport()
        code, out, err = self.send(transport)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(transport.calls), 1)
        url, headers, _, timeout = transport.calls[0]
        self.assertEqual((url, timeout), (nvidia.ENDPOINT, 60.0))
        self.assertEqual(headers["Authorization"], "Bearer " + KEY)
        self.assertIn("HTTP requests handed to the transport: 1", out)

    def test_failures_never_trigger_a_second_request(self):
        for transport in (Transport(status=503, body=b"busy"), Transport(status=429), Transport(status=401),
                          Transport(error=TransportTimeout()), Transport(error=TransportError()),
                          Transport(body=b"{not json"), Transport(body=reply("not the contract"))):
            code, _, _ = self.send(transport)
            self.assertIn(code, (1, 3))
            self.assertEqual(len(transport.calls), 1)

    def test_the_transport_guard_refuses_a_second_request_and_any_other_url(self):
        inner = Transport()
        guard = smoke.GuardedTransport(inner, thinking_off=False)
        body = b'{"model": "m"}'
        guard(nvidia.ENDPOINT, {}, body, 1)
        with self.assertRaises(OneRequestOnly):
            guard(nvidia.ENDPOINT, {}, body, 1)
        self.assertEqual(len(inner.calls), 1)
        other = smoke.GuardedTransport(inner, thinking_off=False)
        with self.assertRaises(smoke.WrongEndpoint):
            other("https://example.com/v1/chat/completions", {}, body, 1)
        self.assertEqual(len(inner.calls), 1)

    def test_a_guard_error_cannot_become_a_retry_or_a_fallback(self):
        # The adapter turns a transport exception into a closed error code; nothing else is attempted.
        inner = Transport()
        guard = smoke.GuardedTransport(inner, thinking_off=False)
        analyzer = nvidia.NvidiaImageAnalyzer(transport=guard, environ={"NVIDIA_API_KEY": KEY})
        from image_analysis.boundary import AnalysisRequest, ImageInput
        request = AnalysisRequest(ImageInput(synthetic_png(), "image/png"), "prompt")
        self.assertEqual(analyzer.analyze(request).status, "ok")
        self.assertEqual(analyzer.analyze(request).error_code, "transport_failed")
        self.assertEqual(len(inner.calls), 1)


class TestRequestBody(Case):
    def sent_body(self, *extra):
        transport = Transport()
        self.send(transport, *extra)
        return json.loads(transport.calls[0][2])

    def test_by_default_the_body_is_the_adapters_unchanged_one(self):
        body = self.sent_body()
        self.assertEqual(set(body), {"model", "max_tokens", "messages"})
        self.assertEqual(body["model"], "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
        self.assertEqual(body["max_tokens"], 1024)
        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": PROMPT})
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        expected = json.loads(nvidia._build_body(NvidiaConfig(), PROMPT, synthetic_png()))
        self.assertEqual(body, expected)

    def test_the_opt_in_flag_adds_exactly_one_top_level_field_and_changes_nothing_else(self):
        plain, off = self.sent_body(), self.sent_body("--thinking-off")
        self.assertEqual(off.pop("chat_template_kwargs"), {"enable_thinking": False})
        self.assertEqual(off, plain)

    def test_the_output_names_the_body_fields_that_were_sent(self):
        _, out, _ = self.send(Transport())
        self.assertIn("Request body fields: max_tokens, messages, model", out)
        _, out, _ = self.send(Transport(), "--thinking-off")
        self.assertIn("chat_template_kwargs, max_tokens, messages, model", out)

    def test_add_thinking_off_is_strict(self):
        self.assertEqual(json.loads(smoke.add_thinking_off(b'{"a": 1}')),
                         {"a": 1, "chat_template_kwargs": {"enable_thinking": False}})
        for bad in (b"[]", b"not json", b'{"chat_template_kwargs": {}}', b'"text"'):
            with self.assertRaises(ValueError):
                smoke.add_thinking_off(bad)

    def test_the_existing_adapter_defaults_and_the_demo_are_untouched(self):
        source = Path(nvidia.__file__).read_text(encoding="utf-8")
        self.assertNotIn("enable_thinking", source)
        self.assertNotIn("chat_template_kwargs", source)
        server = (Path(smoke.__file__).resolve().parent.parent / "demo_app" / "server.py").read_text(encoding="utf-8")
        # IA-7: the demo entry point may name the field, but only for its opt-in --findings-thinking-off flag (its
        # default request body is pinned in test_demo_nvidia_diagnostics.py); the adapter above still never does.
        self.assertIn("--findings-thinking-off", server)
        self.assertEqual(server.count("enable_thinking"), 2)    # the opt-in constant and that flag's help text
        self.assertEqual(set(json.loads(nvidia._build_body(NvidiaConfig(), "p", synthetic_png()))),
                         {"model", "max_tokens", "messages"})
        self.assertEqual((NvidiaConfig().max_tokens, NvidiaConfig().timeout_seconds), (1024, 60.0))


class TestContractAndSafeOutput(Case):
    def test_a_reply_that_fits_the_contract_passes_both_stages(self):
        code, out, _ = self.send(Transport())
        self.assertEqual(code, 0)
        self.assertIn("STAGE 1", out)
        self.assertIn("PASS: NVIDIA answered", out)
        self.assertIn("PASS: the reply is valid findings. Crop candidates: 1; growth stage given: no; "
                      "symptoms: 1; more-information requests: 1", out)
        self.assertIn("does not show the model's observations are correct", out)

    def test_a_code_fenced_reply_is_accepted_like_the_contract_says(self):
        fenced = "```json\n" + json.dumps(good_findings()) + "\n```"
        self.assertEqual(self.send(Transport(body=reply(fenced)))[0], 0)

    def test_replies_that_do_not_fit_fail_only_stage_two_with_safe_shape_facts(self):
        valid = json.dumps(good_findings())
        replies = {
            "prose": ("Probably a plant with spots.", dict(starts="False", fenced="False", parses="False", obj="False")),
            "reasoning first": ("Let me think about this image. " + valid,
                                dict(starts="False", fenced="False", parses="False", obj="False")),
            "extra key": (json.dumps(dict(good_findings(), diagnosis="confirmed")),
                          dict(starts="True", fenced="False", parses="True", obj="True")),
            "empty object": ("{}", dict(starts="True", fenced="False", parses="True", obj="True")),
            "json list": ("[]", dict(starts="False", fenced="False", parses="True", obj="False")),
        }
        for name, (text, facts) in replies.items():
            with self.subTest(reply=name):
                transport = Transport(body=reply(text))
                code, out, _ = self.send(transport)
                self.assertEqual(code, 3)
                self.assertEqual(len(transport.calls), 1)
                self.assertIn("PASS: NVIDIA answered", out)
                self.assertIn("FAIL: the reply was not accepted (code: response_malformed)", out)
                self.assertIn("starts with '{': %(starts)s; code-fenced: %(fenced)s; parses as JSON: %(parses)s; "
                              "JSON is an object: %(obj)s" % facts, out)
                self.assertIn("the connection works but the reply does not fit the findings contract", out)

    def test_an_empty_or_blank_reply_is_a_provider_stage_failure(self):
        code, out, _ = self.send(Transport(body=reply("   ")))
        self.assertEqual(code, 1)
        self.assertIn("FAIL: response_empty", out)
        self.assertIn("stage 2 not applicable", out)

    def test_provider_failures_are_stage_one_with_their_closed_codes(self):
        cases = ((Transport(status=503), "provider_unavailable"), (Transport(status=401), "provider_rejected"),
                 (Transport(error=TransportTimeout()), "provider_timeout"),
                 (Transport(error=nvidia.ResponseTooLarge()), "response_too_large"))
        for transport, code in cases:
            exit_code, out, _ = self.send(transport)
            self.assertEqual(exit_code, 1)
            self.assertIn("FAIL: " + code, out)

    def test_no_model_text_key_header_image_or_request_body_is_ever_printed(self):
        long_reply = (MARKER + " ") * 400
        bodies = [reply(json.dumps(good_findings())), reply(long_reply), reply("Thinking: " + MARKER),
                  reply("```json\n" + json.dumps(good_findings()) + "\n```")]
        for body in bodies:
            for extra in ([], ["--thinking-off"]):
                code, out, err = self.send(Transport(body=body), *extra)
                combined = out + err
                for forbidden in (MARKER, KEY, "Bearer", "Authorization", "base64", "data:image", "image_url",
                                  "RULES", "Reply with ONE JSON", "Example of the shortest valid answer"):
                    self.assertNotIn(forbidden, combined, forbidden)
                self.assertLess(len(out), 2500)

    def test_canaries_in_provider_error_bodies_and_transport_exceptions_never_reach_the_output(self):
        poisoned = (MARKER + " " + KEY).encode()
        transports = (Transport(status=500, body=poisoned), Transport(status=400, body=poisoned),
                      Transport(status=302, body=poisoned), Transport(error=RuntimeError(MARKER + KEY)),
                      Transport(error=OSError(MARKER + KEY)))
        for transport in transports:
            code, out, err = self.send(transport)
            self.assertEqual(code, 1)
            self.assertEqual(len(transport.calls), 1)
            for forbidden in (MARKER, KEY):
                self.assertNotIn(forbidden, out + err)

    def test_an_unexpected_exception_prints_only_its_type_and_no_traceback(self):
        with mock.patch.object(smoke.PromptedFindingsProvider, "analyze_image",
                               side_effect=RuntimeError(MARKER + KEY)):
            code, out, err = self.send(Transport())
        self.assertEqual(code, 1)
        self.assertIn("Unexpected failure (RuntimeError). No result.", err)
        for forbidden in (MARKER, KEY, "Traceback", "File \""):
            self.assertNotIn(forbidden, out + err)

    def test_the_timeout_is_bounded_and_unchanged(self):
        transport = Transport()
        self.send(transport)
        self.assertEqual(transport.calls[0][3], 60.0)
        self.assertEqual(NvidiaConfig().timeout_seconds, 60.0)

    def test_the_outcome_is_deterministic_for_the_same_fake_reply(self):
        first = self.send(Transport())
        second = self.send(Transport())
        # Elapsed time differs; everything else is identical.
        strip = lambda text: "\n".join(l for l in text.splitlines() if "Elapsed" not in l)
        self.assertEqual((first[0], strip(first[1])), (second[0], strip(second[1])))


def valid_findings():
    return {
        "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
        "growth_stage": {"candidate": "stage-x", "evidence": ["plant height"], "uncertainty": "high"},
        "symptoms": [{"observation": "brown spots", "possible_interpretations": ["cause-a", "cause-b"]}],
        "needs_more": ["a close-up photo"],
    }


def edited(path, value, base=None, delete=False):
    """A deep copy of the valid findings with the value at `path` replaced (or deleted)."""
    import copy
    document = copy.deepcopy(base if base is not None else valid_findings())
    node = document
    for step in path[:-1]:
        node = node[step]
    if delete:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return document


def codes_of(document):
    return smoke.diagnose_findings(document)


class TestDiagnosticsAgreeWithTheValidator(unittest.TestCase):
    """The diagnostics must name a rule exactly when the real validator rejects the document."""

    def battery(self):
        from agri_pipeline.findings import MAX_CROP_CANDIDATES, MAX_INTERPRETATIONS, MAX_NEEDS, MAX_SYMPTOMS
        bad_texts = ["", "   ", "x" * 301, "tab\there", "new\nline", 5, None, True, [], {}, 1.5]
        bad_names = bad_texts + ["x" * 65, "<b>bold</b>", "leaf spot (fungal)", "a;b", "a/b", "a:b", 'say "hi"', "50%"]
        bad_enums = ["95%", "HIGH", "", None, 5, True, [], {}, "medium ", "very high"]
        wrong_types = [None, 5, "text", [], {}, True, 1.5]
        documents = [valid_findings()]
        top = ("crop", "growth_stage", "symptoms", "needs_more")
        for key in top:
            documents.append(edited((key,), None, delete=True))
            documents += [edited((key,), value) for value in wrong_types]
        documents.append(dict(valid_findings(), extra=1))
        documents.append(edited(("crop", "extra"), 1))
        documents.append(edited(("crop", "candidates"), None, delete=True))
        documents.append(edited(("crop", "candidates", 0, "extra"), 1))
        for key in ("name", "evidence", "uncertainty"):
            documents.append(edited(("crop", "candidates", 0, key), None, delete=True))
        documents += [edited(("crop", "candidates", 0, "name"), v) for v in bad_names]
        documents += [edited(("crop", "candidates", 0, "uncertainty"), v) for v in bad_enums]
        documents += [edited(("crop", "candidates", 0, "evidence"), v) for v in wrong_types[:4] + [["x"] * 6, [5], [""], ["x" * 201], ["a\nb"]]]
        documents += [edited(("crop", "candidates"), v) for v in wrong_types + [[1], ["x"], [None]]]
        documents.append(edited(("crop", "candidates"), [valid_findings()["crop"]["candidates"][0]] * (MAX_CROP_CANDIDATES + 1)))
        for key in ("candidate", "evidence", "uncertainty"):
            documents.append(edited(("growth_stage", key), None, delete=True))
        documents.append(edited(("growth_stage", "extra"), 1))
        documents += [edited(("growth_stage", "candidate"), v) for v in bad_names if v is not None]
        documents += [edited(("growth_stage", "uncertainty"), v) for v in bad_enums]
        documents += [edited(("growth_stage", "evidence"), v) for v in [None, 5, "x", {}, ["x"] * 6, [5], [""]]]
        documents.append(edited(("growth_stage",), {"candidate": None, "evidence": ["seen"], "uncertainty": "low"}))
        documents.append(edited(("growth_stage",), {"candidate": None, "evidence": [], "uncertainty": "unspecified"}))
        for key in ("observation", "possible_interpretations"):
            documents.append(edited(("symptoms", 0, key), None, delete=True))
        documents.append(edited(("symptoms", 0, "extra"), 1))
        documents += [edited(("symptoms", 0, "observation"), v) for v in bad_texts]
        documents += [edited(("symptoms", 0, "possible_interpretations"), v) for v in [None, 5, "x", {}, ["x"] * (MAX_INTERPRETATIONS + 1), [5], [""], ["(x)"], []]]
        documents += [edited(("symptoms",), v) for v in [None, "x", {}, [], [1], [None], [valid_findings()["symptoms"][0]] * (MAX_SYMPTOMS + 1)]]
        documents += [edited(("needs_more",), v) for v in [None, "x", {}, [], [1], [""], ["x" * 301], ["x"] * (MAX_NEEDS + 1), ["a\nb"]]]
        return documents

    def test_the_diagnostics_are_empty_exactly_when_the_validator_accepts(self):
        from agri_pipeline.findings import validate_findings
        battery = self.battery()
        self.assertGreater(len(battery), 150)
        accepted = rejected = 0
        for document in battery:
            valid = validate_findings(document)
            found = codes_of(document)
            self.assertEqual(bool(found), not valid, (found, document))
            accepted += valid
            rejected += not valid
            self.assertTrue(all(code in smoke.DIAGNOSTIC_CODES for code, _ in found))
        self.assertGreater(accepted, 3)
        self.assertGreater(rejected, 150)

    def test_every_reply_the_parser_rejects_gets_a_non_empty_closed_diagnosis(self):
        from agri_pipeline.findings import parse_model_findings
        replies = [json.dumps(d) for d in self.battery()] + [
            "", "   ", "not json", "[]", "5", "null", "true", "{", "```json\n{}\n```", "```", "x" * 20001,
            "Here you go: " + json.dumps(valid_findings()), json.dumps(valid_findings()) + " thanks"]
        for reply in replies:
            rejected = parse_model_findings(reply) is None
            found = smoke.diagnose_reply(reply)
            self.assertEqual(bool(found), rejected, (reply[:60], found))
            self.assertTrue(all(code in smoke.DIAGNOSTIC_CODES for code, _ in found))

    def test_accepted_replies_have_no_diagnosis(self):
        text = json.dumps(valid_findings())
        for reply in (text, " \n" + text + "\n ", "```json\n" + text + "\n```", "```\n" + text + "\n```"):
            self.assertEqual(smoke.diagnose_reply(reply), [])


class TestDiagnosticCodes(unittest.TestCase):
    def test_each_rule_maps_to_its_closed_code_and_structural_path(self):
        cases = [
            (edited(("crop",), None, delete=True), ("MISSING_REQUIRED_KEY", "crop")),
            (edited(("crop",), "crop-a"), ("WRONG_TYPE", "crop")),
            (edited(("crop",), ["crop-a"]), ("WRONG_TYPE", "crop")),
            (edited(("crop",), None), ("WRONG_TYPE", "crop")),
            (dict(valid_findings(), extra=1), ("EXTRA_FIELD", "")),
            (edited(("crop", "candidates", 0, "uncertainty"), "95%"), ("INVALID_ENUM_VALUE", "crop.candidates[0].uncertainty")),
            (edited(("growth_stage", "uncertainty"), "HIGH"), ("INVALID_ENUM_VALUE", "growth_stage.uncertainty")),
            (edited(("crop", "candidates"), "x"), ("WRONG_TYPE", "crop.candidates")),
            (edited(("symptoms",), 5), ("WRONG_TYPE", "symptoms")),
            (edited(("crop", "candidates", 0, "name"), ""), ("EMPTY_TEXT", "crop.candidates[0].name")),
            (edited(("crop", "candidates", 0, "name"), "x" * 65), ("TEXT_TOO_LONG", "crop.candidates[0].name")),
            (edited(("symptoms", 0, "observation"), "a\nb"), ("CONTROL_CHARACTER", "symptoms[0].observation")),
            (edited(("symptoms", 0, "possible_interpretations", 0), "leaf spot (fungal)"),
             ("DISALLOWED_CHARACTER", "symptoms[0].possible_interpretations[0]")),
            (edited(("growth_stage", "candidate"), "stage; x"), ("DISALLOWED_CHARACTER", "growth_stage.candidate")),
            (edited(("needs_more",), ["x"] * 6), ("TOO_MANY_ITEMS", "needs_more")),
            (edited(("needs_more", 0), 5), ("WRONG_TYPE", "needs_more[0]")),
            (edited(("growth_stage",), {"candidate": None, "evidence": ["seen"], "uncertainty": "low"}),
             ("EVIDENCE_WITHOUT_CANDIDATE", "growth_stage")),
            (edited(("growth_stage", "evidence"), ["x"] * 6), ("TOO_MANY_ITEMS", "growth_stage.evidence")),
            (edited(("crop", "candidates", 0, "evidence", 0), "x" * 201),
             ("TEXT_TOO_LONG", "crop.candidates[0].evidence[0]")),
        ]
        for document, expected in cases:
            self.assertIn(expected, codes_of(document), expected)

    def test_a_document_that_is_not_an_object_is_one_wrong_type_at_the_top(self):
        for document in ([], "text", 5, None, True):
            self.assertEqual(codes_of(document), [("WRONG_TYPE", "")])

    def test_parse_level_failures_have_their_own_codes(self):
        cases = {"": "REPLY_EMPTY", "  \n": "REPLY_EMPTY", None: "REPLY_EMPTY", 5: "REPLY_EMPTY",
                 "x" * 20001: "REPLY_TOO_LONG", "Probably a plant.": "NOT_JSON", "{": "NOT_JSON",
                 "```json\n{": "NOT_JSON", "[" * 19000: "NOT_JSON",  # deep nesting: a RecursionError, handled
                 "[" * 100000: "REPLY_TOO_LONG"}
        for reply, code in cases.items():
            self.assertEqual(smoke.diagnose_reply(reply), [(code, "")], repr(reply)[:20])

    def test_the_reasoning_prefix_case_is_reported_as_not_json(self):
        reply = "Let me think about this image. " + json.dumps(valid_findings())
        self.assertEqual(smoke.diagnose_reply(reply), [("NOT_JSON", "")])

    def test_several_problems_are_all_reported_in_walk_order_without_duplicates(self):
        document = edited(("crop", "candidates", 0, "uncertainty"), "95%")
        document["extra"] = 1
        document["needs_more"] = ["x" * 301]
        found = codes_of(document)
        self.assertEqual(found, [("EXTRA_FIELD", ""), ("INVALID_ENUM_VALUE", "crop.candidates[0].uncertainty"),
                                 ("TEXT_TOO_LONG", "needs_more[0]")])
        self.assertEqual(len(found), len(set(found)))

    def test_a_huge_list_is_scanned_only_up_to_a_bound(self):
        document = edited(("needs_more",), [5] * 100000)
        found = codes_of(document)
        self.assertIn(("TOO_MANY_ITEMS", "needs_more"), found)
        self.assertLessEqual(len(found), smoke.ITEM_SCAN_LIMIT + 1)

    def test_the_constants_come_from_the_validator_not_a_copy(self):
        from agri_pipeline import findings
        from crop_context import schema
        self.assertIs(smoke.UNCERTAINTY, schema.UNCERTAINTY)
        self.assertEqual((smoke.MAX_SYMPTOMS, smoke.MAX_NEEDS, smoke.MAX_CROP_CANDIDATES, smoke.MAX_INTERPRETATIONS,
                          smoke.MAX_REPLY_CHARS),
                         (findings.MAX_SYMPTOMS, findings.MAX_NEEDS, findings.MAX_CROP_CANDIDATES,
                          findings.MAX_INTERPRETATIONS, findings.MAX_REPLY_CHARS))


class TestDiagnosticsInTheRun(Case):
    def send_reply(self, text, *extra):
        return self.send(Transport(body=reply(text)), *extra)

    def test_a_contract_failure_prints_why_in_codes_and_paths_only(self):
        document = valid_findings()
        document["symptoms"][0]["possible_interpretations"] = ["leaf spot (fungal)"]
        code, out, _ = self.send_reply(json.dumps(document))
        self.assertEqual(code, 3)
        self.assertIn("Why (closed codes and structural paths only; nothing the model wrote): "
                      "DISALLOWED_CHARACTER at symptoms[0].possible_interpretations[0]", out)

    def test_hostile_content_never_reaches_the_output_even_as_a_key_name_or_value(self):
        document = valid_findings()
        document[MARKER + KEY] = MARKER
        document["crop"]["candidates"][0]["uncertainty"] = MARKER + KEY
        document["crop"]["candidates"][0]["name"] = "<script>" + MARKER
        code, out, err = self.send_reply(json.dumps(document))
        self.assertEqual(code, 3)
        self.assertIn("EXTRA_FIELD at (top level)", out)
        self.assertIn("INVALID_ENUM_VALUE at crop.candidates[0].uncertainty", out)
        self.assertIn("DISALLOWED_CHARACTER at crop.candidates[0].name", out)
        for forbidden in (MARKER, KEY, "script"):
            self.assertNotIn(forbidden, out + err)

    def test_a_reasoning_prefixed_reply_is_diagnosed_as_not_json(self):
        code, out, _ = self.send_reply("Thinking about it. " + json.dumps(valid_findings()))
        self.assertEqual(code, 3)
        self.assertIn("NOT_JSON at (top level)", out)

    def test_a_long_list_of_problems_is_cut_with_a_count(self):
        document = valid_findings()
        document["needs_more"] = [5] * 20
        document["symptoms"] = [{"observation": 5, "possible_interpretations": [5] * 3} for _ in range(8)]
        code, out, _ = self.send_reply(json.dumps(document))
        self.assertEqual(code, 3)
        self.assertRegex(out, r"\(\+\d+ more\)")

    def test_an_unexplained_rejection_is_named_as_such_not_guessed(self):
        with mock.patch.object(smoke, "diagnose_reply", return_value=[]):
            code, out, _ = self.send_reply("Probably a plant.")
        self.assertEqual(code, 3)
        self.assertIn("UNDIAGNOSED_REJECTION at (top level)", out)

    def test_a_failure_inside_the_diagnostics_is_a_closed_code_not_a_traceback(self):
        with mock.patch.object(smoke, "diagnose_findings", side_effect=RuntimeError(MARKER + KEY)):
            code, out, err = self.send_reply(json.dumps(dict(valid_findings(), extra=1)))
        self.assertEqual(code, 3)
        self.assertIn("DIAGNOSTIC_ERROR at (top level)", out)
        for forbidden in (MARKER, KEY, "Traceback"):
            self.assertNotIn(forbidden, out + err)

    def test_a_passing_reply_and_provider_failures_print_no_diagnosis(self):
        for transport in (Transport(), Transport(status=503), Transport(error=TransportTimeout())):
            _, out, _ = self.send(transport)
            self.assertNotIn("Why (closed codes", out)

    def test_diagnostics_do_not_change_the_request_exit_codes_or_request_count(self):
        transport = Transport(body=reply("Probably a plant."))
        code, _, _ = self.send(transport)
        self.assertEqual((code, len(transport.calls)), (3, 1))

    def test_output_stays_short_and_the_contract_is_unchanged(self):
        from agri_pipeline.findings import validate_findings
        _, out, _ = self.send_reply(json.dumps(dict(valid_findings(), extra=1)))
        self.assertLess(len(out), 3000)
        self.assertFalse(validate_findings(dict(valid_findings(), extra=1)))
        self.assertTrue(validate_findings(valid_findings()))


class TestTransportObservation(Case):
    """The script reports the numeric HTTP status (or the kind of failure) and nothing else about it."""

    def observation_line(self, out):
        return [l for l in out.splitlines() if l.strip().startswith("Transport observation:")][0].strip()

    def test_a_success_reports_status_200(self):
        code, out, _ = self.send(Transport())
        self.assertEqual(code, 0)
        self.assertEqual(self.observation_line(out), "Transport observation: HTTP status 200 (success).")

    def test_every_5xx_reports_its_number_keeps_provider_unavailable_and_does_not_guess_a_cause(self):
        for status in (500, 502, 503, 504, 599):
            transport = Transport(status=status, body=b"anything")
            code, out, _ = self.send(transport)
            self.assertEqual(code, 1)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(self.observation_line(out), "Transport observation: HTTP status %d (server error "
                             "response; the status alone does not say why)." % status)
            self.assertIn("FAIL: provider_unavailable", out)
            self.assertIn("stage 2 not applicable", out)
            line = self.observation_line(out).lower()
            for guess in ("capacity", "credit", "quota", "overload", "busy", "exhaust", "model", "rate", "limit"):
                self.assertNotIn(guess, line)

    def test_4xx_and_3xx_report_their_class_and_their_existing_codes(self):
        cases = ((401, "client error response", "provider_rejected"), (403, "client error response", "provider_rejected"),
                 (429, "client error response", "provider_rejected"),
                 (302, "redirect, not followed", "response_malformed"))
        for status, label, expected_code in cases:
            code, out, _ = self.send(Transport(status=status))
            self.assertEqual(code, 1)
            self.assertIn("HTTP status %d (%s)" % (status, label), out)
            self.assertIn("FAIL: " + expected_code, out)
        self.assertNotIn("rate", self.observation_line(self.send(Transport(status=429))[1]).lower())

    def test_a_timeout_and_a_connection_failure_are_not_reported_as_http_statuses(self):
        code, out, _ = self.send(Transport(error=TransportTimeout()))
        self.assertTrue(self.observation_line(out).startswith(
            "Transport observation: no complete HTTP response: the request timed out ("))
        self.assertIn("cannot tell a timeout before any response from one while the body was being read", out)
        self.assertIn("FAIL: provider_timeout", out)
        self.assertNotIn("HTTP status", out)
        code, out, _ = self.send(Transport(error=TransportError()))
        self.assertTrue(self.observation_line(out).startswith(
            "Transport observation: no complete HTTP response: connection failure ("))
        self.assertIn("does not distinguish DNS, TLS, refused or reset connections", out)
        self.assertIn("FAIL: provider_connection_failed", out)
        self.assertNotIn("HTTP status", out)

    def test_a_5xx_response_is_told_apart_from_a_connection_failure(self):
        five_hundred = self.observation_line(self.send(Transport(status=503))[1])
        refused = self.observation_line(self.send(Transport(error=TransportError()))[1])
        self.assertNotEqual(five_hundred, refused)
        self.assertIn("HTTP status 503", five_hundred)
        self.assertIn("no complete HTTP response", refused)

    def test_an_oversized_body_and_unexpected_errors_and_odd_statuses_are_described_honestly(self):
        _, out, _ = self.send(Transport(error=nvidia.ResponseTooLarge()))
        self.assertIn("exceeded the local size limit; the status is not available", out)
        self.assertIn("FAIL: response_too_large", out)
        _, out, _ = self.send(Transport(error=RuntimeError(MARKER + KEY)))
        self.assertIn("no HTTP response: an unexpected transport error", out)
        self.assertNotIn(MARKER, out)
        for status in ("200", True, None, 2.5):
            transport = Transport()
            transport.status = status
            _, out, _ = self.send(transport)
            self.assertIn("a response arrived without a valid numeric status", out)
            self.assertIn("FAIL: response_malformed", out)

    def test_only_the_number_is_kept_never_a_body_or_a_header(self):
        poisoned = (MARKER + KEY).encode()
        guard = smoke.GuardedTransport(Transport(status=503, body=poisoned), thinking_off=False)
        guard(nvidia.ENDPOINT, {"Authorization": "Bearer " + KEY}, b'{"model": "m"}', 1)
        self.assertEqual((guard.status, guard.observation), (503, "http_response"))
        kept = repr(vars(guard))
        for forbidden in (MARKER, KEY, "Bearer", "Authorization"):
            self.assertNotIn(forbidden, kept)
        _, out, err = self.send(Transport(status=500, body=poisoned))
        for forbidden in (MARKER, KEY):
            self.assertNotIn(forbidden, out + err)

    def test_a_refused_second_request_does_not_overwrite_what_the_first_one_saw(self):
        guard = smoke.GuardedTransport(Transport(status=500), thinking_off=False)
        guard(nvidia.ENDPOINT, {}, b'{"a": 1}', 1)
        with self.assertRaises(OneRequestOnly):
            guard(nvidia.ENDPOINT, {}, b'{"a": 1}', 1)
        self.assertEqual((guard.status, guard.observation, guard.calls), (500, "http_response", 1))
        fresh = smoke.GuardedTransport(Transport(), thinking_off=False)
        with self.assertRaises(smoke.WrongEndpoint):
            fresh("https://example.com/x", {}, b"{}", 1)
        self.assertEqual((fresh.status, fresh.observation, fresh.calls), (None, "refused_by_guard", 0))
        self.assertEqual(smoke.describe_transport(smoke.GuardedTransport(Transport(), False)),
                         "no request reached the transport")

    def test_the_real_transport_reports_the_status_of_an_http_error_response_without_a_network(self):
        import socket
        import urllib.error
        poisoned = (MARKER + KEY).encode()
        errors = {
            "HTTP status 503": urllib.error.HTTPError(nvidia.ENDPOINT, 503, "Service Unavailable", {}, io.BytesIO(poisoned)),
            "HTTP status 500": urllib.error.HTTPError(nvidia.ENDPOINT, 500, "Server Error", {}, io.BytesIO(poisoned)),
            "HTTP status 401": urllib.error.HTTPError(nvidia.ENDPOINT, 401, "Unauthorized", {}, io.BytesIO(poisoned)),
            "the request timed out": urllib.error.URLError(socket.timeout()),
            "connection failure": urllib.error.URLError(ConnectionRefusedError()),
        }
        for expected, error in errors.items():
            with self.subTest(expected=expected), \
                    mock.patch("urllib.request.OpenerDirector.open", side_effect=error) as opener:
                out, err = io.StringIO(), io.StringIO()
                code = smoke.run(["--image", self.image(), "--send", "--image-is-non-sensitive"], stdout=out, stderr=err,
                                 key_present=lambda: True, environ={"NVIDIA_API_KEY": KEY})
            self.assertEqual(code, 1)
            self.assertEqual(opener.call_count, 1)  # exactly one attempt to open a connection
            self.assertIn(expected, self.observation_line(out.getvalue()))
            for forbidden in (MARKER, KEY):
                self.assertNotIn(forbidden, out.getvalue() + err.getvalue())

    def test_the_status_classes_are_neutral_and_cover_the_boundaries(self):
        expected = {99: "outside the standard status ranges", 100: "informational response",
                    199: "informational response", 200: "success", 299: "success", 300: "redirect, not followed",
                    399: "redirect, not followed", 400: "client error response", 499: "client error response",
                    500: "server error response; the status alone does not say why",
                    599: "server error response; the status alone does not say why",
                    600: "outside the standard status ranges"}
        for status, label in expected.items():
            self.assertEqual(smoke.status_class(status), label, status)

    def test_nothing_else_changed_classification_exit_codes_request_count_and_the_adapter(self):
        for status, adapter_code in ((503, "provider_unavailable"), (500, "provider_unavailable"),
                                     (401, "provider_rejected"), (302, "response_malformed")):
            analyzer = nvidia.NvidiaImageAnalyzer(transport=Transport(status=status), environ={"NVIDIA_API_KEY": KEY})
            from image_analysis.boundary import AnalysisRequest, ImageInput
            result = analyzer.analyze(AnalysisRequest(ImageInput(synthetic_png(), "image/png"), "p"))
            self.assertEqual(result.error_code, adapter_code)
        transport = Transport(status=503)
        code, _, _ = self.send(transport)
        self.assertEqual((code, len(transport.calls)), (1, 1))
        source = Path(nvidia.__file__).read_text(encoding="utf-8")
        for name in ("observation", "last_status", "describe_transport", "GuardedTransport"):
            self.assertNotIn(name, source)

    def test_a_dry_run_prints_no_transport_observation(self):
        _, out, _ = self.run_script(["--image", self.image()])
        self.assertNotIn("Transport observation", out)


class TestScriptSource(unittest.TestCase):
    SOURCE = SCRIPT.read_text(encoding="utf-8")

    def test_it_reads_no_environment_variable_itself_and_hardcodes_no_credential(self):
        tree = ast.parse(self.SOURCE)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        # (`environ` appears only as a parameter passed through to the adapter, never as `os.environ`.)
        for banned in ("getenv", "sleep", "socket", "urlopen", "print_exc", "format_exc"):
            self.assertNotIn(banned, names, banned)
        self.assertNotIn("environ", {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)})
        self.assertNotIn("os.environ", self.SOURCE)
        self.assertNotIn("nvapi-", self.SOURCE)

    def test_there_is_no_retry_loop_and_one_transport_call_site(self):
        tree = ast.parse(self.SOURCE)
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.While)
                          and "chunk" not in ast.unparse(n.test) and "position" not in ast.unparse(n.test)], [])
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_inner"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.SOURCE.count("provider.analyze_image("), 1)

    def test_it_uses_only_the_findings_contract_and_the_nvidia_adapter(self):
        allowed = {"argparse", "hashlib", "json", "os", "struct", "sys", "time", "unicodedata", "agri_pipeline",
                   "crop_context", "image_analysis", "scripts"}
        for node in ast.walk(ast.parse(self.SOURCE)):
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module]
            else:
                continue
            for module in modules:
                self.assertIn(module.split(".")[0], allowed, module)
                self.assertNotIn("nebius", module)
                self.assertNotIn("research", module)
                self.assertNotIn("demo_app", module)

    def test_the_documented_field_is_exactly_the_one_that_is_added(self):
        self.assertEqual(smoke.THINKING_OFF, {"enable_thinking": False})
        self.assertIn("chat_template_kwargs", self.SOURCE)
        # The hosted-endpoint uncertainty is stated in the script's own documentation.
        self.assertIn("NOT STATED", " ".join(self.SOURCE.split()))


if __name__ == "__main__":
    unittest.main()
