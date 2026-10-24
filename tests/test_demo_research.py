"""Offline tests for the research (advisory) wiring: the demo endpoint, the start-up flags, the page, and
the pipeline's research status. The real Tavily adapter runs against a FAKE transport.

No request has ever been made to Tavily. These tests show the wiring behaves as designed; they do not show
that the live service works. The hosts and sources are SYNTHETIC examples.
"""

import ast
import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import demo_app
from agri_pipeline import pipeline
from demo_app import server as server_module
from demo_app.app import DemoApp
from demo_app.service import DemoError, DemoService
from image_analysis.nvidia import ResponseTooLarge, TransportError, TransportTimeout
from research import tavily
from research.tavily import ERROR_CODES, TavilyResearch
from storage.record_store import RecordStore

KEY = "canary-tavily-key-1234567890"
HOST = "127.0.0.1:8765"
DOMAINS = ("agri.example.org", "research.example.net")
NOW = "2026-01-02T03:04:05+00:00"
STATIC = Path(demo_app.__file__).resolve().parent / "static"


def reply(*results):
    return json.dumps({"results": list(results), "request_id": "r"}).encode()


def result(url="https://agri.example.org/guide", content="SYNTHETIC excerpt.", title="SYNTHETIC title", **extra):
    entry = {"title": title, "url": url, "content": content, "score": 0.8}
    entry.update(extra)
    return entry


class Transport:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.error = status, error
        self.body = reply(result(published_date="2025-05-06")) if body is None else body
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), json.loads(body), timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body


class NoSockets(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)


def build(testcase, transport=None, environ=None, domains=DOMAINS, configured=True):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    if not configured:
        return DemoApp(DemoService(store), {HOST}), store, None
    client = TavilyResearch(domains, transport=transport, environ={"TAVILY_API_KEY": KEY} if environ is None
                            else environ, clock=lambda: NOW)
    return DemoApp(DemoService(store, research_client=client, research_domains=client.domains), {HOST}), store, client


def post(app, payload, path="/api/advisory", headers=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    sent = {"host": HOST, "x-nelavi-demo": "1", "content-type": "application/json", "content-length": str(len(body))}
    sent.update(headers or {})
    response = app.handle_request("POST", path, sent, io.BytesIO(body).read)
    return response, json.loads(response.body)


class TestStateAndConstruction(NoSockets):
    def test_research_is_off_by_default_and_the_state_says_so(self):
        app, _, _ = build(self, configured=False)
        state = json.loads(app.handle_request("GET", "/api/state", {"host": HOST}, None).body)
        self.assertEqual(state["research"], {"configured": False, "domains": [], "query_max_chars": None})

    def test_a_configured_state_lists_the_owner_chosen_hosts_and_the_query_limit_only(self):
        app, _, _ = build(self, Transport())
        state = json.loads(app.handle_request("GET", "/api/state", {"host": HOST}, None).body)
        self.assertEqual(state["research"], {"configured": True, "domains": list(DOMAINS), "query_max_chars": 200})
        self.assertNotIn(KEY, json.dumps(state))

    def test_the_service_validates_what_it_is_given(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(TypeError):
            DemoService(store, research_client=object(), research_domains=["a.example.org"])
        for domains in ((), [], [""], [5], None):
            with self.assertRaises((ValueError, TypeError)):
                DemoService(store, research_client=TavilyResearch(DOMAINS), research_domains=domains)
        self.assertEqual(DemoService(store).state()["research"]["configured"], False)


class TestAdvisoryEndpoint(NoSockets):
    def test_a_search_returns_sources_with_their_attribution_and_never_a_recommendation(self):
        transport = Transport(body=reply(
            result("https://agri.example.org/a", "First excerpt.", "First", published_date="2025-05-06"),
            result("https://research.example.net/b", "Second excerpt.", "Second")))
        app, store, _ = build(self, transport)
        response, report = post(app, {"question": "soil testing guidance"})
        self.assertEqual(response.status, 200)
        self.assertEqual((report["status"], report["kind"], report["query"], report["retrieved_at"]),
                         ("ok", "research_evidence_only", "soil testing guidance", NOW))
        first, second = report["sources"]
        self.assertEqual((first["url"], first["title"], first["snippet"], first["publication_date"], first["date_source"]),
                         ("https://agri.example.org/a", "First", "First excerpt.", "2025-05-06", "provider_estimate"))
        self.assertEqual((second["publication_date"], second["organization"]), (None, None))
        for source in report["sources"]:
            self.assertEqual(source["review_status"], "unreviewed")
            self.assertEqual(source["applicability"]["status"], "not_established")
            self.assertIn("not_reviewed", source["applicability"]["unmet"])
        self.assertEqual(report["recommendations"]["supported"], [])
        self.assertIn("evidence to assess, not recommendations", report["note"])
        self.assertEqual(store.list_records(), [])  # nothing stored, nothing added to history
        self.assertEqual(len(transport.calls), 1)

    def test_a_snippet_that_sounds_like_advice_stays_an_excerpt_and_is_never_a_recommendation(self):
        transport = Transport(body=reply(result(content="Apply 50 kg of urea per hectare.")))
        app, _, _ = build(self, transport)
        _, report = post(app, {"question": "fertilizer for a crop"})
        self.assertEqual(report["recommendations"]["supported"], [])
        self.assertEqual(report["sources"][0]["supported_claims"], [])
        self.assertNotIn("urea", json.dumps(report["recommendations"]).lower())
        self.assertNotIn("urea", json.dumps(report["next_steps"]).lower())

    def test_the_request_to_the_provider_is_bounded_domain_limited_and_asks_for_no_generated_answer(self):
        transport = Transport()
        app, _, _ = build(self, transport)
        post(app, {"question": "soil testing guidance"})
        url, headers, body, timeout = transport.calls[0]
        self.assertEqual((url, timeout), ("https://api.tavily.com/search", 30.0))
        self.assertEqual(body["include_domains"], list(DOMAINS))
        self.assertIs(body["include_answer"], False)
        self.assertEqual((body["search_depth"], body["max_results"]), ("basic", 5))
        self.assertEqual(headers["Authorization"], "Bearer " + KEY)

    def test_the_users_stated_crop_region_and_stage_are_used_but_never_guessed(self):
        app, _, _ = build(self, Transport())
        _, none = post(app, {"question": "q q"})
        self.assertEqual(none["context_missing"], ["crop_identity", "growth_stage", "location", "visual_observations",
                                                   "soil_observations"])
        _, some = post(app, {"question": "q q", "crop": "Tomato", "region": "India", "growth_stage": ""})
        self.assertEqual(some["context_missing"], ["growth_stage", "visual_observations", "soil_observations"])
        self.assertTrue(any("growth stage" in step.lower() for step in some["next_steps"]))
        # Photos and soil readings are not what a research search needs.
        for report in (none, some):
            joined = " ".join(report["next_steps"]).lower()
            self.assertNotIn("photo", joined)
            self.assertNotIn("soil measurement", joined)
            self.assertIn("a person must review a source", joined)

    def test_provider_failures_return_422_with_a_closed_code_no_sources_and_one_request(self):
        cases = ((Transport(status=429), "rate_limited"), (Transport(status=432), "usage_limit_exceeded"),
                 (Transport(status=401), "provider_rejected"), (Transport(status=400), "provider_rejected"),
                 (Transport(status=503), "provider_unavailable"), (Transport(status=500), "provider_unavailable"),
                 (Transport(error=TransportTimeout()), "provider_timeout"),
                 (Transport(error=TransportError()), "transport_failed"),
                 (Transport(error=ResponseTooLarge()), "response_too_large"),
                 (Transport(body=b"{not json"), "response_malformed"), (Transport(body=b"[]"), "response_malformed"),
                 (Transport(body=b'{"results": "x"}'), "response_malformed"),
                 (Transport(body=reply()), "no_results"),
                 (Transport(body=reply(result("https://evil.example.com/x"))), "no_results"))
        for transport, code in cases:
            with self.subTest(code=code):
                app, store, _ = build(self, transport)
                response, report = post(app, {"question": "q q"})
                self.assertEqual((response.status, report["status"], report["error_code"], report["sources"]),
                                 (422, "error", code, []))
                self.assertEqual(report["recommendations"]["supported"], [])
                self.assertLessEqual(len(transport.calls), 1)  # never a retry
                self.assertNotIn(KEY, response.body.decode())
                self.assertEqual(store.list_records(), [])

    def test_a_missing_key_is_a_closed_error_and_sends_nothing(self):
        transport = Transport()
        app, _, _ = build(self, transport, environ={})
        response, report = post(app, {"question": "q q"})
        self.assertEqual((response.status, report["error_code"]), (422, "credential_missing"))
        self.assertEqual(transport.calls, [])

    def test_a_query_the_adapter_refuses_is_a_closed_error(self):
        for question in ("   ", "x" * 201, "line\nbreak"):
            transport = Transport()
            app, _, _ = build(self, transport)
            response, report = post(app, {"question": question})
            self.assertEqual((response.status, report["error_code"]), (422, "query_invalid"))
            self.assertEqual(transport.calls, [])

    def test_a_malformed_request_is_refused_before_the_provider_is_called(self):
        bad = ({}, {"question": 5}, {"question": "q q", "extra": 1}, {"question": "q q", "crop": ["Tomato"]},
               {"question": "q q", "crop": "<script>alert(1)</script>"}, {"question": "q q", "region": "x" * 65},
               {"question": "q q", "growth_stage": "line\nbreak"}, [], "text", None)
        for payload in bad:
            transport = Transport()
            app, _, _ = build(self, transport)
            response, body = post(app, payload)
            self.assertEqual(response.status, 400, payload)
            self.assertEqual(transport.calls, [])
            self.assertNotIn("script", response.body.decode())

    def test_when_research_is_not_configured_the_endpoint_refuses_and_calls_nothing(self):
        app, _, _ = build(self, configured=False)
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network")):
            response, body = post(app, {"question": "q q"})
        self.assertEqual(response.status, 400)
        self.assertIn("not configured", body["error"])

    def test_routing_rules_match_the_other_post_routes(self):
        app, _, _ = build(self, Transport())
        self.assertEqual(post(app, {"question": "q q"}, headers={"x-nelavi-demo": "0"})[0].status, 403)
        self.assertEqual(post(app, {"question": "q q"}, headers={"content-type": "text/plain"})[0].status, 415)
        self.assertEqual(post(app, None, raw=b"{not json")[0].status, 400)
        self.assertEqual(post(app, None, raw=b"[" * 4000)[0].status, 400)
        self.assertEqual(post(app, None, raw=b"x" * 5000)[0].status, 413)
        self.assertEqual(app.handle_request("GET", "/api/advisory", {"host": HOST}, None).status, 404)
        self.assertEqual(post(app, {"question": "q q"}, headers={"host": "evil.example"})[0].status, 403)

    def test_the_key_never_appears_in_any_response(self):
        transport = Transport(status=500, body=(KEY + "SYNTHETIC").encode())
        app, _, _ = build(self, transport)
        for payload in ({"question": "q q"}, {"question": ""}, {"nope": 1}):
            response, _ = post(app, payload)
            self.assertNotIn(KEY.encode(), response.body)
        state = app.handle_request("GET", "/api/state", {"host": HOST}, None)
        self.assertNotIn(KEY.encode(), state.body)

    def test_the_default_server_makes_no_external_request_for_any_route(self):
        app, _, _ = build(self, configured=False)
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network")):
            self.assertEqual(app.handle_request("GET", "/api/state", {"host": HOST}, None).status, 200)
            self.assertEqual(app.handle_request("GET", "/", {"host": HOST}, None).status, 200)
            self.assertEqual(post(app, {"scenario": "probe_measurement"}, path="/api/run")[0].status, 200)
            self.assertEqual(post(app, {"question": "q q"})[0].status, 400)


class TestPipelineResearchStatus(NoSockets):
    def run_with(self, transport, **kwargs):
        client = TavilyResearch(DOMAINS, transport=transport, environ={"TAVILY_API_KEY": KEY}, clock=lambda: NOW)
        research = client.search("soil testing guidance")
        from mock_rover.scenarios import get_scenario, run_scenario
        from mock_rover.simulator import MockRover
        from mock_rover.config import RoverConfig
        results = run_scenario(MockRover(RoverConfig()), get_scenario("probe_measurement"))
        record = next(v for step, v in results if step == "build_record")
        return pipeline.run(record, origin="simulated", research_result=research, **kwargs), research

    def test_sources_join_the_report_as_unreviewed_evidence_with_their_status(self):
        report, _ = self.run_with(Transport())
        sources = report["sections"]["retrieved_sources"]
        self.assertEqual((sources["status"], sources["error_code"], sources["retrieved_at"]), ("ok", None, NOW))
        self.assertEqual(len(sources["entries"]), 1)
        self.assertEqual(sources["entries"][0]["applicability"]["status"], "not_established")
        self.assertEqual(report["sections"]["recommendations"]["supported"], [])
        codes = {u["code"] for u in report["sections"]["uncertain_or_unavailable"]}
        self.assertNotIn("no_sources_retrieved", codes)
        self.assertIn("no_supported_recommendation", codes)

    def test_a_failed_search_is_shown_as_such_and_never_as_sources_or_advice(self):
        for transport, code in ((Transport(status=503), "provider_unavailable"), (Transport(body=reply()), "no_results"),
                                (Transport(error=TransportTimeout()), "provider_timeout")):
            report, _ = self.run_with(transport)
            sources = report["sections"]["retrieved_sources"]
            self.assertEqual((sources["status"], sources["error_code"], sources["entries"]), ("error", code, []))
            texts = [u["text"] for u in report["sections"]["uncertain_or_unavailable"]]
            self.assertTrue(any("research search returned no usable sources (code: %s)" % code in t for t in texts))
            self.assertEqual(report["sections"]["recommendations"]["supported"], [])

    def test_no_research_means_not_run_and_a_malformed_object_is_an_error_not_sources(self):
        report = pipeline.run(self.record_for_pipeline(), origin="simulated")
        self.assertEqual(report["sections"]["retrieved_sources"]["status"], "not_run")
        for odd in (object(), "text", 5, {"status": "ok"}, mock.Mock(status="ok", items=5)):
            report = pipeline.run(self.record_for_pipeline(), origin="simulated", research_result=odd)
            sources = report["sections"]["retrieved_sources"]
            self.assertEqual((sources["status"], sources["entries"]), ("error", []))
            self.assertEqual(sources["error_code"], "malformed_research_result")

    def record_for_pipeline(self):
        from mock_rover.scenarios import get_scenario, run_scenario
        from mock_rover.simulator import MockRover
        from mock_rover.config import RoverConfig
        results = run_scenario(MockRover(RoverConfig()), get_scenario("probe_measurement"))
        return next(v for step, v in results if step == "build_record")

    def test_the_research_report_function_needs_no_record_and_never_recommends(self):
        research = TavilyResearch(DOMAINS, transport=Transport(), environ={"TAVILY_API_KEY": KEY},
                                  clock=lambda: NOW).search("soil testing guidance")
        report = pipeline.research_report(research, {"crop": "Tomato"})
        self.assertEqual((report["status"], report["kind"]), ("ok", "research_evidence_only"))
        self.assertEqual(report["recommendations"]["supported"], [])
        self.assertIn("growth_stage", report["context_missing"])
        self.assertEqual(pipeline.research_report(None)["status"], "not_run")
        self.assertEqual(pipeline.research_report(research, {"crop": "<b>"})["status"], "invalid_input")
        self.assertEqual(pipeline.research_report(research, {"nope": 1})["status"], "invalid_input")


class TestStartupFlags(NoSockets):
    def run_main(self, argv, key=True):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(server_module, "HTTPServer") as fake_server, \
                mock.patch.object(server_module, "DemoApp") as fake_app, \
                mock.patch.object(server_module, "tavily_key_present", return_value=key):
            instance = fake_server.return_value
            instance.server_address = ("127.0.0.1", 12345)
            instance.serve_forever.side_effect = KeyboardInterrupt
            code = server_module.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), fake_server, fake_app

    def test_the_default_has_no_research_and_prints_nothing_about_it(self):
        code, out, err, _, fake_app = self.run_main([])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(fake_app.call_args.args[0].state()["research"]["configured"], False)
        self.assertIn("SIMULATED DATA ONLY", out)
        self.assertNotIn("Research", out)

    def test_tavily_with_hosts_and_a_key_starts_and_says_what_is_sent(self):
        code, out, err, _, fake_app = self.run_main(
            ["--research-provider", "tavily", "--research-domains", "ICAR.org.in, FAO.org"])
        self.assertEqual((code, err), (0, ""))
        research = fake_app.call_args.args[0].state()["research"]
        self.assertEqual(research, {"configured": True, "domains": ["icar.org.in", "fao.org"], "query_max_chars": 200})
        self.assertIn("MIXED MODE", out)
        self.assertIn("Tavily", out)
        self.assertIn("icar.org.in, fao.org", out)
        self.assertIn("evidence, not recommendations", out)
        self.assertNotIn("SIMULATED DATA ONLY", out)

    def test_every_misconfiguration_refuses_to_start_without_binding(self):
        cases = {
            "no key": (["--research-provider", "tavily", "--research-domains", "icar.org.in"], False, "TAVILY_API_KEY"),
            "no hosts": (["--research-provider", "tavily"], True, "--research-domains"),
            "empty hosts": (["--research-provider", "tavily", "--research-domains", " , "], True, "--research-domains"),
            "wildcard": (["--research-provider", "tavily", "--research-domains", "*.example.org"], True, "plain host names"),
            "scheme": (["--research-provider", "tavily", "--research-domains", "https://icar.org.in"], True, "plain host names"),
            "path": (["--research-provider", "tavily", "--research-domains", "icar.org.in/x"], True, "plain host names"),
            "hosts without provider": (["--research-domains", "icar.org.in"], True, "only applies"),
        }
        for name, (argv, key, expected) in cases.items():
            with self.subTest(case=name):
                code, out, err, fake_server, fake_app = self.run_main(argv, key=key)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(expected, err)
                self.assertIn("Nothing was started", err)
                fake_server.assert_not_called()
                fake_app.assert_not_called()

    def test_an_unknown_research_provider_is_rejected_and_help_documents_the_flags(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            server_module.main(["--research-provider", "other"])
        self.assertEqual(raised.exception.code, 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            server_module.main(["--help"])
        text = " ".join(out.getvalue().split())
        for expected in ("--research-provider", "--research-domains", "tavily", "Use 'agreed' for the agreed list",
                         "never recommendations"):
            self.assertIn(expected, text)

    def test_the_real_key_check_is_used_and_no_value_is_ever_printed(self):
        for env, expected in (({"TAVILY_API_KEY": KEY}, 0), ({}, 2)):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(server_module, "HTTPServer") as fake:
                instance = fake.return_value
                instance.server_address = ("127.0.0.1", 12345)
                instance.serve_forever.side_effect = KeyboardInterrupt
                code = server_module.main(["--research-provider", "tavily", "--research-domains", "icar.org.in"],
                                          stdout=out, stderr=err)
            self.assertEqual(code, expected)
            self.assertNotIn(KEY, out.getvalue() + err.getvalue())

    def test_second_opinion_flags_still_combine_with_research(self):
        with mock.patch.object(server_module, "api_key_present", return_value=True):
            code, out, err, _, fake_app = self.run_main(
                ["--second-opinion-provider", "nvidia", "--research-provider", "tavily",
                 "--research-domains", "icar.org.in"])
        self.assertEqual((code, err), (0, ""))
        state = fake_app.call_args.args[0].state()
        self.assertEqual((state["second_opinion"]["simulated"], state["research"]["configured"]), (False, True))
        self.assertIn("NVIDIA", out)
        self.assertIn("Tavily", out)


class TestPage(unittest.TestCase):
    PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
    SCRIPT = (STATIC / "app.js").read_text(encoding="utf-8")

    def function_text(self, name):
        start = self.SCRIPT.index("function " + name)
        nxt = [i for i in (self.SCRIPT.find("\nfunction ", start + 1), self.SCRIPT.find("\nasync function ", start + 1),
                           self.SCRIPT.find("\nconst ", start + 1), self.SCRIPT.find("\nlet ", start + 1)) if i != -1]
        return self.SCRIPT[start:min(nxt)]

    def test_the_shipped_page_claims_nothing_about_research_before_the_server_answers(self):
        for element in ('id="research"', 'id="rs-form"', 'id="rs-question"', 'id="rs-status"', 'id="rs-sources"'):
            self.assertIn(element, self.PAGE)
        self.assertIn('<button id="rs-submit" type="submit" disabled>', self.PAGE)
        self.assertIn("Checking whether research is configured", self.PAGE)
        for claim in ("REAL SERVICE", "Tavily", "is configured and sends", "no network"):
            self.assertNotIn(claim, self.PAGE.split('<section id="research">')[1])

    def test_the_wording_is_written_only_after_the_configuration_is_confirmed(self):
        configure = self.function_text("configureResearch")
        self.assertIn("REAL SERVICE", configure)
        self.assertIn("Research is not configured on this server", configure)
        self.assertIn("could not be confirmed", configure)
        self.assertIn("if (soConfig) showResearchBanner();", configure)
        self.assertIn("configureResearch(state.research)", self.SCRIPT)
        self.assertIn("configureResearch(null);", self.SCRIPT)  # a failed state load disables research too

    def test_the_banner_never_says_no_search_service_is_used_when_research_is_configured(self):
        banner = self.function_text("showResearchBanner")
        self.assertIn("MIXED MODE.", banner)
        self.assertIn("Tavily search service", banner)
        for claim in ("no network", "No AI model", "SIMULATED DEMONSTRATION", "no NVIDIA / Nebius / Tavily"):
            self.assertNotIn(claim, banner)

    def test_every_closed_research_error_code_has_fixed_wording(self):
        table = self.SCRIPT[self.SCRIPT.index("const RESEARCH_ERRORS"):]
        table = table[:table.index("};")]
        for code in ERROR_CODES:
            if code in ("credential_missing", "configuration_invalid", "query_invalid", "rate_limited",
                        "usage_limit_exceeded", "provider_rejected", "provider_unavailable", "provider_unreachable",
                        "response_malformed", "response_too_large", "no_results", "provider_timeout",
                        "provider_connection_failed", "transport_failed"):
                self.assertIn(code + ":", table, code)
        self.assertEqual(len(ERROR_CODES), 14)

    def test_research_rendering_is_text_only_and_states_what_the_results_are(self):
        for name in ("renderAdvisory", "describeResearchError", "configureResearch", "showResearchBanner"):
            body = self.function_text(name)
            for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval(", "href"):
                self.assertNotIn(forbidden, body, (name, forbidden))
        render = self.function_text("renderAdvisory")
        self.assertIn("Recommendations that are supported", render)
        self.assertIn("report.recommendations.note", render)
        self.assertIn("not established because", render)
        self.assertIn("the search service's estimate", render)
        self.assertIn("No sources were produced", self.function_text("describeResearchError"))

    def test_the_form_submits_through_the_shared_api_helper_with_the_demo_header_and_a_guard(self):
        submit = self.SCRIPT[self.SCRIPT.index("async function submitAdvisory"):]
        submit = submit[:submit.index("async function start")]
        for expected in ("if (rsBusy || !researchConfig) return;", 'api("/api/advisory"', "SEND",
                         "JSON.stringify", "rsBusy = false;", "button.disabled = researchConfig === null;"):
            self.assertIn(expected, submit)
        self.assertIn("addEventListener(\"submit\", submitAdvisory)", self.SCRIPT)

    def test_the_server_never_trusts_the_page_the_limits_come_from_the_state(self):
        self.assertNotIn('maxlength="200"', self.PAGE)
        self.assertIn("researchConfig.maxChars", self.SCRIPT)


class TestSource(unittest.TestCase):
    def test_the_service_and_routes_never_import_a_provider(self):
        for name in ("service.py", "app.py"):
            tree = ast.parse((Path(demo_app.__file__).resolve().parent / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                    [node.module] if isinstance(node, ast.ImportFrom) else [])
                for module in modules:
                    self.assertNotIn("research", module.split("."), (name, module))
                    self.assertNotIn("nebius", module)
                    self.assertNotIn("nvidia", module)


if __name__ == "__main__":
    unittest.main()
