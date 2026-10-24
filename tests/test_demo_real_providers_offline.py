"""End-to-end offline tests of the REAL NVIDIA and Tavily adapters inside the integrated demo route.

`POST /api/run-integrated` is driven through the real route, service and orchestration into the real
`NvidiaImageAnalyzer` and `TavilyResearch`; only their HTTP transport is replaced by a recording fake, so no socket can
be opened and the exact request that WOULD be sent can be inspected. The adapters' own unit tests live in
test_image_analysis.py, test_provider_findings.py and test_research.py; these tests cover the seams between them and the
workflow. A fake transport shows what is sent and how replies are handled, not that either vendor behaves this way.
"""

import base64
import io
import json
import unittest
import urllib.request
from unittest import mock

from agri_pipeline.findings import PROMPT
from demo_app.app import DemoApp
from demo_app.service import DemoService
from image_analysis import nvidia
from image_analysis.nvidia import ResponseTooLarge, TransportError, TransportTimeout
from orchestration.integrated import check_integrated_result
from research import tavily
from research.tavily import TavilyResearch
from storage.record_store import RecordStore

HOST = "127.0.0.1:8765"          # only a Host-header string for the in-process app; no port is opened
ROUTE = "/api/run-integrated"
NV_KEY = "nvapi-canary-not-a-real-key-0001"
TV_KEY = "tvly-canary-not-a-real-key-0002"
SECRET_TEXT = "secret-provider-detail-7731"
QUESTION = "how should soil moisture readings be read"
DOMAINS = ["agri.example.org", "extension.example.net"]
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image \x00\x01\x02\xff"
PNG_B64 = base64.b64encode(PNG).decode("ascii")
CLOCK_TIME = "2026-01-01T00:00:00+00:00"
GOOD = {
    "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
    "growth_stage": {"candidate": "stage-x", "evidence": ["plant height"], "uncertainty": "high"},
    "symptoms": [{"observation": "brown spots on lower leaves", "possible_interpretations": ["possible-cause-a"]}],
    "needs_more": ["a close-up of the underside of a leaf"],
}


class NoNetwork(unittest.TestCase):
    """Any socket or urllib use fails the test, so the default transports can never reach out."""

    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


class Scripted:
    """A recording transport: returns (status, body) or raises, in order; the last outcome repeats."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def nv_reply(content):
    return 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


def tv_reply(results):
    return 200, json.dumps({"results": results}).encode("utf-8")


def good_nv():
    return nv_reply(json.dumps(GOOD))


def good_tv():
    return tv_reply([{"title": "T1", "url": "https://agri.example.org/a", "content": "S1"}])


def make(testcase, nv, tv, nv_env=None, tv_env=None):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    analyzer = NvidiaImageAnalyzer(nv, nv_env)
    search = TavilyResearch(DOMAINS, transport=tv, environ={"TAVILY_API_KEY": TV_KEY} if tv_env is None else tv_env,
                            clock=lambda: CLOCK_TIME)
    service = DemoService(store, findings_analyzer=analyzer, research_client=search, research_domains=list(DOMAINS))
    return DemoApp(service, {HOST}), store


def NvidiaImageAnalyzer(transport, environ=None):
    return nvidia.NvidiaImageAnalyzer(transport=transport,
                                      environ={"NVIDIA_API_KEY": NV_KEY} if environ is None else environ)


def post(app, payload):
    body = json.dumps(payload).encode("utf-8")
    headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": str(len(body)),
               "content-type": "application/json"}
    return app.handle_request("POST", ROUTE, headers, io.BytesIO(body).read)


def get(app, target):
    return app.handle_request("GET", target, {"host": HOST}, io.BytesIO(b"").read)


def body_of(response):
    return json.loads(response.body.decode("utf-8"))


def run(app, image="real", research="real", png=PNG_B64, question=QUESTION):
    return post(app, {"scenario": "normal_operation", "question": question, "image_png_base64": png,
                      "providers": {"image": image, "research": research, "compute": "fake", "synthesis": "fake"}})


def steps_of(body):
    return {step["name"]: step for step in body["integrated"]["run"]["steps"]}


def stages_of(body):
    return {stage["stage"]: stage for stage in body["integrated"]["integration"]["stages"]}


class SecretFree(NoNetwork):
    def assert_clean(self, response):
        text = response.body.decode("utf-8")
        for secret in (NV_KEY, TV_KEY, SECRET_TEXT, "Bearer"):
            self.assertNotIn(secret, text, secret)


class NvidiaChainTests(SecretFree):
    def test_the_uploaded_bytes_and_the_request_format_reach_the_transport_exactly(self):
        nv = Scripted(good_nv())
        app, _ = make(self, nv, Scripted(good_tv()))
        response = run(app)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(nv.calls), 1)
        call = nv.calls[0]
        self.assertEqual((call["url"], call["timeout"]), (nvidia.ENDPOINT, nvidia.DEFAULT_TIMEOUT_SECONDS))
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + NV_KEY)
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        request = json.loads(call["body"].decode("utf-8"))
        self.assertEqual(set(request), {"model", "max_tokens", "messages"})
        self.assertEqual((request["model"], request["max_tokens"]), (nvidia.MODEL, nvidia.DEFAULT_MAX_TOKENS))
        content = request["messages"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["text", "image_url"])
        self.assertEqual(content[0]["text"], PROMPT)                       # the fixed findings prompt, no hints
        url = content[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1], validate=True), PNG)   # the uploaded bytes, unchanged
        self.assertNotIn(NV_KEY.encode(), call["body"])                    # the key travels only in the header
        for name, value in call["headers"].items():
            if name != "Authorization":
                self.assertNotIn(NV_KEY, value)

    def test_a_valid_reply_becomes_validated_findings_with_provenance(self):
        app, _ = make(self, Scripted(good_nv()), Scripted(good_tv()))
        response = run(app)
        body = body_of(response)
        step = steps_of(body)["image_findings"]
        self.assertEqual((step["status"], step["result"]["findings"], step["result"]["model"]),
                         ("completed", GOOD, nvidia.MODEL))
        self.assertEqual(body["integrated"]["report"]["sections"]["image_inference"]["status"], "ok")
        note = body["integrated"]["provenance"]["image_findings"]
        self.assertIs(note["provider_called"], True)
        self.assertEqual(note["model"], nvidia.MODEL)
        self.assertTrue(note["implementation"].endswith("PromptedFindingsProvider"), note["implementation"])
        self.assertEqual(check_integrated_result(body["integrated"]), [])
        self.assertEqual(body["real_stages"], ["image", "research"])
        self.assert_clean(response)

    def test_a_single_code_fence_around_the_json_is_accepted(self):
        app, _ = make(self, Scripted(nv_reply("```json\n" + json.dumps(GOOD) + "\n```")), Scripted(good_tv()))
        self.assertEqual(steps_of(body_of(run(app)))["image_findings"]["status"], "completed")

    def test_replies_that_break_the_findings_contract_fail_the_stage_and_nothing_replaces_them(self):
        extra = dict(GOOD, confidence=0.9)
        cases = (
            ("prose", nv_reply("I think this is wheat"), "response_malformed"),
            ("wrong keys", nv_reply(json.dumps({"x": 1})), "response_malformed"),
            ("extra key", nv_reply(json.dumps(extra)), "response_malformed"),
            ("reasoning before json", nv_reply("Let me think... " + json.dumps(GOOD)), "response_malformed"),
            ("two fences", nv_reply("```json\n{}\n```\n```json\n{}\n```"), "response_malformed"),
            ("empty", nv_reply(""), "response_empty"),
            ("blank", nv_reply("   \n"), "response_empty"),
            ("content not text", (200, json.dumps({"choices": [{"message": {"content": [1]}}]}).encode()),
             "response_malformed"),
            ("no choices", (200, b'{"id": "x"}'), "response_malformed"),
            ("not json", (200, b"<html>gateway</html>"), "response_malformed"),
            ("body not bytes", (200, "text"), "response_malformed"),
        )
        for label, outcome, code in cases:
            with self.subTest(label):
                nv = Scripted(outcome)
                app, _ = make(self, nv, Scripted(good_tv()))
                response = run(app)
                body = body_of(response)
                steps = steps_of(body)
                self.assertEqual((steps["image_findings"]["status"], steps["image_findings"]["error_code"]),
                                 ("failed", code))
                self.assertIsNone(steps["image_findings"]["result"])
                self.assertEqual(len(nv.calls), 1)                                          # no retry
                self.assertEqual((steps["synthesis"]["status"], steps["synthesis"]["skip_reason"]),
                                 ("skipped", "dependency_not_completed"))
                self.assertIsNone(body["integrated"]["synthesis"])
                self.assertEqual(body["integrated"]["run_status"], "failed")
                self.assertEqual(body["integrated"]["report"]["sections"]["image_inference"]["status"], "error")
                self.assertNotIn("I think this is wheat", response.body.decode("utf-8"))
                self.assertEqual(check_integrated_result(body["integrated"]), [])

    def test_http_errors_become_closed_codes_with_one_request_and_no_provider_text(self):
        statuses = {400: "provider_rejected", 401: "provider_rejected", 403: "provider_rejected",
                    404: "provider_rejected", 422: "provider_rejected", 429: "provider_rejected",
                    500: "provider_unavailable", 502: "provider_unavailable", 503: "provider_unavailable",
                    302: "response_malformed"}
        for status, code in statuses.items():
            with self.subTest(status):
                nv = Scripted((status, ('{"detail": "%s %s"}' % (SECRET_TEXT, NV_KEY)).encode()))
                app, _ = make(self, nv, Scripted(good_tv()))
                response = run(app)
                step = steps_of(body_of(response))["image_findings"]
                self.assertEqual((step["status"], step["error_code"]), ("failed", code))
                self.assertEqual(len(nv.calls), 1)
                self.assert_clean(response)

    def test_timeouts_and_transport_failures_get_their_own_codes_and_leak_nothing(self):
        failures = ((TransportTimeout(), "provider_timeout"), (TransportError(), "provider_connection_failed"),
                    (OSError("socket " + NV_KEY), "provider_connection_failed"),
                    (TimeoutError(), "provider_timeout"),
                    (RuntimeError(SECRET_TEXT + " " + NV_KEY), "transport_failed"))
        for failure, expected in failures:
            with self.subTest(type(failure).__name__):
                nv = Scripted(failure)
                app, _ = make(self, nv, Scripted(good_tv()))
                response = run(app)
                step = steps_of(body_of(response))["image_findings"]
                self.assertEqual((step["status"], step["error_code"]), ("failed", expected))
                self.assertEqual(len(nv.calls), 1)
                self.assert_clean(response)
        nv = Scripted(ResponseTooLarge())
        app, _ = make(self, nv, Scripted(good_tv()))
        step = steps_of(body_of(run(app)))["image_findings"]
        self.assertEqual((step["status"], step["error_code"]), ("failed", "response_too_large"))

    def test_a_missing_key_and_a_non_png_send_nothing(self):
        nv = Scripted(good_nv())
        app, _ = make(self, nv, Scripted(good_tv()), nv_env={})
        step = steps_of(body_of(run(app)))["image_findings"]
        self.assertEqual((step["status"], step["error_code"]), ("failed", "credential_missing"))
        self.assertEqual(nv.calls, [])
        stage = stages_of(body_of(run(app)))["image_findings"]
        self.assertIs(stage["provider_called"], True)            # the adapter was invoked; it refused locally
        nv = Scripted(good_nv())
        app, _ = make(self, nv, Scripted(good_tv()))
        body = body_of(run(app, png=base64.b64encode(b"not a png at all").decode("ascii")))
        self.assertEqual(steps_of(body)["image_findings"]["error_code"], "image_unsupported")
        self.assertEqual(nv.calls, [])
        self.assertIs(stages_of(body)["image_findings"]["provider_called"], False)   # never reached the adapter

    def test_a_failed_real_image_stage_is_never_replaced_by_the_fake_analyzer(self):
        app, _ = make(self, Scripted((500, b"")), Scripted(good_tv()))
        body = body_of(run(app))
        self.assertIsNone(steps_of(body)["image_findings"]["result"])
        self.assertNotIn("FAKE-vision-model", json.dumps(body))
        self.assertEqual(body["providers"]["image"], "real")


class TavilyChainTests(SecretFree):
    def test_the_question_and_the_allowed_domains_reach_the_transport_exactly(self):
        tv = Scripted(good_tv())
        app, _ = make(self, Scripted(good_nv()), tv)
        response = run(app)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(tv.calls), 1)
        call = tv.calls[0]
        self.assertEqual((call["url"], call["timeout"]), (tavily.ENDPOINT, tavily.DEFAULT_TIMEOUT_SECONDS))
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + TV_KEY)
        self.assertEqual(json.loads(call["body"].decode("utf-8")), {
            "query": QUESTION, "search_depth": "basic", "topic": "general", "max_results": tavily.DEFAULT_MAX_RESULTS,
            "include_answer": False, "include_domains": DOMAINS, "include_published_date": True})
        self.assertNotIn(TV_KEY.encode(), call["body"])
        self.assert_clean(response)

    def test_results_outside_the_allowed_domains_are_dropped_and_the_rest_keep_their_provenance(self):
        results = [
            {"title": "T1", "url": "https://agri.example.org/a", "content": "S1"},
            {"title": "T2", "url": "https://sub.extension.example.net/b", "content": "S2", "published_date": "2025-01-01",
             "score": 0.9},
            {"title": "off", "url": "https://evil.example/c", "content": "x"},
            {"title": "suffix", "url": "https://agri.example.org.evil.example/d", "content": "x"},
            {"title": "userinfo", "url": "https://agri.example.org@evil.example/e", "content": "x"},
            {"title": "scheme", "url": "ftp://agri.example.org/f", "content": "x"},
            {"title": "empty", "url": "https://agri.example.org/g", "content": ""},
            "not a dict",
        ]
        app, _ = make(self, Scripted(good_nv()), Scripted(tv_reply(results)))
        response = run(app)
        body = body_of(response)
        step = steps_of(body)["research_search"]
        self.assertEqual(step["status"], "completed")
        self.assertEqual([i["url"] for i in step["result"]["items"]],
                         ["https://agri.example.org/a", "https://sub.extension.example.net/b"])   # verbatim
        self.assertEqual((step["result"]["dropped"], step["result"]["query"], step["result"]["retrieved_at"]),
                         (6, QUESTION, CLOCK_TIME))
        entries = body["integrated"]["report"]["sections"]["retrieved_sources"]["entries"]
        self.assertEqual([e["url"] for e in entries], [i["url"] for i in step["result"]["items"]])
        for entry in entries:
            self.assertEqual((entry["provenance"], entry["review_status"]), ("retrieved_source", "unreviewed"))
        self.assertEqual(entries[1]["title"], "T2")
        self.assertNotIn("evil.example", response.body.decode("utf-8"))
        note = body["integrated"]["provenance"]["research_search"]
        self.assertEqual((note["provider_called"], note["query"], note["retrieved_at"], note["dropped"]),
                         (True, QUESTION, CLOCK_TIME, 6))
        codes = [c["code"] for c in body["integrated"]["report"]["sections"]["retrieved_sources"]["caveats"]]
        self.assertIn("domains_are_owner_configured", codes)
        self.assertEqual(check_integrated_result(body["integrated"]), [])

    def test_empty_and_fully_filtered_results_are_a_failed_stage_not_a_success(self):
        for label, results, dropped in (("empty", [], 0),
                                        ("all off-domain", [{"title": "x", "url": "https://evil.example/", "content": "x"}], 1)):
            with self.subTest(label):
                app, _ = make(self, Scripted(good_nv()), Scripted(tv_reply(results)))
                body = body_of(run(app))
                step = steps_of(body)["research_search"]
                self.assertEqual((step["status"], step["error_code"]), ("failed", "no_results"))
                self.assertEqual(body["integrated"]["provenance"]["research_search"]["dropped"], dropped)
                self.assertEqual(steps_of(body)["synthesis"]["skip_reason"], "dependency_not_completed")
                self.assertEqual(body["integrated"]["report"]["sections"]["retrieved_sources"]["entries"], [])

    def test_malformed_replies_fail_the_stage(self):
        for label, outcome in (("not json", (200, b"nope")), ("list", (200, b"[]")),
                               ("results not a list", (200, b'{"results": "x"}')), ("no results key", (200, b'{"a": 1}')),
                               ("body not bytes", (200, "text")), ("3xx", (301, b""))):
            with self.subTest(label):
                tv = Scripted(outcome)
                app, _ = make(self, Scripted(good_nv()), tv)
                step = steps_of(body_of(run(app)))["research_search"]
                self.assertEqual((step["status"], step["error_code"]), ("failed", "response_malformed"))
                self.assertEqual(len(tv.calls), 1)

    def test_http_errors_become_closed_codes_with_one_request_and_no_provider_text(self):
        statuses = {429: "rate_limited", 432: "usage_limit_exceeded", 433: "usage_limit_exceeded",
                    400: "provider_rejected", 401: "provider_rejected", 403: "provider_rejected",
                    422: "provider_rejected", 500: "provider_unavailable", 503: "provider_unavailable"}
        for status, code in statuses.items():
            with self.subTest(status):
                tv = Scripted((status, ('{"detail": "%s %s"}' % (SECRET_TEXT, TV_KEY)).encode()))
                app, _ = make(self, Scripted(good_nv()), tv)
                response = run(app)
                step = steps_of(body_of(response))["research_search"]
                self.assertEqual((step["status"], step["error_code"]), ("failed", code))
                self.assertEqual(len(tv.calls), 1)
                self.assert_clean(response)

    def test_timeouts_and_transport_failures_get_their_own_code_and_leak_nothing(self):
        for failure, code in ((TransportTimeout(), "provider_timeout"), (TransportError(), "transport_failed"),
                              (tavily.TransportConnectionFailed(), "provider_connection_failed"),
                              (OSError("socket " + TV_KEY), "transport_failed"),
                              (RuntimeError(SECRET_TEXT), "transport_failed")):
            with self.subTest(type(failure).__name__):
                tv = Scripted(failure)
                app, _ = make(self, Scripted(good_nv()), tv)
                response = run(app)
                step = steps_of(body_of(response))["research_search"]
                self.assertEqual((step["status"], step["error_code"]), ("failed", code))
                self.assertEqual(len(tv.calls), 1)
                self.assert_clean(response)
        app, _ = make(self, Scripted(good_nv()), Scripted(ResponseTooLarge()))
        self.assertEqual(steps_of(body_of(run(app)))["research_search"]["error_code"], "response_too_large")

    def test_a_missing_key_sends_nothing(self):
        tv = Scripted(good_tv())
        app, _ = make(self, Scripted(good_nv()), tv, tv_env={})
        step = steps_of(body_of(run(app)))["research_search"]
        self.assertEqual((step["status"], step["error_code"]), ("failed", "credential_missing"))
        self.assertEqual(tv.calls, [])

    def test_a_bad_question_or_extra_fields_are_refused_before_any_provider_is_called(self):
        nv, tv = Scripted(good_nv()), Scripted(good_tv())
        app, store = make(self, nv, tv)
        for question in ("", "x" * 201, "bad\ntext", 5):
            self.assertEqual(run(app, question=question).status, 400, repr(question)[:20])
        extra = post(app, {"scenario": "normal_operation", "question": QUESTION, "include_domains": ["evil.example"],
                           "providers": {"research": "real"}})
        self.assertEqual(extra.status, 400)                                 # the domain list cannot be sent by a client
        self.assertEqual((nv.calls, tv.calls), ([], []))
        self.assertEqual(len(store.list_records()), 0)

    def test_a_failed_real_research_stage_is_never_replaced_by_the_fake_client(self):
        app, _ = make(self, Scripted(good_nv()), Scripted((500, b"")))
        body = body_of(run(app))
        self.assertIsNone(steps_of(body)["research_search"]["result"])
        self.assertNotIn("FAKE", json.dumps(body["integrated"]["report"]["sections"]["retrieved_sources"]))


class StateReportingTests(NoNetwork):
    def test_configured_selected_attempted_failed_and_skipped_are_reported_separately(self):
        nv, tv = Scripted((503, b"")), Scripted(good_tv())
        app, _ = make(self, nv, tv)
        state = body_of(get(app, "/api/state"))["integrated"]["real_available"]
        self.assertEqual(state, {"image": True, "research": True, "compute": False, "synthesis": False})   # configured
        self.assertEqual((nv.calls, tv.calls), ([], []))                  # and not called by looking
        body = body_of(run(app, image="real", research="real"))
        self.assertEqual(body["providers"], {"image": "real", "research": "real", "compute": "fake", "synthesis": "fake"})
        stages = stages_of(body)
        self.assertEqual((stages["image_findings"]["status"], stages["image_findings"]["error_code"],
                          stages["image_findings"]["provider_called"]), ("failed", "provider_unavailable", True))
        self.assertEqual((stages["research"]["status"], stages["research"]["provider_called"]), ("completed", True))
        self.assertEqual((stages["synthesis"]["status"], stages["synthesis"]["skip_reason"],
                          stages["synthesis"]["provider_called"]), ("skipped", "dependency_not_completed", False))
        self.assertEqual(body["integrated"]["integration"]["specialist"]["status"], "skipped")

    def test_nothing_real_is_called_unless_that_stage_is_set_to_real(self):
        nv, tv = Scripted(good_nv()), Scripted(good_tv())
        app, _ = make(self, nv, tv)
        for providers in ({}, {"image": "fake", "research": "fake"}, {"image": "skip", "research": "skip"}):
            payload = {"scenario": "normal_operation", "question": QUESTION, "providers": providers}
            if providers.get("image") == "fake":
                payload["image_png_base64"] = PNG_B64
            self.assertEqual(post(app, payload).status, 200)
        self.assertEqual((nv.calls, tv.calls), ([], []))


if __name__ == "__main__":
    unittest.main()
