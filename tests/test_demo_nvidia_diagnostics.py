"""Offline tests of the NVIDIA image-request diagnostics (IA-7): the observing transport, its path into the integrated
run result and page, and the --findings-timeout / --findings-thinking-off flags.

Nothing here opens a socket or reads a key from the environment: the "network" is a scripted fake transport, and
socket, DNS and urllib tripwires are active. The observer is a diagnostic wrapper only; these tests show what it records
and that it changes nothing else, not that NVIDIA behaves in any particular way.
"""

import base64
import io
import json
import socket
import unittest
from pathlib import Path
from unittest import mock

import demo_app
from demo_app import server
from demo_app.app import DemoApp
from demo_app.fake_findings import FakeFindingsAnalyzer
from demo_app.server import ObservingTransport, parse_findings_timeout
from demo_app.service import OBS_FAILURES, OBS_OUTCOMES, DemoService, clean_observation
from image_analysis import nvidia
from image_analysis.nvidia import NvidiaConfig, ResponseTooLarge, TransportError, TransportTimeout
from storage.record_store import RecordStore

HOST = "127.0.0.1:54321"      # only a Host-header string; the same value the mocked server entry point reports
ROUTE = "/api/run-integrated"
NV_KEY = "nvapi-canary-not-a-real-key-0001"
SECRET_TEXT = "secret-provider-detail-7731"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image \x00\x01\x02\xff"
PNG_B64 = base64.b64encode(PNG).decode("ascii")
GOOD = {
    "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
    "growth_stage": {"candidate": "stage-x", "evidence": ["plant height"], "uncertainty": "high"},
    "symptoms": [{"observation": "brown spots on lower leaves", "possible_interpretations": ["possible-cause-a"]}],
    "needs_more": ["a close-up of the underside of a leaf"],
}
OBSERVATION_KEYS = {"request_made", "outcome", "http_status", "failure", "elapsed_seconds", "timeout_seconds",
                    "thinking_off", "thinking_off_applied"}
SCRIPT = (Path(demo_app.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


class Scripted:
    """A recording inner transport: returns (status, body) or raises, in order; the last outcome repeats."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Clock:
    """Returns the given times in order (the last repeats), so elapsed time is exact and no real time passes."""

    def __init__(self, *times):
        self.times = list(times)
        self.index = 0

    def __call__(self):
        value = self.times[min(self.index, len(self.times) - 1)]
        self.index += 1
        return value


def nv_reply(content):
    return 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


def good_nv():
    return nv_reply(json.dumps(GOOD))


BODY = json.dumps({"model": "m", "max_tokens": 1024, "messages": [{"role": "user", "content": "x"}]}).encode("utf-8")
HEADERS = {"Authorization": "Bearer " + NV_KEY}


def call(observer, body=BODY, timeout=60.0):
    return observer(nvidia.ENDPOINT, dict(HEADERS), body, timeout)


class ObservingTransportTests(NoNetwork):
    def test_a_fresh_observer_says_nothing_was_sent(self):
        o = ObservingTransport(Scripted(nv_reply("x")))
        self.assertEqual(o.snapshot(), {
            "request_made": False, "outcome": "not_sent", "http_status": None, "failure": None,
            "elapsed_seconds": None, "timeout_seconds": None, "thinking_off": False, "thinking_off_applied": False})
        self.assertIsNotNone(clean_observation(o.snapshot()))

    def test_a_response_is_returned_unchanged_and_its_status_and_elapsed_time_are_recorded(self):
        inner = Scripted((200, b"reply-bytes"))
        o = ObservingTransport(inner, clock=Clock(10.0, 11.26))
        self.assertEqual(call(o, timeout=60.0), (200, b"reply-bytes"))
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(inner.calls[0]["body"], BODY)              # the request is passed through byte for byte
        self.assertEqual(inner.calls[0]["headers"], HEADERS)
        self.assertEqual(inner.calls[0]["timeout"], 60.0)
        s = o.snapshot()
        self.assertEqual((s["request_made"], s["outcome"], s["http_status"], s["failure"]),
                         (True, "http_response", 200, None))
        self.assertEqual((s["elapsed_seconds"], s["timeout_seconds"]), (1.3, 60.0))
        self.assertIsNotNone(clean_observation(s))

    def test_an_http_error_status_is_a_response_not_a_transport_failure(self):
        for status in (400, 401, 403, 404, 429, 500, 502, 503):
            with self.subTest(status=status):
                o = ObservingTransport(Scripted((status, b"{}")), clock=Clock(0.0, 1.5))
                self.assertEqual(call(o), (status, b"{}"))          # returned, not raised: the adapter maps it
                s = o.snapshot()
                self.assertEqual((s["outcome"], s["http_status"], s["failure"]), ("http_response", status, None))

    def test_each_failure_class_gets_its_own_category_and_the_same_exception_is_raised_again(self):
        cases = (
            (TransportTimeout(), "timeout"), (TimeoutError(), "timeout"), (socket.timeout(), "timeout"),
            (TransportError(), "connection_failed"), (ConnectionResetError(), "connection_failed"),
            (OSError("x"), "connection_failed"), (ConnectionRefusedError(), "connection_failed"),
            (ResponseTooLarge(), "response_too_large"),
            (RuntimeError("x"), "other_transport_error"), (ValueError("x"), "other_transport_error"),
            (UnicodeEncodeError("latin-1", "Ł", 0, 1, "x"), "other_transport_error"),
        )
        for failure, category in cases:
            with self.subTest(failure=type(failure).__name__, category=category):
                inner = Scripted(failure)
                o = ObservingTransport(inner, clock=Clock(5.0, 65.04))
                with self.assertRaises(type(failure)) as raised:
                    call(o)
                self.assertIs(raised.exception, failure)             # nothing is wrapped, swallowed or replaced
                self.assertEqual(len(inner.calls), 1)                # no retry
                s = o.snapshot()
                self.assertEqual((s["outcome"], s["http_status"], s["failure"], s["elapsed_seconds"]),
                                 ("no_http_response", None, category, 60.0))
                self.assertIsNotNone(clean_observation(s))

    def test_a_response_without_a_valid_status_is_labelled_as_such(self):
        for status in (None, "200", 99, 600, True, 200.0):
            with self.subTest(status=status):
                o = ObservingTransport(Scripted((status, b"")), clock=Clock(0.0, 0.2))
                self.assertEqual(call(o), (status, b""))
                s = o.snapshot()
                self.assertEqual((s["outcome"], s["http_status"], s["failure"]),
                                 ("response_without_valid_status", None, None))
                self.assertIsNotNone(clean_observation(s))

    def test_nothing_from_exceptions_headers_bodies_or_keys_is_stored(self):
        secret = SECRET_TEXT + " " + NV_KEY
        for failure in (TransportError(secret), RuntimeError(secret), TimeoutError(secret)):
            o = ObservingTransport(Scripted(failure), clock=Clock(0.0, 1.0))
            with self.assertRaises(type(failure)):
                call(o, body=BODY + NV_KEY.encode())
            s = o.snapshot()
            self.assertEqual(set(s), OBSERVATION_KEYS)
            text = json.dumps(s)
            for forbidden in (SECRET_TEXT, NV_KEY, "Bearer", "Authorization", "messages", "model"):
                self.assertNotIn(forbidden, text)
        o = ObservingTransport(Scripted((200, SECRET_TEXT.encode())), clock=Clock(0.0, 1.0))
        call(o)
        self.assertNotIn(SECRET_TEXT, json.dumps(o.snapshot()))

    def test_the_clock_cannot_produce_a_negative_or_absurd_elapsed_time(self):
        o = ObservingTransport(Scripted((200, b"")), clock=Clock(100.0, 50.0))
        call(o)
        self.assertEqual(o.snapshot()["elapsed_seconds"], 0.0)
        o = ObservingTransport(Scripted((200, b"")), clock=Clock(0.0, 10 ** 9))
        call(o)
        self.assertEqual(o.snapshot()["elapsed_seconds"], 86400.0)

    def test_an_unusable_timeout_argument_is_recorded_as_unknown(self):
        for timeout in (None, True, float("nan"), float("inf"), -1, "60"):
            o = ObservingTransport(Scripted((200, b"")), clock=Clock(0.0, 1.0))
            call(o, timeout=timeout)
            self.assertIsNone(o.snapshot()["timeout_seconds"], timeout)

    def test_reset_clears_the_previous_request(self):
        o = ObservingTransport(Scripted((503, b"")), clock=Clock(0.0, 1.0))
        call(o)
        self.assertEqual(o.snapshot()["http_status"], 503)
        o.reset()
        self.assertEqual(o.snapshot()["outcome"], "not_sent")

    def test_the_snapshot_is_a_copy(self):
        o = ObservingTransport(Scripted((200, b"")), clock=Clock(0.0, 1.0))
        call(o)
        o.snapshot()["http_status"] = 999
        self.assertEqual(o.snapshot()["http_status"], 200)


class ThinkingOffTests(NoNetwork):
    def test_by_default_the_body_is_passed_on_byte_for_byte(self):
        inner = Scripted((200, b""))
        o = ObservingTransport(inner)
        call(o)
        self.assertEqual(inner.calls[0]["body"], BODY)
        self.assertFalse(o.snapshot()["thinking_off"])
        self.assertFalse(o.snapshot()["thinking_off_applied"])

    def test_opt_in_adds_exactly_the_one_top_level_field_and_changes_nothing_else(self):
        inner = Scripted((200, b""))
        o = ObservingTransport(inner, thinking_off=True)
        call(o)
        sent = json.loads(inner.calls[0]["body"].decode("utf-8"))
        original = json.loads(BODY.decode("utf-8"))
        self.assertEqual(sent.pop("chat_template_kwargs"), {"enable_thinking": False})
        self.assertEqual(sent, original)
        self.assertEqual(inner.calls[0]["headers"], HEADERS)
        s = o.snapshot()
        self.assertEqual((s["thinking_off"], s["thinking_off_applied"]), (True, True))

    def test_a_body_that_cannot_take_the_field_is_passed_on_unchanged_and_reported_as_not_applied(self):
        already = json.dumps({"chat_template_kwargs": {"other": 1}}).encode("utf-8")
        for body in (b"not json", b"[1, 2]", b"\xff\xfe", already):
            inner = Scripted((200, b""))
            o = ObservingTransport(inner, thinking_off=True)
            call(o, body=body)
            self.assertEqual(inner.calls[0]["body"], body)
            s = o.snapshot()
            self.assertEqual((s["thinking_off"], s["thinking_off_applied"]), (True, False))

    def test_through_the_real_adapter_only_the_option_differs(self):
        bodies = {}
        for name, flag in (("default", False), ("off", True)):
            inner = Scripted(good_nv())
            adapter = nvidia.NvidiaImageAnalyzer(transport=ObservingTransport(inner, thinking_off=flag),
                                                 environ={"NVIDIA_API_KEY": NV_KEY})
            from image_analysis.boundary import AnalysisRequest, ImageInput
            self.assertEqual(adapter.analyze(AnalysisRequest(ImageInput(PNG, "image/png"), "prompt")).status, "ok")
            bodies[name] = json.loads(inner.calls[0]["body"].decode("utf-8"))
        self.assertEqual(set(bodies["default"]), {"model", "max_tokens", "messages"})
        extra = dict(bodies["off"])
        self.assertEqual(extra.pop("chat_template_kwargs"), {"enable_thinking": False})
        self.assertEqual(extra, bodies["default"])


class CleanObservationTests(unittest.TestCase):
    def good(self, **changes):
        base = {"request_made": True, "outcome": "http_response", "http_status": 503, "failure": None,
                "elapsed_seconds": 1.5, "timeout_seconds": 60.0, "thinking_off": False, "thinking_off_applied": False}
        base.update(changes)
        return base

    def test_the_expected_shape_passes_and_is_copied(self):
        raw = self.good()
        cleaned = clean_observation(raw)
        self.assertEqual(cleaned, raw)
        self.assertIsNot(cleaned, raw)

    def test_anything_else_is_dropped(self):
        bad = (
            None, [], "text", dict(self.good(), extra="x"),
            {k: v for k, v in self.good().items() if k != "failure"},
            self.good(http_status=True), self.good(http_status=99), self.good(http_status=600),
            self.good(http_status="503"), self.good(http_status=None),                  # response without a status
            self.good(outcome="other"), self.good(outcome=SECRET_TEXT),
            self.good(outcome="no_http_response", http_status=None, failure=None),
            self.good(outcome="no_http_response", http_status=503, failure="timeout"),
            self.good(outcome="no_http_response", http_status=None, failure="a free-text reason"),
            self.good(outcome="no_http_response", http_status=None, failure=SECRET_TEXT),
            self.good(failure="timeout"),                                                # failure beside a status
            self.good(elapsed_seconds=-1), self.good(elapsed_seconds=10 ** 9), self.good(elapsed_seconds="1"),
            self.good(timeout_seconds=True), self.good(timeout_seconds=float("nan")),
            self.good(request_made="yes"), self.good(thinking_off=1), self.good(thinking_off_applied=None),
            self.good(request_made=False),
            self.good(outcome="not_sent", request_made=True, http_status=None),
            self.good(outcome="not_sent", request_made=False, http_status=None, elapsed_seconds=1.0),
        )
        for raw in bad:
            self.assertIsNone(clean_observation(raw), repr(raw))

    def test_every_listed_outcome_and_failure_can_be_expressed(self):
        for failure in OBS_FAILURES:
            raw = self.good(outcome="no_http_response", http_status=None, failure=failure)
            self.assertIsNotNone(clean_observation(raw), failure)
        self.assertEqual(len(OBS_OUTCOMES), 4)
        self.assertIsNotNone(clean_observation(
            self.good(outcome="not_sent", request_made=False, http_status=None, elapsed_seconds=None,
                      timeout_seconds=None)))
        self.assertIsNotNone(clean_observation(self.good(outcome="response_without_valid_status", http_status=None)))


def make(testcase, inner, observer=None, thinking_off=False, clock=None, config=None, environ=None):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    observer = observer if observer is not None else ObservingTransport(inner, thinking_off=thinking_off, clock=clock or Clock(0.0, 1.5))
    analyzer = nvidia.NvidiaImageAnalyzer(config=config, transport=observer,
                                          environ={"NVIDIA_API_KEY": NV_KEY} if environ is None else environ)
    service = DemoService(store, findings_analyzer=analyzer, findings_observer=observer)
    return DemoApp(service, {HOST}), observer


def post(app, payload):
    body = json.dumps(payload).encode("utf-8")
    headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": str(len(body)),
               "content-type": "application/json"}
    return app.handle_request("POST", ROUTE, headers, io.BytesIO(body).read)


def run(app, image="real", png=PNG_B64):
    providers = {"image": image, "research": "skip", "compute": "fake", "synthesis": "skip"}
    payload = {"scenario": "normal_operation", "providers": providers}
    if image != "skip":
        payload["image_png_base64"] = png
    return post(app, payload)


def body_of(response):
    return json.loads(response.body.decode("utf-8"))


def image_stage(body):
    return {s["stage"]: s for s in body["integrated"]["integration"]["stages"]}["image_findings"]


class RunResultTests(NoNetwork):
    def assert_clean(self, response):
        text = response.body.decode("utf-8")
        for secret in (NV_KEY, SECRET_TEXT, "Bearer"):
            self.assertNotIn(secret, text, secret)

    def test_a_successful_real_request_reports_status_200_and_elapsed_time(self):
        app, _ = make(self, Scripted(good_nv()))
        response = run(app)
        self.assertEqual(response.status, 200)
        body = body_of(response)
        self.assertEqual(image_stage(body)["status"], "completed")
        o = body["findings_observation"]
        self.assertEqual(set(o), OBSERVATION_KEYS)
        self.assertEqual((o["request_made"], o["outcome"], o["http_status"], o["failure"], o["elapsed_seconds"],
                          o["timeout_seconds"]), (True, "http_response", 200, None, 1.5, 60.0))
        self.assert_clean(response)

    def test_an_http_error_is_shown_as_an_http_status_and_the_stage_keeps_its_closed_code(self):
        for status, code in ((401, "provider_rejected"), (429, "provider_rejected"), (503, "provider_unavailable")):
            with self.subTest(status=status):
                app, _ = make(self, Scripted((status, SECRET_TEXT.encode())))
                response = run(app)
                body = body_of(response)
                stage = image_stage(body)
                self.assertEqual((stage["status"], stage["error_code"]), ("failed", code))
                o = body["findings_observation"]
                self.assertEqual((o["outcome"], o["http_status"], o["failure"]), ("http_response", status, None))
                self.assert_clean(response)

    def test_a_transport_failure_is_shown_as_no_http_response_with_its_category(self):
        cases = ((TransportTimeout(SECRET_TEXT), "provider_timeout", "timeout"),
                 (TransportError(SECRET_TEXT), "provider_connection_failed", "connection_failed"),
                 (RuntimeError(SECRET_TEXT + NV_KEY), "transport_failed", "other_transport_error"),
                 (ResponseTooLarge(), "response_too_large", "response_too_large"))
        for failure, code, category in cases:
            with self.subTest(code=code):
                app, _ = make(self, Scripted(failure), clock=Clock(0.0, 60.04))
                response = run(app)
                body = body_of(response)
                stage = image_stage(body)
                self.assertEqual((stage["status"], stage["error_code"]), ("failed", code))
                o = body["findings_observation"]
                self.assertEqual((o["outcome"], o["http_status"], o["failure"], o["elapsed_seconds"]),
                                 ("no_http_response", None, category, 60.0))
                self.assertIsNone(o["http_status"])
                self.assert_clean(response)

    def test_the_two_kinds_of_outcome_cannot_be_confused(self):
        http_app, _ = make(self, Scripted((503, b"")))
        failed_app, _ = make(self, Scripted(TransportTimeout()))
        http = body_of(run(http_app))["findings_observation"]
        failed = body_of(run(failed_app))["findings_observation"]
        self.assertIsNotNone(http["http_status"])
        self.assertIsNone(http["failure"])
        self.assertIsNone(failed["http_status"])
        self.assertIsNotNone(failed["failure"])

    def test_a_request_refused_locally_is_reported_as_not_sent(self):
        inner = Scripted(good_nv())
        app, _ = make(self, inner, environ={})                       # no key: the adapter refuses before sending
        body = body_of(run(app))
        self.assertEqual(image_stage(body)["error_code"], "credential_missing")
        o = body["findings_observation"]
        self.assertEqual((o["request_made"], o["outcome"], o["elapsed_seconds"]), (False, "not_sent", None))
        self.assertEqual(inner.calls, [])

    def test_a_configured_timeout_reaches_the_request_and_the_report(self):
        inner = Scripted(good_nv())
        app, _ = make(self, inner, config=NvidiaConfig(timeout_seconds=123.0))
        body = body_of(run(app))
        self.assertEqual(inner.calls[0]["timeout"], 123.0)
        self.assertEqual(body["findings_observation"]["timeout_seconds"], 123.0)

    def test_the_reasoning_off_option_reaches_the_request_and_the_report(self):
        inner = Scripted(good_nv())
        app, _ = make(self, inner, thinking_off=True)
        body = body_of(run(app))
        self.assertEqual(json.loads(inner.calls[0]["body"])["chat_template_kwargs"], {"enable_thinking": False})
        o = body["findings_observation"]
        self.assertEqual((o["thinking_off"], o["thinking_off_applied"]), (True, True))

    def test_each_run_reports_only_its_own_request(self):
        inner = Scripted(TransportTimeout(), good_nv())
        app, _ = make(self, inner, clock=Clock(0.0, 60.0, 60.0, 62.0))
        first = body_of(run(app))["findings_observation"]
        second = body_of(run(app))["findings_observation"]
        self.assertEqual(first["failure"], "timeout")
        self.assertEqual((second["outcome"], second["http_status"], second["failure"]), ("http_response", 200, None))

    def test_fake_and_skipped_image_stages_carry_no_observation(self):
        inner = Scripted(good_nv())
        app, _ = make(self, inner)
        for mode in ("fake", "skip"):
            body = body_of(run(app, image=mode))
            self.assertNotIn("findings_observation", body, mode)
        self.assertEqual(inner.calls, [])

    def test_a_failed_real_stage_is_never_replaced_by_the_fake_analyzer(self):
        app, _ = make(self, Scripted(TransportTimeout()))
        body = body_of(run(app))
        step = {s["name"]: s for s in body["integrated"]["run"]["steps"]}["image_findings"]
        self.assertEqual((step["status"], step["error_code"]), ("failed", "provider_timeout"))
        self.assertIsNone(step.get("result"))

    def test_an_unusable_observer_answer_is_omitted_not_invented(self):
        class Bad:
            def __init__(self, answer):
                self.answer = answer

            def reset(self):
                pass

            def snapshot(self):
                if isinstance(self.answer, Exception):
                    raise self.answer
                return self.answer

        for answer in ({"http_status": 200}, None, "text", RuntimeError(SECRET_TEXT)):
            store = RecordStore(":memory:")
            self.addCleanup(store.close)
            service = DemoService(store, findings_analyzer=FakeFindingsAnalyzer(), findings_observer=Bad(answer))
            response = run(DemoApp(service, {HOST}))
            self.assertEqual(response.status, 200)
            self.assertNotIn("findings_observation", body_of(response))
            self.assertNotIn(SECRET_TEXT, response.body.decode("utf-8"))

    def test_the_service_requires_a_usable_observer(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        for bad in (object(), mock.Mock(spec=["reset"]), mock.Mock(spec=["snapshot"])):
            with self.assertRaises(TypeError):
                DemoService(store, findings_observer=bad)

    def test_default_behaviour_is_unchanged_without_the_flags(self):
        inner = Scripted(good_nv())
        app, _ = make(self, inner)
        run(app)
        call_ = inner.calls[0]
        self.assertEqual((call_["url"], call_["timeout"]), (nvidia.ENDPOINT, nvidia.DEFAULT_TIMEOUT_SECONDS))
        self.assertEqual(set(json.loads(call_["body"])), {"model", "max_tokens", "messages"})


class FlagTests(NoNetwork):
    def run_main(self, argv, key_present=True):
        out, err = io.StringIO(), io.StringIO()
        fake_http = mock.MagicMock()
        fake_http.return_value.server_address = ("127.0.0.1", 54321)
        fake_http.return_value.serve_forever.side_effect = KeyboardInterrupt
        analyzer_class = mock.MagicMock(return_value=FakeFindingsAnalyzer())
        with mock.patch.object(server, "api_key_present", return_value=key_present), \
                mock.patch.object(server, "HTTPServer", fake_http), \
                mock.patch.object(server, "NvidiaImageAnalyzer", analyzer_class), \
                mock.patch.object(server, "DemoService", wraps=DemoService) as service_class:
            code = server.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), fake_http, analyzer_class, service_class

    def test_without_the_flags_the_default_configuration_is_used_and_the_request_is_observed(self):
        code, out, err, _, analyzer_class, service_class = self.run_main(["--findings-provider", "nvidia"])
        self.assertEqual(code, 0)
        kwargs = analyzer_class.call_args.kwargs
        self.assertIsNone(kwargs["config"])                                 # the adapter's own 60 s default
        observer = kwargs["transport"]
        self.assertIsInstance(observer, ObservingTransport)
        self.assertFalse(observer.snapshot()["thinking_off"])
        self.assertIs(service_class.call_args.kwargs["findings_observer"], observer)
        self.assertIn("default (60 seconds)", out)
        self.assertIn("not added (default)", out)

    def test_findings_timeout_sets_the_adapter_timeout(self):
        for text, expected in (("120", 120.0), ("0.5", 0.5), ("300", 300.0), ("1e2", 100.0)):
            with self.subTest(text=text):
                code, out, _, _, analyzer_class, _ = self.run_main(
                    ["--findings-provider", "nvidia", "--findings-timeout", text])
                self.assertEqual(code, 0)
                config = analyzer_class.call_args.kwargs["config"]
                self.assertIsInstance(config, NvidiaConfig)
                self.assertEqual(config.timeout_seconds, expected)
                self.assertTrue(config.is_valid())
                self.assertIn("(--findings-timeout)", out)

    def test_findings_thinking_off_is_opt_in(self):
        code, out, _, _, analyzer_class, _ = self.run_main(["--findings-provider", "nvidia", "--findings-thinking-off"])
        self.assertEqual(code, 0)
        self.assertTrue(analyzer_class.call_args.kwargs["transport"].snapshot()["thinking_off"])
        self.assertIsNone(analyzer_class.call_args.kwargs["config"])        # only the option changes
        self.assertIn("ADDED (--findings-thinking-off)", out)

    def test_invalid_timeouts_are_refused_before_anything_starts(self):
        for text in ("0", "0.0", "-5", "=-inf", "nan", "NaN", "inf", "Infinity", "1e400", "301", "300.1", "abc", "",
                     " ", "1,5", "0x10", "None", "1 2"):
            with self.subTest(text=text):
                argv = ["--findings-provider", "nvidia"]
                argv += ["--findings-timeout" + text] if text.startswith("=") else ["--findings-timeout", text]
                try:
                    code, out, err, fake_http, analyzer_class, service_class = self.run_main(argv)
                except SystemExit as exit_:
                    self.assertEqual(exit_.code, 2)                            # argparse refused it
                    continue
                self.assertEqual(code, 2)
                self.assertIn("--findings-timeout must be", err)
                self.assertIn("Nothing was started", err)
                fake_http.assert_not_called()
                analyzer_class.assert_not_called()
                service_class.assert_not_called()

    def test_the_flags_need_the_nvidia_findings_provider(self):
        for argv in (["--findings-timeout", "30"], ["--findings-thinking-off"],
                     ["--findings-provider", "none", "--findings-timeout", "30"],
                     ["--findings-provider", "none", "--findings-thinking-off"]):
            with self.subTest(argv=argv):
                code, out, err, fake_http, analyzer_class, _ = self.run_main(argv)
                self.assertEqual(code, 2)
                self.assertIn("only apply with --findings-provider nvidia", err)
                fake_http.assert_not_called()
                analyzer_class.assert_not_called()

    def test_without_a_key_the_flags_do_not_start_anything_and_there_is_no_fallback(self):
        code, _, err, fake_http, analyzer_class, _ = self.run_main(
            ["--findings-provider", "nvidia", "--findings-timeout", "30", "--findings-thinking-off"], key_present=False)
        self.assertEqual(code, 2)
        self.assertIn("no fallback", err)
        fake_http.assert_not_called()
        analyzer_class.assert_not_called()

    def test_with_no_findings_provider_nothing_new_is_injected(self):
        code, out, _, _, analyzer_class, service_class = self.run_main([])
        self.assertEqual(code, 0)
        analyzer_class.assert_not_called()
        self.assertNotIn("findings_observer", service_class.call_args.kwargs)

    def test_parse_findings_timeout(self):
        for good, expected in (("1", 1.0), ("0.001", 0.001), ("300", 300.0), (" 45 ", 45.0)):
            self.assertEqual(parse_findings_timeout(good), expected)
        for bad in (None, 5, "0", "-1", "nan", "inf", "-inf", "300.0001", "x", "", "1e999"):
            self.assertIsNone(parse_findings_timeout(bad), repr(bad))


class RealServerWiringTests(NoNetwork):
    """The observation must survive the server entry point's own wiring (not just a hand-built service): `server.main`
    builds the observer, the analyzer and the service, and the response served by that very app must carry it."""

    def build_app(self, flags=()):
        captured = []
        fake_http = mock.MagicMock()
        fake_http.return_value.server_address = ("127.0.0.1", 54321)
        fake_http.return_value.serve_forever.side_effect = KeyboardInterrupt

        def keep(*args, **kwargs):
            captured.append(DemoApp(*args, **kwargs))
            return captured[-1]

        out, err = io.StringIO(), io.StringIO()
        # main() closes its store when serve_forever ends (here: at once); keep it usable for the request below.
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        real_close = store.close
        store.close = lambda: None
        self.addCleanup(setattr, store, "close", real_close)
        with mock.patch.dict("os.environ", {"NVIDIA_API_KEY": NV_KEY}), \
                mock.patch.object(server, "HTTPServer", fake_http), \
                mock.patch.object(server, "RecordStore", return_value=store), \
                mock.patch.object(server, "DemoApp", side_effect=keep):
            code = server.main(["--findings-provider", "nvidia", *flags], stdout=out, stderr=err)
        self.assertEqual((code, err.getvalue()), (0, ""))
        return captured[0]

    def run_with(self, inner, flags=()):
        app = self.build_app(flags)
        # The real default transport is replaced only at the innermost step; the real observer, adapter and service run.
        with mock.patch.dict("os.environ", {"NVIDIA_API_KEY": NV_KEY}), \
                mock.patch.object(server, "HeaderCapturingTransport", lambda: inner):
            return run(app)

    def test_a_successful_real_image_stage_carries_the_observation_through_the_server_wiring(self):
        inner = Scripted(good_nv())
        # The inner transport is built when main() runs, so it must be patched before main() (IA-9: the server's default
        # inner transport is now HeaderCapturingTransport, which mirrors urllib_transport and also notes Retry-After).
        with mock.patch.object(server, "HeaderCapturingTransport", lambda: inner):
            app = self.build_app()
        with mock.patch.dict("os.environ", {"NVIDIA_API_KEY": NV_KEY}):
            response = run(app)
        self.assertEqual(response.status, 200, response.body[:300])
        body = body_of(response)
        self.assertEqual(image_stage(body)["status"], "completed")
        self.assertEqual(len(inner.calls), 1)
        o = body.get("findings_observation")
        self.assertIsNotNone(o, "the real wiring dropped the observation")
        self.assertEqual((o["request_made"], o["outcome"], o["http_status"], o["failure"]),
                         (True, "http_response", 200, None))
        self.assertIsInstance(o["elapsed_seconds"], float)
        self.assertEqual(o["timeout_seconds"], nvidia.DEFAULT_TIMEOUT_SECONDS)

    def test_the_flags_reach_the_request_through_the_server_wiring(self):
        inner = Scripted(good_nv())
        with mock.patch.object(server, "HeaderCapturingTransport", lambda: inner):
            app = self.build_app(["--findings-timeout", "123", "--findings-thinking-off"])
        with mock.patch.dict("os.environ", {"NVIDIA_API_KEY": NV_KEY}):
            body = body_of(run(app))
        self.assertEqual(inner.calls[0]["timeout"], 123.0)
        self.assertEqual(json.loads(inner.calls[0]["body"])["chat_template_kwargs"], {"enable_thinking": False})
        o = body["findings_observation"]
        self.assertEqual((o["timeout_seconds"], o["thinking_off"], o["thinking_off_applied"]), (123.0, True, True))

    def test_the_served_page_script_is_the_one_on_disk_when_the_app_is_built(self):
        """Evidence for a stale-server explanation: the app reads app.js once, when it is built, and keeps serving
        those bytes for as long as the process lives. A server started before the diagnostics were added therefore
        serves a script (and runs Python code) that has neither, whatever is on disk now."""
        path = Path(demo_app.__file__).resolve().parent / "static" / "app.js"
        original = path.read_bytes()
        app = self.build_app()
        served_now = app.handle_request("GET", "/static/app.js", {"host": HOST}, io.BytesIO(b"").read).body
        self.assertEqual(served_now, original)
        self.assertIn(b"NVIDIA image request diagnostics", served_now)
        # Changing the file on disk afterwards does not change what this app serves (no re-read per request).
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("re-read after start")):
            again = app.handle_request("GET", "/static/app.js", {"host": HOST}, io.BytesIO(b"").read).body
        self.assertEqual(again, original)


@unittest.skipUnless(__import__("shutil").which("node"), "node is not installed")
class RenderedPageTests(NoNetwork):
    """The real app.js, run in Node against a minimal fake page, renders the diagnostics section from a real response."""
    HARNESS = Path(__file__).resolve().parent / "js_harness_integrated_result.js"

    def render(self, response_body):
        import shutil
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "response.json"
            path.write_text(json.dumps(response_body), encoding="utf-8")
            result = subprocess.run([shutil.which("node"), str(self.HARNESS), str(Path(demo_app.__file__).resolve().parent
                                                                               / "static" / "app.js"), str(path)],
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_each_outcome_is_rendered_with_its_own_wording(self):
        cases = (
            (Scripted(good_nv()), "An HTTP response was received: status 200"),
            (Scripted((503, b"")), "An HTTP response was received: status 503"),
            (Scripted(TransportTimeout()), "No usable HTTP response: no HTTP response arrived before the time limit"),
            (Scripted(TransportError()), "No usable HTTP response: no HTTP response arrived because the connection failed"),
        )
        for inner, expected in cases:
            with self.subTest(expected=expected):
                app, _ = make(self, inner)
                shown = self.render(body_of(run(app)))
                self.assertTrue(shown["rendered"])
                self.assertFalse(shown["hidden"])
                self.assertIn("NVIDIA image request diagnostics", shown["text"])
                self.assertIn(expected, shown["text"])

    def test_a_locally_refused_request_is_rendered_as_not_sent(self):
        app, _ = make(self, Scripted(good_nv()), environ={})
        shown = self.render(body_of(run(app)))
        self.assertIn("No request reached NVIDIA", shown["text"])

    def test_a_response_without_an_observation_renders_but_has_no_diagnostics_section(self):
        """This is what an older server's response looks like: the run result is shown, the section is not."""
        app, _ = make(self, Scripted(good_nv()))
        body = body_of(run(app))
        del body["findings_observation"]
        shown = self.render(body)
        self.assertTrue(shown["rendered"])
        self.assertNotIn("NVIDIA image request diagnostics", shown["text"])
        self.assertIn("Stages", shown["text"])


class PageSourceTests(unittest.TestCase):
    def function_text(self, name):
        body = SCRIPT[SCRIPT.index("function %s(" % name):]
        return body[:body.index("\n}\n")]

    def test_the_result_shows_the_diagnostics_and_every_failure_category_has_wording(self):
        shown = self.function_text("renderIntegrated")
        self.assertIn("findings_observation", shown)
        self.assertIn("observationLines(", shown)
        block = SCRIPT[SCRIPT.index("const IW_OBS_FAILURE = {"):]
        block = block[:block.index("\n};")]
        for category in OBS_FAILURES:
            self.assertIn(category + ":", block, category)

    def test_http_responses_and_transport_failures_are_worded_differently(self):
        text = self.function_text("observationLines")
        self.assertIn("An HTTP response was received: status", text)
        self.assertIn("No usable HTTP response", text)
        self.assertIn("not an HTTP status", text)
        self.assertIn("refused locally", text)
        self.assertNotIn("innerHTML", SCRIPT)

    def test_a_status_is_not_explained_with_an_invented_cause(self):
        text = self.function_text("observationLines")
        self.assertIn("does not say why", text)


if __name__ == "__main__":
    unittest.main()
