import ast
import base64
import contextlib
import dataclasses
import http.client
import importlib
import inspect
import io
import json
import socket
import ssl
import textwrap
import unittest
import urllib.error
import urllib.request
from unittest import mock

import image_analysis
from image_analysis import (
    ERROR_CODES,
    STATUS_ERROR,
    STATUS_OK,
    AnalysisRequest,
    AnalysisResult,
    ImageInput,
    NvidiaConfig,
    NvidiaImageAnalyzer,
)
from image_analysis import boundary, nvidia
from image_analysis.nvidia import ResponseTooLarge, TransportError, TransportTimeout

FAKE_KEY = "test-only-not-a-real-key"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body-not-a-real-image"
PROMPT = "Describe what is visibly present in this image."
EXPECTED_ERROR_CODES = (
    "credential_missing", "configuration_invalid", "image_unsupported", "provider_rejected",
    "provider_unavailable", "provider_unreachable", "response_malformed", "response_empty",
    "response_too_large", "provider_timeout", "provider_connection_failed", "transport_failed",
)
OPENER_OPEN = "urllib.request.OpenerDirector.open"
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
REDIRECT_REASONS = {301: "Moved Permanently", 302: "Found", 303: "See Other",
                    307: "Temporary Redirect", 308: "Permanent Redirect"}
ORIGINAL_HOST = "original.invalid"
ORIGINAL_URL = "https://original.invalid/v1/chat/completions"
REDIRECT_HOST = "redirect-target.invalid"
REDIRECT_URL = "https://redirect-target.invalid/collect"
REDIRECT_BODY = b'{"moved": "see Location"}'
SOCKET_GUARD_TARGETS = (
    "socket.socket", "socket.create_connection", "socket.getaddrinfo",
    "http.client.HTTPConnection.connect",
)


def ok_body(content="Green leaves with brown edges."):
    # The shape seen in the isolated tests: choices[0].message.content.
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": content,
                                 "reasoning_content": "ignored"}, "finish_reason": "stop"}],
    }).encode("utf-8")


class FakeTransport:
    """Records every call and returns or raises a scripted outcome. Never touches a network."""

    def __init__(self, status=200, body=None, raises=None):
        self.status = status
        self.body = ok_body() if body is None else body
        self.raises = raises
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if self.raises is not None:
            raise self.raises
        return self.status, self.body


class ScriptedStream:
    """A readable body that returns at most `chunk` bytes per read, like a socket giving partial reads.

    Records every requested size and how many bytes it served. Production code catches broad
    exceptions, so a helper that only raised AssertionError could be swallowed and let a test
    pass by accident; every misuse is therefore also written to `violations`, and tests call
    `assert_bounded()` afterwards. A read with no size is always a violation. With `bound=N`, a
    read that could take the total served past N is a violation (the adapter must never ask for
    more than limit + 1 bytes in all). With `endless=True` the data repeats forever. With
    `fail_with`, once `fail_after` bytes have been served the next read raises that exception,
    like a socket timing out in the middle of a body.
    """

    def __init__(self, data, chunk=7, endless=False, bound=None, fail_with=None, fail_after=0):
        self._data = data
        self._pos = 0
        self._chunk = chunk
        self._endless = endless
        self._bound = bound
        self._fail_with = fail_with
        self._fail_after = fail_after
        self.requests = []
        self.violations = []
        self.served = 0
        self.closed = False

    def assert_bounded(self):
        if self.violations:
            raise AssertionError("; ".join(self.violations))

    def read(self, n=-1):
        self.requests.append(n)
        if n is None or n < 0:
            self.violations.append("unbounded read")
            raise AssertionError("unbounded read")
        if self._bound is not None and n > self._bound - self.served:
            self.violations.append(
                "read of %d bytes after %d served could pass the bound of %d"
                % (n, self.served, self._bound))
            raise AssertionError("read could pass the bound")
        if self._fail_with is not None and self.served >= self._fail_after:
            raise self._fail_with
        take = min(n, self._chunk)
        if self._fail_with is not None:
            take = min(take, self._fail_after - self.served)
        if self._endless:
            out = (self._data * (take // len(self._data) + 1))[:take]
        else:
            out = self._data[self._pos:self._pos + take]
            self._pos += len(out)
        self.served += len(out)
        return out

    def close(self):
        self.closed = True


class ScriptedResponse(ScriptedStream):
    """A successful HTTP response as OpenerDirector.open would return it (a context manager)."""

    def __init__(self, data, status=200, **kwargs):
        super().__init__(data, **kwargs)
        self.status = status
        self.exits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.exits += 1
        self.closed = True
        return False


class BytesSocket:
    """Just enough of a socket for http.client.HTTPResponse to parse canned bytes."""

    def __init__(self, data):
        self._data = data

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self._data)


class FakeHttpsServer:
    """Replaces http.client.HTTPSConnection with an in-memory 3xx responder.

    Only the connection is fake. The HTTPSHandler, HTTPErrorProcessor, HTTPRedirectHandler
    and the project's _NoRedirect that sit above it are the real urllib code. Records every
    connection constructed and every request sent. It opens no socket and resolves no name.
    """

    def __init__(self, status, body=REDIRECT_BODY):
        self.status = status
        self.body = body
        self.created = []   # host of every connection constructed
        self.requests = []  # every request sent: host, method, selector, body, headers
        self.guards = []    # the socket/DNS guard mocks active while this server is in use

    def raw_response(self):
        head = ("HTTP/1.1 %d %s\r\nLocation: %s\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (self.status, REDIRECT_REASONS[self.status], REDIRECT_URL, len(self.body)))
        return head.encode("ascii") + self.body

    def connection_class(self):
        server = self

        class FakeHttpsConnection(http.client.HTTPSConnection):
            # Inherits the class-level attributes HTTPSHandler.__init__ reads at build_opener
            # time (debuglevel, _http_vsn, ...). __init__ deliberately skips super().__init__
            # and every method that could connect is overridden, so no socket is ever created.
            debuglevel = 0

            def __init__(self, host, *args, **kwargs):
                self.sock = None
                self._host = host
                server.created.append(host)

            def set_debuglevel(self, level):
                pass

            def set_tunnel(self, *args, **kwargs):
                pass

            def request(self, method, url, body=None, headers=None, **kwargs):
                server.requests.append({
                    "host": self._host, "method": method, "selector": url, "body": body,
                    "headers": {k.lower(): v for k, v in (headers or {}).items()},
                })

            def getresponse(self):
                response = http.client.HTTPResponse(
                    BytesSocket(server.raw_response()), method="POST")
                response.begin()
                return response

            def close(self):
                pass

        return FakeHttpsConnection


def socket_guards(stack):
    """Make socket creation, connection creation and DNS fail loudly; return the mocks."""
    return [stack.enter_context(mock.patch(target, side_effect=AssertionError("network used")))
            for target in SOCKET_GUARD_TARGETS]


def offline_guards(stack):
    """The socket guards plus the stdlib entry points the default transport could use."""
    guards = socket_guards(stack)
    for target in ("urllib.request.urlopen", OPENER_OPEN):
        guards.append(stack.enter_context(
            mock.patch(target, side_effect=AssertionError("default transport used"))))
    return guards


@contextlib.contextmanager
def fake_https(status):
    """Serve a canned 3xx for any HTTPS request, with the network guards on and proxies off."""
    server = FakeHttpsServer(status)
    with contextlib.ExitStack() as stack:
        server.guards = socket_guards(stack)
        stack.enter_context(mock.patch("http.client.HTTPSConnection", server.connection_class()))
        stack.enter_context(mock.patch("urllib.request.getproxies", return_value={}))
        yield server


def request(data=PNG_BYTES, media_type="image/png", prompt=PROMPT):
    return AnalysisRequest(ImageInput(data, media_type), prompt)


def png_of_size(size):
    """Synthetic bytes of exactly `size` bytes that pass the PNG signature check (not an image)."""
    signature = b"\x89PNG\r\n\x1a\n"
    return signature + b"x" * (size - len(signature))


def analyzer(transport=None, environ=None, config=None):
    transport = transport if transport is not None else FakeTransport()
    environ = {"NVIDIA_API_KEY": FAKE_KEY} if environ is None else environ
    return NvidiaImageAnalyzer(config=config, transport=transport, environ=environ), transport


class TestPngValidation(unittest.TestCase):
    def assertRejected(self, req):
        a, t = analyzer()
        result = a.analyze(req)
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_code, "image_unsupported")
        self.assertEqual(t.calls, [])

    def test_valid_png_is_accepted(self):
        a, t = analyzer()
        result = a.analyze(request())
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(len(t.calls), 1)

    def test_wrong_media_type_is_rejected(self):
        self.assertRejected(request(media_type="image/jpeg"))
        self.assertRejected(request(media_type="IMAGE/PNG"))
        self.assertRejected(request(media_type=None))

    def test_invalid_png_signature_is_rejected(self):
        self.assertRejected(request(data=b"\xff\xd8\xff\xe0not-a-png"))
        self.assertRejected(request(data=b"\x89PNG"))  # truncated signature

    def test_empty_image_is_rejected(self):
        self.assertRejected(request(data=b""))

    def test_non_bytes_is_rejected(self):
        self.assertRejected(request(data="\x89PNG\r\n\x1a\n text"))
        self.assertRejected(request(data=bytearray(PNG_BYTES)))
        self.assertRejected(request(data=None))
        self.assertRejected(AnalysisRequest(None, PROMPT))

    def test_non_request_is_a_programmer_error(self):
        a, t = analyzer()
        with self.assertRaises(TypeError):
            a.analyze("not a request")
        self.assertEqual(t.calls, [])


class TestRequestSizeLimit(unittest.TestCase):
    """The local image-size limit: checked before encoding, no new error code."""

    LIMIT = 64

    def analyze(self, size, config=None, **kwargs):
        config = config if config is not None else NvidiaConfig(max_image_bytes=self.LIMIT)
        a, t = analyzer(config=config, **kwargs)
        return a.analyze(request(data=png_of_size(size))), t

    def test_default_limit_is_one_mebibyte(self):
        self.assertEqual(nvidia.DEFAULT_MAX_IMAGE_BYTES, 1048576)
        self.assertEqual(NvidiaConfig().max_image_bytes, 1048576)

    def test_image_exactly_at_the_limit_is_accepted(self):
        result, t = self.analyze(self.LIMIT)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(len(t.calls), 1)

    def test_image_below_the_limit_is_accepted(self):
        result, t = self.analyze(self.LIMIT - 1)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(len(t.calls), 1)

    def test_image_one_byte_over_the_limit_is_unsupported(self):
        result, t = self.analyze(self.LIMIT + 1)
        self.assertEqual((result.status, result.error_code), (STATUS_ERROR, "image_unsupported"))
        self.assertIsNone(result.text)
        self.assertEqual(t.calls, [])

    def test_oversized_image_is_never_encoded_or_sent(self):
        a, t = analyzer(config=NvidiaConfig(max_image_bytes=self.LIMIT))
        with mock.patch.object(nvidia, "_build_body", side_effect=AssertionError("body built")):
            result = a.analyze(request(data=png_of_size(self.LIMIT + 1)))
        self.assertEqual(result.error_code, "image_unsupported")
        self.assertEqual(t.calls, [])

    def test_the_default_limit_is_enforced_at_exactly_one_mebibyte(self):
        at_limit, t = self.analyze(1048576, config=NvidiaConfig())
        self.assertEqual(at_limit.status, STATUS_OK)
        self.assertEqual(len(t.calls), 1)
        over, t = self.analyze(1048577, config=NvidiaConfig())
        self.assertEqual(over.error_code, "image_unsupported")
        self.assertEqual(t.calls, [])

    def test_a_custom_limit_is_honored_in_both_directions(self):
        exact = len(PNG_BYTES)
        a, t = analyzer(config=NvidiaConfig(max_image_bytes=exact))
        self.assertEqual(a.analyze(request()).status, STATUS_OK)
        a, t = analyzer(config=NvidiaConfig(max_image_bytes=exact - 1))
        self.assertEqual(a.analyze(request()).error_code, "image_unsupported")
        self.assertEqual(t.calls, [])
        result, t = self.analyze(500, config=NvidiaConfig(max_image_bytes=1000))
        self.assertEqual(result.status, STATUS_OK)

    def test_the_size_check_comes_after_png_validation_and_before_everything_else(self):
        # An oversized image is reported as such even with no credential and a bad prompt.
        a, t = analyzer(environ={}, config=NvidiaConfig(max_image_bytes=self.LIMIT))
        result = a.analyze(request(data=png_of_size(self.LIMIT + 1), prompt=""))
        self.assertEqual(result.error_code, "image_unsupported")
        self.assertEqual(t.calls, [])

    def test_png_validation_still_applies_within_the_limit(self):
        a, t = analyzer(config=NvidiaConfig(max_image_bytes=self.LIMIT))
        for bad in (b"x" * 10, b"", b"\x89PNG"):
            self.assertEqual(a.analyze(request(data=bad)).error_code, "image_unsupported")
        self.assertEqual(a.analyze(request(media_type="image/jpeg")).error_code,
                         "image_unsupported")
        self.assertEqual(t.calls, [])

    def test_positional_config_arguments_keep_their_order(self):
        config = NvidiaConfig(nvidia.ENDPOINT, nvidia.MODEL, 7, 100)
        self.assertEqual((config.timeout_seconds, config.max_tokens), (7, 100))
        self.assertEqual(config.max_image_bytes, nvidia.DEFAULT_MAX_IMAGE_BYTES)

    def test_oversized_result_leaks_nothing(self):
        result, _ = self.analyze(self.LIMIT + 1)
        text = repr(result) + repr(dataclasses.asdict(result))
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn("Bearer", text)


class TestCredentialAndConfiguration(unittest.TestCase):
    def test_missing_credential(self):
        a, t = analyzer(environ={})
        result = a.analyze(request())
        self.assertEqual((result.status, result.error_code), (STATUS_ERROR, "credential_missing"))
        self.assertEqual(t.calls, [])

    def test_empty_or_unusable_credential(self):
        for value in ("", "   ", None, 123, "bad\nkey", "bad\rkey"):
            a, t = analyzer(environ={"NVIDIA_API_KEY": value})
            result = a.analyze(request())
            self.assertEqual(result.error_code, "credential_missing", repr(value))
            self.assertEqual(t.calls, [])

    def test_credential_is_read_from_the_process_environment_when_not_injected(self):
        t = FakeTransport()
        a = NvidiaImageAnalyzer(transport=t)
        with mock.patch.dict("os.environ", {"NVIDIA_API_KEY": FAKE_KEY}):
            self.assertEqual(a.analyze(request()).status, STATUS_OK)
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(a.analyze(request()).error_code, "credential_missing")

    def test_invalid_configuration(self):
        bad = (
            NvidiaConfig(endpoint="http://insecure.example/v1"),
            NvidiaConfig(endpoint=""),
            NvidiaConfig(endpoint=None),
            NvidiaConfig(model=""),
            NvidiaConfig(model=7),
            NvidiaConfig(timeout_seconds=0),
            NvidiaConfig(timeout_seconds=-1),
            NvidiaConfig(timeout_seconds=True),
            NvidiaConfig(timeout_seconds="30"),
            NvidiaConfig(max_tokens=0),
            NvidiaConfig(max_tokens=1.5),
            NvidiaConfig(max_image_bytes=0),
            NvidiaConfig(max_image_bytes=-1),
            NvidiaConfig(max_image_bytes=True),
            NvidiaConfig(max_image_bytes=1.5),
            NvidiaConfig(max_image_bytes="10"),
            NvidiaConfig(max_image_bytes=None),
            "not a config",
        )
        for config in bad:
            a, t = analyzer(config=config)
            result = a.analyze(request())
            self.assertEqual(result.error_code, "configuration_invalid", repr(config))
            self.assertEqual(t.calls, [])

    def test_unusable_prompt(self):
        for prompt in ("", "   ", None, 5):
            a, t = analyzer()
            result = a.analyze(request(prompt=prompt))
            self.assertEqual(result.error_code, "configuration_invalid", repr(prompt))
            self.assertEqual(t.calls, [])


class TestRequestConstruction(unittest.TestCase):
    def setUp(self):
        self.analyzer, self.transport = analyzer()
        self.analyzer.analyze(request())
        self.url, self.headers, self.body, self.timeout = self.transport.calls[0]
        self.payload = json.loads(self.body.decode("utf-8"))

    def test_endpoint(self):
        self.assertEqual(self.url, "https://integrate.api.nvidia.com/v1/chat/completions")

    def test_model(self):
        self.assertEqual(self.payload["model"], "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")

    def test_authorization_header_is_a_bearer_header_and_the_body_does_not_hold_the_key(self):
        self.assertEqual(self.headers["Authorization"], "Bearer " + FAKE_KEY)
        self.assertEqual(self.headers["Content-Type"], "application/json")
        self.assertNotIn(FAKE_KEY.encode("utf-8"), self.body)

    def test_message_has_a_text_item_and_a_png_data_url_item(self):
        (message,) = self.payload["messages"]
        self.assertEqual(message["role"], "user")
        text_item, image_item = message["content"]
        self.assertEqual(text_item, {"type": "text", "text": PROMPT})
        self.assertEqual(image_item["type"], "image_url")
        expected = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        self.assertEqual(image_item["image_url"], {"url": expected})

    def test_caller_prompt_is_sent_unchanged(self):
        odd = "  Prompt with  spaces,\nnewlines and \"quotes\" é  "
        a, t = analyzer()
        a.analyze(request(prompt=odd))
        sent = json.loads(t.calls[0][2].decode("utf-8"))
        self.assertEqual(sent["messages"][0]["content"][0]["text"], odd)

    def test_timeout_is_passed_to_the_transport(self):
        self.assertEqual(self.timeout, nvidia.DEFAULT_TIMEOUT_SECONDS)
        a, t = analyzer(config=NvidiaConfig(timeout_seconds=7))
        a.analyze(request())
        self.assertEqual(t.calls[0][3], 7)

    def test_request_is_json_with_only_the_expected_keys(self):
        self.assertEqual(set(self.payload), {"model", "max_tokens", "messages"})
        self.assertEqual(self.payload["max_tokens"], 1024)


class TestResponseParsing(unittest.TestCase):
    def run_with(self, status=200, body=None):
        a, t = analyzer(transport=FakeTransport(status, body))
        return a.analyze(request()), t

    def test_successful_response(self):
        result, t = self.run_with(body=ok_body("A close-up of leaves."))
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.text, "A close-up of leaves.")
        self.assertIsNone(result.error_code)
        self.assertEqual(result.model, "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
        self.assertEqual(len(t.calls), 1)

    def test_text_is_returned_unchanged(self):
        result, _ = self.run_with(body=ok_body("  spaced \n text  "))
        self.assertEqual(result.text, "  spaced \n text  ")

    def test_only_choices_message_content_is_used(self):
        body = json.dumps({
            "choices": [{"message": {"content": "answer", "reasoning_content": "thoughts"}}],
            "usage": {"x": 1}, "nvext": {}, "service_tier": "t", "system_fingerprint": "f",
        }).encode("utf-8")
        result, _ = self.run_with(body=body)
        self.assertEqual(result.text, "answer")
        self.assertNotIn("thoughts", repr(result))

    def test_reasoning_content_alone_is_not_an_answer(self):
        body = json.dumps({"choices": [{"message": {"reasoning_content": "thoughts"}}]}).encode()
        result, _ = self.run_with(body=body)
        self.assertEqual(result.error_code, "response_malformed")

    def test_empty_content_is_response_empty(self):
        for content in ("", "   \n"):
            result, _ = self.run_with(body=ok_body(content))
            self.assertEqual((result.status, result.error_code), (STATUS_ERROR, "response_empty"))
            self.assertIsNone(result.text)

    def test_malformed_responses(self):
        cases = {
            "not json": b"<html>nope</html>",
            "invalid utf-8": b"\xff\xfe\x00",
            "empty body": b"",
            "json list": b"[]",
            "json string": b'"text"',
            "no choices": b"{}",
            "choices not a list": b'{"choices": {}}',
            "empty choices": b'{"choices": []}',
            "choice not a dict": b'{"choices": ["x"]}',
            "no message": b'{"choices": [{}]}',
            "message not a dict": b'{"choices": [{"message": "x"}]}',
            "no content": b'{"choices": [{"message": {}}]}',
            "content not a string": b'{"choices": [{"message": {"content": 5}}]}',
            "content null": b'{"choices": [{"message": {"content": null}}]}',
            "content a list": b'{"choices": [{"message": {"content": ["a"]}}]}',
        }
        for name, body in cases.items():
            result, _ = self.run_with(body=body)
            self.assertEqual(result.error_code, "response_malformed", name)
            self.assertIsNone(result.text, name)

    def test_non_bytes_body_or_status_is_malformed(self):
        self.assertEqual(self.run_with(body="text")[0].error_code, "response_malformed")
        self.assertEqual(self.run_with(status="200")[0].error_code, "response_malformed")
        self.assertEqual(self.run_with(status=None)[0].error_code, "response_malformed")
        self.assertEqual(self.run_with(status=True)[0].error_code, "response_malformed")

    def test_unexpected_non_error_status_is_malformed(self):
        for status in (100, 301, 302, 199, 600):
            self.assertEqual(self.run_with(status=status)[0].error_code,
                             "response_malformed", status)


class TestErrorHandling(unittest.TestCase):
    def test_4xx_is_provider_rejected(self):
        for status in (400, 401, 403, 404, 422, 429, 499):
            a, t = analyzer(transport=FakeTransport(status, b'{"error": "secret detail"}'))
            result = a.analyze(request())
            self.assertEqual((result.status, result.error_code), (STATUS_ERROR, "provider_rejected"))
            self.assertEqual(len(t.calls), 1)

    def test_5xx_is_provider_unavailable(self):
        for status in (500, 502, 503, 504, 599):
            a, t = analyzer(transport=FakeTransport(status, b'{"error": "secret detail"}'))
            result = a.analyze(request())
            self.assertEqual((result.status, result.error_code),
                             (STATUS_ERROR, "provider_unavailable"))
            self.assertEqual(len(t.calls), 1)

    def test_resource_exhausted_503_is_reported_not_retried(self):
        body = (b'{"error":{"message":"ResourceExhausted: Worker local total request limit '
                b'reached (16/16)","type":"Service Unavailable","code":503}}')
        a, t = analyzer(transport=FakeTransport(503, body))
        result = a.analyze(request())
        self.assertEqual(result.error_code, "provider_unavailable")
        self.assertEqual(len(t.calls), 1)
        self.assertNotIn("ResourceExhausted", repr(result))

    def test_connection_failure_is_provider_connection_failed(self):
        for failure in (TransportError(), ConnectionError("x"), OSError("x"),
                        ConnectionResetError(), ssl.SSLError("x"), socket.gaierror("x")):
            a, t = analyzer(transport=FakeTransport(raises=failure))
            result = a.analyze(request())
            self.assertEqual((result.status, result.error_code),
                             (STATUS_ERROR, "provider_connection_failed"), repr(failure))
            self.assertEqual(len(t.calls), 1)

    def test_other_transport_exceptions_are_transport_failed(self):
        for failure in (RuntimeError("x"), ValueError("x"), TypeError("x"), KeyError("x"),
                        UnicodeEncodeError("latin-1", "Ł", 0, 1, "x"),
                        http.client.IncompleteRead(b""), http.client.BadStatusLine("x"),
                        Exception("x")):
            a, t = analyzer(transport=FakeTransport(raises=failure))
            result = a.analyze(request())
            self.assertEqual((result.status, result.error_code),
                             (STATUS_ERROR, "transport_failed"), repr(failure))
            self.assertEqual(len(t.calls), 1)

    def test_timeout_is_provider_timeout(self):
        for failure in (TransportTimeout(), TimeoutError(), socket.timeout()):
            a, t = analyzer(transport=FakeTransport(raises=failure))
            self.assertEqual(a.analyze(request()).error_code, "provider_timeout")
            self.assertEqual(len(t.calls), 1)

    def test_the_three_diagnostic_codes_are_distinct_from_each_other_and_from_http_codes(self):
        codes = {"provider_timeout", "provider_connection_failed", "transport_failed"}
        self.assertEqual(len(codes), 3)
        self.assertTrue(codes <= set(ERROR_CODES))
        self.assertTrue(codes.isdisjoint({"provider_rejected", "provider_unavailable",
                                          "response_malformed", "response_empty",
                                          "response_too_large"}))

    def test_no_diagnostic_result_carries_exception_text_or_request_data(self):
        secret = "SECRET-" + FAKE_KEY
        for failure in (TransportTimeout(secret), TimeoutError(secret), TransportError(secret),
                        ConnectionResetError(secret), RuntimeError(secret), ValueError(secret),
                        UnicodeEncodeError("latin-1", secret, 0, 1, secret)):
            a, t = analyzer(transport=FakeTransport(raises=failure))
            result = a.analyze(request())
            text = repr(result) + repr(dataclasses.asdict(result))
            for forbidden in (secret, FAKE_KEY, "Bearer", "Authorization", "SECRET"):
                self.assertNotIn(forbidden, text, repr(type(failure)))
            self.assertIsNone(result.text)

    def test_the_legacy_unreachable_code_is_still_in_the_closed_set_but_never_produced_here(self):
        self.assertIn("provider_unreachable", ERROR_CODES)
        self.assertIn("provider_unreachable", boundary.ERROR_CODES)
        every_failure = (TransportTimeout(), TimeoutError(), TransportError(), OSError("x"),
                         RuntimeError("x"), ResponseTooLarge())
        for failure in every_failure:
            a, _ = analyzer(transport=FakeTransport(raises=failure))
            self.assertNotEqual(a.analyze(request()).error_code, "provider_unreachable")

    def test_error_codes_are_the_closed_set(self):
        self.assertEqual(tuple(ERROR_CODES), EXPECTED_ERROR_CODES)
        self.assertEqual(boundary.ERROR_CODES, ERROR_CODES)


class TestNoRetry(unittest.TestCase):
    def test_every_failure_makes_exactly_one_transport_call(self):
        outcomes = [
            FakeTransport(503, b"{}"), FakeTransport(429, b"{}"), FakeTransport(200, b"junk"),
            FakeTransport(200, ok_body("")), FakeTransport(raises=TransportTimeout()),
            FakeTransport(raises=TransportError()),
        ]
        for transport in outcomes:
            a, _ = analyzer(transport=transport)
            with mock.patch("time.sleep", side_effect=AssertionError("sleep/backoff used")):
                a.analyze(request())
            self.assertEqual(len(transport.calls), 1)

    def test_a_second_call_is_a_new_request_made_only_when_the_caller_asks(self):
        a, t = analyzer(transport=FakeTransport(503, b"{}"))
        a.analyze(request())
        self.assertEqual(len(t.calls), 1)
        a.analyze(request())
        self.assertEqual(len(t.calls), 2)

    def test_the_adapter_contains_no_loops(self):
        tree = ast.parse(inspect.getsource(nvidia.NvidiaImageAnalyzer))
        loops = [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While, ast.AsyncFor))]
        self.assertEqual(loops, [])


class TestDefaultTransportWithoutNetwork(unittest.TestCase):
    """The default transport is exercised with OpenerDirector.open replaced (the call it
    makes, since it builds its own no-redirect opener); no socket is opened."""

    def call(self):
        return nvidia.urllib_transport("https://example.invalid/v1", {"A": "b"}, b"{}", 5)

    def test_success_returns_status_and_body(self):
        response = mock.MagicMock()
        response.status = 200
        # A realistic stream: the body, then end-of-data. The transport now reads in a loop until
        # the stream is exhausted, so a mock that returns the same bytes forever is not a stream.
        response.read.side_effect = io.BytesIO(b"{}").read
        response.__enter__.return_value = response
        with mock.patch(OPENER_OPEN, return_value=response) as opener_open:
            self.assertEqual(self.call(), (200, b"{}"))
        self.assertEqual(opener_open.call_args.kwargs["timeout"], 5)
        sent = opener_open.call_args.args[0]
        self.assertEqual(sent.get_method(), "POST")

    def test_http_error_returns_status(self):
        error = urllib.error.HTTPError("u", 503, "m", {}, io.BytesIO(b"body"))
        # The transport now closes the error itself; this second close is harmless and keeps
        # the test from leaking if the transport ever stops closing. Close counts are asserted
        # in the dedicated cleanup tests below, not here.
        self.addCleanup(error.close)
        with mock.patch(OPENER_OPEN, side_effect=error):
            self.assertEqual(self.call(), (503, b"body"))

    def call_with_http_error(self, error):
        with mock.patch(OPENER_OPEN, side_effect=error):
            return self.call()

    def test_http_error_is_closed_once_after_its_body_is_read(self):
        error = urllib.error.HTTPError("u", 503, "m", {}, io.BytesIO(b"body"))
        with mock.patch.object(error, "close", wraps=error.close) as close:
            self.assertEqual(self.call_with_http_error(error), (503, b"body"))
        self.assertEqual(close.call_count, 1)

    def test_http_error_is_closed_once_when_reading_its_body_raises(self):
        error = urllib.error.HTTPError("u", 503, "m", {}, io.BytesIO(b"body"))
        with mock.patch.object(error, "read", side_effect=OSError("read failed")), \
                mock.patch.object(error, "close", wraps=error.close) as close:
            self.assertEqual(self.call_with_http_error(error), (503, b""))
        self.assertEqual(close.call_count, 1)

    def test_a_failing_close_does_not_replace_the_result(self):
        for read_raises, expected in ((False, (503, b"body")), (True, (503, b""))):
            error = urllib.error.HTTPError("u", 503, "m", {}, io.BytesIO(b"body"))
            self.addCleanup(error.close)
            read = mock.patch.object(error, "read", side_effect=OSError("read failed")) \
                if read_raises else contextlib.nullcontext()
            with read, mock.patch.object(error, "close", side_effect=OSError("close failed")) as close:
                self.assertEqual(self.call_with_http_error(error), expected, read_raises)
            self.assertEqual(close.call_count, 1)

    def test_timeouts_and_connection_failures_become_transport_errors(self):
        cases = [
            (socket.timeout(), TransportTimeout),
            (TimeoutError(), TransportTimeout),
            (urllib.error.URLError(socket.timeout()), TransportTimeout),
            (urllib.error.URLError("refused"), TransportError),
            (ConnectionResetError(), TransportError),
        ]
        for failure, expected in cases:
            with mock.patch(OPENER_OPEN, side_effect=failure):
                with self.assertRaises(expected):
                    self.call()

    def test_transport_errors_carry_no_detail(self):
        with mock.patch(OPENER_OPEN, side_effect=urllib.error.URLError("Bearer " + FAKE_KEY)):
            with self.assertRaises(TransportError) as context:
                self.call()
        self.assertNotIn(FAKE_KEY, str(context.exception) + repr(context.exception))


class TestTransportDiagnostics(unittest.TestCase):
    """IA-6: the default transport and the analyzer tell timeout, connection failure and other
    transport exceptions apart by exception class only. Nothing here opens a socket."""

    def analyze_over_default_transport(self, opener_error, key=FAKE_KEY):
        a = NvidiaImageAnalyzer(environ={"NVIDIA_API_KEY": key})
        with contextlib.ExitStack() as stack:
            guards = socket_guards(stack)
            opener_open = stack.enter_context(mock.patch(OPENER_OPEN, side_effect=opener_error))
            result = a.analyze(request())
        for guard in guards:
            self.assertEqual(guard.call_count, 0)
        return result, opener_open

    def test_each_default_transport_failure_maps_to_its_own_code(self):
        cases = (
            (socket.timeout(), "provider_timeout"),
            (TimeoutError(), "provider_timeout"),
            (urllib.error.URLError(socket.timeout()), "provider_timeout"),
            (urllib.error.URLError("refused"), "provider_connection_failed"),
            (urllib.error.URLError(ssl.SSLError("x")), "provider_connection_failed"),
            (ConnectionResetError(), "provider_connection_failed"),
            (socket.gaierror("x"), "provider_connection_failed"),
            (http.client.IncompleteRead(b""), "transport_failed"),
            (RuntimeError("x"), "transport_failed"),
        )
        for failure, expected in cases:
            with self.subTest(failure=type(failure).__name__, expected=expected):
                result, opener_open = self.analyze_over_default_transport(failure)
                self.assertEqual((result.status, result.error_code), (STATUS_ERROR, expected))
                self.assertEqual(opener_open.call_count, 1)  # no retry

    def test_a_key_that_cannot_be_encoded_in_a_header_is_transport_failed_and_sends_nothing(self):
        key = "key-with-non-latin-1-Ł"
        self.assertTrue(nvidia._key_is_usable(key))  # the adapter's own check lets it through
        a = NvidiaImageAnalyzer(environ={"NVIDIA_API_KEY": key})
        with contextlib.ExitStack() as stack:
            guards = socket_guards(stack)
            result = a.analyze(request())  # the real urllib transport; headers are encoded first
        self.assertEqual((result.status, result.error_code), (STATUS_ERROR, "transport_failed"))
        # A guard that fired would also have been swallowed into an error code, so the counts
        # are what show the failure happened before any socket, connection or DNS use.
        for guard in guards:
            self.assertEqual(guard.call_count, 0)
        text = repr(result) + repr(dataclasses.asdict(result))
        for forbidden in (key, "Ł", "Bearer", "Authorization", "latin-1", "codec"):
            self.assertNotIn(forbidden, text)

    def test_http_responses_the_oversize_condition_and_local_refusals_keep_their_codes(self):
        statuses = ((401, "provider_rejected"), (429, "provider_rejected"),
                    (503, "provider_unavailable"), (302, "response_malformed"))
        for status, expected in statuses:
            a, t = analyzer(transport=FakeTransport(status, b"{}"))
            self.assertEqual(a.analyze(request()).error_code, expected)
        a, _ = analyzer(transport=FakeTransport(raises=ResponseTooLarge()))
        self.assertEqual(a.analyze(request()).error_code, "response_too_large")
        a, t = analyzer(environ={})
        self.assertEqual(a.analyze(request()).error_code, "credential_missing")
        self.assertEqual(t.calls, [])

    def test_an_http_error_whose_body_cannot_be_read_is_not_a_connection_failure(self):
        error = urllib.error.HTTPError("u", 503, "m", {}, io.BytesIO(b"x"))
        self.addCleanup(error.close)
        with mock.patch.object(error, "read", side_effect=TimeoutError()):
            result, _ = self.analyze_over_default_transport(error)
        self.assertEqual(result.error_code, "provider_unavailable")

    def test_every_diagnostic_failure_makes_exactly_one_call_and_never_sleeps(self):
        for failure in (TransportTimeout(), TransportError(), RuntimeError("x")):
            transport = FakeTransport(raises=failure)
            a, _ = analyzer(transport=transport)
            with mock.patch("time.sleep", side_effect=AssertionError("sleep/backoff used")):
                a.analyze(request())
            self.assertEqual(len(transport.calls), 1)

    def test_the_classification_reads_only_the_exception_class(self):
        # The handlers must not bind or inspect the exception object.
        tree = ast.parse(textwrap.dedent(inspect.getsource(nvidia.NvidiaImageAnalyzer.analyze)))
        handlers = [h for n in ast.walk(tree) if isinstance(n, ast.Try) for h in n.handlers]
        self.assertGreaterEqual(len(handlers), 4)
        for handler in handlers:
            self.assertIsNone(handler.name)


class TestResponseSizeLimit(unittest.TestCase):
    """The local 4 MiB response limit: bounded reads, partial-read safe, success and HTTP error bodies.

    Most tests patch the limit down to a small number so they stay fast; two use the real
    4 MiB constant. Everything runs through the real analyzer and default transport with only
    OpenerDirector.open replaced, and the socket and DNS guards on.
    """

    LIMIT = 100

    def limit(self, n=None):
        return mock.patch.object(nvidia, "_MAX_RESPONSE_BYTES", self.LIMIT if n is None else n)

    def analyze_over_default_transport(self, opener_result=None, opener_error=None):
        a = NvidiaImageAnalyzer(environ={"NVIDIA_API_KEY": FAKE_KEY})
        with contextlib.ExitStack() as stack:
            guards = socket_guards(stack)
            opener_open = stack.enter_context(
                mock.patch(OPENER_OPEN, return_value=opener_result, side_effect=opener_error))
            result = a.analyze(request())
        for guard in guards:
            self.assertEqual(guard.call_count, 0)
        return result, opener_open

    def http_error(self, status, stream):
        error = urllib.error.HTTPError("u", status, "m", {}, stream)
        self.addCleanup(error.close)
        return error

    def test_the_limit_is_exactly_four_mebibytes(self):
        self.assertEqual(nvidia._MAX_RESPONSE_BYTES, 4 * 1024 * 1024)

    def test_a_body_exactly_at_the_real_limit_is_accepted(self):
        size = 4 * 1024 * 1024
        stream = ScriptedStream(b"x" * size, chunk=64 * 1024, bound=size + 1)
        self.assertEqual(len(nvidia._read_limited(stream, size)), size)
        stream.assert_bounded()

    def test_a_body_one_byte_over_the_real_limit_is_detected(self):
        size = 4 * 1024 * 1024
        stream = ScriptedStream(b"x" * (size + 1), chunk=64 * 1024, bound=size + 1)
        with self.assertRaises(ResponseTooLarge):
            nvidia._read_limited(stream, size)
        self.assertEqual(stream.served, size + 1)
        stream.assert_bounded()

    def test_partial_reads_do_not_end_a_body_early(self):
        for chunk in (1, 2, 3, 7, 64, 1000):
            at_limit = ScriptedStream(b"y" * self.LIMIT, chunk=chunk, bound=self.LIMIT + 1)
            self.assertEqual(nvidia._read_limited(at_limit, self.LIMIT), b"y" * self.LIMIT, chunk)
            at_limit.assert_bounded()
            over = ScriptedStream(b"y" * (self.LIMIT + 1), chunk=chunk, bound=self.LIMIT + 1)
            with self.assertRaises(ResponseTooLarge, msg=chunk):
                nvidia._read_limited(over, self.LIMIT)
            over.assert_bounded()

    def test_an_endless_body_is_never_read_past_the_limit_plus_one_byte(self):
        stream = ScriptedStream(b"z", chunk=64, endless=True, bound=self.LIMIT + 1)
        with self.assertRaises(ResponseTooLarge):
            nvidia._read_limited(stream, self.LIMIT)
        stream.assert_bounded()
        self.assertEqual(stream.served, self.LIMIT + 1)
        self.assertTrue(all(isinstance(n, int) and 0 < n <= nvidia._READ_CHUNK_BYTES
                            for n in stream.requests))

    def test_an_oversized_successful_response_is_response_too_large(self):
        response = ScriptedResponse(b"x" * (self.LIMIT + 1), chunk=13, bound=self.LIMIT + 1)
        with self.limit():
            result, opener_open = self.analyze_over_default_transport(opener_result=response)
        self.assertEqual((result.status, result.error_code), (STATUS_ERROR, "response_too_large"))
        self.assertIsNone(result.text)
        self.assertEqual(opener_open.call_count, 1)  # no retry
        self.assertEqual(response.exits, 1)  # the response was closed
        self.assertEqual(response.served, self.LIMIT + 1)
        response.assert_bounded()

    def test_a_successful_response_exactly_at_the_limit_is_processed_normally(self):
        body = ok_body("A leaf.")
        at_limit = ScriptedResponse(body, chunk=5, bound=len(body) + 1)
        with self.limit(len(body)):
            result, _ = self.analyze_over_default_transport(opener_result=at_limit)
        self.assertEqual((result.status, result.text), (STATUS_OK, "A leaf."))
        at_limit.assert_bounded()
        one_short = ScriptedResponse(body, chunk=5, bound=len(body))
        with self.limit(len(body) - 1):
            result, _ = self.analyze_over_default_transport(opener_result=one_short)
        self.assertEqual(result.error_code, "response_too_large")
        one_short.assert_bounded()

    def test_an_ordinary_response_still_parses_at_the_real_limit(self):
        result, _ = self.analyze_over_default_transport(
            opener_result=ScriptedResponse(ok_body(), chunk=5))
        self.assertEqual((result.status, result.text),
                         (STATUS_OK, "Green leaves with brown edges."))
        self.assertEqual(result.model, nvidia.MODEL)

    def test_an_oversized_http_error_body_is_bounded_and_response_too_large(self):
        for status in (301, 400, 503):
            with self.subTest(status=status):
                stream = ScriptedStream(b"E", chunk=17, endless=True, bound=self.LIMIT + 1)
                error = self.http_error(status, stream)
                with self.limit(), \
                        mock.patch.object(error, "close", wraps=error.close) as close:
                    result, _ = self.analyze_over_default_transport(opener_error=error)
                self.assertEqual((result.status, result.error_code),
                                 (STATUS_ERROR, "response_too_large"))
                self.assertEqual(stream.served, self.LIMIT + 1)
                stream.assert_bounded()
                self.assertEqual(close.call_count, 1)
                self.assertTrue(stream.closed)  # the underlying stream itself was closed

    def test_http_error_bodies_within_the_limit_keep_their_status_mapping(self):
        cases = ((301, "response_malformed"), (400, "provider_rejected"),
                 (401, "provider_rejected"), (503, "provider_unavailable"))
        for status, code in cases:
            with self.subTest(status=status):
                stream = ScriptedStream(b"e" * self.LIMIT, chunk=9, bound=self.LIMIT + 1)
                error = self.http_error(status, stream)
                with self.limit():
                    result, _ = self.analyze_over_default_transport(opener_error=error)
                self.assertEqual(result.error_code, code)
                stream.assert_bounded()
                self.assertTrue(stream.closed)

    def test_the_stream_helper_reports_an_over_read_even_if_the_exception_is_swallowed(self):
        stream = ScriptedStream(b"x" * 10, chunk=10, bound=5)
        try:
            stream.read(6)  # could take the total past the bound of 5
        except AssertionError:
            pass  # production code with a broad `except Exception` would do exactly this
        with self.assertRaises(AssertionError):
            stream.assert_bounded()
        unbounded = ScriptedStream(b"x")
        try:
            unbounded.read()
        except AssertionError:
            pass
        with self.assertRaises(AssertionError):
            unbounded.assert_bounded()
        fine = ScriptedStream(b"x" * 10, chunk=10, bound=5)
        fine.read(5)
        fine.assert_bounded()

    def padded_ok_body(self, size):
        """A valid chat-completion body of exactly `size` bytes (ASCII padding in the content)."""
        padding = size - len(ok_body(""))
        body = ok_body("a" * padding)
        self.assertEqual(len(body), size)
        return body, padding

    def test_the_analyzer_processes_a_response_of_exactly_four_mebibytes(self):
        size = 4 * 1024 * 1024
        body, padding = self.padded_ok_body(size)
        response = ScriptedResponse(body, chunk=64 * 1024, bound=size + 1)
        result, opener_open = self.analyze_over_default_transport(opener_result=response)
        self.assertEqual((result.status, result.error_code), (STATUS_OK, None))
        self.assertEqual(result.text, "a" * padding)  # parsed and returned, not merely read
        self.assertEqual(opener_open.call_count, 1)
        self.assertEqual(response.exits, 1)
        response.assert_bounded()

    def test_the_analyzer_rejects_a_response_one_byte_over_four_mebibytes(self):
        size = 4 * 1024 * 1024
        body, _ = self.padded_ok_body(size + 1)
        response = ScriptedResponse(body, chunk=64 * 1024, bound=size + 1)
        result, opener_open = self.analyze_over_default_transport(opener_result=response)
        self.assertEqual((result.status, result.error_code), (STATUS_ERROR, "response_too_large"))
        self.assertIsNone(result.text)
        self.assertEqual(response.served, size + 1)  # stopped at the detection byte
        self.assertEqual(opener_open.call_count, 1)
        self.assertEqual(response.exits, 1)
        response.assert_bounded()

    def test_a_read_failure_in_the_middle_of_a_successful_body_keeps_its_transport_type(self):
        cases = ((TimeoutError(), TransportTimeout), (socket.timeout(), TransportTimeout),
                 (ConnectionResetError(), TransportError))
        for failure, expected in cases:
            with self.subTest(failure=type(failure).__name__):
                response = ScriptedResponse(b"x" * 50, chunk=10, fail_with=failure, fail_after=20)
                with mock.patch(OPENER_OPEN, return_value=response):
                    with self.assertRaises(expected) as context:
                        nvidia.urllib_transport("https://example.invalid/v1", {"A": "b"}, b"{}", 5)
                self.assertIs(type(context.exception), expected)
                self.assertEqual(response.served, 20)  # failed part-way through the body
                self.assertEqual(response.exits, 1)

    def test_a_timeout_in_the_middle_of_a_successful_body_is_provider_timeout(self):
        response = ScriptedResponse(b"x" * 50, chunk=10, fail_with=TimeoutError(), fail_after=20)
        result, opener_open = self.analyze_over_default_transport(opener_result=response)
        self.assertEqual((result.status, result.error_code), (STATUS_ERROR, "provider_timeout"))
        self.assertEqual(opener_open.call_count, 1)  # no retry
        self.assertEqual(response.exits, 1)

    def test_a_timeout_in_the_middle_of_an_http_error_body_keeps_the_status_mapping(self):
        # Existing, intentional behaviour: a body that cannot be read becomes an empty body and
        # the HTTP status alone decides the result.
        stream = ScriptedStream(b"e" * 50, chunk=10, fail_with=TimeoutError(), fail_after=20)
        error = self.http_error(503, stream)
        with mock.patch.object(error, "close", wraps=error.close) as close:
            result, _ = self.analyze_over_default_transport(opener_error=error)
        self.assertEqual((result.status, result.error_code), (STATUS_ERROR, "provider_unavailable"))
        self.assertEqual(stream.served, 20)
        self.assertEqual(close.call_count, 1)
        self.assertTrue(stream.closed)

    def test_the_oversize_condition_is_not_provider_unreachable(self):
        transport = FakeTransport(raises=ResponseTooLarge())
        a, _ = analyzer(transport=transport)
        self.assertEqual(a.analyze(request()).error_code, "response_too_large")
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(issubclass(ResponseTooLarge, TransportError))
        for failure, expected in ((TransportError(), "provider_connection_failed"),
                                  (TransportTimeout(), "provider_timeout"),
                                  (ConnectionError("x"), "provider_connection_failed")):
            a, _ = analyzer(transport=FakeTransport(raises=failure))
            self.assertEqual(a.analyze(request()).error_code, expected)

    def test_response_too_large_is_part_of_the_closed_public_error_set(self):
        self.assertIn("response_too_large", ERROR_CODES)
        self.assertEqual(len(ERROR_CODES), 12)
        self.assertEqual(len(set(ERROR_CODES)), 12)
        self.assertEqual(tuple(ERROR_CODES), EXPECTED_ERROR_CODES)

    def test_nothing_leaks_when_a_response_is_too_large(self):
        secret_body = (FAKE_KEY + " provider secret detail Bearer " + FAKE_KEY).encode("utf-8") * 10
        out, err = io.StringIO(), io.StringIO()
        with self.limit(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            from_success, _ = self.analyze_over_default_transport(
                opener_result=ScriptedResponse(secret_body, chunk=11))
            error = self.http_error(503, ScriptedStream(secret_body, chunk=11))
            from_error, _ = self.analyze_over_default_transport(opener_error=error)
        for result in (from_success, from_error):
            self.assertEqual(result.error_code, "response_too_large")
            text = repr(result) + repr(dataclasses.asdict(result))
            for forbidden in (FAKE_KEY, "provider secret detail", "Bearer", "Authorization"):
                self.assertNotIn(forbidden, text)
        self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertEqual(str(ResponseTooLarge()) + repr(ResponseTooLarge()),
                         "ResponseTooLarge()")

    def test_the_adapter_neither_prints_nor_logs(self):
        tree = ast.parse(inspect.getsource(nvidia))
        imported = {a.name.split(".")[0] for n in ast.walk(tree)
                    if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                     if isinstance(n, ast.ImportFrom)}
        self.assertNotIn("logging", imported)
        printed = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id == "print"]
        self.assertEqual(printed, [])


class TestSecretNonLeakage(unittest.TestCase):
    def test_no_result_contains_the_key_or_headers(self):
        transports = [
            FakeTransport(), FakeTransport(401, b"Bearer " + FAKE_KEY.encode()),
            FakeTransport(503, FAKE_KEY.encode()), FakeTransport(200, FAKE_KEY.encode()),
            FakeTransport(raises=TransportError("Bearer " + FAKE_KEY)),
            FakeTransport(raises=RuntimeError(FAKE_KEY)),
        ]
        for transport in transports:
            a, _ = analyzer(transport=transport)
            result = a.analyze(request())
            text = repr(result) + repr(dataclasses.asdict(result))
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn("Bearer", text)
            self.assertNotIn("Authorization", text)

    def test_config_and_analyzer_do_not_expose_the_key(self):
        a, _ = analyzer()
        self.assertNotIn(FAKE_KEY, repr(NvidiaConfig()) + repr(a) + str(a.__dict__.keys()))

    def test_missing_credential_error_does_not_echo_environment(self):
        a, _ = analyzer(environ={"NVIDIA_API_KEY": "", "OTHER_SECRET": "other-secret-value"})
        self.assertNotIn("other-secret-value", repr(a.analyze(request())))

    def test_sources_and_tests_hold_no_real_looking_key(self):
        for module in (boundary, nvidia, image_analysis):
            source = inspect.getsource(module)
            self.assertNotIn("nvapi-", source)


class TestFakeTransportAndOffline(unittest.TestCase):
    def test_the_fake_transport_is_actually_used(self):
        a, t = analyzer()
        with contextlib.ExitStack() as stack:
            guards = offline_guards(stack)
            result = a.analyze(request())
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(len(t.calls), 1)
        # analyze() swallows exceptions from the transport, so a guard that fired would only
        # show up as an error result; the call counts are what prove the guards were untouched.
        for guard in guards:
            self.assertEqual(guard.call_count, 0)

    def test_every_failure_path_stays_offline(self):
        cases = (
            (FakeTransport(500, b"{}"), "provider_unavailable"),
            (FakeTransport(200, b"x"), "response_malformed"),
            (FakeTransport(raises=TransportTimeout()), "provider_timeout"),
            (FakeTransport(raises=TransportError()), "provider_connection_failed"),
            (FakeTransport(raises=RuntimeError("x")), "transport_failed"),
        )
        with contextlib.ExitStack() as stack:
            guards = offline_guards(stack)
            for transport, expected_code in cases:
                a, _ = analyzer(transport=transport)
                result = a.analyze(request())
                self.assertEqual(result.status, STATUS_ERROR)
                self.assertEqual(result.error_code, expected_code)
                self.assertEqual(len(transport.calls), 1)
        for guard in guards:
            self.assertEqual(guard.call_count, 0)

    def test_results_are_deterministic(self):
        first = analyzer()[0].analyze(request())
        second = analyzer()[0].analyze(request())
        self.assertEqual(first, second)

    def test_inputs_are_not_modified(self):
        req = request()
        before = (req.image.data, req.image.media_type, req.prompt)
        analyzer()[0].analyze(req)
        self.assertEqual((req.image.data, req.image.media_type, req.prompt), before)


class TestRedirectsAreNeverFollowed(unittest.TestCase):
    """Real urllib redirect handling over a fake HTTPS connection; no socket, no DNS.

    A 301/302/303 on a POST is exactly what the stock HTTPRedirectHandler would follow, so
    those cases fail if _NoRedirect is removed. 307/308 on a POST are refused by the stock
    handler too, so for them the tests pin the behaviour but do not by themselves detect
    the removal of _NoRedirect.
    """

    HEADERS = {"Authorization": "Bearer " + FAKE_KEY, "Content-Type": "application/json"}
    BODY = b'{"payload": "request body"}'

    def assertOneRequestToTheOriginalUrlOnly(self, server):
        self.assertEqual(server.created, [ORIGINAL_HOST])
        self.assertNotIn(REDIRECT_HOST, server.created)
        self.assertEqual(len(server.requests), 1)
        self.assertFalse(any(r["host"] == REDIRECT_HOST for r in server.requests),
                         "a request reached the redirect destination")
        (sent,) = server.requests
        self.assertEqual((sent["host"], sent["method"], sent["selector"]),
                         (ORIGINAL_HOST, "POST", "/v1/chat/completions"))
        # Compared as booleans so a failure never prints the credential.
        self.assertTrue(sent["headers"].get("authorization") == "Bearer " + FAKE_KEY,
                        "Authorization was not sent unchanged to the original URL")
        self.assertTrue(sent["body"] is not None and len(sent["body"]) > 0,
                        "the request body was not sent to the original URL")
        for guard in server.guards:
            self.assertEqual(guard.call_count, 0)

    def test_the_default_transport_returns_the_redirect_response_unfollowed(self):
        for status in REDIRECT_STATUSES:
            with self.subTest(status=status), fake_https(status) as server:
                result = nvidia.urllib_transport(ORIGINAL_URL, self.HEADERS, self.BODY, 5)
                self.assertEqual(result, (status, REDIRECT_BODY))
                self.assertOneRequestToTheOriginalUrlOnly(server)
                self.assertEqual(server.requests[0]["body"], self.BODY)

    def test_the_analyzer_reports_a_redirect_as_malformed_after_one_request(self):
        for status in REDIRECT_STATUSES:
            with self.subTest(status=status), fake_https(status) as server:
                a = NvidiaImageAnalyzer(config=NvidiaConfig(endpoint=ORIGINAL_URL),
                                        environ={"NVIDIA_API_KEY": FAKE_KEY})
                result = a.analyze(request())
                self.assertEqual(result.status, STATUS_ERROR)
                self.assertEqual(result.error_code, "response_malformed")
                self.assertIsNone(result.text)
                self.assertOneRequestToTheOriginalUrlOnly(server)
                self.assertFalse(FAKE_KEY in repr(result) + repr(dataclasses.asdict(result)),
                                 "the credential appeared in the result")

    def test_no_redirect_declines_to_build_a_redirected_request(self):
        handler = nvidia._NoRedirect()
        for status in REDIRECT_STATUSES:
            with self.subTest(status=status):
                original = urllib.request.Request(
                    ORIGINAL_URL, data=self.BODY, headers=self.HEADERS, method="POST")
                headers = http.client.HTTPMessage()
                headers["Location"] = REDIRECT_URL
                declined = handler.redirect_request(
                    original, io.BytesIO(REDIRECT_BODY), status, REDIRECT_REASONS[status],
                    headers, REDIRECT_URL)
                self.assertIsNone(declined)


class TestProviderNeutralBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = inspect.getsource(boundary)
        cls.tree = ast.parse(cls.source)

    def test_boundary_contains_no_provider_specifics(self):
        tree = ast.parse(self.source)
        tree.body = tree.body[1:]  # the docstring names what the module is not
        text = ast.unparse(tree).lower()
        for term in ("nvidia", "nemotron", "integrate.api", "bearer", "authorization",
                     "https://", "http://", "chat/completions", "image_url", "api_key",
                     "choices", "tavily", "nebius", "urllib", "socket", "os.environ"):
            self.assertNotIn(term, text, term)

    def test_boundary_imports_only_the_standard_library_it_needs(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"dataclasses", "typing"})

    def test_result_is_provider_neutral_and_immutable(self):
        fields = [f.name for f in dataclasses.fields(AnalysisResult)]
        self.assertEqual(fields, ["status", "text", "error_code", "model", "caveats"])
        result = analyzer()[0].analyze(request())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.text = "changed"
        self.assertEqual({c.code for c in result.caveats},
                         {"descriptive_text_only", "image_source_unresolved"})

    def test_input_types_are_frozen_with_the_approved_fields(self):
        self.assertEqual([f.name for f in dataclasses.fields(ImageInput)], ["data", "media_type"])
        self.assertEqual([f.name for f in dataclasses.fields(AnalysisRequest)], ["image", "prompt"])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ImageInput(PNG_BYTES, "image/png").data = b""

    def test_a_second_provider_can_satisfy_the_interface(self):
        class OtherProvider:
            def analyze(self, req):
                return boundary.ok_result("text", "other-model")

        result = OtherProvider().analyze(request())
        self.assertEqual((result.status, result.model), (STATUS_OK, "other-model"))

    def test_png_only_no_decoding_or_conversion_names(self):
        names = {n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(self.tree) if isinstance(n, ast.Attribute)}
        for banned in ("open", "resize", "decode", "encode", "convert", "read", "write"):
            self.assertNotIn(banned, names, banned)
        self.assertEqual(boundary.MEDIA_TYPE_PNG, "image/png")
        self.assertNotIn("jpeg", ast.unparse(self.tree).lower())

    def test_public_api_is_small(self):
        self.assertEqual(set(image_analysis.__all__), {
            "ERROR_CODES", "STATUS_ERROR", "STATUS_OK", "AnalysisRequest", "AnalysisResult",
            "ImageAnalyzer", "ImageInput", "NvidiaConfig", "NvidiaImageAnalyzer"})


class TestIsolationFromTheRestOfTheProject(unittest.TestCase):
    def test_other_packages_do_not_import_image_analysis(self):
        for name in ("image_pipeline.reference", "record.validation", "storage.record_store",
                     "mock_rover.simulator"):
            source = inspect.getsource(importlib.import_module(name))
            self.assertNotIn("image_analysis", source, name)

    def test_adapter_imports_nothing_from_the_rest_of_the_project(self):
        allowed = {"base64", "json", "os", "socket", "urllib", "image_analysis"}
        for module in (nvidia, boundary):
            imported = set()
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add(node.module.split(".")[0])
            imported -= {"dataclasses", "typing"}
            self.assertTrue(imported <= allowed, (module.__name__, imported))

    def test_nvidia_specifics_are_in_nvidia_module(self):
        self.assertIn("integrate.api.nvidia.com", inspect.getsource(nvidia))
        self.assertIn("NVIDIA_API_KEY", inspect.getsource(nvidia))
        self.assertNotIn("integrate.api.nvidia.com", inspect.getsource(image_analysis))


class TestApiKeyPresent(unittest.TestCase):
    def test_it_returns_only_a_boolean_for_a_usable_key(self):
        result = nvidia.api_key_present({"NVIDIA_API_KEY": "canary-key-value"})
        self.assertIs(result, True)

    def test_missing_blank_or_malformed_keys_are_not_present(self):
        for environ in ({}, {"NVIDIA_API_KEY": ""}, {"NVIDIA_API_KEY": "   "},
                        {"NVIDIA_API_KEY": "bad\nkey"}, {"NVIDIA_API_KEY": "bad\x00key"},
                        {"NVIDIA_API_KEY": 123}, {"NVIDIA_API_KEY": None},
                        {"OTHER": "canary-key-value"}):
            self.assertIs(nvidia.api_key_present(environ), False, environ)

    def test_it_reads_the_process_environment_by_default_and_prints_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            with mock.patch.dict("os.environ", {"NVIDIA_API_KEY": "canary-key-value"}):
                self.assertIs(nvidia.api_key_present(), True)
            with mock.patch.dict("os.environ", {}, clear=True):
                self.assertIs(nvidia.api_key_present(), False)
        self.assertEqual(out.getvalue(), "")

    def test_it_agrees_with_what_analyze_accepts(self):
        for key, expected in (("canary-key-value", True), ("", False), ("a\rb", False)):
            environ = {"NVIDIA_API_KEY": key}
            transport_calls = []

            def transport(url, headers, body, timeout):
                transport_calls.append(1)
                return 200, b'{"choices": [{"message": {"content": "ok"}}]}'

            result = nvidia.NvidiaImageAnalyzer(transport=transport, environ=environ).analyze(request())
            self.assertEqual(nvidia.api_key_present(environ), expected)
            self.assertEqual(result.error_code != "credential_missing", expected)


if __name__ == "__main__":
    unittest.main()
