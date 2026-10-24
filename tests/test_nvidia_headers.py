"""Offline tests of the NVIDIA transport that also notes `Retry-After` (IA-9) and of the HTTP-error classification.

No test opens a socket: `urllib.request.build_opener` is replaced by a scripted fake opener and socket, DNS and urllib
tripwires are active. A canary string stands in for provider text and header text and must never reach a result. These
tests show that the new transport behaves exactly like the adapter's own transport on scripted replies and failures
and what it records; they do not show what NVIDIA sends.
"""

import base64
import email.message
import io
import json
import socket
import unittest
import urllib.error
from unittest import mock

from demo_app import server
from demo_app.app import DemoApp
from demo_app.server import ObservingTransport, status_kind
from demo_app.service import RETRY_AFTER_MAX_SECONDS, STATUS_KINDS, DemoService
from image_analysis import nvidia
from image_analysis.nvidia import ResponseTooLarge, TransportError, TransportTimeout
from image_analysis.nvidia_headers import RETRY_AFTER_KINDS, HeaderCapturingTransport, parse_retry_after
from storage.record_store import RecordStore

CANARY = "reply-text-canary-3307"
KEY = "nvapi-canary-not-a-real-key-0004"
URL = nvidia.ENDPOINT
HEADERS = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
BODY = b'{"model": "m"}'
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image \x00\x01").decode("ascii")


def message(**headers):
    result = email.message.Message()
    for name, value in headers.items():
        result[name.replace("_", "-")] = value
    return result


class FakeReply:
    """A fake urllib response: a context manager with .status, .headers and a chunked .read()."""

    def __init__(self, status, body, headers=None, fail_read=False):
        self.status, self.headers, self._stream, self._fail = status, headers or message(), io.BytesIO(body), fail_read

    def read(self, size=-1):
        if self._fail:
            raise OSError(CANARY)
        return self._stream.read(size)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def open(self, request, timeout=None):
        self.calls.append((request.full_url, request.get_method(), request.data, dict(request.header_items()), timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def http_error(code, body=b"", headers=None, fail_read=False):
    reply = FakeReply(code, body, headers, fail_read)
    return urllib.error.HTTPError(URL, code, "msg", headers or message(), reply)


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


def run_transport(transport, outcome_factory):
    """(result or exception class, opener) for one call of `transport` against a fresh fake opener."""
    opener = FakeOpener(outcome_factory())
    with mock.patch("urllib.request.build_opener", return_value=opener) as builder:
        try:
            result = transport(URL, dict(HEADERS), BODY, 7.5)
        except BaseException as error:           # the exact class is what is compared
            result = type(error)
    return result, opener, builder


class ParseRetryAfterTests(unittest.TestCase):
    def test_each_form_is_recognised_and_only_its_form_and_seconds_are_returned(self):
        cases = ((message(), "absent", None), (message(Retry_After="30"), "seconds", 30),
                 (message(Retry_After=" 5 "), "seconds", 5), (message(Retry_After="0"), "seconds", 0),
                 (message(Retry_After=str(RETRY_AFTER_MAX_SECONDS)), "seconds", RETRY_AFTER_MAX_SECONDS),
                 (message(Retry_After=str(RETRY_AFTER_MAX_SECONDS + 1)), "unusable", None),
                 (message(Retry_After="-5"), "unusable", None), (message(Retry_After="1e3"), "unusable", None),
                 (message(Retry_After="1234567890"), "unusable", None), (message(Retry_After=CANARY), "unusable", None),
                 (message(Retry_After=""), "unusable", None),
                 (message(Retry_After="Wed, 21 Oct 2026 07:28:00 GMT"), "date", None))
        for headers, kind, seconds in cases:
            with self.subTest(kind=kind, value=headers.get("Retry-After")):
                found = parse_retry_after(headers)
                self.assertEqual(found, {"retry_after": kind, "retry_after_seconds": seconds})
                self.assertIn(found["retry_after"], RETRY_AFTER_KINDS)
                self.assertNotIn(CANARY, json.dumps(found))

    def test_odd_header_objects_never_raise_and_never_leak(self):
        class Raises:
            def get(self, name):
                raise RuntimeError(CANARY)

        class NotText:
            def get(self, name):
                return [CANARY]

        self.assertEqual(parse_retry_after(None), {"retry_after": "absent", "retry_after_seconds": None})
        for headers in (Raises(), NotText()):
            found = parse_retry_after(headers)
            self.assertEqual(found, {"retry_after": "unusable", "retry_after_seconds": None})
        self.assertEqual(RETRY_AFTER_MAX_SECONDS, 86400)


class StatusKindTests(unittest.TestCase):
    def test_every_status_from_400_to_599_has_one_listed_kind(self):
        for status in range(400, 600):
            self.assertIn(status_kind(status), STATUS_KINDS, status)
        self.assertEqual([status_kind(s) for s in (429, 401, 403, 402, 408, 404, 503, 502, 504, 500, 599)],
                         ["rate_limited", "authentication_or_permission", "authentication_or_permission",
                          "payment_required", "request_timeout", "client_error", "server_unavailable",
                          "gateway_error", "gateway_error", "server_error", "server_error"])


class SameAsTheAdaptersTransportTests(NoNetwork):
    """`HeaderCapturingTransport` must give exactly the results and exceptions of `nvidia.urllib_transport`."""

    def outcomes(self):
        big = b"x" * (nvidia._MAX_RESPONSE_BYTES + 10)
        return {
            "ok": lambda: FakeReply(200, b'{"ok": true}'),
            "ok with header": lambda: FakeReply(200, b"{}", message(Retry_After="3")),
            "empty ok": lambda: FakeReply(200, b""),
            "503 with body": lambda: http_error(503, b"ResourceExhausted"),
            "429 with retry-after": lambda: http_error(429, b"slow", message(Retry_After="30")),
            "401": lambda: http_error(401, b"denied"),
            "error body unreadable": lambda: http_error(500, b"x", fail_read=True),
            "error body too large": lambda: http_error(503, big),
            "ok body too large": lambda: FakeReply(200, big),
            "ok body unreadable": lambda: FakeReply(200, b"x", fail_read=True),
            "redirect as http error": lambda: http_error(302, b""),
            "urlerror timeout": lambda: urllib.error.URLError(socket.timeout()),
            "urlerror builtin timeout": lambda: urllib.error.URLError(TimeoutError()),
            "urlerror refused": lambda: urllib.error.URLError(ConnectionRefusedError()),
            "urlerror text": lambda: urllib.error.URLError("no host"),
            "socket timeout": lambda: socket.timeout(),
            "timeout error": lambda: TimeoutError(),
            "os error": lambda: OSError("boom"),
            "connection reset": lambda: ConnectionResetError(),
            "unexpected": lambda: RuntimeError(CANARY),
            "value error": lambda: ValueError("x"),
        }

    def test_results_and_exceptions_are_identical_on_every_scripted_outcome(self):
        for name, factory in self.outcomes().items():
            with self.subTest(outcome=name):
                theirs, their_opener, _ = run_transport(nvidia.urllib_transport, factory)
                ours, our_opener, _ = run_transport(HeaderCapturingTransport(), factory)
                self.assertEqual(ours, theirs)
                self.assertEqual(our_opener.calls, their_opener.calls)
                self.assertEqual(len(our_opener.calls), 1)                        # one request, no retry

    def test_the_request_is_one_post_with_the_same_url_headers_body_and_timeout_and_no_redirect_handler_change(self):
        _, opener, builder = run_transport(HeaderCapturingTransport(), lambda: FakeReply(200, b"{}"))
        (url, method, data, headers, timeout), = opener.calls
        self.assertEqual((url, method, data, timeout), (URL, "POST", BODY, 7.5))
        self.assertEqual(headers["Authorization"], "Bearer " + KEY)
        handlers = builder.call_args.args
        self.assertEqual(handlers, (nvidia._NoRedirect,))                           # redirects are never followed


class RetryAfterCaptureTests(NoNetwork):
    def info_after(self, factory):
        transport = HeaderCapturingTransport()
        run_transport(transport, factory)
        return transport.retry_after_info()

    def test_a_header_on_an_error_reply_or_a_success_reply_is_noted_by_form(self):
        self.assertEqual(self.info_after(lambda: http_error(429, b"", message(Retry_After="30"))),
                         {"retry_after": "seconds", "retry_after_seconds": 30})
        self.assertEqual(self.info_after(lambda: http_error(503, b"")),
                         {"retry_after": "absent", "retry_after_seconds": None})
        self.assertEqual(self.info_after(lambda: http_error(503, b"", message(Retry_After=CANARY)))["retry_after"],
                         "unusable")
        self.assertEqual(self.info_after(lambda: FakeReply(200, b"{}", message(Retry_After="7")))["retry_after"],
                         "seconds")

    def test_no_reply_means_no_note_and_each_call_replaces_the_last(self):
        self.assertIsNone(self.info_after(lambda: TimeoutError()))
        self.assertIsNone(self.info_after(lambda: urllib.error.URLError("x")))
        transport = HeaderCapturingTransport()
        run_transport(transport, lambda: http_error(429, b"", message(Retry_After="9")))
        self.assertEqual(transport.retry_after_info()["retry_after_seconds"], 9)
        run_transport(transport, lambda: TimeoutError())
        self.assertIsNone(transport.retry_after_info())

    def test_the_note_never_holds_the_header_text_and_is_a_copy(self):
        transport = HeaderCapturingTransport()
        run_transport(transport, lambda: http_error(503, b"", message(Retry_After=CANARY)))
        note = transport.retry_after_info()
        self.assertNotIn(CANARY, json.dumps(note))
        note["retry_after"] = "changed"
        self.assertEqual(transport.retry_after_info()["retry_after"], "unusable")


class EndToEndTests(NoNetwork):
    """The real adapter, observer, service and app, with the fake opener as the only network."""

    def app(self, outcome_factory):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        observer = ObservingTransport(HeaderCapturingTransport())
        analyzer = nvidia.NvidiaImageAnalyzer(transport=observer, environ={"NVIDIA_API_KEY": KEY})
        service = DemoService(store, findings_analyzer=analyzer, findings_observer=observer)
        return DemoApp(service, {"127.0.0.1:54321"}), outcome_factory

    def post(self, built):
        app, factory = built
        payload = {"scenario": "normal_operation", "image_png_base64": PNG_B64,
                   "providers": {"image": "real", "research": "skip", "compute": "skip", "synthesis": "skip"}}
        body = json.dumps(payload).encode("utf-8")
        headers = {"host": "127.0.0.1:54321", "x-nelavi-demo": "1", "content-length": str(len(body)),
                   "content-type": "application/json"}
        with mock.patch("urllib.request.build_opener", return_value=FakeOpener(factory())):
            return json.loads(app.handle_request("POST", "/api/run-integrated", headers, io.BytesIO(body).read)
                              .body.decode("utf-8"))

    def test_a_503_with_a_retry_after_header_is_reported_by_status_kind_body_class_and_wait(self):
        out = self.post(self.app(lambda: http_error(
            503, b'{"error": "ResourceExhausted ' + CANARY.encode() + b'"}', message(Retry_After="45"))))
        self.assertEqual(out["findings_observation"]["http_status"], 503)
        self.assertEqual(out["findings_error_class"],
                         {"status_kind": "server_unavailable", "body_class": "resource_exhausted",
                          "body_bytes": out["findings_error_class"]["body_bytes"], "retry_after": "seconds",
                          "retry_after_seconds": 45})
        stage = {s["stage"]: s for s in out["integrated"]["integration"]["stages"]}["image_findings"]
        self.assertEqual((stage["status"], stage["error_code"]), ("failed", "provider_unavailable"))
        text = json.dumps(out)
        for forbidden in (CANARY, KEY, "Bearer", "ResourceExhausted"):
            self.assertNotIn(forbidden, text)

    def test_a_429_is_a_rate_limit_status_and_a_401_an_authentication_one_with_unchanged_adapter_codes(self):
        out = self.post(self.app(lambda: http_error(429, b"slow down")))
        self.assertEqual(out["findings_error_class"]["status_kind"], "rate_limited")
        self.assertEqual(out["findings_error_class"]["retry_after"], "absent")
        stage = {s["stage"]: s for s in out["integrated"]["integration"]["stages"]}["image_findings"]
        self.assertEqual(stage["error_code"], "provider_rejected")      # the adapter's own mapping is unchanged
        out = self.post(self.app(lambda: http_error(401, b"denied")))
        self.assertEqual(out["findings_error_class"]["status_kind"], "authentication_or_permission")

    def test_timeouts_and_connection_failures_stay_transport_failures_with_no_error_class(self):
        for factory, failure in ((lambda: urllib.error.URLError(socket.timeout()), "timeout"),
                                 (lambda: urllib.error.URLError(ConnectionRefusedError()), "connection_failed")):
            out = self.post(self.app(factory))
            self.assertEqual(out["findings_observation"]["failure"], failure)
            self.assertNotIn("findings_error_class", out)

    def test_a_successful_reply_has_no_error_class_and_the_server_default_transport_is_the_new_one(self):
        out = self.post(self.app(lambda: FakeReply(200, json.dumps(
            {"choices": [{"message": {"content": "not findings"}}]}).encode())))
        self.assertNotIn("findings_error_class", out)
        sources = []
        original = server.HeaderCapturingTransport

        def spy():
            sources.append(original())
            return sources[-1]

        fake_http = mock.MagicMock()
        fake_http.return_value.server_address = ("127.0.0.1", 54321)
        fake_http.return_value.serve_forever.side_effect = KeyboardInterrupt
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        store.close = lambda: None
        with mock.patch.dict("os.environ", {"NVIDIA_API_KEY": KEY}), \
                mock.patch.object(server, "HTTPServer", fake_http), \
                mock.patch.object(server, "RecordStore", return_value=store), \
                mock.patch.object(server, "HeaderCapturingTransport", spy):
            code = server.main(["--findings-provider", "nvidia"], stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual((code, len(sources)), (0, 1))


if __name__ == "__main__":
    unittest.main()
