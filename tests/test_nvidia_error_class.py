"""Offline tests of the 5xx error-body classification (IA-8), the user-stated research query context, and the
Compute Skip / Fake mapping.

Nothing here opens a socket or reads a key from the environment: the "network" is a scripted fake transport and socket,
DNS and urllib tripwires are active. A canary string stands in for provider text and must never reach a result.
"""

import base64
import io
import json
import unittest
from unittest import mock

from demo_app.app import DemoApp
from demo_app.fake_research import FakeResearchClient
from demo_app.server import ObservingTransport, classify_error_body
from demo_app.service import ERROR_BODY_CLASSES, ERROR_BODY_MAX_BYTES, DemoService, clean_error_class
from image_analysis import nvidia
from storage.record_store import RecordStore

HOST = "127.0.0.1:54321"
ROUTE = "/api/run-integrated"
NV_KEY = "nvapi-canary-not-a-real-key-0002"
CANARY = "provider-text-canary-5519"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image \x00\x01\x02\xff"
PNG_B64 = base64.b64encode(PNG).decode("ascii")
QUESTION = "What visible signs of plant stress are present?"


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


class Scripted:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append(body)
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Clock:
    def __init__(self):
        self.value = -1.0

    def __call__(self):
        self.value += 1.0
        return self.value


def body_with(text):
    return ("{\"error\": \"%s\"}" % text).encode("utf-8")


class ClassifyErrorBodyTests(unittest.TestCase):
    def cls(self, body):
        return classify_error_body(body)["body_class"]

    def test_each_class_is_reachable_and_the_length_is_reported(self):
        cases = ((b"", "empty"), (b"  \n ", "empty"), (b"\xff\xfe\x00", "not_text"),
                 (body_with("ResourceExhausted: worker limit reached (16/16)"), "resource_exhausted"),
                 (body_with("resource_exhausted"), "resource_exhausted"),
                 (body_with("Too Many Requests"), "capacity_or_rate_limit"),
                 (body_with("model is at capacity"), "capacity_or_rate_limit"),
                 (body_with("Bad Gateway"), "gateway_or_unavailable"),
                 (body_with("something else entirely"), "unclassified"))
        for body, expected in cases:
            with self.subTest(expected=expected):
                result = classify_error_body(body)
                self.assertEqual(result["body_class"], expected)
                self.assertEqual(result["body_bytes"], len(body))
                self.assertIn(result["body_class"], ERROR_BODY_CLASSES)
                self.assertEqual(set(result), {"body_class", "body_bytes"})

    def test_a_resource_wording_wins_over_a_gateway_wording(self):
        self.assertEqual(self.cls(body_with("ResourceExhausted upstream gateway")), "resource_exhausted")

    def test_the_result_never_contains_any_text_of_the_body(self):
        result = classify_error_body(body_with("ResourceExhausted " + CANARY))
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertNotIn("ResourceExhausted", json.dumps(result))

    def test_a_non_bytes_body_is_not_classified_and_a_huge_length_is_clamped(self):
        for body in (None, "text", 5, ["x"]):
            self.assertIsNone(classify_error_body(body))
        self.assertEqual(classify_error_body(b"x" * (ERROR_BODY_MAX_BYTES + 10))["body_bytes"], ERROR_BODY_MAX_BYTES)

    def test_bytearray_is_accepted(self):
        self.assertEqual(self.cls(bytearray(b"")), "empty")


class CleanErrorClassTests(unittest.TestCase):
    def good(self, **changes):
        base = {"status_kind": "server_unavailable", "body_class": "unclassified", "body_bytes": 12,
                "retry_after": "absent", "retry_after_seconds": None}
        base.update(changes)
        return base

    def test_the_exact_shape_passes_and_is_copied(self):
        for raw in (self.good(), self.good(retry_after="seconds", retry_after_seconds=30),
                    self.good(retry_after="not_captured"), self.good(status_kind="rate_limited")):
            cleaned = clean_error_class(raw)
            self.assertEqual(cleaned, raw)
            self.assertIsNot(cleaned, raw)

    def test_anything_else_is_dropped(self):
        for raw in (None, [], "x", {"body_class": "unclassified", "body_bytes": 12},
                    {k: v for k, v in self.good().items() if k != "retry_after"}, dict(self.good(), extra=1),
                    self.good(body_class=CANARY), self.good(body_bytes=-1), self.good(body_bytes=True),
                    self.good(body_bytes="5"), self.good(body_bytes=ERROR_BODY_MAX_BYTES + 1),
                    self.good(body_bytes=1.5), self.good(status_kind=CANARY), self.good(status_kind=None),
                    self.good(retry_after=CANARY), self.good(retry_after="seconds"),
                    self.good(retry_after="absent", retry_after_seconds=5),
                    self.good(retry_after="seconds", retry_after_seconds=-1),
                    self.good(retry_after="seconds", retry_after_seconds=86401),
                    self.good(retry_after="seconds", retry_after_seconds=True),
                    self.good(retry_after="seconds", retry_after_seconds="5")):
            self.assertIsNone(clean_error_class(raw), repr(raw))


class ObserverTests(NoNetwork):
    def observe(self, outcome):
        observer = ObservingTransport(Scripted(outcome), clock=Clock())
        try:
            observer(nvidia.ENDPOINT, {"Authorization": "Bearer " + NV_KEY}, b"{}", 60.0)
        except Exception:
            pass
        return observer

    def test_a_5xx_reply_is_classified_and_the_reply_is_passed_on_unchanged(self):
        body = body_with("ResourceExhausted " + CANARY)
        observer = ObservingTransport(Scripted((503, body)), clock=Clock())
        self.assertEqual(observer(nvidia.ENDPOINT, {}, b"{}", 60.0), (503, body))
        found = observer.error_class()
        self.assertEqual(found["body_class"], "resource_exhausted")
        self.assertEqual(found["body_bytes"], len(body))
        text = json.dumps([found, observer.snapshot()])
        for forbidden in (CANARY, "ResourceExhausted", NV_KEY):
            self.assertNotIn(forbidden, text)

    def test_success_and_transport_failures_have_no_classification(self):
        for outcome in ((200, b"ok"), (204, b""), RuntimeError(CANARY), TimeoutError()):
            with self.subTest(outcome=repr(outcome)):
                self.assertIsNone(self.observe(outcome).error_class())

    def test_every_error_status_is_classified_with_its_kind(self):
        expected = {400: "client_error", 401: "authentication_or_permission", 402: "payment_required",
                    403: "authentication_or_permission", 404: "client_error", 408: "request_timeout",
                    422: "client_error", 429: "rate_limited", 500: "server_error", 502: "gateway_error",
                    503: "server_unavailable", 504: "gateway_error", 599: "server_error"}
        for status, kind in expected.items():
            with self.subTest(status=status):
                found = self.observe((status, body_with(CANARY))).error_class()
                self.assertEqual(found["status_kind"], kind)
                self.assertNotIn(CANARY, json.dumps(found))
                self.assertIsNotNone(clean_error_class(found))

    def test_a_transport_without_header_access_reports_not_captured(self):
        found = self.observe((503, b"")).error_class()
        self.assertEqual((found["retry_after"], found["retry_after_seconds"]), ("not_captured", None))

    def test_reset_and_the_next_request_clear_it_and_the_copy_is_independent(self):
        observer = self.observe((503, b""))
        found = observer.error_class()
        found["body_class"] = "changed"
        self.assertEqual(observer.error_class()["body_class"], "empty")
        observer.reset()
        self.assertIsNone(observer.error_class())

    def test_the_snapshot_keys_are_unchanged(self):
        self.assertEqual(set(self.observe((503, b"")).snapshot()),
                         {"request_made", "outcome", "http_status", "failure", "elapsed_seconds", "timeout_seconds",
                          "thinking_off", "thinking_off_applied"})


def make(testcase, inner, observer=None, research=None):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    observer = observer if observer is not None else ObservingTransport(inner, clock=Clock())
    analyzer = nvidia.NvidiaImageAnalyzer(transport=observer, environ={"NVIDIA_API_KEY": NV_KEY})
    service = DemoService(store, findings_analyzer=analyzer, findings_observer=observer,
                          fake_research_client=research)
    return DemoApp(service, {HOST}), service


def post(app, payload):
    body = json.dumps(payload).encode("utf-8")
    headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": str(len(body)),
               "content-type": "application/json"}
    return json.loads(app.handle_request("POST", ROUTE, headers, io.BytesIO(body).read).body.decode("utf-8"))


def image_run(app):
    return post(app, {"scenario": "normal_operation", "image_png_base64": PNG_B64,
                      "providers": {"image": "real", "research": "skip", "compute": "skip", "synthesis": "skip"}})


class RouteTests(NoNetwork):
    def test_a_503_response_carries_the_class_and_none_of_the_reply_text(self):
        app, _ = make(self, Scripted((503, body_with("ResourceExhausted " + CANARY))))
        out = image_run(app)
        self.assertEqual(out["findings_observation"]["http_status"], 503)
        self.assertEqual(out["findings_error_class"]["body_class"], "resource_exhausted")
        text = json.dumps(out)
        for forbidden in (CANARY, "ResourceExhausted", NV_KEY, "Bearer"):
            self.assertNotIn(forbidden, text)

    def test_the_class_is_absent_for_success_and_present_with_its_kind_for_a_client_error(self):
        app, _ = make(self, Scripted((200, b"{}")))
        self.assertNotIn("findings_error_class", image_run(app))
        for status, kind in ((401, "authentication_or_permission"), (429, "rate_limited")):
            with self.subTest(status=status):
                app, _ = make(self, Scripted((status, body_with(CANARY))))
                out = image_run(app)
                self.assertEqual(out["findings_error_class"]["status_kind"], kind)
                self.assertEqual(out["findings_observation"]["http_status"], status)
                self.assertNotIn(CANARY, json.dumps(out))

    def test_an_observer_without_or_with_a_broken_error_class_adds_nothing(self):
        class Plain:
            def __init__(self, inner):
                self._inner = ObservingTransport(inner, clock=Clock())

            def __call__(self, *args):
                return self._inner(*args)

            def reset(self):
                self._inner.reset()

            def snapshot(self):
                return self._inner.snapshot()

        class Broken(Plain):
            def error_class(self):
                raise RuntimeError(CANARY)

        class Wrong(Plain):
            def error_class(self):
                return {"body_class": CANARY, "body_bytes": 3}

        for kind in (Plain, Broken, Wrong):
            with self.subTest(kind=kind.__name__):
                app, _ = make(self, None, observer=kind(Scripted((503, b""))))
                out = image_run(app)
                self.assertNotIn("findings_error_class", out)
                self.assertNotIn(CANARY, json.dumps(out))
                self.assertEqual(out["integrated"]["run_status"], "failed")


class RecordingResearch:
    def __init__(self):
        self.queries, self._inner = [], FakeResearchClient()

    def search(self, query):
        self.queries.append(query)
        return self._inner.search(query)


class ResearchQueryTests(NoNetwork):
    def run_research(self, **extra):
        research = RecordingResearch()
        _, service = make(self, None, observer=ObservingTransport(Scripted((200, b"{}")), clock=Clock()),
                          research=research)
        payload = {"scenario": "normal_operation", "question": QUESTION,
                   "providers": {"image": "skip", "research": "fake", "compute": "skip", "synthesis": "skip"}}
        payload.update(extra)
        service.run_integrated(payload)
        return research.queries

    def test_without_stated_context_the_query_is_the_question_unchanged(self):
        self.assertEqual(self.run_research(), [QUESTION])
        self.assertEqual(self.run_research(crop="  ", region=""), [QUESTION])

    def test_stated_context_is_appended_as_plain_labels_and_nothing_is_added(self):
        self.assertEqual(self.run_research(crop="tomato", region="Kerala", growth_stage="flowering"),
                         ["%s (crop: tomato; region: Kerala; growth stage: flowering)" % QUESTION])
        self.assertEqual(self.run_research(region="Kerala"), ["%s (region: Kerala)" % QUESTION])

    def test_the_question_is_used_unchanged_when_the_longer_text_would_not_fit(self):
        question = "q" * 190
        self.assertEqual(DemoService._research_query(question, {"crop": "tomato", "region": "Kerala"}), question)
        self.assertEqual(DemoService._research_query(None, {"crop": "tomato"}), None)
        self.assertEqual(DemoService._research_query(QUESTION, None), QUESTION)

    def test_an_invalid_stated_value_is_refused_before_anything_runs(self):
        research = RecordingResearch()
        _, service = make(self, None, observer=ObservingTransport(Scripted((200, b"{}")), clock=Clock()),
                          research=research)
        from demo_app.service import DemoError
        with self.assertRaises(DemoError):
            service.run_integrated({"scenario": "normal_operation", "question": QUESTION, "crop": "to;mato",
                                    "providers": {"research": "fake"}})
        self.assertEqual(research.queries, [])


class ComputeSelectionTests(NoNetwork):
    def compute_stage(self, mode):
        _, service = make(self, None, observer=ObservingTransport(Scripted((200, b"{}")), clock=Clock()))
        out = service.run_integrated({"scenario": "normal_operation",
                                      "providers": {"image": "skip", "research": "skip", "compute": mode,
                                                    "synthesis": "skip"}})
        stages = {s["stage"]: s for s in out["integrated"]["integration"]["stages"]}
        return out["providers"]["compute"], stages["sensor_summary"]

    def test_skip_is_skipped_and_fake_is_the_only_way_to_run_the_fake_runner(self):
        mode, stage = self.compute_stage("skip")
        self.assertEqual((mode, stage["status"], stage["skip_reason"], stage["provider_called"], stage["fake"]),
                         ("skip", "skipped", "no_provider_configured", False, None))
        mode, stage = self.compute_stage("fake")
        self.assertEqual((mode, stage["status"], stage["provider_called"]), ("fake", "completed", True))

    def test_an_omitted_providers_object_skips_every_stage(self):
        _, service = make(self, None, observer=ObservingTransport(Scripted((200, b"{}")), clock=Clock()))
        out = service.run_integrated({"scenario": "normal_operation"})
        self.assertEqual(out["providers"], {"image": "skip", "research": "skip", "compute": "skip", "synthesis": "skip"})


if __name__ == "__main__":
    unittest.main()
