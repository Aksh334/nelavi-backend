"""Offline tests for two Tavily Search adapter changes: HTTPS-only result URLs and the total time budget.

Everything is deterministic: a fake clock replaces the monotonic clock, `OpenerDirector.open` is replaced by a fake that
advances that clock, and sockets and DNS are patched to fail. No real sleeping, no network, no provider. The tests show
how the code behaves against stand-in objects; they do not show how real sockets or Tavily behave.

An https URL on an agreed host says nothing about whether the page is correct, current, relevant or safe.
"""

import io
import json
import re
import types
import unittest
import urllib.error
from dataclasses import fields
from pathlib import Path
from unittest import mock

from demo_app.app import DemoApp
from demo_app.service import DemoService
from image_analysis.nvidia import ResponseTooLarge, TransportError, TransportTimeout
from research import tavily
from research.tavily import ResearchItem, ResearchResult, TavilyResearch, deadline_transport
from storage.record_store import RecordStore

KEY = "tvly-canary-not-a-real-key-0004"
AGREED = ("icar.org.in", "agritech.tnau.ac.in", "fao.org")
HOST = "127.0.0.1:8765"
NOW = "2026-01-01T00:00:00+00:00"
URL = "https://api.tavily.com/search"


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


class Scripted:
    def __init__(self, results):
        self.results, self.calls = results, []

    def __call__(self, url, headers, body, timeout):
        self.calls.append(timeout)
        return 200, json.dumps({"results": self.results}).encode("utf-8")


def hit(url, **extra):
    entry = {"title": "A title", "url": url, "content": "A snippet."}
    entry.update(extra)
    return entry


def client(results, **kwargs):
    return TavilyResearch(list(AGREED), transport=Scripted(results), environ={"TAVILY_API_KEY": KEY},
                          clock=lambda: NOW, **kwargs)


# --------------------------------------------------------------------------- HTTPS-only URLs
ACCEPTED = (
    "https://icar.org.in/article", "https://fao.org:443/article", "https://www.fao.org/a/b?c=d#e",
    "https://agritech.tnau.ac.in/x", "https://x.agritech.tnau.ac.in:443/x", "https://a.b.icar.org.in/",
    "HTTPS://WWW.FAO.ORG/Upper",
)
REJECTED = (
    ("http", "http://icar.org.in/article"),
    ("port 8080", "https://icar.org.in:8080/article"),
    ("port 444", "https://icar.org.in:444/article"),
    ("port 80 on https", "https://fao.org:80/a"),
    ("port zero", "https://icar.org.in:0/x"),
    ("port not a number", "https://icar.org.in:abc/x"),
    ("port out of range", "https://icar.org.in:99999/x"),
    ("suffix look-alike", "https://fao.org.evil.example/article"),
    ("prefix look-alike", "https://notfao.org/article"),
    ("parent of an agreed host", "https://tnau.ac.in/x"),
    ("user-info disguise", "https://icar.org.in@evil.example/article"),
    ("user-info on an agreed host", "https://user:pw@fao.org/a"),
    ("user-info same host", "https://fao.org@fao.org/a"),
    ("password only", "https://:pw@fao.org/a"),
    ("backslash authority", "https://evil.example\\@fao.org/a"),
    ("space in host", "https://fao.org /a"),
    ("newline", "https://fao.org\n/a"),
    ("tab", "https://fao.org\t.evil.example/a"),
    ("no host", "https:///article"),
    ("no scheme", "//fao.org/a"),
    ("no scheme or slashes", "fao.org/a"),
    ("ftp", "ftp://fao.org/a"),
    ("javascript", "javascript:alert(1)"),
    ("data", "data:text/html,fao.org"),
    ("ip literal", "https://[::1]/a"),
    ("ip address", "https://127.0.0.1/a"),
    ("host only in the path", "https://evil.example/fao.org"),
    ("host only in the query", "https://evil.example/?u=https://fao.org/"),
    ("empty", ""),
    ("too long", "https://fao.org/" + "a" * 2100),
    ("not text: number", 5),
    ("not text: none", None),
    ("not text: list", ["https://fao.org/a"]),
)


class TestHttpsOnly(NoNetwork):
    def test_valid_https_urls_on_agreed_hosts_are_accepted_verbatim(self):
        outcome = client([hit(u) for u in ACCEPTED], max_results=10).search("a question")
        self.assertEqual((outcome.status, outcome.dropped), ("ok", 0))
        self.assertEqual([i.url for i in outcome.items], list(ACCEPTED))

    def test_every_rejected_form_is_dropped_and_counted_never_exposed(self):
        for name, url in REJECTED:
            with self.subTest(name):
                outcome = client([hit(url), hit("https://fao.org/ok")]).search("a question")
                self.assertEqual((outcome.status, outcome.dropped), ("ok", 1))
                self.assertEqual([i.url for i in outcome.items], ["https://fao.org/ok"])

    def test_only_rejected_urls_is_a_failed_search_with_the_count(self):
        outcome = client([hit(u) for _, u in REJECTED]).search("a question")
        self.assertEqual((outcome.status, outcome.error_code, outcome.items), ("error", "no_results", ()))
        self.assertEqual(outcome.dropped, len(REJECTED))

    def test_the_configured_hosts_still_decide_which_hosts_pass(self):
        outcome = TavilyResearch(["fao.org"], transport=Scripted([hit("https://icar.org.in/a"), hit("https://fao.org/a")]),
                                 environ={"TAVILY_API_KEY": KEY}).search("a question")
        self.assertEqual([i.url for i in outcome.items], ["https://fao.org/a"])

    def test_the_request_body_and_result_shape_are_unchanged(self):
        sent = []
        client_ = TavilyResearch(list(AGREED), environ={"TAVILY_API_KEY": KEY},
                                 transport=lambda u, h, b, t: (sent.append(json.loads(b)), (200, b'{"results": []}'))[1])
        client_.search("a question")
        self.assertEqual(sent[0]["include_domains"], list(AGREED))
        self.assertEqual(sent[0]["max_results"], 5)
        outcome = client([hit("https://fao.org/a", published_date="2025-01-02", score=0.5)]).search("q q")
        self.assertEqual([f.name for f in fields(ResearchResult)],
                         ["status", "error_code", "query", "items", "dropped", "retrieved_at", "kind", "caveats"])
        self.assertEqual([f.name for f in fields(ResearchItem)], ["title", "url", "snippet", "published_date", "score"])
        item = outcome.items[0]
        self.assertEqual((item.title, item.url, item.snippet, item.published_date, item.score),
                         ("A title", "https://fao.org/a", "A snippet.", "2025-01-02", 0.5))

    def test_the_result_cap_and_dropped_count_still_apply_to_valid_and_invalid_results(self):
        results = [hit("https://fao.org/%d" % i) for i in range(30)] + [hit("http://fao.org/x")] * 4
        outcome = client(results, max_results=3).search("a question")
        self.assertEqual(len(outcome.items), 3)
        self.assertEqual(outcome.dropped, 31)   # 27 over the cap plus 4 invalid
        self.assertEqual(client([hit("https://fao.org/a")] * 6).search("q q").dropped, 1)


class TestNoRejectedUrlInTheIntegratedResponse(NoNetwork):
    def run_integrated(self, results):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        search = client(results)
        app = DemoApp(DemoService(store, research_client=search, research_domains=list(search.domains)), {HOST})
        payload = {"scenario": "normal_operation", "question": "how should soil moisture readings be read",
                   "providers": {"image": "skip", "research": "real", "compute": "fake", "synthesis": "fake"}}
        raw = json.dumps(payload).encode("utf-8")
        headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": str(len(raw)),
                   "content-type": "application/json"}
        response = app.handle_request("POST", "/api/run-integrated", headers, io.BytesIO(raw).read)
        return response.status, response.body.decode("utf-8"), json.loads(response.body.decode("utf-8"))

    def test_rejected_urls_do_not_appear_anywhere_even_in_extra_result_fields(self):
        good = ["https://fao.org/a", "https://www.icar.org.in/b", "https://x.agritech.tnau.ac.in:443/c"]
        results = [hit(u, raw_content="https://leak.example/raw", favicon="http://leak.example/f.ico",
                       images=["https://leak.example/i.png"]) for u in good]
        results += [hit(u, raw_content="https://leak.example/raw2") for _, u in REJECTED if isinstance(u, str) and u]
        status, text, body = self.run_integrated(results)
        self.assertEqual(status, 200)
        step = {s["name"]: s for s in body["integrated"]["run"]["steps"]}["research_search"]
        self.assertEqual([i["url"] for i in step["result"]["items"]], good)
        entries = body["integrated"]["report"]["sections"]["retrieved_sources"]["entries"]
        self.assertEqual([e["url"] for e in entries], good)
        for needle in ("leak.example", "evil.example", "http://icar.org.in", "icar.org.in:8080", "icar.org.in:444",
                       "notfao", "ftp://", "javascript:", "data:text", "127.0.0.1", KEY):
            self.assertNotIn(needle, text, needle)
        self.assertEqual(step["result"]["dropped"], len([1 for _, u in REJECTED if isinstance(u, str) and u]))

    def test_entries_stay_unreviewed_evidence(self):
        _, _, body = self.run_integrated([hit("https://fao.org/a")])
        (entry,) = body["integrated"]["report"]["sections"]["retrieved_sources"]["entries"]
        self.assertEqual((entry["provenance"], entry["review_status"]), ("retrieved_source", "unreviewed"))


# --------------------------------------------------------------------------- total time budget
class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeResponse:
    """Stands in for a response: each read costs `read_cost` seconds on the fake clock."""

    def __init__(self, clock, chunks, status=200, read_cost=0.0):
        self.clock, self.chunks, self.status, self.read_cost = clock, list(chunks), status, read_cost
        self.reads = 0
        self.timeouts = []
        sock = types.SimpleNamespace(settimeout=self.timeouts.append)
        self.fp = types.SimpleNamespace(raw=types.SimpleNamespace(_sock=sock))

    def read(self, size=-1):
        self.reads += 1
        self.clock.advance(self.read_cost)
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        return chunk[:size] if size > 0 else chunk

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpen:
    """Replaces OpenerDirector.open: records each call, spends `cost` seconds, then returns or raises."""

    def __init__(self, clock, response=None, error=None, cost=0.0):
        self.clock, self.response, self.error, self.cost = clock, response, error, cost
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append({"url": request.full_url, "method": request.get_method(), "timeout": timeout})
        self.clock.advance(self.cost)
        if self.error is not None:
            raise self.error
        return self.response


class TestTotalTimeBudgetAndErrorCodes(NoNetwork):
    def setUp(self):
        super().setUp()
        self.clock = Clock()

    def transport(self, opener, budget=10.0):
        with mock.patch("urllib.request.OpenerDirector.open", new=opener):
            return deadline_transport(URL, {"Authorization": "Bearer " + KEY}, b"{}", budget, self.clock)

    def test_a_reply_inside_the_budget_is_returned_and_each_read_gets_only_the_time_left(self):
        response = FakeResponse(self.clock, [b'{"results"', b": [], ", b'"x": 1}'], read_cost=1.0)
        opener = FakeOpen(self.clock, response, cost=2.0)
        status, body = self.transport(opener)
        self.assertEqual((status, body), (200, b'{"results": [], "x": 1}'))
        self.assertEqual(opener.calls, [{"url": URL, "method": "POST", "timeout": 10.0}])
        self.assertEqual(response.timeouts, [8.0, 7.0, 6.0, 5.0])   # 3 chunks and the final empty read

    def test_the_budget_is_spent_before_the_headers_arrive(self):
        response = FakeResponse(self.clock, [b"late"])
        opener = FakeOpen(self.clock, response, cost=10.0)
        with self.assertRaises(TransportTimeout):
            self.transport(opener)
        self.assertEqual((len(opener.calls), response.reads), (1, 0))   # no body read once the budget is gone

    def test_the_budget_runs_out_while_the_body_is_being_read(self):
        response = FakeResponse(self.clock, [b"a", b"b", b"c", b"d"], read_cost=2.0)
        opener = FakeOpen(self.clock, response, cost=1.0)
        with self.assertRaises(TransportTimeout):
            self.transport(opener, budget=5.0)
        self.assertEqual(response.reads, 2)           # a third read was never started
        self.assertEqual(response.timeouts, [4.0, 2.0])
        self.assertEqual(len(opener.calls), 1)        # and nothing is retried

    def test_the_body_is_kept_when_end_of_file_is_read_before_the_deadline(self):
        response = FakeResponse(self.clock, [b"ab"], read_cost=3.0)
        status, body = self.transport(FakeOpen(self.clock, response, cost=1.0), budget=5.0)
        self.assertEqual(body, b"ab")   # the final empty read starts at 4 s, inside the budget

    def test_the_configured_budget_reaches_the_connection_and_the_adapter_default(self):
        response = FakeResponse(self.clock, [b'{"results": []}'])
        opener = FakeOpen(self.clock, response)
        with mock.patch("urllib.request.OpenerDirector.open", new=opener):
            outcome = TavilyResearch(list(AGREED), environ={"TAVILY_API_KEY": KEY}, monotonic=self.clock,
                                     timeout_seconds=7).search("a question")
        self.assertEqual(outcome.error_code, "no_results")
        self.assertEqual([c["timeout"] for c in opener.calls], [7])
        self.assertEqual(tavily.DEFAULT_TIMEOUT_SECONDS, 30.0)

    def test_an_exhausted_budget_through_the_adapter_is_one_request_a_closed_code_and_no_leak(self):
        response = FakeResponse(self.clock, [b"x"], read_cost=40.0)
        opener = FakeOpen(self.clock, response)
        with mock.patch("urllib.request.OpenerDirector.open", new=opener):
            outcome = TavilyResearch(list(AGREED), environ={"TAVILY_API_KEY": KEY}, monotonic=self.clock,
                                     timeout_seconds=30).search("a question")
        self.assertEqual((outcome.status, outcome.error_code, outcome.items), ("error", "provider_timeout", ()))
        self.assertEqual(len(opener.calls), 1)
        self.assertNotIn(KEY, repr(outcome))

    def test_failures_before_a_response_keep_their_exception_types_without_text(self):
        cases = (
            (TimeoutError(KEY), TransportTimeout),
            (urllib.error.URLError(TimeoutError(KEY)), TransportTimeout),
            (urllib.error.URLError(ConnectionRefusedError(KEY)), tavily.TransportConnectionFailed),
            (urllib.error.URLError(KEY), tavily.TransportConnectionFailed),
            (ConnectionResetError(KEY), tavily.TransportConnectionFailed),
            (OSError(KEY), tavily.TransportConnectionFailed),
        )
        for error, expected in cases:
            with self.subTest(type(error).__name__):
                opener = FakeOpen(self.clock, error=error)
                with self.assertRaises(TransportError) as raised:
                    self.transport(opener)
                self.assertIs(type(raised.exception), expected)
                self.assertNotIn(KEY, repr(raised.exception) + str(raised.exception))
                self.assertEqual(len(opener.calls), 1)

    def test_a_protocol_error_from_the_http_layer_is_not_called_a_connection_failure(self):
        import http.client
        opener = FakeOpen(self.clock, error=http.client.BadStatusLine(KEY))
        with self.assertRaises(http.client.BadStatusLine):
            self.transport(opener)   # not an OSError: it propagates and the adapter calls it transport_failed
        self.assertEqual(len(opener.calls), 1)

    def test_a_failure_after_the_connection_was_made_is_a_transport_failure_or_a_timeout(self):
        class Breaks(FakeResponse):
            def __init__(self, clock, error):
                super().__init__(clock, [])
                self.error = error

            def read(self, size=-1):
                raise self.error

        for error, expected in ((ConnectionResetError(KEY), TransportError), (OSError(KEY), TransportError),
                                (TimeoutError(KEY), TransportTimeout)):
            with self.subTest(type(error).__name__):
                with self.assertRaises(TransportError) as raised:
                    self.transport(FakeOpen(self.clock, Breaks(self.clock, error)))
                self.assertIs(type(raised.exception), expected)
                self.assertNotIn(KEY, repr(raised.exception) + str(raised.exception))


    def search_with(self, transport):
        return TavilyResearch(list(AGREED), transport=transport, environ={"TAVILY_API_KEY": KEY}).search("a question")

    def test_each_failure_maps_to_one_code_with_one_request_and_no_leak(self):
        class Raises:
            def __init__(self, error):
                self.error, self.calls = error, 0

            def __call__(self, *args):
                self.calls += 1
                raise self.error

        cases = (
            (TransportTimeout(), "provider_timeout"), (TimeoutError(KEY), "provider_timeout"),
            (tavily.TransportConnectionFailed(), "provider_connection_failed"),
            (TransportError(), "transport_failed"), (OSError(KEY), "transport_failed"),
            (ConnectionError(KEY), "transport_failed"), (ValueError(KEY), "transport_failed"),
            (RuntimeError(KEY), "transport_failed"), (ResponseTooLarge(), "response_too_large"),
        )
        for error, code in cases:
            with self.subTest(type(error).__name__):
                transport = Raises(error)
                outcome = self.search_with(transport)
                self.assertEqual((outcome.status, outcome.error_code, outcome.items), ("error", code, ()))
                self.assertEqual(transport.calls, 1)
                self.assertNotIn(KEY, repr(outcome))
                self.assertIn(code, tavily.ERROR_CODES)

    def test_http_statuses_are_never_connection_or_timeout_errors(self):
        cases = ((401, "provider_rejected"), (403, "provider_rejected"), (422, "provider_rejected"),
                 (429, "rate_limited"), (432, "usage_limit_exceeded"), (500, "provider_unavailable"),
                 (503, "provider_unavailable"), (302, "response_malformed"))
        for status, code in cases:
            with self.subTest(status):
                outcome = self.search_with(lambda u, h, b, t, s=status: (s, b"secret " + KEY.encode()))
                self.assertEqual((outcome.error_code, outcome.items), (code, ()))
                self.assertNotIn(KEY, repr(outcome))

    def test_malformed_empty_and_oversize_replies_keep_their_codes(self):
        for reply, code in ((b"{no", "response_malformed"), (b"[]", "response_malformed"),
                            (b'{"results": []}', "no_results")):
            with self.subTest(reply):
                self.assertEqual(self.search_with(lambda u, h, b, t, r=reply: (200, r)).error_code, code)

    def test_the_legacy_code_stays_in_the_closed_set_but_is_never_produced(self):
        self.assertIn("provider_unreachable", tavily.ERROR_CODES)
        self.assertEqual(len(tavily.ERROR_CODES), 14)
        self.assertEqual(len(set(tavily.ERROR_CODES)), 14)
        failures = (TransportTimeout(), TransportError(), tavily.TransportConnectionFailed(), OSError(), RuntimeError())
        for error in failures:
            def raises(*args, error=error):
                raise error
            self.assertNotEqual(self.search_with(raises).error_code, "provider_unreachable")

    def test_http_error_replies_keep_their_status_and_their_body(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status):
                fp = FakeResponse(self.clock, [b'{"detail": "no"}'])
                error = urllib.error.HTTPError(URL, status, "x", {}, fp)
                self.assertEqual(self.transport(FakeOpen(self.clock, error=error)), (status, b'{"detail": "no"}'))

    def test_an_http_error_body_that_outruns_the_budget_keeps_the_status_with_an_empty_body(self):
        fp = FakeResponse(self.clock, [b"x"], read_cost=20.0)
        error = urllib.error.HTTPError(URL, 503, "x", {}, fp)
        self.assertEqual(self.transport(FakeOpen(self.clock, error=error), budget=5.0), (503, b""))

    def test_an_oversized_body_is_reported_as_such_for_replies_and_for_error_replies(self):
        with mock.patch.object(tavily, "_MAX_RESPONSE_BYTES", 10):
            with self.assertRaises(ResponseTooLarge):
                self.transport(FakeOpen(self.clock, FakeResponse(self.clock, [b"123456", b"78901"])))
            self.assertEqual(self.transport(FakeOpen(self.clock, FakeResponse(self.clock, [b"123456", b"7890"]))),
                             (200, b"1234567890"))
            error = urllib.error.HTTPError(URL, 500, "x", {}, FakeResponse(self.clock, [b"123456", b"78901"]))
            with self.assertRaises(ResponseTooLarge):
                self.transport(FakeOpen(self.clock, error=error))

    def test_a_response_object_without_the_expected_layout_is_still_read(self):
        class Plain:
            status = 200

            def __init__(self):
                self.parts = [b"ok"]

            def read(self, size=-1):
                return self.parts.pop(0) if self.parts else b""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        self.assertEqual(self.transport(FakeOpen(self.clock, Plain())), (200, b"ok"))

    def test_the_budget_uses_the_monotonic_clock_not_wall_time(self):
        source = Path(tavily.__file__).read_text(encoding="utf-8")
        self.assertIn("monotonic", source)
        self.assertNotIn("time.time(", source)
        self.assertNotIn("sleep", source)


class TestPageWording(unittest.TestCase):
    SCRIPT = (Path(tavily.__file__).resolve().parents[1] / "demo_app" / "static" / "app.js").read_text(encoding="utf-8")

    def test_the_not_configured_help_names_the_agreed_allowlist_and_claims_no_trust(self):
        start = self.SCRIPT.index("To enable it, start the demo with --research-provider tavily")
        text = re.sub(r'"\s*\+\s*"', "", self.SCRIPT[start:start + 600])   # join the adjacent string literals
        self.assertIn("--research-domains agreed", text)
        for host in AGREED:
            self.assertIn(host, text)
        self.assertIn("Matching a domain does not verify the content", text)
        self.assertNotIn("sites you trust", self.SCRIPT)


if __name__ == "__main__":
    unittest.main()
