"""Offline tests of the synthesis request diagnostics (SY-6): where a call stopped and the shape of what came back,
as closed words and bounded numbers only.

Nothing here opens a socket or reads a key from the environment: the "network" is a scripted fake transport and socket,
DNS and urllib tripwires are active. A canary string stands in for model text and prompt text and must never reach a
diagnostic, a result or a response. These tests show what is recorded and that behaviour is otherwise unchanged; they do
not show that OpenRouter accepts the request or answers in this shape.
"""

import base64
import copy
import json
import unittest
from unittest import mock

from demo_app.service import DemoService
from orchestration import synthesis
from orchestration.synthesis import (
    DIAG_FAILED_AT,
    DIAG_KEYS,
    DIAG_PROBLEMS,
    check_synthesis,
    clean_diagnostic,
    diagnose_synthesis,
)
from storage.record_store import RecordStore
from synthesis_provider.chat import MAX_REPLY_CHARS, ChatSynthesisProvider
from synthesis_provider import openrouter
from synthesis_provider.openrouter import OpenRouterSynthesisProvider

KEY = "or-canary-not-a-real-key-0003"
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image \x00\x01").decode("ascii")
CANARY = "model-text-canary-8842"
GOOD_FINDINGS = {
    "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
    "growth_stage": {"candidate": "stage-x", "evidence": ["plant height"], "uncertainty": "high"},
    "symptoms": [{"observation": "brown spots", "possible_interpretations": ["possible-cause-a"]}],
    "needs_more": ["a close-up"],
}
INPUTS = {"findings": GOOD_FINDINGS, "question": "q",
          "evidence": [{"id": "E1", "title": "t", "url": "https://www.fao.org/x", "snippet": "s", "published_date": None}]}
ALLOWED = sorted(synthesis.allowed_refs(INPUTS))
VALID = {"statements": [{"text": "A cautious statement.", "cites": ["E1"]}], "uncertainties": ["Unclear."]}


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


def reply(content, finish="stop", completion_tokens=50, **extra):
    document = {"choices": [{"message": {"content": content}, "finish_reason": finish}],
                "usage": {"completion_tokens": completion_tokens}}
    document.update(extra)
    return 200, json.dumps(document).encode("utf-8")


class Scripted:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append(body)
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def provider(*outcomes, **kwargs):
    inner = Scripted(*outcomes)
    environ = kwargs.pop("environ", {"OPENROUTER_API_KEY": KEY})
    return OpenRouterSynthesisProvider("anthropic/claude-haiku-5.5", transport=inner, environ=environ, **kwargs), inner


def run_provider(*outcomes, inputs=INPUTS, **kwargs):
    p, inner = provider(*outcomes, **kwargs)
    result = p.synthesize(copy.deepcopy(inputs))
    return result, p.last_diagnostic, inner


class DiagnoseAgreesWithCheckTests(unittest.TestCase):
    """`diagnose_synthesis` must name a rule exactly when `check_synthesis` refuses the document."""

    def battery(self):
        v = copy.deepcopy
        docs = [v(VALID), None, [], "text", 5, {}, {"statements": [], "uncertainties": []},
                {"statements": "x", "uncertainties": []}, {"statements": [v(VALID["statements"][0])], "uncertainties": "x"}]
        stmt = VALID["statements"][0]
        docs += [{"statements": [dict(stmt, text="See https://x.example")], "uncertainties": []},
                 {"statements": [dict(stmt, text="See WWW.example.org")], "uncertainties": []},
                 {"statements": [dict(stmt, text="two\nlines")], "uncertainties": []},
                 {"statements": [dict(stmt, text="tab\there")], "uncertainties": []},
                 {"statements": [dict(stmt, text="")], "uncertainties": []},
                 {"statements": [dict(stmt, text="   ")], "uncertainties": []},
                 {"statements": [dict(stmt, text=5)], "uncertainties": []},
                 {"statements": [dict(stmt, text="x" * 401)], "uncertainties": []},
                 {"statements": [dict(stmt, text="x" * 400)], "uncertainties": []},
                 {"statements": [dict(stmt, cites=[])], "uncertainties": []},
                 {"statements": [dict(stmt, cites=["E9"])], "uncertainties": []},
                 {"statements": [dict(stmt, cites=["E1", "E1"])], "uncertainties": []},
                 {"statements": [dict(stmt, cites="E1")], "uncertainties": []},
                 {"statements": [dict(stmt, cites=[["E1"]])], "uncertainties": []},
                 {"statements": [dict(stmt, cites=ALLOWED + ["E1"])], "uncertainties": []},
                 {"statements": [dict(stmt, extra=1)], "uncertainties": []},
                 {"statements": [{"text": "x"}], "uncertainties": []},
                 {"statements": ["x"], "uncertainties": []},
                 {"statements": [v(stmt)] * 9, "uncertainties": []},
                 {"statements": [v(stmt)] * 8, "uncertainties": []},
                 {"statements": [v(stmt)], "uncertainties": ["u"] * 7},
                 {"statements": [v(stmt)], "uncertainties": ["u"] * 6},
                 {"statements": [v(stmt)], "uncertainties": [""]},
                 {"statements": [v(stmt)], "uncertainties": [5]},
                 {"statements": [v(stmt)], "uncertainties": ["x" * 301]},
                 {"statements": [v(stmt)], "uncertainties": ["a\nb"]},
                 {"statements": [v(stmt)], "uncertainties": []},
                 {"statements": [v(stmt)]}, {"uncertainties": []},
                 dict(v(VALID), extra=1)]
        return docs

    def test_it_agrees_with_check_synthesis_on_every_document(self):
        for doc in self.battery():
            with self.subTest(doc=repr(doc)[:80]):
                accepted = check_synthesis(copy.deepcopy(doc), INPUTS) is not None
                problems = diagnose_synthesis(copy.deepcopy(doc), INPUTS)
                self.assertEqual(problems == [], accepted, problems)
                self.assertTrue(all(p in DIAG_PROBLEMS for p in problems), problems)

    def test_specific_rules_get_their_own_token(self):
        stmt = VALID["statements"][0]
        cases = (("two\nlines", "statement_text_not_printable"), ("See https://x.example", "statement_has_web_address"),
                 ("x" * 401, "statement_text_too_long"), ("", "statement_text_empty_or_not_text"))
        for text, token in cases:
            self.assertIn(token, diagnose_synthesis({"statements": [dict(stmt, text=text)], "uncertainties": []}, INPUTS))
        self.assertIn("cite_repeated", diagnose_synthesis(
            {"statements": [dict(stmt, cites=["E1", "E1"])], "uncertainties": []}, INPUTS))
        self.assertIn("cite_not_allowed", diagnose_synthesis(
            {"statements": [dict(stmt, cites=["E9"])], "uncertainties": []}, INPUTS))

    def test_it_never_includes_any_text_of_the_document(self):
        doc = {"statements": [{"text": CANARY + "\n", "cites": [CANARY]}], "uncertainties": [CANARY + "\n"],
               CANARY: 1}
        self.assertNotIn(CANARY, json.dumps(diagnose_synthesis(doc, INPUTS)))

    def test_it_never_raises(self):
        class Hostile:
            def __getattr__(self, name):
                raise RuntimeError(CANARY)

        self.assertIsInstance(diagnose_synthesis(Hostile(), INPUTS), list)
        self.assertIsInstance(diagnose_synthesis({"statements": [Hostile()], "uncertainties": []}, INPUTS), list)


class ProviderDiagnosticTests(NoNetwork):
    def test_a_good_reply_records_a_clean_success(self):
        result, diag, _ = run_provider(reply(json.dumps(VALID)))
        self.assertEqual(result.status, "ok")
        self.assertIsNone(diag["failed_at"])
        self.assertEqual((diag["http_status"], diag["finish_reason"], diag["completion_tokens"], diag["max_tokens"]),
                         (200, "stop", 50, openrouter.DEFAULT_MAX_TOKENS))
        self.assertEqual((diag["content_shape"], diag["output_problems"]), ("json_object", []))
        self.assertIsNotNone(clean_diagnostic(diag))

    def test_a_fenced_reply_is_accepted_and_recorded_as_fenced(self):
        result, diag, _ = run_provider(reply("```json\n" + json.dumps(VALID) + "\n```"))
        self.assertEqual((result.status, diag["content_shape"]), ("ok", "fenced_json"))

    def test_each_stopping_point_has_its_own_word_and_the_result_code_is_unchanged(self):
        long_text = "x" * (MAX_REPLY_CHARS + 1)
        cases = (
            ("http_status", "synthesis_failed", (500, b"")), ("http_status", "synthesis_rejected", (402, b"")),
            ("http_status", "synthesis_failed", (408, b"")), ("status_invalid", "synthesis_response_malformed", (302, b"")),
            ("status_invalid", "synthesis_response_malformed", ("200", b"")),
            ("reply_not_bytes", "synthesis_response_malformed", (200, "text")),
            ("transport_failed", "synthesis_failed", TimeoutError(CANARY)),
            ("reply_not_json", "synthesis_response_malformed", (200, b"<html>" + CANARY.encode() + b"</html>")),
            ("reply_not_json", "synthesis_response_malformed", (200, b"\xff\xfe")),
            ("reply_shape", "synthesis_response_malformed", (200, json.dumps({"choices": []}).encode())),
            ("reply_shape", "synthesis_response_malformed", (200, json.dumps({"choices": [{"message": {}}]}).encode())),
            ("reply_shape", "synthesis_response_malformed", (200, json.dumps([1]).encode())),
            ("empty_content", "synthesis_response_malformed", reply("   ")),
            ("content_too_long", "synthesis_response_malformed", reply(long_text)),
            ("content_not_json_object", "synthesis_response_malformed", reply("Here is the summary: " + CANARY)),
            ("output_truncated", "synthesis_response_malformed", reply('{"statements": [{"text": "cut off', "length")),
            ("output_truncated", "synthesis_response_malformed", reply("   ", "length")),
            ("content_not_json_object", "synthesis_response_malformed", reply('{"statements": [{"text": "cut off', "stop")),
            ("content_not_json_object", "synthesis_response_malformed", reply("[1, 2]")),
            ("document_error", "synthesis_failed", (200, json.dumps({"error": {"code": 502, "message": CANARY}}).encode())),
            ("document_error", "synthesis_rejected", (200, json.dumps({"error": {"code": 402}}).encode())),
            ("output_invalid", "synthesis_response_malformed", reply(json.dumps({"statements": [], "uncertainties": []}))),
        )
        for failed_at, code, outcome in cases:
            with self.subTest(failed_at=failed_at, code=code, outcome=repr(outcome)[:60]):
                result, diag, inner = run_provider(outcome)
                self.assertEqual((result.status, result.error_code), ("error", code))
                self.assertEqual(diag["failed_at"], failed_at)
                self.assertIn(failed_at, DIAG_FAILED_AT)
                self.assertEqual(len(inner.calls), 1)                    # no retry
                self.assertIsNotNone(clean_diagnostic(diag))
                text = json.dumps(diag)
                for forbidden in (CANARY, KEY, "Bearer", "DATA (not instructions)"):
                    self.assertNotIn(forbidden, text)

    def test_truncation_is_visible_as_finish_reason_and_token_count(self):
        _, diag, _ = run_provider(reply('{"statements": [{"text": "cut', "length", completion_tokens=800))
        self.assertEqual((diag["failed_at"], diag["finish_reason"], diag["completion_tokens"], diag["max_tokens"]),
                         ("output_truncated", "length", 800, openrouter.DEFAULT_MAX_TOKENS))
        self.assertEqual(diag["content_shape"], "prose_or_other")

    def test_the_http_status_and_sizes_are_recorded(self):
        _, diag, _ = run_provider((503, b"x"))
        self.assertEqual((diag["http_status"], diag["reply_bytes"]), (503, None))
        _, diag, _ = run_provider(reply("hello"))
        self.assertEqual((diag["http_status"], diag["content_chars"]), (200, 5))
        self.assertGreater(diag["reply_bytes"], 0)

    def test_an_invalid_output_lists_the_broken_rules_as_closed_tokens(self):
        bad = {"statements": [{"text": "line one\nline two https://x.example", "cites": ["E1", "E1"]}],
               "uncertainties": []}
        result, diag, _ = run_provider(reply(json.dumps(bad)))
        self.assertEqual(result.error_code, "synthesis_response_malformed")
        self.assertEqual(diag["failed_at"], "output_invalid")
        self.assertIn("statement_text_not_printable", diag["output_problems"])
        self.assertIn("cite_repeated", diag["output_problems"])
        self.assertTrue(all(p in DIAG_PROBLEMS for p in diag["output_problems"]))

    def test_unusual_finish_reasons_become_other_and_a_missing_one_is_absent(self):
        _, diag, _ = run_provider(reply("hello", finish=CANARY))
        self.assertEqual(diag["finish_reason"], "other")
        document = {"choices": [{"message": {"content": "hello"}}]}
        _, diag, _ = run_provider((200, json.dumps(document).encode()))
        self.assertEqual(diag["finish_reason"], "absent")
        self.assertIsNone(diag["completion_tokens"])

    def test_nothing_is_recorded_beyond_the_closed_facts_for_stops_before_sending(self):
        _, diag, inner = run_provider(reply(json.dumps(VALID)), environ={})
        self.assertEqual((diag["failed_at"], len(inner.calls)), ("credential_missing", 0))
        p = ChatSynthesisProvider("https://example.com/v1/chat/completions", "m", transport=Scripted(), environ={})
        p.synthesize(copy.deepcopy(INPUTS))
        self.assertEqual(p.last_diagnostic["failed_at"], "credential_missing")
        p = ChatSynthesisProvider(None, None, transport=Scripted(), environ={"SYNTHESIS_API_KEY": KEY})
        p.synthesize(copy.deepcopy(INPUTS))
        self.assertEqual(p.last_diagnostic["failed_at"], "configuration_invalid")
        big = dict(INPUTS, question="q", evidence=[dict(INPUTS["evidence"][0], snippet="s" * 300000)])
        _, diag, inner = run_provider(reply(json.dumps(VALID)), inputs=big)
        self.assertEqual((diag["failed_at"], len(inner.calls)), ("request_too_large", 0))
        _, diag, inner = run_provider(reply(json.dumps(VALID)), inputs={"findings": object()})
        self.assertEqual((diag["failed_at"], len(inner.calls)), ("request_not_built", 0))

    def test_reset_clears_it_and_the_returned_record_is_a_copy(self):
        p, _ = provider(reply(json.dumps(VALID)))
        self.assertIsNone(p.last_diagnostic)
        p.synthesize(copy.deepcopy(INPUTS))
        copy_ = p.last_diagnostic
        copy_["failed_at"] = "changed"
        copy_["output_problems"].append("changed")
        self.assertIsNone(p.last_diagnostic["failed_at"])
        self.assertEqual(p.last_diagnostic["output_problems"], [])
        p.reset_diagnostic()
        self.assertIsNone(p.last_diagnostic)

    def test_each_call_replaces_the_previous_record(self):
        p, _ = provider((500, b""), reply(json.dumps(VALID)))
        p.synthesize(copy.deepcopy(INPUTS))
        self.assertEqual(p.last_diagnostic["failed_at"], "http_status")
        p.synthesize(copy.deepcopy(INPUTS))
        self.assertIsNone(p.last_diagnostic["failed_at"])
        self.assertEqual(p.last_diagnostic["http_status"], 200)

    def test_results_and_stored_synthesis_are_unchanged_by_the_diagnostic(self):
        result, _, _ = run_provider(reply(json.dumps(VALID)))
        self.assertEqual(result.output, check_synthesis(copy.deepcopy(VALID), INPUTS))
        self.assertIs(result.fake, False)


class TruncationFixTests(NoNetwork):
    """SY-7: the OpenRouter completion-token limit, the shorter prompt and the handling of a reply that is cut off."""

    def test_the_enforced_limits_are_unchanged_and_the_prompt_asks_for_less(self):
        self.assertEqual((synthesis.MAX_STATEMENTS, synthesis.MAX_STATEMENT_CHARS, synthesis.MAX_UNCERTAINTIES,
                          synthesis.MAX_UNCERTAINTY_CHARS, synthesis.MAX_CITES), (8, 400, 6, 300, 6))
        from synthesis_provider import chat
        self.assertLessEqual(chat.PROMPT_STATEMENTS, synthesis.MAX_STATEMENTS)
        self.assertLessEqual(chat.PROMPT_STATEMENT_CHARS, synthesis.MAX_STATEMENT_CHARS)
        self.assertLessEqual(chat.PROMPT_UNCERTAINTIES, synthesis.MAX_UNCERTAINTIES)
        self.assertLessEqual(chat.PROMPT_UNCERTAINTY_CHARS, synthesis.MAX_UNCERTAINTY_CHARS)
        for fragment in ("at most %d statements of at most %d characters" % (chat.PROMPT_STATEMENTS,
                                                                            chat.PROMPT_STATEMENT_CHARS),
                         "single line with no line breaks or tabs", "Do not write web addresses",
                         "allowed_references", "ONE JSON object"):
            self.assertIn(fragment, chat.SYSTEM_PROMPT)

    def test_the_request_carries_the_higher_limit_inside_the_adapters_cap(self):
        _, _, inner = run_provider(reply(json.dumps(VALID)))
        body = json.loads(inner.calls[0].decode("utf-8"))
        self.assertEqual(body["max_tokens"], 2400)
        self.assertTrue(1 <= body["max_tokens"] <= 4096)
        self.assertNotIn("temperature", body)
        self.assertEqual(set(body), {"model", "messages", "max_tokens", "stream"})
        _, _, inner = run_provider(reply(json.dumps(VALID)), max_tokens=300)
        self.assertEqual(json.loads(inner.calls[0].decode("utf-8"))["max_tokens"], 300)
        p, _ = provider(max_tokens=4097)
        self.assertFalse(p.configuration_ok())

    def test_a_complete_reply_of_the_largest_size_the_prompt_asks_for_is_accepted(self):
        from synthesis_provider import chat
        full = {"statements": [{"text": "s" * chat.PROMPT_STATEMENT_CHARS, "cites": [ref]}
                               for ref in ALLOWED[:chat.PROMPT_STATEMENTS]],
                "uncertainties": ["u" * chat.PROMPT_UNCERTAINTY_CHARS] * chat.PROMPT_UNCERTAINTIES}
        result, diag, inner = run_provider(reply(json.dumps(full), "stop", completion_tokens=900))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output, check_synthesis(copy.deepcopy(full), INPUTS))
        self.assertEqual((diag["failed_at"], diag["finish_reason"], len(inner.calls)), (None, "stop", 1))

    def test_a_reply_cut_off_at_the_limit_is_refused_named_and_never_repaired_or_retried(self):
        cut = json.dumps(VALID)[:-12]
        result, diag, inner = run_provider(reply(cut, "length", completion_tokens=2400))
        self.assertEqual((result.status, result.error_code, result.output),
                         ("error", "synthesis_response_malformed", None))
        self.assertEqual((diag["failed_at"], diag["finish_reason"], diag["content_shape"]),
                         ("output_truncated", "length", "prose_or_other"))
        self.assertEqual(len(inner.calls), 1)                       # no retry with a bigger limit, no second request
        self.assertNotIn(CANARY, json.dumps(diag))

    def test_a_complete_valid_object_is_accepted_even_if_the_service_says_length(self):
        result, diag, _ = run_provider(reply(json.dumps(VALID), "length"))
        self.assertEqual(result.status, "ok")                       # validity, not the finish reason, decides
        self.assertIsNone(diag["failed_at"])

    def test_a_complete_but_invalid_object_is_still_refused_by_the_unchanged_validator(self):
        bad = {"statements": [{"text": "two\nlines", "cites": ["E1"]}], "uncertainties": []}
        result, diag, _ = run_provider(reply(json.dumps(bad), "stop"))
        self.assertEqual((result.status, diag["failed_at"]), ("error", "output_invalid"))
        self.assertIn("statement_text_not_printable", diag["output_problems"])

    def test_hidden_reasoning_tokens_are_recorded_only_as_a_bounded_number(self):
        document = {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}],
                    "usage": {"completion_tokens": 800, "completion_tokens_details": {"reasoning_tokens": 600}}}
        _, diag, _ = run_provider((200, json.dumps(document).encode()))
        self.assertEqual((diag["completion_tokens"], diag["reasoning_tokens"]), (800, 600))
        for hidden in (CANARY, -1, True, 10 ** 9, [1], None):
            document["usage"]["completion_tokens_details"]["reasoning_tokens"] = hidden
            _, diag, _ = run_provider((200, json.dumps(document).encode()))
            self.assertIsNone(diag["reasoning_tokens"])
            self.assertNotIn(CANARY, json.dumps(diag))
        _, diag, _ = run_provider(reply(json.dumps(VALID)))
        self.assertIsNone(diag["reasoning_tokens"])


class CleanDiagnosticTests(unittest.TestCase):
    def good(self, **changes):
        base = {"failed_at": None, "http_status": 200, "finish_reason": "stop", "reply_bytes": 10, "content_chars": 5,
                "completion_tokens": 3, "reasoning_tokens": None, "max_tokens": 800, "content_shape": "json_object",
                "output_problems": []}
        base.update(changes)
        return base

    def test_the_expected_shape_passes_and_is_copied(self):
        raw = self.good(output_problems=["cite_repeated"])
        cleaned = clean_diagnostic(raw)
        self.assertEqual(cleaned, raw)
        self.assertIsNot(cleaned["output_problems"], raw["output_problems"])
        self.assertEqual(set(cleaned), set(DIAG_KEYS))

    def test_anything_else_is_dropped(self):
        bad = (None, [], "x", dict(self.good(), extra=1), {k: v for k, v in self.good().items() if k != "failed_at"},
               self.good(failed_at=CANARY), self.good(failed_at="free text"), self.good(http_status=99),
               self.good(http_status=True), self.good(http_status="200"), self.good(finish_reason=CANARY),
               self.good(reply_bytes=-1), self.good(reply_bytes=True), self.good(content_chars=10 ** 12),
               self.good(completion_tokens=1.5), self.good(max_tokens=0), self.good(max_tokens=5000),
               self.good(reasoning_tokens=-1), self.good(reasoning_tokens=True), self.good(reasoning_tokens="3"),
               self.good(content_shape=CANARY), self.good(output_problems="x"), self.good(output_problems=[CANARY]),
               self.good(output_problems=[1]), self.good(output_problems=["cite_repeated"] * 13))
        for raw in bad:
            self.assertIsNone(clean_diagnostic(raw), repr(raw))


class RouteTests(NoNetwork):
    def service(self, *outcomes, **kwargs):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        p, inner = provider(*outcomes, **kwargs)
        return DemoService(store, synthesis_provider=p), p, inner

    def run_it(self, service, synthesis_mode="real"):
        return service.run_integrated({
            "scenario": "normal_operation", "question": "what is visible?", "image_png_base64": PNG_B64,
            "providers": {"image": "fake", "research": "fake", "compute": "skip", "synthesis": synthesis_mode}})

    @staticmethod
    def valid_reply_for(body_holder):
        def make(body):
            message = json.loads(body.decode("utf-8"))["messages"][1]["content"]
            data = json.loads(message.split("\n", 1)[1])
            return reply(json.dumps({"statements": [{"text": "A cautious statement.",
                                                     "cites": [data["allowed_references"][0]]}],
                                     "uncertainties": ["Unclear."]}))
        return make

    def test_a_failed_synthesis_response_carries_the_diagnostic_and_none_of_the_text(self):
        service, _, inner = self.service(reply("Sure! " + CANARY, "length", completion_tokens=800))
        out = self.run_it(service)
        self.assertEqual(out["integrated"]["run_status"], "failed")
        diag = out["synthesis_diagnostic"]
        self.assertEqual((diag["failed_at"], diag["finish_reason"]), ("output_truncated", "length"))
        stage = {s["stage"]: s for s in out["integrated"]["integration"]["stages"]}["synthesis"]
        self.assertEqual((stage["status"], stage["error_code"]), ("failed", "synthesis_response_malformed"))
        self.assertEqual(len(inner.calls), 1)
        text = json.dumps(out)
        for forbidden in (CANARY, KEY, "Bearer"):
            self.assertNotIn(forbidden, text)

    def test_a_successful_synthesis_also_reports_a_clean_diagnostic(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        inner = Scripted()
        make = self.valid_reply_for(None)

        def transport(url, headers, body, timeout):
            inner.calls.append(body)
            return make(body)

        p = OpenRouterSynthesisProvider("anthropic/claude-haiku-5.5", transport=transport,
                                        environ={"OPENROUTER_API_KEY": KEY})
        out = DemoService(store, synthesis_provider=p).run_integrated({
            "scenario": "normal_operation", "question": "what is visible?", "image_png_base64": PNG_B64,
            "providers": {"image": "fake", "research": "fake", "compute": "skip", "synthesis": "real"}})
        stage = {s["stage"]: s for s in out["integrated"]["integration"]["stages"]}["synthesis"]
        self.assertEqual(stage["status"], "completed")           # the run is "partial" only because Compute was skipped
        self.assertEqual(out["integrated"]["synthesis"]["kind"], "model_generated_unverified")
        self.assertIsNone(out["synthesis_diagnostic"]["failed_at"])
        self.assertEqual(len(inner.calls), 1)

    def test_no_diagnostic_when_synthesis_is_not_real_or_was_not_reached(self):
        service, _, inner = self.service(reply(json.dumps(VALID)))
        for mode in ("skip", "fake"):
            out = self.run_it(service, mode)
            self.assertNotIn("synthesis_diagnostic", out)
        self.assertEqual(len(inner.calls), 0)
        out = service.run_integrated({"scenario": "normal_operation", "question": "q",
                                      "providers": {"image": "skip", "research": "fake", "compute": "skip",
                                                    "synthesis": "real"}})
        self.assertNotIn("synthesis_diagnostic", out)       # dependency not met: the provider was never called
        self.assertEqual(len(inner.calls), 0)

    def test_a_previous_runs_diagnostic_is_never_shown_for_a_later_run(self):
        service, p, _ = self.service((500, b""))
        first = self.run_it(service)
        self.assertEqual(first["synthesis_diagnostic"]["failed_at"], "http_status")
        later = service.run_integrated({"scenario": "normal_operation", "question": "q",
                                        "providers": {"image": "skip", "research": "fake", "compute": "skip",
                                                      "synthesis": "real"}})
        self.assertNotIn("synthesis_diagnostic", later)

    def test_a_broken_or_wrong_diagnostic_adds_nothing(self):
        class Broken:
            def synthesize(self, inputs):
                return synthesis.error_result("synthesis_failed", "x")

            @property
            def last_diagnostic(self):
                raise RuntimeError(CANARY)

        class Wrong(Broken):
            @property
            def last_diagnostic(self):
                return {"failed_at": CANARY}

        for kind in (Broken, Wrong):
            store = RecordStore(":memory:")
            self.addCleanup(store.close)
            out = self.run_it(DemoService(store, synthesis_provider=kind()))
            self.assertNotIn("synthesis_diagnostic", out)
            self.assertNotIn(CANARY, json.dumps(out))


if __name__ == "__main__":
    unittest.main()
