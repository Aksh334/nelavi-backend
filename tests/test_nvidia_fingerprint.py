"""Offline tests of the NVIDIA request fingerprint (IA-10): the length and short SHA-256 prefix of the request body the
transport was handed and of the decoded image a run used.

Nothing here opens a socket or reads a key from the environment: the "network" is a scripted fake transport and socket,
DNS and urllib tripwires are active. Every expected fingerprint is computed in the test from the bytes the fake transport
captured and from the image the test sent, never hardcoded. A canary string stands in for content and must never reach a
response. The fingerprint is a diagnostic only: it changes no request, no error class and no behaviour.
"""

import base64
import hashlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import demo_app
from agri_pipeline.findings import PROMPT
from demo_app.app import DemoApp
from demo_app.server import ObservingTransport
from demo_app.service import (
    FINGERPRINT_HEX_CHARS,
    DemoService,
    clean_request_fingerprint,
    fingerprint_of,
)
from image_analysis import nvidia
from storage.record_store import RecordStore

HOST = "127.0.0.1:54321"
ROUTE = "/api/run-integrated"
KEY = "nvapi-canary-not-a-real-key-0005"
CANARY = "image-text-canary-6120"
SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_A = SIGNATURE + b"first synthetic image " + CANARY.encode() + b" \x00\x01\x02\xff" * 20
PNG_B = SIGNATURE + b"second synthetic image, different bytes \x03\x04\x05" * 20
SCRIPT = (Path(demo_app.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
OK_REPLY = (200, json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8"))


def sha_prefix(data):
    return hashlib.sha256(data).hexdigest()[:FINGERPRINT_HEX_CHARS]


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


class Capture:
    """A fake inner transport that keeps the bytes it was handed."""

    def __init__(self, *outcomes):
        self.outcomes, self.bodies = list(outcomes) or [OK_REPLY], []

    def __call__(self, url, headers, body, timeout):
        self.bodies.append(body)
        outcome = self.outcomes[min(len(self.bodies), len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def build(testcase, inner, environ=None, thinking_off=False, observer=None):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    observer = observer if observer is not None else ObservingTransport(inner, thinking_off=thinking_off)
    analyzer = nvidia.NvidiaImageAnalyzer(transport=observer, environ={"NVIDIA_API_KEY": KEY} if environ is None
                                          else environ)
    service = DemoService(store, findings_analyzer=analyzer, findings_observer=observer)
    return DemoApp(service, {HOST}), observer


def post(app, png, image="real"):
    payload = {"scenario": "normal_operation",
               "providers": {"image": image, "research": "skip", "compute": "skip", "synthesis": "skip"}}
    if image != "skip":
        payload["image_png_base64"] = base64.b64encode(png).decode("ascii")
    body = json.dumps(payload).encode("utf-8")
    headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": str(len(body)),
               "content-type": "application/json"}
    response = app.handle_request("POST", ROUTE, headers, io.BytesIO(body).read)
    return json.loads(response.body.decode("utf-8")), response.body.decode("utf-8")


class FingerprintOfTests(unittest.TestCase):
    def test_only_a_length_and_sixteen_hex_digits_come_back(self):
        found = fingerprint_of(b"abc")
        self.assertEqual(found, {"bytes": 3, "sha256_prefix": hashlib.sha256(b"abc").hexdigest()[:16]})
        self.assertEqual(len(found["sha256_prefix"]), FINGERPRINT_HEX_CHARS)
        self.assertEqual(fingerprint_of(bytearray(b"abc")), found)
        self.assertEqual(fingerprint_of(b""), {"bytes": 0, "sha256_prefix": hashlib.sha256(b"").hexdigest()[:16]})

    def test_anything_that_is_not_bytes_has_no_fingerprint(self):
        for value in (None, "text", 5, ["x"], {"a": 1}):
            self.assertIsNone(fingerprint_of(value))


class CleanRequestFingerprintTests(unittest.TestCase):
    def good(self, **changes):
        base = {"body_bytes": 312565, "body_sha256_prefix": "8529fad6d9dc0ff8", "image_bytes": 232255,
                "image_sha256_prefix": "e87e04a4cb91b053"}
        base.update(changes)
        return base

    def test_the_expected_shapes_pass_and_are_copied(self):
        for raw in (self.good(), self.good(body_bytes=None, body_sha256_prefix=None),
                    self.good(image_bytes=None, image_sha256_prefix=None),
                    self.good(body_bytes=None, body_sha256_prefix=None, image_bytes=None, image_sha256_prefix=None)):
            cleaned = clean_request_fingerprint(raw)
            self.assertEqual(cleaned, raw)
            self.assertIsNot(cleaned, raw)

    def test_anything_else_is_dropped(self):
        for raw in (None, [], "x", {"body_bytes": 1}, dict(self.good(), extra=1),
                    {k: v for k, v in self.good().items() if k != "image_bytes"},
                    self.good(body_sha256_prefix="8529FAD6D9DC0FF8"), self.good(body_sha256_prefix="8529fad6d9dc0ff"),
                    self.good(body_sha256_prefix="8529fad6d9dc0ff8aa"), self.good(body_sha256_prefix=CANARY),
                    self.good(body_sha256_prefix="zzzzzzzzzzzzzzzz"), self.good(body_sha256_prefix=None),
                    self.good(body_bytes=None), self.good(body_bytes=-1), self.good(body_bytes=True),
                    self.good(body_bytes="5"), self.good(body_bytes=64 * 1024 * 1024 + 1),
                    self.good(image_bytes=1024 * 1024 + 1), self.good(image_bytes=1.5),
                    self.good(image_sha256_prefix=PROMPT[:16]), self.good(image_sha256_prefix=123)):
            self.assertIsNone(clean_request_fingerprint(raw), repr(raw)[:80])


class ObserverFingerprintTests(NoNetwork):
    def test_it_is_the_length_and_digest_of_exactly_the_bytes_the_transport_was_handed(self):
        inner = Capture()
        observer = ObservingTransport(inner)
        body = b'{"model": "m", "messages": []}'
        observer(nvidia.ENDPOINT, {"Authorization": "Bearer " + KEY}, body, 60.0)
        self.assertEqual(inner.bodies, [body])
        self.assertEqual(observer.request_fingerprint(), {"bytes": len(body), "sha256_prefix": sha_prefix(body)})

    def test_with_the_reasoning_off_option_it_describes_the_changed_body_that_was_actually_sent(self):
        body = json.dumps({"model": "m", "max_tokens": 1, "messages": []}).encode("utf-8")
        plain, off = Capture(), Capture()
        a, b = ObservingTransport(plain), ObservingTransport(off, thinking_off=True)
        a(nvidia.ENDPOINT, {}, body, 60.0)
        b(nvidia.ENDPOINT, {}, body, 60.0)
        self.assertEqual(a.request_fingerprint()["sha256_prefix"], sha_prefix(plain.bodies[0]))
        self.assertEqual(b.request_fingerprint()["sha256_prefix"], sha_prefix(off.bodies[0]))
        self.assertNotEqual(plain.bodies[0], off.bodies[0])
        self.assertNotEqual(a.request_fingerprint(), b.request_fingerprint())

    def test_a_request_that_fails_in_transport_still_has_its_fingerprint(self):
        for failure in (TimeoutError(), OSError("x"), RuntimeError(CANARY)):
            inner = Capture(failure)
            observer = ObservingTransport(inner)
            with self.assertRaises(type(failure)):
                observer(nvidia.ENDPOINT, {}, b"body", 60.0)
            self.assertEqual(observer.request_fingerprint(), {"bytes": 4, "sha256_prefix": sha_prefix(b"body")})

    def test_nothing_before_a_request_reset_clears_it_and_the_value_is_a_copy(self):
        observer = ObservingTransport(Capture())
        self.assertIsNone(observer.request_fingerprint())
        observer(nvidia.ENDPOINT, {}, b"body", 60.0)
        copy_ = observer.request_fingerprint()
        copy_["bytes"] = 999
        self.assertEqual(observer.request_fingerprint()["bytes"], 4)
        observer.reset()
        self.assertIsNone(observer.request_fingerprint())

    def test_the_observation_snapshot_and_the_pass_through_are_unchanged(self):
        inner = Capture()
        observer = ObservingTransport(inner)
        self.assertEqual(set(observer.snapshot()),
                         {"request_made", "outcome", "http_status", "failure", "elapsed_seconds", "timeout_seconds",
                          "thinking_off", "thinking_off_applied"})
        observer(nvidia.ENDPOINT, {"A": "b"}, b"body", 12.0)
        self.assertEqual(inner.bodies, [b"body"])


class RunFingerprintTests(NoNetwork):
    def test_the_response_carries_fingerprints_computed_from_this_runs_own_bytes(self):
        inner = Capture()
        app, _ = build(self, inner)
        out, _ = post(app, PNG_A)
        sent = inner.bodies[0]
        self.assertEqual(out["findings_request_fingerprint"],
                         {"body_bytes": len(sent), "body_sha256_prefix": sha_prefix(sent),
                          "image_bytes": len(PNG_A), "image_sha256_prefix": sha_prefix(PNG_A)})

    def test_the_image_in_the_sent_body_is_the_image_that_was_fingerprinted(self):
        inner = Capture()
        app, _ = build(self, inner)
        out, _ = post(app, PNG_A)
        document = json.loads(inner.bodies[0].decode("utf-8"))
        url = [p for p in document["messages"][0]["content"] if p["type"] == "image_url"][0]["image_url"]["url"]
        decoded = base64.b64decode(url.split(",", 1)[1])
        self.assertEqual(decoded, PNG_A)
        self.assertEqual(out["findings_request_fingerprint"]["image_sha256_prefix"], sha_prefix(decoded))

    def test_the_same_image_gives_the_same_fingerprint_and_another_image_a_different_one(self):
        results = []
        for png in (PNG_A, PNG_A, PNG_B):
            app, _ = build(self, Capture())
            results.append(post(app, png)[0]["findings_request_fingerprint"])
        self.assertEqual(results[0], results[1])
        self.assertNotEqual(results[0]["image_sha256_prefix"], results[2]["image_sha256_prefix"])
        self.assertNotEqual(results[0]["body_sha256_prefix"], results[2]["body_sha256_prefix"])
        self.assertNotEqual(results[0]["image_bytes"], results[2]["image_bytes"])

    def test_the_fingerprint_is_there_for_a_failed_request_too_and_the_error_handling_is_unchanged(self):
        inner = Capture((503, b"ResourceExhausted " + CANARY.encode()))
        app, _ = build(self, inner)
        out, _ = post(app, PNG_A)
        stage = {s["stage"]: s for s in out["integrated"]["integration"]["stages"]}["image_findings"]
        self.assertEqual((stage["status"], stage["error_code"]), ("failed", "provider_unavailable"))
        self.assertEqual(out["findings_observation"]["http_status"], 503)
        self.assertEqual(out["findings_error_class"]["status_kind"], "server_unavailable")
        self.assertEqual(out["findings_request_fingerprint"]["body_sha256_prefix"], sha_prefix(inner.bodies[0]))
        self.assertEqual(len(inner.bodies), 1)                              # no retry

    def test_a_request_refused_locally_has_the_image_fingerprint_and_no_body_fingerprint(self):
        inner = Capture()
        app, _ = build(self, inner, environ={})                                # no key: refused before any transport
        out, _ = post(app, PNG_A)
        self.assertEqual(inner.bodies, [])
        self.assertEqual(out["findings_request_fingerprint"],
                         {"body_bytes": None, "body_sha256_prefix": None, "image_bytes": len(PNG_A),
                          "image_sha256_prefix": sha_prefix(PNG_A)})

    def test_it_is_absent_when_the_image_stage_is_not_real(self):
        app, _ = build(self, Capture())
        for mode in ("skip", "fake"):
            out, _ = post(app, PNG_A, image=mode)
            self.assertNotIn("findings_request_fingerprint", out)

    def test_no_image_data_prompt_key_or_header_value_reaches_the_response(self):
        app, _ = build(self, Capture())
        out, text = post(app, PNG_A)
        for forbidden in (base64.b64encode(PNG_A).decode("ascii")[:60], CANARY, PROMPT[:40], KEY, "Bearer",
                          "Authorization", "data:image"):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertEqual(set(out["findings_request_fingerprint"]),
                         {"body_bytes", "body_sha256_prefix", "image_bytes", "image_sha256_prefix"})

    def test_an_observer_without_or_with_a_broken_or_wrong_fingerprint_never_adds_anything_unsafe(self):
        class Plain:
            def __init__(self, inner):
                self._inner = ObservingTransport(inner)

            def __call__(self, *args):
                return self._inner(*args)

            def reset(self):
                self._inner.reset()

            def snapshot(self):
                return self._inner.snapshot()

        class Broken(Plain):
            def request_fingerprint(self):
                raise RuntimeError(CANARY)

        class Wrong(Plain):
            def request_fingerprint(self):
                return {"bytes": CANARY, "sha256_prefix": CANARY}

        for kind, expected_body in ((Plain, None), (Broken, None)):
            with self.subTest(kind=kind.__name__):
                app, _ = build(self, None, observer=kind(Capture()))
                out, text = post(app, PNG_A)
                self.assertIsNone(out["findings_request_fingerprint"]["body_bytes"])
                self.assertEqual(out["findings_request_fingerprint"]["image_sha256_prefix"], sha_prefix(PNG_A))
                self.assertNotIn(CANARY, text)
        app, _ = build(self, None, observer=Wrong(Capture()))
        out, text = post(app, PNG_A)
        self.assertNotIn("findings_request_fingerprint", out)               # a wrong shape is dropped whole
        self.assertNotIn(CANARY, text)

    def test_the_request_the_adapter_builds_is_not_changed_by_the_diagnostic(self):
        with_observer = Capture()
        app, _ = build(self, with_observer)
        post(app, PNG_A)
        bare = Capture()
        analyzer = nvidia.NvidiaImageAnalyzer(transport=bare, environ={"NVIDIA_API_KEY": KEY})
        from agri_pipeline.findings import PromptedFindingsProvider
        PromptedFindingsProvider(analyzer).analyze_image(PNG_A)
        self.assertEqual(with_observer.bodies, bare.bodies)                 # byte for byte, with and without the observer


class PageSourceTests(unittest.TestCase):
    def function_text(self, name):
        body = SCRIPT[SCRIPT.index("function %s(" % name):]
        return body[:body.index("\n}\n")]

    def test_the_page_shows_only_validated_numbers_and_hex_digits(self):
        text = self.function_text("fingerprintLines")
        self.assertIn("[0-9a-f]{16}", text)
        self.assertIn("Number.isInteger", text)
        self.assertIn("no image data or prompt is shown", text)
        self.assertNotIn("innerHTML", SCRIPT)
        self.assertIn("findings_request_fingerprint", self.function_text("renderIntegrated"))


if __name__ == "__main__":
    unittest.main()
