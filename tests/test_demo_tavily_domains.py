"""Offline tests of the agreed research-domain allowlist (server configuration) for the Tavily Search integration.

The agreed list (icar.org.in, agritech.tnau.ac.in, fao.org) is a SERVER-CONFIGURATION allowlist. It is not proof that a
retrieved page is trustworthy, relevant to agriculture, authentic or safe: results stay `unreviewed` retrieved
evidence. Only fake transports are used, so no socket is opened and no provider is contacted. The adapter's own unit
tests are in test_research.py; the not-yet-approved HTTPS-only filter in research/tavily.py is NOT tested here.
"""

import io
import json
import unittest
from unittest import mock

from demo_app import server as server_module
from demo_app.app import DemoApp
from demo_app.server import AGREED_RESEARCH_DOMAINS
from demo_app.service import DemoService
from orchestration.integrated import check_integrated_result
from research.tavily import TavilyResearch
from storage.record_store import RecordStore

AGREED = ("icar.org.in", "agritech.tnau.ac.in", "fao.org")
HOST = "127.0.0.1:8765"          # only a Host-header string for the in-process app; no port is opened
KEY = "tvly-canary-not-a-real-key-0003"
QUESTION = "how should soil moisture readings be read"
CLOCK_TIME = "2026-01-01T00:00:00+00:00"

VALID_SUBDOMAINS = ("www.fao.org", "a.b.icar.org.in", "x.agritech.tnau.ac.in", "WWW.FAO.ORG")
LOOK_ALIKES = (
    "https://fao.org.evil.example/a", "https://notfao.org/a", "https://evilfao.org/a",
    "https://icar.org.in.evil.example/a", "https://xicar.org.in/a", "https://agritech.tnau.ac.in.evil.example/a",
    "https://tnau.ac.in/a",                      # the parent of an agreed host is not itself agreed
    "https://evil.example/fao.org", "https://evil.example/?u=https://fao.org/",
    "https://fao.org@evil.example/a", "https://evil.example:pw@fao.org/a", "ftp://fao.org/a",
    "javascript:alert(1)",
)


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


class Scripted:
    def __init__(self, results):
        self.results, self.calls = results, []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "body": body})
        return 200, json.dumps({"results": self.results}).encode("utf-8")


def hit(url, content="A snippet.", title="A title"):
    return {"title": title, "url": url, "content": content}


def client(results, domains=AGREED):
    return TavilyResearch(list(domains), transport=Scripted(results), environ={"TAVILY_API_KEY": KEY},
                          clock=lambda: CLOCK_TIME, max_results=10)


class TestAgreedList(NoNetwork):
    def test_the_canonical_tuple_is_exactly_the_three_agreed_domains(self):
        self.assertEqual(AGREED_RESEARCH_DOMAINS, AGREED)
        self.assertIsInstance(AGREED_RESEARCH_DOMAINS, tuple)

    def test_the_request_restricts_the_search_to_exactly_those_domains(self):
        transport = Scripted([hit("https://fao.org/a")])
        TavilyResearch(list(AGREED_RESEARCH_DOMAINS), transport=transport, environ={"TAVILY_API_KEY": KEY}).search(QUESTION)
        sent = json.loads(transport.calls[0]["body"])
        self.assertEqual(sent["include_domains"], list(AGREED))
        self.assertIs(sent["include_answer"], False)

    def test_each_agreed_domain_and_its_subdomains_are_accepted_by_the_adapter(self):
        urls = ["https://%s/page" % d for d in AGREED] + ["https://%s/page" % d for d in VALID_SUBDOMAINS]
        outcome = client([hit(u) for u in urls]).search(QUESTION)
        self.assertEqual((outcome.status, outcome.dropped), ("ok", 0))
        self.assertEqual([i.url for i in outcome.items], urls)   # source URLs preserved verbatim

    def test_look_alike_and_malformed_hosts_are_dropped(self):
        for url in LOOK_ALIKES:
            with self.subTest(url=url):
                outcome = client([hit(url), hit("https://fao.org/ok")]).search(QUESTION)
                self.assertEqual([i.url for i in outcome.items], ["https://fao.org/ok"])
                self.assertEqual(outcome.dropped, 1)

    def test_only_look_alikes_is_a_failed_search_not_a_success(self):
        outcome = client([hit(u) for u in LOOK_ALIKES]).search(QUESTION)
        self.assertEqual((outcome.status, outcome.error_code, outcome.items), ("error", "no_results", ()))
        self.assertEqual(outcome.dropped, len(LOOK_ALIKES))

    def test_results_stay_labelled_as_unreviewed_evidence_not_trust(self):
        outcome = client([hit("https://fao.org/a")]).search(QUESTION)
        self.assertEqual(outcome.kind, "research_evidence_only")
        codes = {c[0] for c in outcome.caveats}
        self.assertTrue({"not_a_recommendation", "domains_are_owner_configured"} <= codes)


class TestStartupConfiguration(NoNetwork):
    def run_main(self, argv, key=True):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(server_module, "HTTPServer") as fake_server, \
                mock.patch.object(server_module, "DemoApp") as fake_app, \
                mock.patch.object(server_module, "tavily_key_present", return_value=key), \
                mock.patch.dict("os.environ", {"TAVILY_API_KEY": KEY}):
            instance = fake_server.return_value
            instance.server_address = ("127.0.0.1", 12345)
            instance.serve_forever.side_effect = KeyboardInterrupt
            code = server_module.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), fake_server, fake_app

    def domains_of(self, fake_app):
        return fake_app.call_args.args[0].state()["research"]["domains"]

    def test_the_agreed_shortcut_expands_to_exactly_the_agreed_domains_and_prints_them(self):
        for value in ("agreed", " Agreed ", "AGREED"):
            with self.subTest(value=value):
                code, out, err, _, fake_app = self.run_main(
                    ["--research-provider", "tavily", "--research-domains", value])
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(self.domains_of(fake_app), list(AGREED))
                self.assertIn("limited to: icar.org.in, agritech.tnau.ac.in, fao.org.", out)
                self.assertNotIn(KEY, out + err)

    def test_explicit_agreed_domains_and_their_subdomains_are_accepted(self):
        for value, expected in (("fao.org", ["fao.org"]),
                                ("ICAR.org.in, agritech.tnau.ac.in", ["icar.org.in", "agritech.tnau.ac.in"]),
                                ("www.fao.org,x.agritech.tnau.ac.in", ["www.fao.org", "x.agritech.tnau.ac.in"])):
            with self.subTest(value=value):
                code, out, err, _, fake_app = self.run_main(
                    ["--research-provider", "tavily", "--research-domains", value])
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(self.domains_of(fake_app), expected)

    def test_off_list_explicit_domains_are_refused_and_named_without_starting(self):
        cases = {
            "other site": ("example.org", ["example.org"]),
            "old placeholder": ("agri.example.org,research.example.net", ["agri.example.org", "research.example.net"]),
            "parent of an agreed host": ("tnau.ac.in", ["tnau.ac.in"]),
            "suffix look-alike": ("fao.org.evil.example", ["fao.org.evil.example"]),
            "prefix look-alike": ("notfao.org", ["notfao.org"]),
            "one bad among good": ("fao.org,evil.example,icar.org.in", ["evil.example"]),
        }
        for name, (value, named) in cases.items():
            with self.subTest(case=name):
                code, out, err, fake_server, fake_app = self.run_main(
                    ["--research-provider", "tavily", "--research-domains", value])
                self.assertEqual((code, out), (2, ""))
                self.assertIn("may only name the agreed domains", err)
                self.assertIn("Nothing was started", err)
                self.assertIn("not allowed: " + ", ".join(named) + ".", err)
                self.assertNotIn(KEY, err)
                fake_server.assert_not_called()
                fake_app.assert_not_called()

    def test_malformed_values_still_get_the_plain_host_names_error_first(self):
        for value in ("*.fao.org", "https://fao.org", "fao.org/x", "fao.org:8080", "agreed,fao.org", " , "):
            with self.subTest(value=value):
                code, out, err, fake_server, fake_app = self.run_main(
                    ["--research-provider", "tavily", "--research-domains", value])
                self.assertEqual((code, out), (2, ""))
                self.assertIn("plain host names", err)
                self.assertIn("Nothing was started", err)
                fake_server.assert_not_called()
                fake_app.assert_not_called()

    def test_a_missing_domain_list_and_a_missing_key_are_still_reported_without_secrets(self):
        cases = (
            (["--research-provider", "tavily"], True, "--research-domains"),
            (["--research-provider", "tavily", "--research-domains", "agreed"], False, "TAVILY_API_KEY"),
            (["--research-domains", "agreed"], True, "only applies"),
        )
        for argv, key, expected in cases:
            with self.subTest(argv=argv, key=key):
                code, out, err, fake_server, fake_app = self.run_main(argv, key=key)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(expected, err)
                self.assertIn("Nothing was started", err)
                self.assertNotIn(KEY, err)
                fake_server.assert_not_called()
                fake_app.assert_not_called()

    def test_the_off_list_error_comes_before_the_key_error(self):
        code, _, err, _, _ = self.run_main(
            ["--research-provider", "tavily", "--research-domains", "example.org"], key=False)
        self.assertEqual(code, 2)
        self.assertIn("may only name the agreed domains", err)
        self.assertNotIn("TAVILY_API_KEY", err)

    def test_help_documents_the_shortcut_and_the_restriction(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            server_module.main(["--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("Use 'agreed' for the agreed list (icar.org.in, agritech.tnau.ac.in, fao.org)", text)
        self.assertIn("anything else stops the server from starting", text)


class TestIntegratedResponse(NoNetwork):
    def make_app(self, results, domains=AGREED):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        search = client(results, domains)
        service = DemoService(store, research_client=search, research_domains=list(search.domains))
        return DemoApp(service, {HOST})

    def run_research(self, app):
        payload = {"scenario": "normal_operation", "question": QUESTION,
                   "providers": {"image": "skip", "research": "real", "compute": "fake", "synthesis": "fake"}}
        raw = json.dumps(payload).encode("utf-8")
        headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": str(len(raw)),
                   "content-type": "application/json"}
        response = app.handle_request("POST", "/api/run-integrated", headers, io.BytesIO(raw).read)
        return response, json.loads(response.body.decode("utf-8"))

    def test_no_off_list_url_appears_anywhere_in_the_integrated_response(self):
        good = ["https://fao.org/a", "https://www.icar.org.in/b", "https://x.agritech.tnau.ac.in/c"]
        results = [hit(u, title="T%d" % i) for i, u in enumerate(good)] + \
                  [hit(u, title="BAD") for u in LOOK_ALIKES] + \
                  [hit("https://off.example/z", title="BAD"), "not a dict", hit("https://fao.org/empty", content="")]
        response, body = self.run_research(self.make_app(results))
        self.assertEqual(response.status, 200)
        step = {s["name"]: s for s in body["integrated"]["run"]["steps"]}["research_search"]
        self.assertEqual(step["status"], "completed")
        self.assertEqual([i["url"] for i in step["result"]["items"]], good)
        entries = body["integrated"]["report"]["sections"]["retrieved_sources"]["entries"]
        self.assertEqual([e["url"] for e in entries], good)
        text = response.body.decode("utf-8")
        for needle in ("off.example", "evil.example", "notfao", "evilfao", "xicar", "javascript:", "ftp://", "BAD"):
            self.assertNotIn(needle, text, needle)
        self.assertNotIn(KEY, text)
        self.assertEqual(check_integrated_result(body["integrated"]), [])

    def test_retrieved_entries_stay_unreviewed_with_no_claim_of_authority(self):
        _, body = self.run_research(self.make_app([hit("https://fao.org/a")]))
        sources = body["integrated"]["report"]["sections"]["retrieved_sources"]
        (entry,) = sources["entries"]
        self.assertEqual((entry["provenance"], entry["review_status"]), ("retrieved_source", "unreviewed"))
        self.assertIsNone(entry["organization"])
        self.assertEqual((entry["crop"], entry["growth_stage"], entry["region"], entry["supported_claims"]),
                         (None, None, None, []))
        self.assertIn("domains_are_owner_configured", [c["code"] for c in sources["caveats"]])

    def test_a_response_with_only_off_list_results_is_a_failed_stage(self):
        _, body = self.run_research(self.make_app([hit(u) for u in LOOK_ALIKES]))
        steps = {s["name"]: s for s in body["integrated"]["run"]["steps"]}
        self.assertEqual((steps["research_search"]["status"], steps["research_search"]["error_code"]),
                         ("failed", "no_results"))
        self.assertEqual(body["integrated"]["report"]["sections"]["retrieved_sources"]["entries"], [])
        self.assertEqual(steps["synthesis"]["skip_reason"], "dependency_not_completed")


if __name__ == "__main__":
    unittest.main()
