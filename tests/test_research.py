"""Offline tests for the Tavily research adapter. A fake transport is used throughout.

No request has ever been made to Tavily. The hosts below are SYNTHETIC examples (example.org style),
not a list of trusted agricultural sources.
"""

import ast
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import advice
import demo_app
from image_analysis.nvidia import ResponseTooLarge, TransportError, TransportTimeout
from research import tavily
from research.tavily import ERROR_CODES, TavilyResearch, key_present

KEY = "canary-tavily-key-1234567890"
ENV = {"TAVILY_API_KEY": KEY}
DOMAINS = ("agri.example.org", "research.example.net")
NOW = "2026-01-02T03:04:05+00:00"


def result(url="https://agri.example.org/guide", content="A snippet.", title="A title", **extra):
    entry = {"title": title, "url": url, "content": content, "score": 0.9}
    entry.update(extra)
    return entry


def body(*results, **extra):
    document = {"query": "q", "results": list(results), "response_time": 0.5, "request_id": "r"}
    document.update(extra)
    return json.dumps(document).encode("utf-8")


class Transport:
    def __init__(self, status=200, response=None, error=None):
        self.status, self.error = status, error
        self.response = body(result()) if response is None else response
        self.calls = []

    def __call__(self, url, headers, request_body, timeout):
        self.calls.append((url, dict(headers), request_body, timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.response


def client(transport, environ=None, domains=DOMAINS, **kwargs):
    return TavilyResearch(domains, transport=transport, clock=lambda: NOW,
                          environ=dict(ENV) if environ is None else environ, **kwargs)


class NoSockets(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestRequest(NoSockets):
    def test_the_request_is_explicit_bounded_and_restricted(self):
        transport = Transport()
        client(transport).search("soil testing guidance")
        (url, headers, raw, timeout), = transport.calls
        self.assertEqual(url, "https://api.tavily.com/search")
        self.assertEqual(headers["Authorization"], "Bearer " + KEY)
        self.assertEqual(timeout, 30.0)
        sent = json.loads(raw)
        self.assertEqual(sent, {
            "query": "soil testing guidance", "search_depth": "basic", "topic": "general",
            "max_results": 5, "include_answer": False,
            "include_domains": ["agri.example.org", "research.example.net"],
            "include_published_date": True})

    def test_a_search_always_needs_a_non_empty_owner_supplied_list_of_plain_host_names(self):
        for domains in (None, (), [], "agri.example.org", ("*.example.org",), ("https://agri.example.org",),
                        ("agri.example.org/path",), ("agri.example.org:8080",), ("localhost",),
                        ("bad host.org",), (5,), ("a.example.org",) * 0, tuple("d%d.example.org" % i for i in range(51))):
            with self.subTest(domains=str(domains)[:40]):
                transport = Transport()
                outcome = client(transport, domains=domains).search("a question")
                self.assertEqual(outcome.error_code, "configuration_invalid")
                self.assertEqual(transport.calls, [])

    def test_the_query_is_bounded_and_validated_before_any_request(self):
        for query in ("", "   ", None, 5, "x" * 201, "line\nbreak", "tab\there", "nul\x00"):
            with self.subTest(query=repr(query)[:20]):
                transport = Transport()
                outcome = client(transport).search(query)
                self.assertEqual(outcome.error_code, "query_invalid")
                self.assertEqual(transport.calls, [])
        self.assertEqual(client(Transport()).search("x" * 200).status, "ok")

    def test_bad_settings_are_refused(self):
        for kwargs in ({"max_results": 0}, {"max_results": 11}, {"max_results": True},
                       {"timeout_seconds": 0}, {"timeout_seconds": True}, {"max_results": "5"}):
            transport = Transport()
            self.assertEqual(client(transport, **kwargs).search("q q").error_code, "configuration_invalid")
            self.assertEqual(transport.calls, [])

    def test_a_missing_or_malformed_key_sends_nothing(self):
        for environ in ({}, {"TAVILY_API_KEY": ""}, {"TAVILY_API_KEY": "a\nb"}, {"NVIDIA_API_KEY": KEY}):
            transport = Transport()
            outcome = client(transport, environ=environ).search("a question")
            self.assertEqual(outcome.error_code, "credential_missing")
            self.assertEqual(transport.calls, [])


class TestResults(NoSockets):
    def test_sources_dates_scores_and_retrieval_time_are_preserved(self):
        transport = Transport(response=body(
            result("https://agri.example.org/a", "First.", "One", published_date="2025-05-06"),
            result("https://research.example.net/b", "Second.", "Two")))
        outcome = client(transport).search("a question")
        self.assertEqual((outcome.status, outcome.error_code, outcome.dropped), ("ok", None, 0))
        self.assertEqual(outcome.retrieved_at, NOW)
        self.assertEqual(outcome.query, "a question")
        first, second = outcome.items
        self.assertEqual((first.url, first.title, first.snippet, first.published_date, first.score),
                         ("https://agri.example.org/a", "One", "First.", "2025-05-06", 0.9))
        self.assertIsNone(second.published_date)

    def test_results_from_hosts_off_the_list_are_dropped_even_if_tavily_returned_them(self):
        transport = Transport(response=body(
            result("https://agri.example.org/ok"), result("https://evil.example.com/x"),
            result("https://agri.example.org.evil.com/x"), result("https://notagri.example.org/x"),
            result("https://sub.agri.example.org/ok2"), result("ftp://agri.example.org/x"),
            result("javascript:alert(1)"), result("https://user@evil.com@agri.example.org/x"),
            result("https://evil.com:pw@agri.example.org/x")))
        outcome = client(transport).search("a question")
        self.assertEqual([i.url for i in outcome.items],
                         ["https://agri.example.org/ok", "https://sub.agri.example.org/ok2"])
        self.assertEqual(outcome.dropped, 7)

    def test_generated_answers_are_never_returned(self):
        transport = Transport(response=body(result(), answer="APPLY 50 KG OF UREA"))
        outcome = client(transport).search("a question")
        self.assertNotIn("UREA", repr(outcome))
        self.assertFalse(hasattr(outcome, "answer"))

    def test_results_are_labelled_evidence_only_with_caveats(self):
        outcome = client(Transport()).search("a question")
        self.assertEqual(outcome.kind, "research_evidence_only")
        self.assertEqual({c[0] for c in outcome.caveats},
                         {"not_a_recommendation", "domains_are_owner_configured", "published_date_unreliable"})

    def test_snippets_titles_and_counts_are_bounded(self):
        many = [result("https://agri.example.org/%d" % i, "s" * 5000, "t" * 5000) for i in range(30)]
        outcome = client(Transport(response=body(*many)), max_results=3).search("a question")
        self.assertEqual(len(outcome.items), 3)
        self.assertEqual(outcome.dropped, 27)
        self.assertTrue(all(len(i.snippet) <= 1000 and len(i.title) <= 300 for i in outcome.items))

    def test_unusable_individual_results_are_dropped_not_fatal(self):
        transport = Transport(response=body(
            result(), "text", None, {"url": 5, "content": "x"}, result(content=""), result(content=5),
            result("https://agri.example.org/n", score="high", published_date=5)))
        outcome = client(transport).search("a question")
        self.assertEqual(len(outcome.items), 2)
        self.assertIsNone(outcome.items[1].score)
        self.assertIsNone(outcome.items[1].published_date)
        self.assertEqual(outcome.dropped, 5)

    def test_zero_usable_results_is_reported_as_no_results_not_as_success(self):
        for response in (body(), body(result("https://evil.example.com/x"))):
            outcome = client(Transport(response=response)).search("a question")
            self.assertEqual((outcome.status, outcome.error_code, outcome.items), ("error", "no_results", ()))


class TestFailures(NoSockets):
    def test_documented_http_statuses_map_to_closed_codes_with_one_request_each(self):
        cases = ((429, "rate_limited"), (432, "usage_limit_exceeded"), (433, "usage_limit_exceeded"),
                 (400, "provider_rejected"), (401, "provider_rejected"), (422, "provider_rejected"),
                 (500, "provider_unavailable"), (503, "provider_unavailable"),
                 (302, "response_malformed"), (199, "response_malformed"))
        for status, code in cases:
            with self.subTest(status=status):
                transport = Transport(status=status, response=b"secret " + KEY.encode())
                outcome = client(transport).search("a question")
                self.assertEqual((outcome.status, outcome.error_code, outcome.items), ("error", code, ()))
                self.assertEqual(len(transport.calls), 1)  # never a retry, even on 429

    def test_transport_failures_map_without_a_retry(self):
        for error, code in ((TransportTimeout(), "provider_timeout"), (TimeoutError(KEY), "provider_timeout"),
                            (tavily.TransportConnectionFailed(), "provider_connection_failed"),
                            (TransportError(), "transport_failed"), (OSError(KEY), "transport_failed"),
                            (ConnectionError(KEY), "transport_failed"), (ResponseTooLarge(), "response_too_large"),
                            (RuntimeError(KEY), "transport_failed")):
            transport = Transport(error=error)
            outcome = client(transport).search("a question")
            self.assertEqual(outcome.error_code, code)
            self.assertEqual(len(transport.calls), 1)
            self.assertNotIn(KEY, repr(outcome))

    def test_malformed_responses(self):
        for response in (b"{no", b"\xff", b"[]", b"{}", b'{"results": "x"}', b'{"results": {}}'):
            outcome = client(Transport(response=response)).search("a question")
            self.assertEqual(outcome.error_code, "response_malformed", response)
        for transport in (lambda *a: ("200", b"{}"), lambda *a: (True, b"{}"), lambda *a: (200, "text")):
            self.assertEqual(client(transport).search("a question").error_code, "response_malformed")

    def test_every_error_code_returned_is_in_the_closed_set(self):
        seen = {client(Transport(status=s)).search("q q").error_code for s in (429, 432, 400, 500, 302)}
        seen |= {client(Transport(error=e)).search("q q").error_code for e in (TransportTimeout(), ResponseTooLarge())}
        seen |= {client(Transport(), environ={}).search("q q").error_code,
                 client(Transport()).search("").error_code,
                 client(Transport(response=body())).search("q q").error_code,
                 client(Transport(), domains=()).search("q q").error_code}
        self.assertTrue(seen <= set(ERROR_CODES), seen)

    def test_the_key_never_appears_in_results_errors_or_reprs(self):
        for transport in (Transport(status=401, response=KEY.encode()), Transport(), Transport(error=TransportError())):
            outcome = client(transport).search("a question")
            self.assertNotIn(KEY, repr(outcome))
        self.assertNotIn(KEY, repr(client(Transport())))


class TestKeyPresent(unittest.TestCase):
    def test_only_a_boolean_is_returned(self):
        self.assertIs(key_present(dict(ENV)), True)
        for environ in ({}, {"TAVILY_API_KEY": " "}, {"TAVILY_API_KEY": "x\ry"}):
            self.assertIs(key_present(environ), False)
        with mock.patch.dict(os.environ, dict(ENV), clear=True):
            self.assertIs(key_present(), True)


class TestIsolationAndHonesty(unittest.TestCase):
    SOURCE = Path(tavily.__file__).read_text(encoding="utf-8")

    def test_only_the_server_entry_point_imports_it_and_the_advice_engine_never_does(self):
        # (The page's own wording names Tavily only to describe the research section, so text is not
        # searched; what matters is which modules import the research package.)
        for package in (advice, demo_app):
            for path in Path(package.__file__).resolve().parent.glob("*.py"):
                if path.name == "server.py" and package is demo_app:
                    continue  # the one approved import, checked in tests/test_demo_app.py
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.Import):
                        modules = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    else:
                        continue
                    for module in modules:
                        self.assertNotEqual(module.split(".")[0], "research", str(path))
        server = (Path(demo_app.__file__).resolve().parent / "server.py").read_text(encoding="utf-8")
        self.assertIn("from research.tavily import TavilyResearch", server)
        for name in ("service.py", "app.py"):
            text = (Path(demo_app.__file__).resolve().parent / name).read_text(encoding="utf-8")
            self.assertNotIn("research.tavily", text)

    def test_it_does_not_import_advice_or_produce_rules(self):
        tree = ast.parse(self.SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"json", "os", "re", "time", "unicodedata", "dataclasses", "datetime",
                                    "urllib", "image_analysis"})
        identifiers = {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)}
        identifiers |= {n.attr.lower() for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        identifiers |= {n.name.lower() for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for banned in ("threshold", "fertilizer", "dose", "rule", "recommendation", "advice"):
            self.assertFalse([i for i in identifiers if banned in i], banned)

    def test_there_is_no_retry_loop_sleep_or_built_in_domain_list(self):
        tree = ast.parse(self.SOURCE)
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.While)], [])
        transport_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                           and isinstance(n.func, ast.Attribute) and n.func.attr == "_transport"]
        self.assertEqual(len(transport_calls), 1)  # one call site, outside any loop or handler
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for banned in ("sleep", "socket", "print", "getenv"):
            self.assertNotIn(banned, names)
        for host in ("icar.org.in", "gov.in", "nic.in"):
            self.assertNotIn(host, self.SOURCE)
        with self.assertRaises(TypeError):
            TavilyResearch()  # the domain list is required, not defaulted


if __name__ == "__main__":
    unittest.main()
