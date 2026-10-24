"""Offline tests for the Tavily Research (pro) adapter, its demo wiring, the start-up flags and the page.

Every test uses a fake transport, a fake clock or a patched class. No request is ever made to Tavily (or
anyone), and no test sleeps. These tests show the code handles the response shapes Tavily's documentation
describes; they do NOT show that the live service behaves that way. No live Research request has been made.
The sources and texts below are SYNTHETIC examples.
"""

import ast
import contextlib
import io
import itertools
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import demo_app
from demo_app import server as server_module
from demo_app.app import DemoApp
from demo_app.service import DemoService
from image_analysis.nvidia import ResponseTooLarge, TransportError, TransportTimeout
from research import tavily, tavily_research
from research.tavily_research import (
    APPROVED_DOMAIN,
    DEADLINE_SECONDS,
    ENDPOINT,
    ERROR_CODES,
    LABEL,
    MAX_CONTENT_CHARS,
    MAX_TASKS,
    TavilyResearchPro,
    approved_source_url,
    key_present,
)
from storage.record_store import RecordStore

KEY = "canary-tavily-key-1234567890"
NOW = "2026-01-02T03:04:05+00:00"
HOST = "127.0.0.1:8765"
REQ = "req-0001"
STATIC = Path(demo_app.__file__).resolve().parent / "static"
HARNESS = Path(__file__).resolve().parent / "js_harness_research_pro.js"
GOOD = {"title": "Good page", "url": "https://www.icar.org.in/guide"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Script:
    """A fake transport. Each call takes the next scripted reply; the last reply repeats."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(item, BaseException):
            raise item
        status, payload, *rest = item
        data = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else payload
        return status, data, (rest[0] if rest else None)


def created(request_id=REQ, status="pending"):
    return 201, {"request_id": request_id, "created_at": NOW, "status": status, "input": "q", "model": "pro",
                 "response_time": 1.23}


def waiting(status="in_progress"):
    return 202, {"request_id": REQ, "status": status, "response_time": 2}


def done(sources=(GOOD,), content="SYNTHETIC generated text."):
    return 200, {"request_id": REQ, "created_at": NOW, "status": "completed", "content": content,
                 "sources": list(sources), "response_time": 40}


def make(*replies, environ=None):
    clock = Clock()
    transport = Script(*replies)
    ids = itertools.count()
    client = TavilyResearchPro(
        transport=transport, environ={"TAVILY_API_KEY": KEY} if environ is None else environ, clock=clock,
        now=lambda: NOW, id_factory=lambda: "task_%016d" % next(ids))
    return client, transport, clock


class NoSockets(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertNoKey(self, view):
        self.assertNotIn(KEY, json.dumps(view))


class TestSourceValidation(unittest.TestCase):
    def test_the_approved_domain_and_true_subdomains_are_accepted(self):
        self.assertEqual(APPROVED_DOMAIN, "icar.org.in")
        for url in ("https://icar.org.in", "https://icar.org.in/", "https://icar.org.in/a/b?x=1#top",
                    "https://www.icar.org.in/guide", "https://a.b.c.icar.org.in/x", "https://ICAR.ORG.IN/",
                    "https://Www.Icar.Org.In/page", "https://icar.org.in./", "https://icar.org.in:443/x"):
            self.assertTrue(approved_source_url(url), url)

    def test_look_alike_and_malformed_addresses_are_refused(self):
        refused = (
            "http://icar.org.in/",                      # plain HTTP: nothing documented requires it
            "https://icar.org.in.example.com/", "https://noticar.org.in/", "https://evilicar.org.in/",
            "https://icar.org.in@evil.example/", "https://user:pw@icar.org.in/", "https://@icar.org.in/",
            "https://icar.org.in:pw@evil.example/", "https://evil.example\\@icar.org.in/",
            "https://evil.example/icar.org.in", "https://evil.example/?u=https://icar.org.in",
            "https://org.in/", "https://icar.org/", "https://in/", "https://.icar.org.in/",
            "https://icar.org.in../", "https://-icar.org.in/", "https://icar..org.in/",
            "https://icar%2eorg%2ein/", "https://ícar.org.in/", "https://xn--icar.org.in.evil.com/",
            "https://icar.org.in:8443/", "https://icar.org.in:99999/", "https://icar.org.in:abc/",
            "ftp://icar.org.in/", "javascript:alert(1)", "data:text/html,x", "file:///icar.org.in",
            "//icar.org.in/", "icar.org.in", "https:icar.org.in", "https:///icar.org.in", "https://",
            "https://icar.org.in/ x", "https://icar.org.in/\n", "https://icar.org.in/\x00", " https://icar.org.in/",
            "https://icar.org.in/" + "a" * 2100, "", None, 5, ["https://icar.org.in/"], b"https://icar.org.in/",
        )
        for url in refused:
            self.assertFalse(approved_source_url(url), repr(url)[:80])

    def test_validation_never_makes_a_request(self):
        with mock.patch("socket.socket", side_effect=AssertionError("socket used")), \
                mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network")):
            self.assertTrue(approved_source_url("https://icar.org.in/x"))
            self.assertFalse(approved_source_url("https://evil.example/x"))


class TestStart(NoSockets):
    def test_the_request_uses_the_documented_endpoint_fields_model_pro_and_the_approved_domain_only(self):
        client, transport, _ = make(created())
        view = client.start("How is soil sampled?")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual((call["method"], call["url"], call["timeout"]), ("POST", ENDPOINT, 15.0))
        self.assertEqual(ENDPOINT, "https://api.tavily.com/research")
        self.assertEqual(json.loads(call["body"]), {
            "input": "How is soil sampled?", "model": "pro", "stream": False, "citation_format": "numbered",
            "output_length": "standard", "include_domains": ["icar.org.in"]})
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual((view["status"], view["error_code"], view["query"]), ("running", None, "How is soil sampled?"))
        self.assertEqual((view["deadline_seconds"], view["kind"], view["label"]), (120, "research_pro_unreviewed", LABEL))
        self.assertTrue(view["task_id"].startswith("task_"))
        self.assertEqual((view["content"], view["sources"]), (None, []))
        self.assertNoKey(view)

    def test_nothing_but_the_question_is_ever_sent(self):
        client, transport, _ = make(created())
        client.start("A general question")
        sent = json.loads(transport.calls[0]["body"])
        for forbidden in ("files", "output_schema", "stream_options", "crop", "region", "location", "readings"):
            self.assertNotIn(forbidden, sent)
        self.assertEqual(sorted(sent), ["citation_format", "include_domains", "input", "model", "output_length", "stream"])

    def test_a_missing_or_unusable_key_is_refused_before_any_request(self):
        for environ in ({}, {"TAVILY_API_KEY": ""}, {"TAVILY_API_KEY": "  "}, {"TAVILY_API_KEY": "bad\nkey"},
                        {"OTHER_API_KEY": KEY}):
            client, transport, _ = make(created(), environ=environ)
            view = client.start("question")
            self.assertEqual((view["status"], view["error_code"], view["task_id"]), ("error", "credential_missing", None))
            self.assertEqual(transport.calls, [])
            self.assertNoKey(view)

    def test_an_unusable_question_is_refused_before_any_request(self):
        for question in ("", "   ", "x" * 201, "a\x00b", "a\nb", None, 5, ["q"]):
            client, transport, _ = make(created())
            view = client.start(question)
            self.assertEqual((view["status"], view["error_code"]), ("error", "query_invalid"), repr(question)[:30])
            self.assertEqual(transport.calls, [])
        client, transport, _ = make(created())
        self.assertEqual(client.start("x" * 200)["status"], "running")

    def test_every_http_failure_is_a_closed_error_with_one_request_and_no_task(self):
        cases = ((400, "provider_rejected"), (401, "provider_rejected"), (403, "provider_rejected"),
                 (404, "task_not_found"), (422, "provider_rejected"), (429, "rate_limited"),
                 (432, "usage_limit_exceeded"), (433, "usage_limit_exceeded"), (500, "provider_unavailable"),
                 (503, "provider_unavailable"), (302, "response_malformed"), (100, "response_malformed"))
        for status, code in cases:
            with self.subTest(status=status):
                client, transport, _ = make((status, {"detail": {"error": "PROVIDER TEXT " + KEY}}))
                view = client.start("question")
                self.assertEqual((view["status"], view["error_code"], view["task_id"]), ("error", code, None))
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn("PROVIDER TEXT", json.dumps(view))
                self.assertNoKey(view)

    def test_transport_failures_are_closed_errors_and_are_not_retried(self):
        for error, code in ((TransportTimeout(), "provider_unreachable"), (TransportError(), "provider_unreachable"),
                            (OSError(KEY), "provider_unreachable"), (ResponseTooLarge(), "response_too_large")):
            client, transport, _ = make(error)
            view = client.start("question")
            self.assertEqual((view["status"], view["error_code"]), ("error", code))
            self.assertEqual(len(transport.calls), 1)
            self.assertNoKey(view)

    def test_a_start_reply_that_is_not_the_documented_shape_creates_no_task(self):
        bad = (b"not json", b"[]", b"{}", {"status": "pending"}, {"request_id": REQ},
               {"request_id": REQ, "status": "completed"}, {"request_id": REQ, "status": "weird"},
               {"request_id": 5, "status": "pending"}, {"request_id": "", "status": "pending"},
               {"request_id": "../../x", "status": "pending"}, {"request_id": "a/b", "status": "pending"},
               {"request_id": "a b", "status": "pending"}, {"request_id": "x" * 200, "status": "pending"})
        for payload in bad:
            client, transport, _ = make((201, payload))
            view = client.start("question")
            self.assertEqual((view["status"], view["error_code"], view["task_id"]), ("error", "response_malformed", None),
                             repr(payload)[:40])
            self.assertEqual(len(transport.calls), 1)
            self.assertIsNone(client.poll("task_%016d" % 0))

    def test_an_unusable_task_id_from_the_factory_is_refused(self):
        client = TavilyResearchPro(transport=Script(created()), environ={"TAVILY_API_KEY": KEY}, clock=Clock(),
                                   id_factory=lambda: "../etc")
        self.assertEqual(client.start("question")["error_code"], "configuration_invalid")


class TestPolling(NoSockets):
    def started(self, *replies):
        client, transport, clock = make(created(), *replies)
        task = client.start("question")["task_id"]
        return client, transport, clock, task

    def test_polling_asks_the_provider_at_most_once_per_call_and_not_inside_the_gap(self):
        client, transport, clock, task = self.started(waiting())
        view = client.poll(task)
        self.assertEqual((view["status"], view["retry_after_seconds"]), ("running", 4))
        self.assertEqual(len(transport.calls), 1)  # only the start request
        clock.advance(4)
        self.assertEqual(client.poll(task)["status"], "running")
        self.assertEqual(len(transport.calls), 2)
        call = transport.calls[1]
        self.assertEqual((call["method"], call["url"], call["body"]), ("GET", ENDPOINT + "/" + REQ, None))
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(call["timeout"], 10.0)
        client.poll(task)
        client.poll(task)
        self.assertEqual(len(transport.calls), 2)  # inside the gap again

    def test_pending_and_in_progress_both_mean_running(self):
        for reply in (waiting("pending"), waiting("in_progress"), (200, {"request_id": REQ, "status": "pending"})):
            client, _, clock, task = self.started(reply)
            clock.advance(5)
            view = client.poll(task)
            self.assertEqual((view["status"], view["content"], view["sources"]), ("running", None, []))

    def test_a_completed_task_returns_the_text_and_only_approved_sources(self):
        mixed = [GOOD, {"title": "Evil", "url": "https://evil.example/x"},
                 {"title": "Look-alike", "url": "https://icar.org.in.evil.com/"},
                 {"title": "Credentials", "url": "https://icar.org.in@evil.example/"},
                 {"title": "Plain http", "url": "http://icar.org.in/x"},
                 {"title": "Script", "url": "javascript:alert(1)"},
                 {"title": "Sub", "url": "https://krishi.icar.org.in/page"}, GOOD, 5, None, {"title": "no url"}]
        client, transport, clock, task = self.started(done(mixed, "Line one\nLine two"))
        clock.advance(5)
        view = client.poll(task)
        self.assertEqual((view["status"], view["error_code"]), ("completed", None))
        self.assertEqual(view["content"], "Line one\nLine two")
        self.assertEqual(view["sources"], [GOOD, {"title": "Sub", "url": "https://krishi.icar.org.in/page"}])
        self.assertEqual((view["sources_dropped"], view["sources_state"], view["retrieved_at"]), (9, "approved", NOW))
        self.assertEqual(view["label"], LABEL)
        self.assertIn("unreviewed_ai_generated", [c["code"] for c in view["caveats"]])
        self.assertIn("text_not_limited_to_approved_sources", [c["code"] for c in view["caveats"]])
        self.assertNotIn("evil.example", json.dumps(view["sources"]))
        calls = len(transport.calls)
        self.assertEqual(client.poll(task), view)            # terminal: stays the same
        self.assertEqual(len(transport.calls), calls)        # and asks nobody again
        self.assertNoKey(view)

    def test_when_no_source_is_approved_the_text_is_kept_and_no_link_is_returned(self):
        for sources in ([], [{"title": "Evil", "url": "https://evil.example/x"}]):
            client, _, clock, task = self.started(done(sources))
            clock.advance(5)
            view = client.poll(task)
            self.assertEqual((view["status"], view["sources"], view["sources_state"]), ("completed", [], "none_approved"))
            self.assertEqual(view["content"], "SYNTHETIC generated text.")

    def test_completed_text_is_bounded_and_cleaned(self):
        client, _, clock, task = self.started(done(content="x" * (MAX_CONTENT_CHARS + 50)))
        clock.advance(5)
        view = client.poll(task)
        self.assertEqual(len(view["content"]), MAX_CONTENT_CHARS)
        self.assertIn("text_truncated", [c["code"] for c in view["caveats"]])
        client, _, clock, task = self.started(done(content="a\x00b\x07c\td\ne"))
        clock.advance(5)
        self.assertEqual(client.poll(task)["content"], "abc\td\ne")
        many = [{"title": "t", "url": "https://icar.org.in/%d" % i} for i in range(40)]
        client, _, clock, task = self.started(done(many))
        clock.advance(5)
        view = client.poll(task)
        self.assertEqual((len(view["sources"]), view["sources_dropped"]), (20, 20))

    def test_a_failed_task_is_a_failure_not_a_result(self):
        client, _, clock, task = self.started((200, {"request_id": REQ, "status": "failed", "response_time": 3}))
        clock.advance(5)
        view = client.poll(task)
        self.assertEqual((view["status"], view["error_code"], view["content"], view["sources"]),
                         ("failed", "task_failed", None, []))

    def test_completed_replies_that_break_the_documented_shape_are_errors_never_results(self):
        base = {"request_id": REQ, "status": "completed"}
        bad = (dict(base, content="text"), dict(base, sources=[GOOD]), dict(base, content={"k": "v"}, sources=[GOOD]),
               dict(base, content="", sources=[GOOD]), dict(base, content="  ", sources=[GOOD]),
               dict(base, content=5, sources=[GOOD]), dict(base, content="text", sources="x"),
               dict(base, content="text", sources={"url": "https://icar.org.in/"}),
               {"request_id": REQ, "status": "weird"}, {"request_id": REQ}, {"status": 5}, [], "plain", b"{")
        for payload in bad:
            client, _, clock, task = self.started((200, payload))
            clock.advance(5)
            view = client.poll(task)
            self.assertEqual((view["status"], view["error_code"], view["content"], view["sources"]),
                             ("error", "response_malformed", None, []), repr(payload)[:50])
        client, _, clock, task = self.started((200, b"not json"))
        clock.advance(5)
        self.assertEqual(client.poll(task)["error_code"], "response_malformed")

    def test_each_http_status_while_polling_maps_to_a_closed_state(self):
        cases = ((401, "provider_rejected"), (400, "provider_rejected"), (404, "task_not_found"),
                 (432, "usage_limit_exceeded"), (433, "usage_limit_exceeded"), (500, "provider_unavailable"),
                 (502, "provider_unavailable"), (302, "response_malformed"))
        for status, code in cases:
            with self.subTest(status=status):
                client, transport, clock, task = self.started((status, {"detail": {"error": "PROVIDER TEXT"}}))
                clock.advance(5)
                view = client.poll(task)
                self.assertEqual((view["status"], view["error_code"], view["content"]), ("error", code, None))
                self.assertNotIn("PROVIDER TEXT", json.dumps(view))
                calls = len(transport.calls)
                client.poll(task)
                self.assertEqual(len(transport.calls), calls)  # terminal: no automatic retry

    def test_transport_failures_while_polling_end_the_task_with_a_closed_error(self):
        for error, code in ((TransportTimeout(), "provider_unreachable"), (TransportError(), "provider_unreachable"),
                            (ResponseTooLarge(), "response_too_large"), (RuntimeError(KEY), "provider_unreachable")):
            client, _, clock, task = self.started(error)
            clock.advance(5)
            view = client.poll(task)
            self.assertEqual((view["status"], view["error_code"]), ("error", code))
            self.assertNoKey(view)

    def test_a_rate_limit_waits_for_retry_after_without_exceeding_the_deadline(self):
        client, transport, clock, task = self.started((429, {"detail": {"error": "slow down"}}, "7"), done())
        clock.advance(4)
        view = client.poll(task)
        self.assertEqual((view["status"], view["retry_after_seconds"], view["error_code"]), ("running", 7, None))
        calls = len(transport.calls)
        clock.advance(3)
        self.assertEqual(client.poll(task)["retry_after_seconds"], 4)
        self.assertEqual(len(transport.calls), calls)      # still waiting: the provider is not asked
        clock.advance(4)
        self.assertEqual(client.poll(task)["status"], "completed")
        self.assertEqual(len(transport.calls), calls + 1)

    def test_retry_after_is_floored_defaulted_and_clamped(self):
        for header, expected in (("0", 4), (None, 5), ("soon", 5), ("-3", 5), ("1.5", 5), ("9", 9)):
            client, _, clock, task = self.started((429, {}, header), waiting())
            clock.advance(4)
            self.assertEqual(client.poll(task)["retry_after_seconds"], expected, header)
        client, _, clock, task = self.started((429, {}, "99999"), waiting())
        clock.advance(4)
        view = client.poll(task)
        self.assertEqual((view["status"], view["error_code"]), ("timed_out", "deadline_exceeded"))

    def test_the_hard_deadline_ends_the_task_without_asking_the_provider_again(self):
        client, transport, clock, task = self.started(waiting())
        clock.advance(119)
        view = client.poll(task)
        self.assertEqual(view["status"], "running")
        self.assertEqual(transport.calls[-1]["timeout"], 1.0)  # never longer than the time left
        calls = len(transport.calls)
        clock.advance(1)
        view = client.poll(task)
        self.assertEqual((view["status"], view["error_code"], view["content"], view["sources"]),
                         ("timed_out", "deadline_exceeded", None, []))
        self.assertEqual(view["elapsed_seconds"], DEADLINE_SECONDS)
        self.assertEqual(len(transport.calls), calls)
        clock.advance(500)
        self.assertEqual(client.poll(task)["status"], "timed_out")
        self.assertEqual(len(transport.calls), calls)

    def test_a_slow_reply_that_arrives_after_the_deadline_is_a_timeout_not_a_result(self):
        class Slow(Script):
            def __call__(self, method, url, headers, body, timeout):
                result = super().__call__(method, url, headers, body, timeout)
                if method == "GET":
                    clock.advance(200)
                return result
        clock = Clock()
        transport = Slow(created(), waiting())
        client = TavilyResearchPro(transport=transport, environ={"TAVILY_API_KEY": KEY}, clock=clock,
                                   id_factory=lambda: "task_0000000000000000")
        task = client.start("question")["task_id"]
        clock.advance(5)
        self.assertEqual(client.poll(task)["status"], "timed_out")

    def test_a_key_removed_after_the_start_ends_the_task_without_a_request(self):
        environ = {"TAVILY_API_KEY": KEY}
        client, transport, clock = make(created(), waiting(), environ=environ)
        task = client.start("question")["task_id"]
        del environ["TAVILY_API_KEY"]
        clock.advance(5)
        view = client.poll(task)
        self.assertEqual((view["status"], view["error_code"]), ("error", "credential_missing"))
        self.assertEqual(len(transport.calls), 1)

    def test_unknown_or_unsafe_task_ids_are_unknown_and_reach_no_provider(self):
        client, transport, _, task = self.started(waiting())
        calls = len(transport.calls)
        for bad in (None, 5, "", "short", "../../x", "task/../../x", "x" * 80, "task_0000000000000000\n", REQ,
                    "task_ffffffffffffffff"):
            self.assertIsNone(client.poll(bad), repr(bad))
        self.assertEqual(len(transport.calls), calls)
        self.assertIsNotNone(client.poll(task))

    def test_the_provider_request_id_is_never_part_of_a_view(self):
        client, _, clock, task = self.started(done())
        clock.advance(5)
        self.assertNotIn(REQ, json.dumps(client.poll(task)))

    def test_only_a_bounded_number_of_tasks_are_kept_and_a_finished_one_makes_room(self):
        client, transport, clock = make(created())
        ids = [client.start("question")["task_id"] for _ in range(MAX_TASKS)]
        self.assertTrue(all(ids))
        view = client.start("question")
        self.assertEqual((view["status"], view["error_code"]), ("error", "too_many_tasks"))
        self.assertEqual(len(transport.calls), MAX_TASKS)    # the refused start sent nothing
        clock.advance(DEADLINE_SECONDS)
        self.assertEqual(client.poll(ids[0])["status"], "timed_out")
        self.assertEqual(client.start("question")["status"], "running")
        self.assertIsNone(client.poll(ids[0]))               # the finished task made room

    def test_no_failure_falls_back_to_anything_that_looks_like_a_result(self):
        for reply in (waiting(), (500, {}), (404, {}), (200, b"x"), (200, {"status": "failed"}), TransportError()):
            client, _, clock, task = self.started(reply)
            clock.advance(DEADLINE_SECONDS + 1)
            view = client.poll(task)
            self.assertNotEqual(view["status"], "completed")
            self.assertEqual((view["content"], view["sources"]), (None, []))


class TestIsolationAndSource(unittest.TestCase):
    SOURCE = Path(tavily_research.__file__).read_text(encoding="utf-8")

    def test_the_search_adapter_is_separate_and_unchanged_in_contract(self):
        self.assertEqual(tavily.ENDPOINT, "https://api.tavily.com/search")
        self.assertEqual(len(tavily.ERROR_CODES), 14)
        tree = ast.parse(self.SOURCE)
        imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertNotIn("research.tavily", imported)
        self.assertEqual([m for m in imported if m.startswith("image_analysis")], ["image_analysis.nvidia"])

    def test_there_is_no_sleep_thread_or_hard_coded_credential(self):
        tree = ast.parse(self.SOURCE)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for banned in ("sleep", "Thread", "ThreadPoolExecutor", "subprocess", "print", "getenv", "asyncio"):
            self.assertNotIn(banned, names, banned)
        self.assertNotIn("tvly-", self.SOURCE)
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.While)], [])

    def test_the_environment_is_read_only_through_the_one_documented_name(self):
        self.assertEqual(tavily_research.API_KEY_ENV_VAR, "TAVILY_API_KEY")
        self.assertIs(key_present({"TAVILY_API_KEY": KEY}), True)
        for environ in ({}, {"TAVILY_API_KEY": " "}, {"NVIDIA_API_KEY": KEY}):
            self.assertIs(key_present(environ), False)

    def test_every_closed_error_code_is_distinct_and_documented_in_the_page(self):
        self.assertEqual(len(set(ERROR_CODES)), len(ERROR_CODES))
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        table = script[script.index("const RESEARCH_PRO_ERRORS"):]
        table = table[:table.index("};")]
        for code in ERROR_CODES:
            self.assertIn(code + ":", table, code)


def build_app(testcase, *replies, configured=True, search=None):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    clock = Clock()
    transport = Script(*replies)
    client = TavilyResearchPro(transport=transport, environ={"TAVILY_API_KEY": KEY}, clock=clock, now=lambda: NOW,
                               id_factory=lambda: "task_0123456789abcdef") if configured else None
    service = DemoService(store, research_pro_client=client)
    return DemoApp(service, {HOST}), transport, clock


def call(app, method, path, payload=None, headers=None, raw=None):
    body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")
    sent = {"host": HOST}
    if method == "POST":
        sent.update({"x-nelavi-demo": "1", "content-type": "application/json", "content-length": str(len(body))})
    sent.update(headers or {})
    response = app.handle_request(method, path, sent, io.BytesIO(body).read)
    return response, json.loads(response.body)


class TestDemoWiring(NoSockets):
    def test_the_state_says_whether_research_pro_is_configured_and_never_includes_secrets(self):
        app, _, _ = build_app(self, created(), configured=False)
        _, state = call(app, "GET", "/api/state")
        self.assertEqual(state["research_pro"], {"configured": False, "domain": None, "model": None,
                                                 "query_max_chars": None, "deadline_seconds": None})
        self.assertFalse(state["research"]["configured"])
        app, _, _ = build_app(self, created())
        _, state = call(app, "GET", "/api/state")
        self.assertEqual(state["research_pro"], {"configured": True, "domain": "icar.org.in", "model": "pro",
                                                 "query_max_chars": 200, "deadline_seconds": 120})
        self.assertNotIn(KEY, json.dumps(state))

    def test_start_and_poll_run_end_to_end_through_the_routes(self):
        app, transport, clock = build_app(self, created(), done([GOOD, {"title": "Evil", "url": "https://evil.example/"}]))
        response, view = call(app, "POST", "/api/research-pro/start", {"question": "How is soil sampled?"})
        self.assertEqual((response.status, view["status"], view["task_id"]), (200, "running", "task_0123456789abcdef"))
        self.assertEqual(json.loads(transport.calls[0]["body"])["input"], "How is soil sampled?")
        response, view = call(app, "GET", "/api/research-pro/status?task=" + view["task_id"])
        self.assertEqual((response.status, view["status"]), (200, "running"))
        self.assertEqual(len(transport.calls), 1)           # inside the gap: no provider request
        clock.advance(5)
        response, view = call(app, "GET", "/api/research-pro/status?task=task_0123456789abcdef")
        self.assertEqual((response.status, view["status"], view["sources"]), (200, "completed", [GOOD]))
        self.assertEqual((view["sources_dropped"], view["label"]), (1, LABEL))
        self.assertNotIn(KEY, json.dumps(view))
        self.assertNotIn("evil.example", json.dumps(view))

    def test_a_failed_start_is_422_with_a_closed_code_and_no_provider_text(self):
        app, transport, _ = build_app(self, (429, {"detail": {"error": "PROVIDER TEXT"}}, "9"))
        response, view = call(app, "POST", "/api/research-pro/start", {"question": "q"})
        self.assertEqual((response.status, view["status"], view["error_code"]), (422, "error", "rate_limited"))
        self.assertNotIn("PROVIDER TEXT", response.body.decode())
        self.assertNotIn(KEY, response.body.decode())

    def test_only_the_question_is_accepted_so_crop_region_and_readings_cannot_be_sent(self):
        app, transport, _ = build_app(self, created())
        for payload in ({"question": "q", "crop": "Tomato"}, {"question": "q", "region": "x", "growth_stage": "y"},
                        {"question": "q", "readings": {"soil": {}}}, {"crop": "Tomato"}, {"question": 5},
                        {"question": None}, [], "q", 5, None, {}):
            response, _ = call(app, "POST", "/api/research-pro/start", raw=json.dumps(payload).encode())
            self.assertEqual(response.status, 400, payload)
        self.assertEqual(transport.calls, [])

    def test_the_routes_refuse_everything_that_is_not_a_well_formed_demo_request(self):
        app, transport, _ = build_app(self, created())
        path = "/api/research-pro/start"
        body = json.dumps({"question": "q"}).encode()
        self.assertEqual(call(app, "POST", path, {"question": "q"}, headers={"x-nelavi-demo": "0"})[0].status, 403)
        self.assertEqual(call(app, "POST", path, {"question": "q"}, headers={"content-type": "text/plain"})[0].status, 415)
        self.assertEqual(call(app, "POST", path, raw=b"{")[0].status, 400)
        self.assertEqual(call(app, "POST", path, raw=b"x" * 5000)[0].status, 413)
        self.assertEqual(call(app, "POST", path, {"question": "q"}, headers={"host": "evil.example"})[0].status, 403)
        self.assertEqual(call(app, "GET", "/api/research-pro/status?task=task_0123456789abcdef",
                              headers={"host": "evil.example"})[0].status, 403)
        self.assertEqual(call(app, "GET", "/api/research-pro/start")[0].status, 404)
        self.assertEqual(call(app, "POST", "/api/research-pro/status", {"task": "x"})[0].status, 404)
        self.assertEqual(transport.calls, [])
        self.assertEqual(body, b'{"question": "q"}')

    def test_status_requires_exactly_one_known_task(self):
        app, transport, _ = build_app(self, created())
        for target in ("/api/research-pro/status", "/api/research-pro/status?task=",
                       "/api/research-pro/status?task=a&task=b", "/api/research-pro/status?other=1"):
            self.assertEqual(call(app, "GET", target)[0].status, 400, target)
        for task in ("task_0123456789abcdef", "../../etc", "x" * 100):
            self.assertEqual(call(app, "GET", "/api/research-pro/status?task=" + task)[0].status, 404, task)
        self.assertEqual(transport.calls, [])

    def test_when_research_pro_is_off_the_routes_say_so_and_send_nothing(self):
        app, transport, _ = build_app(self, created(), configured=False)
        self.assertEqual(call(app, "POST", "/api/research-pro/start", {"question": "q"})[0].status, 400)
        self.assertEqual(call(app, "GET", "/api/research-pro/status?task=task_0123456789abcdef")[0].status, 400)
        self.assertEqual(transport.calls, [])

    def test_the_service_validates_what_it_is_given(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(TypeError):
            DemoService(store, research_pro_client=object())
        with self.assertRaises(ValueError):
            DemoService(store, research_pro_client=TavilyResearchPro(), research_client=tavily.TavilyResearch(["a.example.org"]),
                        research_domains=["a.example.org"])
        self.assertFalse(DemoService(store).state()["research_pro"]["configured"])

    def test_simulated_mode_makes_no_external_request(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        app = DemoApp(DemoService(store), {HOST})
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network")), \
                mock.patch.object(tavily_research, "urllib_transport", side_effect=AssertionError("research used")):
            response, state = call(app, "GET", "/api/state")
            self.assertEqual(response.status, 200)
            response, data = call(app, "POST", "/api/run", {"scenario": "normal_operation"})
            self.assertEqual(response.status, 200)


class TestStartupFlags(NoSockets):
    def run_main(self, argv, key=True):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(server_module, "HTTPServer") as fake_server, \
                mock.patch.object(server_module, "DemoApp") as fake_app, \
                mock.patch.object(server_module, "tavily_key_present", return_value=key), \
                mock.patch.object(server_module, "tavily_pro_key_present", return_value=key):
            instance = fake_server.return_value
            instance.server_address = ("127.0.0.1", 12345)
            instance.serve_forever.side_effect = KeyboardInterrupt
            code = server_module.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), fake_server, fake_app

    def test_tavily_pro_with_a_key_starts_and_says_exactly_what_is_sent_and_the_limits(self):
        code, out, err, _, fake_app = self.run_main(["--research-provider", "tavily-pro"])
        self.assertEqual((code, err), (0, ""))
        state = fake_app.call_args.args[0].state()
        self.assertEqual(state["research_pro"]["configured"], True)
        self.assertEqual(state["research_pro"]["domain"], "icar.org.in")
        self.assertEqual(state["research"]["configured"], False)
        for expected in ("MIXED MODE", "Tavily Research", "pro model", "icar.org.in", "120 seconds", "15 to 250 credits",
                         "unreviewed AI output", "only the question you type"):
            self.assertIn(expected, out)
        self.assertNotIn("SIMULATED DATA ONLY", out)

    def test_every_misconfiguration_refuses_to_start_without_binding(self):
        cases = {
            "no key": (["--research-provider", "tavily-pro"], False, "TAVILY_API_KEY"),
            "domains are fixed": (["--research-provider", "tavily-pro", "--research-domains", "icar.org.in"], True,
                                  "cannot be used with"),
            "other domains": (["--research-provider", "tavily-pro", "--research-domains", "other.example.org"], True,
                              "fixed to icar.org.in"),
        }
        for name, (argv, key, expected) in cases.items():
            with self.subTest(case=name):
                code, out, err, fake_server, fake_app = self.run_main(argv, key=key)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(expected, err)
                self.assertIn("Nothing was started", err)
                fake_server.assert_not_called()
                fake_app.assert_not_called()

    def test_the_other_modes_do_not_enable_research_pro(self):
        for argv in ([], ["--research-provider", "none"]):
            code, out, err, _, fake_app = self.run_main(argv)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(fake_app.call_args.args[0].state()["research_pro"]["configured"], False)
        code, _, _, _, fake_app = self.run_main(["--research-provider", "tavily", "--research-domains", "fao.org"])
        self.assertEqual(code, 0)
        state = fake_app.call_args.args[0].state()
        self.assertEqual((state["research"]["configured"], state["research_pro"]["configured"]), (True, False))

    def test_the_real_key_check_is_used_and_no_value_is_ever_printed(self):
        for env, expected in (({"TAVILY_API_KEY": KEY}, 0), ({}, 2), ({"TAVILY_API_KEY": " "}, 2)):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(server_module, "HTTPServer") as fake:
                instance = fake.return_value
                instance.server_address = ("127.0.0.1", 12345)
                instance.serve_forever.side_effect = KeyboardInterrupt
                code = server_module.main(["--research-provider", "tavily-pro"], stdout=out, stderr=err)
            self.assertEqual(code, expected)
            self.assertNotIn(KEY, out.getvalue() + err.getvalue())

    def test_second_opinion_still_combines_and_nebius_is_no_longer_a_choice(self):
        with mock.patch.object(server_module, "api_key_present", return_value=True):
            code, out, err, _, fake_app = self.run_main(
                ["--second-opinion-provider", "nvidia", "--research-provider", "tavily-pro"])
        self.assertEqual((code, err), (0, ""))
        state = fake_app.call_args.args[0].state()
        self.assertEqual((state["second_opinion"]["simulated"], state["research_pro"]["configured"]), (False, True))
        self.assertIn("NVIDIA", out)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            server_module.main(["--second-opinion-provider", "nebius"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", err.getvalue())
        self.assertEqual(server_module.PROVIDER_CHOICES, ("simulated", "nvidia"))

    def test_the_help_lists_the_new_choice_the_cost_and_the_deadline_and_no_longer_mentions_nebius(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            server_module.main(["--help"])
        text = " ".join(out.getvalue().split())
        for expected in ("tavily-pro", "120 seconds", "icar.org.in", "15 to 250 credits", "never recommendations"):
            self.assertIn(expected, text)
        self.assertNotIn("nebius", text.lower())

    def test_nebius_is_gone_from_the_active_code(self):
        root = Path(demo_app.__file__).resolve().parent.parent
        self.assertFalse((root / "image_analysis" / "nebius.py").exists())
        for package in ("demo_app", "image_analysis", "research", "second_opinion", "agri_pipeline", "advice",
                        "crop_context", "scripts"):
            for path in (root / package).rglob("*.py"):
                text = path.read_text(encoding="utf-8").lower()
                if path.name in ("demo_analyzer.py", "simulated_provider.py"):
                    continue  # they say, in a fixed sentence, that no provider (naming them) was used
                self.assertNotIn("nebius", text, str(path))
        self.assertNotIn("nebius", (STATIC / "index.html").read_text(encoding="utf-8").lower())


class TestPage(unittest.TestCase):
    PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
    SCRIPT = (STATIC / "app.js").read_text(encoding="utf-8")

    def function_text(self, name):
        start = self.SCRIPT.index("function " + name)
        nxt = [i for i in (self.SCRIPT.find("\nfunction ", start + 1), self.SCRIPT.find("\nasync function ", start + 1),
                           self.SCRIPT.find("\nconst ", start + 1), self.SCRIPT.find("\nlet ", start + 1)) if i != -1]
        return self.SCRIPT[start:min(nxt)]

    def test_the_shipped_page_claims_nothing_about_research_pro_before_the_server_answers(self):
        section = self.PAGE.split('<section id="research">')[1]
        for element in ('id="rp-result"', 'id="rp-label"', 'id="rp-text"', 'id="rp-sources"', 'id="rp-caveats"'):
            self.assertIn(element, section)
        self.assertIn('<div id="rp-result" hidden>', section)
        self.assertIn('<button id="rs-submit" type="submit" disabled>', section)
        for claim in ("REAL SERVICE", "Tavily", "icar.org.in", "AI-GENERATED"):
            self.assertNotIn(claim, section)

    def test_the_label_is_a_fixed_constant_and_matches_the_server(self):
        dash = "\\u2014" if "RESEARCH \\u2014 NOT" in self.SCRIPT else "—"  # either spelling is valid JavaScript
        self.assertIn("const RP_LABEL = \"UNREVIEWED AI-GENERATED RESEARCH " + dash + " NOT VERIFIED AGRICULTURAL ADVICE.\";",
                      self.SCRIPT)
        self.assertEqual(LABEL, "UNREVIEWED AI-GENERATED RESEARCH — NOT VERIFIED AGRICULTURAL ADVICE.")
        self.assertIn("$(\"rp-label\").textContent = RP_LABEL;", self.function_text("renderResearchPro"))

    def test_research_pro_rendering_is_text_only_and_links_are_checked_again_in_the_page(self):
        for name in ("renderResearchPro", "showResearchProView", "proStatusText", "describeResearchProError",
                     "configureResearchPro", "showResearchProBanner", "isApprovedLink", "pollResearchPro",
                     "submitResearchPro"):
            body = self.function_text(name)
            for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval(", "localStorage"):
                self.assertNotIn(forbidden, body, (name, forbidden))
        render = self.function_text("renderResearchPro")
        self.assertEqual(render.count("a.href"), 1)
        self.assertLess(render.index("isApprovedLink(s.url, domain)"), render.index("a.href"))
        self.assertIn("noopener noreferrer nofollow", render)
        self.assertIn("No approved sources available", render)
        self.assertIn('parsed.protocol !== "https:"', self.function_text("isApprovedLink"))

    def test_polling_is_bounded_and_duplicate_clicks_are_ignored(self):
        self.assertIn("if (rsBusy || !researchProConfig) return;", self.function_text("submitResearchPro"))
        poll = self.function_text("pollResearchPro")
        self.assertIn("RP_PAGE_MARGIN_MS", poll)
        self.assertIn("RP_MAX_POLL_FAILURES", poll)
        self.assertIn("runId !== rpRunId", poll)
        self.assertIn("const RP_POLL_MS = 5000;", self.SCRIPT)
        self.assertIn("researchProConfig) return submitResearchPro(event)", self.SCRIPT)

    def test_the_page_never_sends_the_section_1_fields_to_research_pro(self):
        submit = self.function_text("submitResearchPro")
        self.assertIn("JSON.stringify({ question: question })", submit)
        for forbidden in ("user-crop", "user-region", "user-stage", "photo", "record"):
            self.assertNotIn(forbidden, submit)

    def test_the_search_path_is_still_wired(self):
        self.assertIn("configureResearch(state.research);", self.SCRIPT)
        self.assertIn('api("/api/advisory"', self.SCRIPT)
        self.assertIn("addEventListener(\"submit\", submitAdvisory)", self.SCRIPT)


class TestPageBehaviourInNode(unittest.TestCase):
    """Runs the real app.js against a minimal fake page in Node (no browser, no network, no timers)."""

    def test_the_start_and_poll_flow_in_a_fake_page(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not installed, so the page logic cannot be exercised here")
        result = subprocess.run([node, str(HARNESS), str(STATIC / "app.js")], capture_output=True, text=True,
                                timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all scenarios passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
