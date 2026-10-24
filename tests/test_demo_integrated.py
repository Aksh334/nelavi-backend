"""Tests for the integrated demo path (IW-3): POST /api/run-integrated and its server wiring.

Everything here is offline. A socket tripwire and an urllib tripwire fail any test that tries to reach out. The
"real" provider slots are filled with local stand-ins that count their calls, because a test cannot show that a
vendor works; what it can show is that the route calls the injected object for a stage set to "real", never calls a
fake instead, never falls back, and refuses a "real" stage the server was not started with.
"""

import base64
import io
import json
import re
import unittest
import urllib.request
from unittest import mock

from demo_app import server
from demo_app.app import MAX_SECOND_OPINION_BYTES, DemoApp
from demo_app.fake_findings import FakeFindingsAnalyzer
from demo_app.service import INTEGRATED_FAKE_LABEL, INTEGRATED_MIXED_LABEL, DemoService
from image_analysis.boundary import ERROR_CODES as IMAGE_ERROR_CODES
from image_analysis.boundary import error_result
from orchestration.integrated import check_integrated_result
from orchestration.synthesis import FakeSynthesisProvider
from orchestration.task_runner import FakeTaskRunner
from research.tavily import ERROR_CODES as RESEARCH_ERROR_CODES
from research.tavily import ResearchItem, ResearchResult
from storage.record_store import RecordStore

HOST = "127.0.0.1:8765"          # only a Host-header string for the in-process app; no port is opened
ROUTE = "/api/run-integrated"
QUESTION = "how should soil moisture readings be read"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image"
PNG_B64 = base64.b64encode(PNG).decode("ascii")
ALL_FAKE = {"image": "fake", "research": "fake", "compute": "fake", "synthesis": "fake"}
ALL_REAL = {"image": "real", "research": "real", "compute": "real", "synthesis": "real"}


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


def call(app, method, target, body=b"", headers=None, csrf=True):
    sent = {"host": HOST}
    if method == "POST":
        if csrf:
            sent["x-nelavi-demo"] = "1"
        sent["content-length"] = str(len(body))
    sent.update(headers or {})
    return app.handle_request(method, target, sent, io.BytesIO(body).read)


def post(app, payload, route=ROUTE):
    return call(app, "POST", route, json.dumps(payload).encode("utf-8"), {"content-type": "application/json"})


def body_of(response):
    return json.loads(response.body.decode("utf-8"))


def stages_of(body):
    return {stage["stage"]: stage for stage in body["integrated"]["integration"]["stages"]}


def steps_of(body):
    return {entry["name"]: entry for entry in body["integrated"]["run"]["steps"]}


def history_total(app):
    return body_of(call(app, "GET", "/api/history"))["total"]


def joined(text):
    """Source text with whitespace collapsed and adjacent string literals ("a" + "b") joined, to search phrases."""
    return re.sub(r'"\s*\+\s*"', "", " ".join(text.split()))


class CountingAnalyzer(FakeFindingsAnalyzer):
    """A local stand-in for a REAL image analyzer: valid findings, counts its calls, contacts nothing."""

    def __init__(self):
        self.calls = 0

    def analyze(self, request):
        self.calls += 1
        return super().analyze(request)


class FailingAnalyzer:
    def __init__(self):
        self.calls = 0

    def analyze(self, request):
        self.calls += 1
        return error_result("provider_unavailable", "stand-in")


class CountingSearch:
    def __init__(self, result=None):
        self.result, self.queries = result, []

    def search(self, query):
        self.queries.append(query)
        return self.result


def search_result():
    items = (ResearchItem(title="T", url="https://agri.example.org/a", snippet="S", published_date=None, score=None),)
    return ResearchResult(status="ok", error_code=None, query=QUESTION, items=items, dropped=0,
                          retrieved_at="2026-01-01T00:00:00+00:00")


def failing_search_result():
    return ResearchResult(status="error", error_code="provider_unavailable", query=QUESTION, items=(), dropped=0,
                          retrieved_at=None)


class CountingRunner(FakeTaskRunner):
    def __init__(self):
        super().__init__()
        self.count = 0

    def run(self, task, payload):
        self.count += 1
        return super().run(task, payload)


class CountingWriter(FakeSynthesisProvider):
    def __init__(self):
        super().__init__()
        self.count = 0

    def synthesize(self, inputs):
        self.count += 1
        return super().synthesize(inputs)


def make(testcase, **kwargs):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    service = DemoService(store, **kwargs)
    return DemoApp(service, {HOST}), service


def make_real(testcase, analyzer=None, search=None):
    parts = {"analyzer": analyzer or CountingAnalyzer(), "search": search or CountingSearch(search_result()),
             "runner": CountingRunner(), "writer": CountingWriter()}
    app, service = make(testcase, findings_analyzer=parts["analyzer"], research_client=parts["search"],
                        research_domains=["agri.example.org"], compute_runner=parts["runner"],
                        synthesis_provider=parts["writer"])
    return app, service, parts


class DefaultAndFakeTests(NoNetwork):
    def setUp(self):
        super().setUp()
        self.app, self.service = make(self)

    def test_the_default_request_skips_every_stage_and_stores_one_record(self):
        response = post(self.app, {"scenario": "normal_operation"})
        self.assertEqual(response.status, 200)
        body = body_of(response)
        self.assertEqual((body["workflow"], body["label"], body["real_stages"], body["data_sent"]),
                         ("integrated", INTEGRATED_FAKE_LABEL, [], {}))
        self.assertEqual(body["providers"], {"image": "skip", "research": "skip", "compute": "skip", "synthesis": "skip"})
        self.assertEqual(body["integrated"]["run_status"], "skipped")
        self.assertTrue(all(s["status"] == "skipped" for s in body["integrated"]["run"]["steps"]))
        self.assertIsNone(body["integrated"]["synthesis"])
        self.assertEqual(check_integrated_result(body["integrated"]), [])
        self.assertEqual(history_total(self.app), 1)
        self.assertEqual(body["stored_id"], body["integrated"]["record_id"])

    def test_all_fake_stages_complete_and_the_synthesis_is_labelled_unverified(self):
        response = post(self.app, {"scenario": "normal_operation", "question": QUESTION, "image_png_base64": PNG_B64,
                                   "providers": ALL_FAKE})
        self.assertEqual(response.status, 200)
        body = body_of(response)
        self.assertEqual((body["integrated"]["run_status"], body["label"], body["real_stages"]),
                         ("completed", INTEGRATED_FAKE_LABEL, []))
        self.assertTrue(all(s["status"] == "completed" for s in stages_of(body).values()))
        self.assertEqual(body["integrated"]["synthesis"]["kind"], "model_generated_unverified")
        self.assertIs(body["integrated"]["synthesis"]["fake"], True)
        self.assertEqual(body["integrated"]["integration"]["specialist"]["skip_reason"], "specialist_role_undefined")
        self.assertEqual(check_integrated_result(body["integrated"]), [])
        self.assertEqual(history_total(self.app), 1)

    def test_a_non_png_image_fails_only_the_image_stage_and_synthesis_is_skipped(self):
        junk = base64.b64encode(b"not a png at all").decode("ascii")
        body = body_of(post(self.app, {"scenario": "normal_operation", "question": QUESTION,
                                       "image_png_base64": junk, "providers": ALL_FAKE}))
        steps = steps_of(body)
        self.assertEqual((steps["image_findings"]["status"], steps["image_findings"]["error_code"]),
                         ("failed", "image_unsupported"))
        self.assertEqual((steps["synthesis"]["status"], steps["synthesis"]["skip_reason"]),
                         ("skipped", "dependency_not_completed"))
        self.assertEqual(steps["research_search"]["status"], "completed")
        self.assertEqual(body["integrated"]["run_status"], "failed")
        self.assertEqual(check_integrated_result(body["integrated"]), [])

    def test_a_sensor_fault_scenario_still_runs(self):
        body = body_of(post(self.app, {"scenario": "sensor_fault", "providers": {"compute": "fake"}}))
        self.assertEqual(stages_of(body)["sensor_summary"]["status"], "completed")
        self.assertGreater(body["integrated"]["sensor_summary"]["summary"]["sensors_fault"], 0)

    def test_the_state_lists_the_integrated_path_and_no_real_provider_by_default(self):
        state = body_of(call(self.app, "GET", "/api/state"))["integrated"]
        self.assertTrue(state["available"])
        self.assertEqual(state["stages"], ["image", "research", "compute", "synthesis"])
        self.assertEqual(state["modes"], ["skip", "fake", "real"])
        self.assertEqual(state["real_available"], {"image": False, "research": False, "compute": False,
                                                    "synthesis": False})


class RealSlotTests(NoNetwork):
    """The 'real' slots, filled with counting stand-ins."""

    def test_a_real_stage_that_is_not_configured_is_refused_and_nothing_runs_or_is_stored(self):
        app, _ = make(self)
        for stage in ("image", "research", "compute", "synthesis"):
            response = post(app, {"scenario": "normal_operation", "question": QUESTION, "image_png_base64": PNG_B64,
                                  "providers": {stage: "real"}})
            self.assertEqual(response.status, 400, stage)
            self.assertIn("no real provider configured", body_of(response)["error"])
        self.assertEqual(history_total(app), 0)

    def test_every_real_stage_calls_its_injected_object_exactly_once(self):
        app, _, parts = make_real(self)
        response = post(app, {"scenario": "normal_operation", "question": QUESTION, "image_png_base64": PNG_B64,
                              "providers": ALL_REAL})
        body = body_of(response)
        self.assertEqual(response.status, 200)
        self.assertEqual((parts["analyzer"].calls, len(parts["search"].queries), parts["runner"].count,
                          parts["writer"].count), (1, 1, 1, 1))
        self.assertEqual((body["integrated"]["run_status"], body["label"]), ("completed", INTEGRATED_MIXED_LABEL))
        self.assertEqual(body["real_stages"], ["image", "research", "compute", "synthesis"])
        self.assertEqual(set(body["data_sent"]), {"image", "research", "compute", "synthesis"})
        self.assertTrue(all(s["provider_called"] is True for s in stages_of(body).values()))
        self.assertEqual(check_integrated_result(body["integrated"]), [])

    def test_merely_opening_the_demo_calls_no_provider(self):
        app, _, parts = make_real(self)
        for method, target in (("GET", "/"), ("GET", "/static/app.js"), ("GET", "/static/style.css"),
                               ("GET", "/api/state"), ("GET", "/api/history")):
            self.assertEqual(call(app, method, target).status, 200, target)
        self.assertEqual((parts["analyzer"].calls, len(parts["search"].queries), parts["runner"].count,
                          parts["writer"].count), (0, 0, 0, 0))

    def test_a_failing_real_compute_stage_stays_failed_and_is_never_replaced_by_the_fake_runner(self):
        from orchestration.nodus_adapter import NodusTaskRunner

        class Unreachable:
            def spawn(self, payload):
                raise RuntimeError("service said " + "canary-not-a-real-key")

        app, _ = make(self, compute_runner=NodusTaskRunner(lambda: Unreachable()))
        body = body_of(post(app, {"scenario": "normal_operation", "providers": {"compute": "real"}}))
        step = steps_of(body)["summarize_record"]
        self.assertEqual((step["status"], step["error_code"]), ("failed", "task_failed"))
        self.assertIsNone(body["integrated"]["sensor_summary"])
        self.assertEqual(body["real_stages"], ["compute"])
        self.assertNotIn("canary", json.dumps(body))
        self.assertEqual(check_integrated_result(body["integrated"]), [])

    def test_choosing_real_compute_on_a_server_without_it_is_refused_not_faked(self):
        app, _ = make(self)
        response = post(app, {"scenario": "normal_operation", "providers": {"compute": "real"}})
        self.assertEqual(response.status, 400)
        self.assertIn("no real provider configured", body_of(response)["error"])
        self.assertEqual(history_total(app), 0)

    def test_the_state_reports_which_real_stages_are_available(self):
        app, _, _ = make_real(self)
        self.assertEqual(body_of(call(app, "GET", "/api/state"))["integrated"]["real_available"],
                         {"image": True, "research": True, "compute": True, "synthesis": True})

    def test_a_failing_real_stage_stays_failed_and_no_fake_takes_its_place(self):
        failing = FailingAnalyzer()
        app, _, parts = make_real(self, analyzer=failing)
        body = body_of(post(app, {"scenario": "normal_operation", "question": QUESTION, "image_png_base64": PNG_B64,
                                  "providers": ALL_REAL}))
        steps = steps_of(body)
        self.assertEqual((steps["image_findings"]["status"], steps["image_findings"]["error_code"]),
                         ("failed", "provider_unavailable"))
        self.assertEqual(failing.calls, 1)
        self.assertEqual((steps["synthesis"]["status"], steps["synthesis"]["skip_reason"]),
                         ("skipped", "dependency_not_completed"))
        self.assertEqual(parts["writer"].count, 0)
        self.assertIsNone(body["integrated"]["synthesis"])
        self.assertEqual(check_integrated_result(body["integrated"]), [])

    def test_a_failing_real_search_skips_the_synthesis(self):
        app, _, parts = make_real(self, search=CountingSearch(failing_search_result()))
        body = body_of(post(app, {"scenario": "normal_operation", "question": QUESTION, "image_png_base64": PNG_B64,
                                  "providers": ALL_REAL}))
        self.assertEqual(steps_of(body)["research_search"]["status"], "failed")
        self.assertEqual(steps_of(body)["synthesis"]["status"], "skipped")
        self.assertEqual(parts["writer"].count, 0)

    def test_a_mix_of_real_and_fake_stages_reports_exactly_which_are_real(self):
        app, _, parts = make_real(self)
        body = body_of(post(app, {"scenario": "normal_operation", "question": QUESTION, "image_png_base64": PNG_B64,
                                  "providers": {"image": "real", "research": "fake", "compute": "fake",
                                                "synthesis": "fake"}}))
        self.assertEqual((body["real_stages"], list(body["data_sent"])), (["image"], ["image"]))
        self.assertEqual((parts["analyzer"].calls, len(parts["search"].queries), parts["runner"].count,
                          parts["writer"].count), (1, 0, 0, 0))
        self.assertEqual(body["integrated"]["run_status"], "completed")

    def test_constructor_refuses_providers_without_the_expected_method(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        for key in ("findings_analyzer", "compute_runner", "synthesis_provider"):
            with self.assertRaises(TypeError):
                DemoService(store, **{key: object()})


class ValidationTests(NoNetwork):
    def setUp(self):
        super().setUp()
        self.app, self.service = make(self)

    def refused(self, payload, expected_status=400):
        response = post(self.app, payload)
        self.assertEqual(response.status, expected_status, payload)
        self.assertIn("error", body_of(response))
        self.assertEqual(history_total(self.app), 0, payload)        # nothing was stored
        return body_of(response)["error"]

    def test_bad_requests_are_refused_before_anything_runs(self):
        for payload in (
                None, [], "x", {}, {"scenario": 5}, {"scenario": "no_such_scenario"},
                {"scenario": "normal_operation", "extra": 1},
                {"scenario": "normal_operation", "providers": "fake"},
                {"scenario": "normal_operation", "providers": {"nope": "fake"}},
                {"scenario": "normal_operation", "providers": {"image": "maybe"}},
                {"scenario": "normal_operation", "providers": {"image": 1}},
                {"scenario": "normal_operation", "providers": {"research": "fake"}},        # a question is needed
                {"scenario": "normal_operation", "providers": {"image": "fake"}},           # an image is needed
                {"scenario": "normal_operation", "image_png_base64": PNG_B64},              # image stage is skip
                {"scenario": "normal_operation", "question": ""},
                {"scenario": "normal_operation", "question": "x" * 201},
                {"scenario": "normal_operation", "question": "bad\ntext"},
                {"scenario": "normal_operation", "providers": {"image": "fake"}, "image_png_base64": "***"},
                {"scenario": "normal_operation", "providers": {"image": "fake"}, "image_png_base64": ""},
                {"scenario": "normal_operation", "providers": {"image": "fake"}, "image_png_base64": 5},
                {"scenario": "normal_operation", "crop": 5},
                {"scenario": "normal_operation", "crop": "<b>"},
        ):
            with self.subTest(payload=str(payload)[:60]):
                self.refused(payload)

    def test_an_oversized_image_is_refused(self):
        big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (1024 * 1024)).decode("ascii")
        self.refused({"scenario": "normal_operation", "providers": {"image": "fake"}, "image_png_base64": big})

    def test_the_wrong_content_type_the_csrf_header_and_bad_json_are_refused(self):
        raw = json.dumps({"scenario": "normal_operation"}).encode("utf-8")
        self.assertEqual(call(self.app, "POST", ROUTE, raw, {"content-type": "text/plain"}).status, 415)
        self.assertEqual(call(self.app, "POST", ROUTE, raw, {"content-type": "application/json"}, csrf=False).status, 403)
        self.assertEqual(call(self.app, "POST", ROUTE, b"{not json", {"content-type": "application/json"}).status, 400)
        self.assertEqual(call(self.app, "POST", ROUTE, b"[" * 100000, {"content-type": "application/json"}).status, 400)
        self.assertEqual(call(self.app, "GET", ROUTE).status, 404)
        self.assertEqual(history_total(self.app), 0)

    def test_a_body_over_the_route_limit_is_refused_before_it_is_read(self):
        too_long = b"x" * (MAX_SECOND_OPINION_BYTES + 1)
        response = call(self.app, "POST", ROUTE, too_long, {"content-type": "application/json"})
        self.assertEqual(response.status, 413)

    def test_the_other_routes_are_unchanged(self):
        self.assertEqual(post(self.app, {"scenario": "normal_operation"}, route="/api/run").status, 200)
        self.assertEqual(post(self.app, {"scenario": "normal_operation"}, route="/api/run-orchestrated").status, 200)


class PageTests(NoNetwork):
    """What can be checked without a browser: the served text. The page script has NOT been run in a browser."""

    def setUp(self):
        super().setUp()
        app, _ = make(self)
        self.html = call(app, "GET", "/").body.decode("utf-8")
        self.script = call(app, "GET", "/static/app.js").body.decode("utf-8")

    def function_text(self, name):
        body = self.script[self.script.index("function %s(" % name):]
        return body[:body.index("\n}\n")]

    def primary_panel(self):
        start = self.html.index('<section id="integrated">')
        return self.html[start:self.html.index('<section id="supporting-tools">')]

    def help_table(self):
        start = self.script.index("const IW_CODE_HELP = {")
        block = self.script[start:self.script.index("\n};", start)]
        return dict(re.findall(r'^  ([a-z_]+): "([^"\n]+)",?$', block, re.M))

    def test_every_closed_image_and_research_code_has_fixed_wording_on_the_main_page(self):
        table = self.help_table()
        for code in set(IMAGE_ERROR_CODES) | set(RESEARCH_ERROR_CODES):
            self.assertIn(code, table, code)
            self.assertGreater(len(table[code]), 20, code)
        for code in ("provider_timeout", "provider_connection_failed", "transport_failed",
                     "provider_unreachable"):
            self.assertIn(code, table)

    def test_stage_step_and_research_failures_use_the_fixed_wording_and_nothing_from_the_provider(self):
        shown = self.function_text("renderIntegrated")
        self.assertEqual(shown.count("errorDetail("), 3)
        self.assertNotIn('"error code " +', shown)
        self.assertNotIn("innerHTML", self.script)
        helper = self.function_text("codeHelp")
        self.assertIn("hasOwnProperty", helper)  # a code such as "constructor" gets the neutral line
        self.assertIn("No further explanation is available", helper)
        for text in self.help_table().values():
            self.assertNotIn("<", text)

    def test_the_integrated_panel_is_the_first_panel_after_the_hero_and_starts_disabled(self):
        sections = re.findall(r'<section id="([^"]+)">', self.html)
        self.assertEqual(sections[0], "integrated")
        self.assertEqual(sections[1], "supporting-tools")
        self.assertLess(self.html.index('<div class="hero">'), self.html.index('<main id="main">'))
        self.assertLess(self.html.index('<main id="main">'), self.html.index('<section id="integrated">'))
        self.assertIn("<h2>Run the integrated workflow</h2>", self.html)
        self.assertIn("<h2>Supporting tools</h2>", self.html)
        self.assertRegex(self.html, r'<button id="iw-run" type="button" disabled>')
        nav = self.html[self.html.index('<nav class="nav"'):self.html.index("</nav>")]
        self.assertEqual(re.findall(r'href="#([^"]+)"', nav)[:2], ["integrated", "supporting-tools"])
        self.assertNotIn("8 Integrated", self.html)

    def test_every_standalone_panel_follows_the_supporting_tools_heading(self):
        marker = self.html.index('<section id="supporting-tools">')
        for name in ("overview", "rover", "record", "image", "history", "second-opinion", "research", "orchestrated"):
            self.assertGreater(self.html.index('<section id="%s">' % name), marker, name)
        self.assertEqual(len(re.findall(r'<section id="integrated">', self.html)), 1)

    def test_the_panel_has_its_own_inputs_that_do_not_depend_on_section_one(self):
        panel = self.primary_panel()
        for element in ("iw-scenario", "iw-crop", "iw-region", "iw-stage", "iw-scenario-description"):
            self.assertIn('id="%s"' % element, panel, element)
        for control in ("iw-scenario", "iw-crop", "iw-region", "iw-stage"):
            self.assertIn('<label for="%s">' % control, panel, control)
        run = self.function_text("runIntegrated")
        for used in ('$("iw-scenario")', '$("iw-crop")', '$("iw-region")', '$("iw-stage")'):
            self.assertIn(used, run, used)
        for section_one in ('$("scenario")', '$("user-crop")', '$("user-region")', '$("user-stage")'):
            self.assertNotIn(section_one, run, section_one)
        self.assertIn("older stand-alone runner", self.html)          # Section 1 says it saves its own Record
        self.assertIn("running both saves two Records", " ".join(self.html.split()))

    def test_the_text_says_real_stages_send_data_and_the_synthesis_is_not_evidence(self):
        text = " ".join(self.primary_panel().split())
        for fragment in ("A real provider is used only for a stage you set to Real and only when you press Run",
                         "a provider being configured never means it has been called",
                         "never replaced by a fake", "not evidence", "specialist role has no definition yet",
                         "Nothing is sent anywhere until you press Run"):
            self.assertIn(fragment, text, fragment)

    def test_the_shipped_page_makes_no_claim_about_the_modes_before_the_server_answers(self):
        lowered = " ".join(self.html.lower().split())
        for claim in ("mixed mode", "no network", "no ai model", "simulated demonstration", "real providers are available"):
            self.assertNotIn(claim, lowered, claim)
        self.assertIn("Checking application mode", self.html[self.html.index('id="banner"'):][:300])

    def test_defaults_are_local_and_never_a_real_provider(self):
        configure = self.function_text("configureIntegrated")
        self.assertIn('select.value = iwConfig.modes.indexOf("fake") >= 0 ? "fake" : "skip";', configure)
        self.assertNotIn('"real";', configure.replace('mode === "real"', ""))
        run = self.function_text("runIntegrated")
        self.assertLess(run.index('providers.image === "real"'), run.index("IW_PLACEHOLDER_PNG"))
        self.assertEqual(len(re.findall(r"\brunIntegrated\b", self.script)), 2)    # the definition and the click handler
        self.assertIn('$("iw-run").addEventListener("click", runIntegrated)', self.script)
        self.assertEqual(self.script.count('"/api/run-integrated"'), 1)

    def test_the_first_click_defaults_give_a_complete_local_run_without_any_key(self):
        constants = dict(re.findall(r'const (IW_DEFAULT_QUESTION|IW_PLACEHOLDER_PNG) = "([^"]*)";', self.script))
        self.assertEqual(set(constants), {"IW_DEFAULT_QUESTION", "IW_PLACEHOLDER_PNG"})
        png = base64.b64decode(constants["IW_PLACEHOLDER_PNG"], validate=True)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        app, _ = make(self)                                            # no provider and no key anywhere
        scenario = body_of(call(app, "GET", "/api/state"))["scenarios"][0]["name"]
        payload = {"scenario": scenario, "crop": "", "region": "", "growth_stage": "",
                   "question": constants["IW_DEFAULT_QUESTION"], "image_png_base64": constants["IW_PLACEHOLDER_PNG"],
                   "providers": ALL_FAKE}
        response = post(app, payload)
        body = body_of(response)
        self.assertEqual(response.status, 200)
        self.assertEqual(body["integrated"]["run_status"], "completed")
        self.assertTrue(all(s["status"] == "completed" for s in stages_of(body).values()))
        self.assertEqual(body["real_stages"], [])
        self.assertEqual(history_total(app), 1)

    def test_the_waiting_message_is_honest_about_a_synchronous_server(self):
        run = joined(self.function_text("runIntegrated"))
        self.assertIn("cannot show progress while the server works", run)
        self.assertIn("the rest of this demo will not respond meanwhile", run)
        for claim in ("stage 1 of", "% complete", "progress bar"):
            self.assertNotIn(claim, run.lower())

    def test_the_script_uses_the_shared_helper_the_demo_header_and_stays_text_only(self):
        self.assertIn('api("/api/run-integrated"', self.script)
        body = self.function_text("runIntegrated")
        for expected in ("SEND", "iwBusy", "JSON.stringify", "finally", "readFileAsBase64"):
            self.assertIn(expected, body)
        render = self.function_text("renderIntegrated")
        self.assertNotIn("href", render)
        self.assertNotIn('createElement("a")', render)
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write"):
            self.assertNotIn(banned, self.script)

    def test_the_page_separates_fake_runs_real_calls_and_unverified_output(self):
        for fragment in ("a fake stand-in ran locally (no service was contacted)",
                         "the real provider's adapter was invoked and a request was sent or attempted",
                         "adapter was invoked but refused locally",
                         "Unverified model output", "Unreviewed search results", "Model-generated and unverified",
                         "not verified evidence"):
            self.assertIn(fragment, self.script, fragment)
        self.assertNotIn("an injected provider object was invoked", self.script)

    def test_a_real_stage_is_offered_only_when_the_server_state_says_it_is_configured(self):
        configure = self.function_text("configureIntegrated")
        self.assertIn("iwConfig.real[stage] !== true", configure)
        self.assertIn("raw.real_available", configure)
        self.assertIn("state.integrated", self.script)

    def test_every_part_of_the_response_has_a_place_on_the_page(self):
        render = self.function_text("renderIntegrated")
        for fragment in ("result.run_status", "result.record_id", "result.run_id", "result.integration.stages",
                         "st.error_code", "st.skip_reason", "st.provider_called", "result.integration.specialist",
                         "result.run.steps", "result.sensor_summary", "result.synthesis", "syn.statements", "st.cites",
                         "syn.uncertainties", "sources.error_code", "sources.entries",
                         "s.recommendations.supported", "s.uncertain_or_unavailable", "result.provenance",
                         "data.real_stages", "data.data_sent", "data.data_source", "data.label"):
            self.assertIn(fragment, render, fragment)
        self.assertIn("The response was not understood", self.script)


class BannerTests(NoNetwork):
    """The hero banner is built by the page script from /api/state. The script was NOT run (no browser or Node run is
    authorized), so these tests check the code that builds it and the state it reads, not the rendered text."""

    def setUp(self):
        super().setUp()
        app, _ = make(self)
        self.script = call(app, "GET", "/static/app.js").body.decode("utf-8")

    def function_text(self, name):
        body = self.script[self.script.index("function %s(" % name):]
        return joined(body[:body.index("\n}\n")])

    def test_each_available_provider_has_its_own_condition_and_names_the_data_it_would_receive(self):
        text = self.function_text("availableRealProviders")
        expectations = (
            ("real.image === true", "NVIDIA", "the PNG image you choose with a fixed findings prompt, and no crop, "
                                             "region, sensor values or identifiers"),
            ("real.research === true", "Tavily search", "the question text you type and the host names this server "
                                                        "limits the search to"),
            ("real.compute === true", "Nodus", "sensor_status, soil, air and battery sections of the simulated Record"),
            ("real.synthesis === true", "synthesis provider", "validated image findings, the retrieved evidence items"),
            ("state.research_pro.configured === true", "Tavily Research Pro", "the question text you type"),
            ("state.second_opinion.simulated === false", "cloud AI provider", "the PNG you choose plus the crop"),
        )
        for condition, who, data in expectations:
            start = text.index(condition)
            end = text.find("if (", start + len(condition))
            segment = text[start:end if end > 0 else len(text)]
            self.assertIn(who, segment, condition)
            self.assertIn(data, segment, condition)
            self.assertIn("would receive", segment, condition)             # configured, not yet called

    def test_the_per_run_summary_names_what_each_selected_real_stage_sends(self):
        script = joined(self.script)
        send = script[script.index("const IW_SEND"):script.index("};", script.index("const IW_SEND"))]
        for fragment in ("the PNG image you choose with a fixed findings prompt (no crop, region, sensor values or "
                         "identifiers)", "the sensor_status, soil, air and battery sections of the simulated Record",
                         "the validated image findings, the retrieved evidence items and the question text"):
            self.assertIn(fragment, send, fragment)
        summary = self.function_text("iwSendText") + self.function_text("updateRunSummary")
        for fragment in ("the host names this server limits the search to", "REAL provider. Sends", "FAKE stand-in on this machine",
                         "Nothing is sent to any provider", ": skipped."):
            self.assertIn(fragment, summary, fragment)
        self.assertEqual(self.script.count('addEventListener("change", updateRunSummary)'), 1)   # selection updates it
        self.assertIn('$("iw-question").addEventListener("input", updateRunSummary)', self.script)

    def test_the_data_really_sent_matches_what_the_page_says(self):
        """The page says a real image stage sends a fixed prompt and no hints, a real compute stage only four sections."""
        analyzer = CountingAnalyzer()
        seen = []
        original = analyzer.analyze

        def spy(request):
            seen.append(request)
            return original(request)

        analyzer.analyze = spy
        app, _, parts = make_real(self, analyzer=analyzer)
        body = body_of(post(app, {"scenario": "normal_operation", "crop": "wheat", "region": "northern", "growth_stage":
                                  "tillering", "question": QUESTION, "image_png_base64": PNG_B64, "providers": ALL_REAL}))
        self.assertEqual(len(seen), 1)
        prompt = seen[0].prompt
        record = body["record"]
        private_values = ["wheat", "northern", "tillering", record["rover_id"], record["time"]]
        if record["photo_id"] is not None:
            private_values.append(record["photo_id"])
        for private in private_values:
            self.assertNotIn(str(private), prompt, private)
        self.assertEqual(seen[0].image.data, PNG)
        payload = parts["runner"].calls[0][1]
        self.assertEqual(tuple(payload), ("sensor_status", "soil", "air", "battery"))

    def test_the_banner_function_claims_neither_that_nothing_is_used_nor_that_anything_was_called(self):
        refresh = self.function_text("refreshBanner")
        both = refresh + self.function_text("availableRealProviders")
        self.assertIn("if (items.length === 0) return;", refresh)           # with nothing real, the old wording stays
        for needed in ("REAL PROVIDERS ARE AVAILABLE.", "configured on this server but have NOT been called",
                       "Nothing is sent until you set a stage to Real", "produced by local code"):
            self.assertIn(needed, refresh, needed)
        for claim in ("no network", "No AI model", "SIMULATED DEMONSTRATION", "no NVIDIA / Nebius / Tavily",
                      "only to this local server", "fixed demonstration text", "has been called", "was called",
                      "were sent"):
            self.assertNotIn(claim, both, claim)

    def test_the_banner_is_applied_after_every_other_section_has_set_its_own_wording(self):
        start = self.script[self.script.index("async function start("):]
        start = start[:start.index("\n}\n")]
        for earlier in ("configureSecondOpinion(state.second_opinion)", "configureResearch(state.research)",
                        "configureResearchPro(state.research_pro)", "configureIntegrated(state.integrated)"):
            self.assertLess(start.index(earlier), start.index("refreshBanner(state)"), earlier)

    def test_the_server_state_the_banner_reads_matches_what_is_configured_in_every_combination(self):
        base = {"image": False, "research": False, "compute": False, "synthesis": False}
        combinations = (
            ({}, {}),
            ({"findings_analyzer": FakeFindingsAnalyzer()}, {"image": True}),
            ({"research_client": CountingSearch(search_result()), "research_domains": ["agri.example.org"]},
             {"research": True}),
            ({"compute_runner": FakeTaskRunner()}, {"compute": True}),
            ({"synthesis_provider": FakeSynthesisProvider()}, {"synthesis": True}),
            ({"findings_analyzer": FakeFindingsAnalyzer(), "research_client": CountingSearch(search_result()),
              "research_domains": ["agri.example.org"], "compute_runner": FakeTaskRunner(),
              "synthesis_provider": FakeSynthesisProvider()},
             {"image": True, "research": True, "compute": True, "synthesis": True}),
        )
        for kwargs, expected in combinations:
            with self.subTest(configured=sorted(expected)):
                app, _ = make(self, **kwargs)
                state = body_of(call(app, "GET", "/api/state"))
                self.assertEqual(state["integrated"]["real_available"], dict(base, **expected))
                self.assertIs(state["research"]["configured"], expected.get("research", False))
                self.assertIs(state["second_opinion"]["simulated"], True)

    def test_configured_providers_are_not_called_by_reading_the_state_or_the_page(self):
        app, _, parts = make_real(self)
        for target in ("/", "/api/state", "/static/app.js"):
            call(app, "GET", target)
        self.assertEqual((parts["analyzer"].calls, len(parts["search"].queries), parts["runner"].count,
                          parts["writer"].count), (0, 0, 0, 0))


class ServerWiringTests(NoNetwork):
    """`--findings-provider nvidia` is fail-closed and injects the analyzer; no port is bound and no key is read."""

    def run_main(self, argv, key_present):
        out, err = io.StringIO(), io.StringIO()
        fake_http = mock.MagicMock()
        fake_http.return_value.server_address = ("127.0.0.1", 54321)
        fake_http.return_value.serve_forever.side_effect = KeyboardInterrupt
        analyzer = FakeFindingsAnalyzer()           # a stand-in with analyze(); the real adapter is never built here
        with mock.patch.object(server, "api_key_present", return_value=key_present), \
                mock.patch.object(server, "HTTPServer", fake_http), \
                mock.patch.object(server, "NvidiaImageAnalyzer", return_value=analyzer), \
                mock.patch.object(server, "DemoService", wraps=DemoService) as service_class:
            code = server.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), fake_http, service_class, analyzer

    def test_without_the_key_nothing_starts_and_there_is_no_fallback(self):
        code, out, err, fake_http, service_class, _ = self.run_main(["--findings-provider", "nvidia"], False)
        self.assertEqual(code, 2)
        self.assertIn("Nothing was started", err)
        self.assertIn("no fallback", err)
        fake_http.assert_not_called()
        service_class.assert_not_called()

    def test_with_the_key_the_analyzer_is_injected_and_the_banner_names_the_data_sent(self):
        code, out, err, fake_http, service_class, analyzer = self.run_main(["--findings-provider", "nvidia"], True)
        self.assertEqual(code, 0)
        self.assertIs(service_class.call_args.kwargs["findings_analyzer"], analyzer)
        self.assertIn("INTEGRATED WORKFLOW", out)
        self.assertIn("sends the PNG image", out)

    def test_a_launcher_supplied_compute_runner_reaches_the_service_and_the_banner(self):
        out, err = io.StringIO(), io.StringIO()
        fake_http = mock.MagicMock()
        fake_http.return_value.server_address = ("127.0.0.1", 54321)
        fake_http.return_value.serve_forever.side_effect = KeyboardInterrupt
        runner = FakeTaskRunner()
        with mock.patch.object(server, "HTTPServer", fake_http), \
                mock.patch.object(server, "DemoService", wraps=DemoService) as service_class:
            code = server.main([], stdout=out, stderr=err, compute_runner=runner, compute_note="NOTE-FROM-LAUNCHER")
        self.assertEqual(code, 0)
        self.assertIs(service_class.call_args.kwargs["compute_runner"], runner)
        self.assertIn("NOTE-FROM-LAUNCHER", out.getvalue())
        self.assertIn("REAL compute provider", out.getvalue())

    def test_the_default_injects_no_real_integrated_provider(self):
        code, out, err, fake_http, service_class, _ = self.run_main([], False)
        self.assertEqual(code, 0)
        self.assertNotIn("findings_analyzer", service_class.call_args.kwargs)
        self.assertNotIn("INTEGRATED WORKFLOW", out)


if __name__ == "__main__":
    unittest.main()
