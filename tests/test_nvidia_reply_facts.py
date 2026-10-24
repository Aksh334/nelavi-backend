"""Offline tests of the closed-word reply facts for the image-findings request (IA-11).

Nothing here opens a socket or reads a key from the environment: the "network" is a scripted fake transport and socket,
DNS and urllib tripwires are active. Every reply is synthetic. A canary string stands in for model text and provider
strings and must never reach a fact, a response or the page. The facts change no request, no error code and no
validation: they only describe a reply that the unchanged code has already handled.
"""

import base64
import copy
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import demo_app
from agri_pipeline.findings import MAX_REPLY_CHARS, parse_model_findings
from agri_pipeline.reply_facts import (
    ENVELOPES,
    FACT_KEYS,
    FINDINGS_CHECKS,
    FINISH_REASONS,
    SHAPES,
    clean_reply_facts,
    reply_facts_of,
)
from demo_app.app import DemoApp
from demo_app.server import ObservingTransport
from demo_app.service import DemoService
from image_analysis import nvidia
from storage.record_store import RecordStore

HOST = "127.0.0.1:54321"
ROUTE = "/api/run-integrated"
KEY = "nvapi-canary-not-a-real-key-0006"
CANARY = "model-text-canary-9041"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image \x00\x01\x02\xff"
PNG_B64 = base64.b64encode(PNG).decode("ascii")
GOOD = {
    "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
    "growth_stage": {"candidate": "stage-x", "evidence": ["plant height"], "uncertainty": "high"},
    "symptoms": [{"observation": "brown spots on lower leaves", "possible_interpretations": ["possible-cause-a"]}],
    "needs_more": ["a close-up of the underside of a leaf"],
}
SCRIPT = (Path(demo_app.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")


def envelope(content, finish="stop", usage=None, **extra):
    document = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    if usage is not None:
        document["usage"] = usage
    document.update(extra)
    return json.dumps(document).encode("utf-8")


def raw(document):
    return json.dumps(document).encode("utf-8")


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


class EnvelopeTests(unittest.TestCase):
    def test_each_envelope_branch_has_its_own_word(self):
        cases = (
            (b"<html>" + CANARY.encode() + b"</html>", "not_json"), (b"\xff\xfe\x00", "not_json"), (b"", "not_json"),
            (b"[1, 2]", "not_an_object"), (b'"text"', "not_an_object"), (b"5", "not_an_object"),
            (raw({}), "no_choices"), (raw({"choices": []}), "no_choices"), (raw({"choices": "x"}), "no_choices"),
            (raw({"choices": [5]}), "no_choices"), (raw({"choices": [[]]}), "no_choices"),
            (raw({"choices": [{}]}), "no_message"), (raw({"choices": [{"message": "x"}]}), "no_message"),
            (raw({"choices": [{"message": {}}]}), "content_absent"),
            (raw({"choices": [{"message": {"content": None}}]}), "content_null"),
            (raw({"choices": [{"message": {"content": [CANARY]}}]}), "content_not_text"),
            (raw({"choices": [{"message": {"content": 5}}]}), "content_not_text"),
            (envelope(""), "content_blank"), (envelope("  \n\t "), "content_blank"),
            (envelope(json.dumps(GOOD)), "content_text"), (envelope("prose " + CANARY), "content_text"),
        )
        for body, expected in cases:
            with self.subTest(expected=expected, body=repr(body)[:50]):
                facts = reply_facts_of(body)
                self.assertEqual(facts["envelope"], expected)
                self.assertEqual(set(facts), set(FACT_KEYS))
                self.assertIsNotNone(clean_reply_facts(facts))

    def test_a_non_bytes_body_has_no_facts(self):
        for body in (None, "text", 5, ["x"], {"a": 1}):
            self.assertIsNone(reply_facts_of(body))
        self.assertEqual(reply_facts_of(bytearray(envelope("x")))["envelope"], "content_text")

    def test_text_dependent_facts_exist_only_when_there_is_text(self):
        for body in (b"{", raw({"choices": [{"message": {"content": None}}]}), envelope("")):
            facts = reply_facts_of(body)
            self.assertIsNone(facts["content_shape"])
            self.assertIsNone(facts["findings_check"])


class FindingsStepTests(unittest.TestCase):
    def facts(self, content, **kwargs):
        return reply_facts_of(envelope(content, **kwargs))

    def test_every_findings_step_is_reachable_and_named(self):
        long_text = json.dumps(dict(GOOD, needs_more=["x" * (MAX_REPLY_CHARS + 1)]))
        extra_key = json.dumps(dict(GOOD, diagnosis="confirmed"))
        cases = (
            (json.dumps(GOOD), "json_object", "accepted"),
            ("```json\n" + json.dumps(GOOD) + "\n```", "fenced_json", "accepted"),
            ("```\n" + json.dumps(GOOD) + "\n```", "fenced_json", "accepted"),
            (extra_key, "json_object", "contract_failed"),
            (json.dumps({"crop": {"candidates": []}}), "json_object", "contract_failed"),
            ("{}", "json_object", "contract_failed"),
            (json.dumps(GOOD)[:-15], "other", "not_json"),                     # cut off
            ("Here are the findings: " + json.dumps(GOOD), "other", "not_json"),  # prose before
            (json.dumps(GOOD) + " hope this helps", "other", "not_json"),         # prose after
            ("<think>" + CANARY + "</think>" + json.dumps(GOOD), "other", "not_json"),
            ("```json\n" + json.dumps(GOOD)[:-9] + "\n```", "other", "not_json"),
            ("[1, 2, 3]", "other", "not_an_object"), ('"text"', "other", "not_an_object"), ("5", "other", "not_an_object"),
            (long_text, None, "too_long"),
        )
        for content, shape, check in cases:
            with self.subTest(check=check, shape=shape, text=content[:30]):
                facts = self.facts(content)
                self.assertEqual((facts["envelope"], facts["content_shape"], facts["findings_check"]),
                                 ("content_text", shape, check))
                self.assertIn(facts["findings_check"], FINDINGS_CHECKS)

    def test_the_findings_step_agrees_with_the_real_parser_on_every_reply(self):
        """`accepted` exactly when `parse_model_findings` accepts; the envelope agrees with the adapter's own parser."""
        bodies = [envelope(c) for c in (
            json.dumps(GOOD), "```json\n" + json.dumps(GOOD) + "\n```", "{}", "[]", "x", "  " + json.dumps(GOOD) + "  ",
            json.dumps(dict(GOOD, diagnosis="x")), json.dumps(GOOD)[:-3], "```json\n{}\n```", "```\n```",
            json.dumps(dict(GOOD, needs_more=["a\nb"])), json.dumps(dict(GOOD, needs_more=["x" * 400])),
            json.dumps(dict(GOOD, symptoms=[{"observation": "ok", "possible_interpretations": ["bad (name)"]}])),
            "x" * (MAX_REPLY_CHARS + 1), json.dumps(GOOD) + "\n" * 5)]
        bodies += [raw({"choices": [{"message": {"content": None}}]}), raw({"choices": []}), b"not json", b"[1]",
                   raw({"choices": [{"message": {}}]}), envelope(""), envelope("   "), raw({"choices": [{"message": 5}]}),
                   raw({"choices": [{"message": {"content": 5}}]})]
        for body in bodies:
            with self.subTest(body=repr(body)[:60]):
                facts = reply_facts_of(body)
                text, code = nvidia._parse_content(body)
                self.assertEqual(facts["envelope"] == "content_text", text is not None)
                if code is None:
                    self.assertEqual(facts["findings_check"] == "accepted", parse_model_findings(text) is not None)
                    self.assertEqual(facts["envelope"], "content_text")
                elif code == "response_empty":
                    self.assertEqual(facts["envelope"], "content_blank")
                else:
                    self.assertEqual(code, "response_malformed")
                    self.assertNotIn(facts["envelope"], ("content_text", "content_blank"))


class NumbersAndReasonsTests(unittest.TestCase):
    def test_finish_reasons_are_normalised_to_the_allowlist(self):
        for given, expected in (("stop", "stop"), ("length", "length"), ("content_filter", "content_filter"),
                                ("tool_calls", "tool_calls"), ("error", "error"), (None, "absent"),
                                (CANARY, "other"), ("LENGTH", "other"), ("", "other"), (5, "other"), (["length"], "other"),
                                ("absent", "other")):
            with self.subTest(given=given):
                self.assertEqual(reply_facts_of(envelope("x", finish=given))["finish_reason"], expected)
        facts = reply_facts_of(raw({"choices": [{"message": {"content": "x"}}]}))
        self.assertEqual(facts["finish_reason"], "absent")
        self.assertIn(facts["finish_reason"], FINISH_REASONS)

    def test_token_counts_are_kept_only_as_bounded_whole_numbers(self):
        usage = {"completion_tokens": 800, "completion_tokens_details": {"reasoning_tokens": 650}}
        facts = reply_facts_of(envelope("x", usage=usage))
        self.assertEqual((facts["completion_tokens"], facts["reasoning_tokens"]), (800, 650))
        for bad in (-1, True, 1.5, "800", None, CANARY, [800], 10 ** 8):
            usage = {"completion_tokens": bad, "completion_tokens_details": {"reasoning_tokens": bad}}
            facts = reply_facts_of(envelope("x", usage=usage))
            self.assertEqual((facts["completion_tokens"], facts["reasoning_tokens"]), (None, None), bad)
        for odd in ("x", [], 5, {"completion_tokens_details": 5}):
            facts = reply_facts_of(envelope("x", usage=odd))
            self.assertEqual((facts["completion_tokens"], facts["reasoning_tokens"]), (None, None))
        self.assertIsNone(reply_facts_of(envelope("x"))["completion_tokens"])

    def test_tokens_are_reported_even_when_the_content_is_null_and_truncation_is_not_inferred(self):
        body = raw({"choices": [{"message": {"content": None}, "finish_reason": "length"}],
                    "usage": {"completion_tokens": 1024, "completion_tokens_details": {"reasoning_tokens": 1024}}})
        facts = reply_facts_of(body)
        self.assertEqual((facts["envelope"], facts["finish_reason"], facts["completion_tokens"],
                          facts["reasoning_tokens"]), ("content_null", "length", 1024, 1024))
        # A cut-off looking text with finish reason "stop" is not called truncation; only the shape and step are named.
        facts = reply_facts_of(envelope(json.dumps(GOOD)[:-15], finish="stop"))
        self.assertEqual((facts["finish_reason"], facts["findings_check"]), ("stop", "not_json"))
        for name in ("truncat", "cut", "reasoning_leak", "timeout"):
            self.assertFalse([k for k in facts if name in k])

    def test_the_length_of_the_text_is_reported_but_never_the_text(self):
        facts = reply_facts_of(envelope("abc " + CANARY))
        self.assertEqual(facts["content_chars"], len("abc " + CANARY))
        self.assertEqual(reply_facts_of(envelope(""))["content_chars"], 0)
        self.assertIsNone(reply_facts_of(raw({"choices": [{"message": {"content": None}}]}))["content_chars"])


class NoLeakTests(unittest.TestCase):
    def test_no_string_from_the_reply_can_appear_in_the_facts(self):
        hostile = envelope(CANARY + json.dumps(dict(GOOD, diagnosis=CANARY)), finish=CANARY,
                           usage={"completion_tokens": CANARY, "completion_tokens_details": CANARY, CANARY: CANARY},
                           **{CANARY: CANARY, "model": CANARY, "id": CANARY})
        facts = reply_facts_of(hostile)
        text = json.dumps(facts)
        self.assertNotIn(CANARY, text)
        for key, value in facts.items():
            if key == "findings_rules":    # a list of closed (code, path) pairs: checked in test_nvidia_findings_rules.py
                continue
            self.assertTrue(value is None or isinstance(value, int) or value in
                            ENVELOPES + FINISH_REASONS + SHAPES + FINDINGS_CHECKS, (key, value))
        self.assertEqual(clean_reply_facts(facts), facts)

    def test_the_allowlist_drops_anything_that_is_not_exactly_the_closed_shape(self):
        good = reply_facts_of(envelope(json.dumps(GOOD), usage={"completion_tokens": 5}))
        self.assertEqual(clean_reply_facts(good), good)
        self.assertIsNot(clean_reply_facts(good), good)
        for key, bad in (("envelope", CANARY), ("envelope", None), ("envelope", "content_text "), ("finish_reason", CANARY),
                         ("content_shape", CANARY), ("findings_check", CANARY), ("completion_tokens", -1),
                         ("completion_tokens", True), ("completion_tokens", "5"), ("reasoning_tokens", 10 ** 8),
                         ("content_chars", -1), ("content_chars", 1.5), ("content_chars", CANARY)):
            changed = dict(good, **{key: bad})
            self.assertIsNone(clean_reply_facts(changed), (key, bad))
        for bad in (None, [], "x", dict(good, extra=1), {k: v for k, v in good.items() if k != "envelope"}):
            self.assertIsNone(clean_reply_facts(bad))


class ObserverTests(NoNetwork):
    class Inner:
        def __init__(self, *outcomes):
            self.outcomes, self.calls = list(outcomes), []

        def __call__(self, url, headers, body, timeout):
            self.calls.append(body)
            outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    def observer(self, *outcomes, inspector=reply_facts_of):
        inner = self.Inner(*outcomes)
        observer = ObservingTransport(inner)
        observer.set_reply_inspector(inspector)
        return observer, inner

    def test_facts_exist_only_for_http_success_replies(self):
        for outcome, expected in (((200, envelope(json.dumps(GOOD))), "content_text"), ((204, b""), "not_json"),
                                  ((503, envelope("x")), None), ((429, envelope("x")), None),
                                  ((401, b"denied"), None), (RuntimeError(CANARY), None), (TimeoutError(), None)):
            with self.subTest(outcome=repr(outcome)[:40]):
                observer, _ = self.observer(outcome)
                try:
                    observer(nvidia.ENDPOINT, {}, b"{}", 60.0)
                except Exception:
                    pass
                found = observer.reply_facts()
                self.assertEqual(None if found is None else found["envelope"], expected)

    def test_the_reply_passes_through_unchanged_and_nothing_but_the_facts_is_kept(self):
        body = envelope("secret " + CANARY)
        observer, inner = self.observer((200, body))
        self.assertEqual(observer(nvidia.ENDPOINT, {}, b"{}", 60.0), (200, body))
        self.assertEqual(inner.calls, [b"{}"])
        self.assertNotIn(CANARY, json.dumps(observer.reply_facts()))
        self.assertNotIn(CANARY, json.dumps(observer.snapshot()))
        for value in vars(observer).values():                      # the observer holds no copy of the reply
            self.assertNotEqual(value, body)

    def test_a_missing_or_raising_inspector_changes_nothing(self):
        observer, _ = self.observer((200, envelope("x")), inspector=None)
        self.assertEqual(observer(nvidia.ENDPOINT, {}, b"{}", 60.0)[0], 200)
        self.assertIsNone(observer.reply_facts())

        def explode(body):
            raise RuntimeError(CANARY + body.decode("utf-8", "replace"))

        observer, _ = self.observer((200, envelope("x")), inspector=explode)
        self.assertEqual(observer(nvidia.ENDPOINT, {}, b"{}", 60.0), (200, envelope("x")))
        self.assertIsNone(observer.reply_facts())
        observer.set_reply_inspector("not callable")
        self.assertIsNone(observer._reply_inspector)

    def test_reset_and_the_next_request_clear_the_facts_and_the_value_is_a_copy(self):
        observer, _ = self.observer((200, envelope("x")), (503, b""))
        observer(nvidia.ENDPOINT, {}, b"{}", 60.0)
        copy_ = observer.reply_facts()
        copy_["envelope"] = "changed"
        self.assertEqual(observer.reply_facts()["envelope"], "content_text")
        observer(nvidia.ENDPOINT, {}, b"{}", 60.0)
        self.assertIsNone(observer.reply_facts())
        observer, _ = self.observer((200, envelope("x")))
        observer(nvidia.ENDPOINT, {}, b"{}", 60.0)
        observer.reset()
        self.assertIsNone(observer.reply_facts())

    def test_the_observation_snapshot_keys_are_unchanged(self):
        observer, _ = self.observer((200, envelope("x")))
        self.assertEqual(set(observer.snapshot()),
                         {"request_made", "outcome", "http_status", "failure", "elapsed_seconds", "timeout_seconds",
                          "thinking_off", "thinking_off_applied"})


def build(testcase, outcome, observer=None):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    inner = ObserverTests.Inner(outcome)
    observer = observer if observer is not None else ObservingTransport(inner)
    analyzer = nvidia.NvidiaImageAnalyzer(transport=observer, environ={"NVIDIA_API_KEY": KEY})
    service = DemoService(store, findings_analyzer=analyzer, findings_observer=observer)
    return DemoApp(service, {HOST}), inner


def post(app, image="real"):
    payload = {"scenario": "normal_operation", "providers": {"image": image, "research": "skip", "compute": "skip",
                                                             "synthesis": "skip"}}
    if image != "skip":
        payload["image_png_base64"] = PNG_B64
    body = json.dumps(payload).encode("utf-8")
    headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": str(len(body)),
               "content-type": "application/json"}
    response = app.handle_request("POST", ROUTE, headers, io.BytesIO(body).read)
    return json.loads(response.body.decode("utf-8")), response.body.decode("utf-8")


def image_stage(out):
    return {s["stage"]: s for s in out["integrated"]["integration"]["stages"]}["image_findings"]


class RouteTests(NoNetwork):
    def test_a_valid_reply_completes_and_its_facts_say_so(self):
        app, inner = build(self, (200, envelope(json.dumps(GOOD), usage={"completion_tokens": 200})))
        out, text = post(app)
        self.assertEqual(image_stage(out)["status"], "completed")
        facts = out["findings_reply_facts"]
        self.assertEqual((facts["envelope"], facts["content_shape"], facts["findings_check"], facts["finish_reason"],
                          facts["completion_tokens"]), ("content_text", "json_object", "accepted", "stop", 200))
        self.assertEqual(len(inner.calls), 1)

    def test_each_malformed_branch_keeps_its_public_code_and_gains_a_distinguishing_fact(self):
        cases = (
            ("null content cut at the limit", raw({"choices": [{"message": {"content": None}, "finish_reason": "length"}],
                                                    "usage": {"completion_tokens": 1024}}),
             "response_malformed", ("content_null", None), "length"),
            ("not a completion", b"<html>" + CANARY.encode() + b"</html>", "response_malformed", ("not_json", None), None),
            ("no first choice", raw({"choices": []}), "response_malformed", ("no_choices", None), None),
            ("prose around the JSON", envelope("Sure! " + json.dumps(GOOD)), "response_malformed",
             ("content_text", "not_json"), "stop"),
            ("cut-off JSON", envelope(json.dumps(GOOD)[:-20], finish="length"), "response_malformed",
             ("content_text", "not_json"), "length"),
            ("breaks the contract", envelope(json.dumps(dict(GOOD, diagnosis=CANARY))), "response_malformed",
             ("content_text", "contract_failed"), "stop"),
            ("a list", envelope("[1]"), "response_malformed", ("content_text", "not_an_object"), "stop"),
            ("blank text", envelope("   "), "response_empty", ("content_blank", None), "stop"),
        )
        for name, body, code, (env, check), finish in cases:
            with self.subTest(name=name):
                app, inner = build(self, (200, body))
                out, text = post(app)
                stage = image_stage(out)
                self.assertEqual((stage["status"], stage["error_code"]), ("failed", code))
                facts = out["findings_reply_facts"]
                self.assertEqual((facts["envelope"], facts["findings_check"]), (env, check))
                if finish is not None:
                    self.assertEqual(facts["finish_reason"], finish)
                self.assertEqual(len(inner.calls), 1)                     # no retry
                for forbidden in (CANARY, KEY, "Bearer", "<html>"):
                    self.assertNotIn(forbidden, text)

    def test_error_statuses_and_transport_failures_carry_no_reply_facts(self):
        for outcome in ((503, envelope("x")), (429, b"slow"), (401, b"denied"), TimeoutError(), OSError("x")):
            with self.subTest(outcome=repr(outcome)[:30]):
                app, _ = build(self, outcome)
                out, _ = post(app)
                self.assertNotIn("findings_reply_facts", out)

    def test_it_is_absent_when_the_image_stage_is_not_real(self):
        app, _ = build(self, (200, envelope(json.dumps(GOOD))))
        for mode in ("skip", "fake"):
            out, _ = post(app, image=mode)
            self.assertNotIn("findings_reply_facts", out)

    def test_a_service_without_the_inspector_or_with_a_wrong_or_raising_one_adds_nothing_unsafe(self):
        class Plain:
            def __init__(self, inner):
                self._inner = ObservingTransport(inner)

            def __call__(self, *args):
                return self._inner(*args)

            def reset(self):
                self._inner.reset()

            def snapshot(self):
                return self._inner.snapshot()

        class Wrong(Plain):
            def set_reply_inspector(self, inspector):
                pass

            def reply_facts(self):
                return {"envelope": CANARY}

        class Raising(Plain):
            def set_reply_inspector(self, inspector):
                pass

            def reply_facts(self):
                raise RuntimeError(CANARY)

        for kind in (Plain, Wrong, Raising):
            with self.subTest(kind=kind.__name__):
                app, _ = build(self, None, observer=kind(ObserverTests.Inner((200, envelope(json.dumps(GOOD))))))
                out, text = post(app)
                self.assertNotIn("findings_reply_facts", out)
                self.assertNotIn(CANARY, text)
                self.assertEqual(image_stage(out)["status"], "completed")     # the run itself is unaffected

    def test_the_request_and_error_handling_are_byte_for_byte_those_of_the_adapter_without_the_diagnostic(self):
        with_facts = ObserverTests.Inner((200, envelope("Sure! " + json.dumps(GOOD))))
        app, _ = build(self, None, observer=ObservingTransport(with_facts))
        out, _ = post(app)
        bare = ObserverTests.Inner((200, envelope("Sure! " + json.dumps(GOOD))))
        from agri_pipeline.findings import PromptedFindingsProvider
        result = PromptedFindingsProvider(nvidia.NvidiaImageAnalyzer(
            transport=bare, environ={"NVIDIA_API_KEY": KEY})).analyze_image(PNG)
        self.assertEqual(with_facts.calls, bare.calls)
        self.assertEqual(image_stage(out)["error_code"], result.error_code)   # same public code with and without


class PageSourceTests(unittest.TestCase):
    def function_text(self, name):
        body = SCRIPT[SCRIPT.index("function %s(" % name):]
        return body[:body.index("\n}\n")]

    def test_the_page_shows_only_listed_words_and_validated_numbers(self):
        text = self.function_text("replyFactsLines")
        for fragment in ("hasOwnProperty", "Number.isInteger", "closed categories and numbers only",
                         "Finish reason reported by the service"):
            self.assertIn(fragment, text)
        self.assertNotIn("innerHTML", SCRIPT)
        self.assertIn("findings_reply_facts", self.function_text("renderIntegrated"))
        for category in ENVELOPES + FINDINGS_CHECKS + SHAPES:
            self.assertIn(category + ":", SCRIPT, category)

    def test_the_page_does_not_claim_a_cause(self):
        text = self.function_text("replyFactsLines")
        self.assertIn("do not show why the service answered as it did", text)
        self.assertNotIn("truncated because", text)


if __name__ == "__main__":
    unittest.main()
