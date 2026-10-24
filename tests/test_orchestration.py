import ast
import copy
import itertools
import json
import socket
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover
from orchestration import run as run_module
from orchestration.run import Step, StepFailure, execute_run

SECRET = "sk-test-SECRET-do-not-leak"


def mock_record(seed=0):
    rover = MockRover(replace(RoverConfig(), seed=seed))
    rover.startup()
    rover.insert_probe(depth_ok=True)
    rover.take_measurement()
    return rover.build_record()


RECORD = mock_record()


def make_clock():
    counter = itertools.count()
    return lambda: "t%d" % next(counter)


def ok(value):
    return lambda context: value


def boom(context):
    raise RuntimeError("provider said: " + SECRET)


def by_name(result):
    return {s["name"]: s for s in result["steps"]}


class OrderAndStatusTests(unittest.TestCase):
    def test_steps_run_in_the_requested_order(self):
        calls = []

        def make(name):
            def call(context):
                calls.append(name)
                return name
            return call

        steps = [Step("c", "local", make("c")), Step("a", "local", make("a")), Step("b", "local", make("b"))]
        result = execute_run(RECORD, steps, run_id="run-1", clock=make_clock())
        self.assertEqual(calls, ["c", "a", "b"])
        self.assertEqual([s["name"] for s in result["steps"]], ["c", "a", "b"])

    def test_successful_run_reports_completed_steps_and_status(self):
        steps = [Step("one", "local", ok({"n": 1})), Step("two", "nvidia", ok([1, 2]), depends_on=("one",))]
        result = execute_run(RECORD, steps, run_id="run-1", clock=make_clock())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([s["status"] for s in result["steps"]], ["completed", "completed"])
        self.assertEqual(result["steps"][0]["result"], {"n": 1})
        self.assertEqual(result["steps"][1]["provider"], "nvidia")
        self.assertEqual(result["errors"], [])

    def test_step_receives_a_private_record_copy_and_only_its_dependencies_results(self):
        seen = {}

        def first(context):
            context["record"]["rover_id"] = "tampered"
            return {"x": 1}

        def second(context):
            seen.update(context)
            return None

        steps = [Step("first", "local", first), Step("other", "local", ok(5)),
                 Step("second", "local", second, depends_on=("first",))]
        original = copy.deepcopy(RECORD)
        execute_run(RECORD, steps, run_id="run-1", record_id=7, clock=make_clock())
        self.assertEqual(RECORD, original)
        self.assertEqual(seen["record"], original)
        self.assertEqual(seen["results"], {"first": {"x": 1}})
        self.assertEqual((seen["run_id"], seen["record_id"]), ("run-1", 7))


class FailureAndSkipTests(unittest.TestCase):
    def test_failed_step_records_a_safe_code_without_the_exception_message(self):
        result = execute_run(RECORD, [Step("bad", "tavily", boom)], run_id="run-1", clock=make_clock())
        step = result["steps"][0]
        self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", "step_failed", None))
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn("RuntimeError", json.dumps(result))

    def test_step_failure_code_is_kept_only_if_it_is_a_token(self):
        def good(context):
            raise StepFailure("rate_limited")

        def bad(context):
            raise StepFailure("Bad Code: " + SECRET)

        result = execute_run(RECORD, [Step("a", "local", good), Step("b", "local", bad)],
                             run_id="run-1", clock=make_clock())
        steps = by_name(result)
        self.assertEqual(steps["a"]["error_code"], "rate_limited")
        self.assertEqual(steps["b"]["error_code"], "step_failed")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_unserialisable_result_fails_the_step(self):
        for value in (object(), {1, 2}, float("nan")):
            with self.subTest(value=repr(value)):
                result = execute_run(RECORD, [Step("s", "local", ok(value))], run_id="run-1",
                                     clock=make_clock())
                self.assertEqual(result["steps"][0]["status"], "failed")
                self.assertEqual(result["steps"][0]["error_code"], "result_not_serializable")
                json.dumps(result)

    def test_dependants_are_skipped_after_a_failure_but_independent_steps_still_run(self):
        ran = []

        def track(name):
            def call(context):
                ran.append(name)
                return name
            return call

        steps = [Step("image", "nvidia", boom),
                 Step("research", "tavily", track("research")),
                 Step("synthesis", "openrouter", track("synthesis"), depends_on=("image", "research")),
                 Step("report", "local", track("report"), depends_on=("synthesis",))]
        result = execute_run(RECORD, steps, run_id="run-1", clock=make_clock())
        steps_by_name = by_name(result)
        self.assertEqual(ran, ["research"])
        self.assertEqual(steps_by_name["research"]["status"], "completed")
        for name in ("synthesis", "report"):
            self.assertEqual(steps_by_name[name]["status"], "skipped")
            self.assertEqual(steps_by_name[name]["skip_reason"], "dependency_not_completed")
            self.assertIsNone(steps_by_name[name]["started_at"])
            self.assertIsNone(steps_by_name[name]["completed_at"])
        self.assertEqual(result["status"], "failed")

    def test_explicitly_skipped_provider_is_not_reported_as_completed(self):
        called = []
        steps = [Step("image", "nvidia", lambda c: called.append(1), skip_reason="provider_not_configured"),
                 Step("after", "local", ok(1), depends_on=("image",)),
                 Step("free", "local", ok(2))]
        result = execute_run(RECORD, steps, run_id="run-1", clock=make_clock())
        steps_by_name = by_name(result)
        self.assertEqual(called, [])
        self.assertEqual(steps_by_name["image"]["status"], "skipped")
        self.assertEqual(steps_by_name["image"]["skip_reason"], "provider_not_configured")
        self.assertIsNone(steps_by_name["image"]["result"])
        self.assertEqual(steps_by_name["after"]["status"], "skipped")
        self.assertEqual(steps_by_name["free"]["status"], "completed")
        self.assertEqual(result["status"], "partial")
        self.assertNotEqual(result["status"], "completed")

    def test_run_status_for_each_combination(self):
        skipped = lambda name: Step(name, "local", ok(1), skip_reason="not_configured")
        cases = {
            "all completed": ([Step("a", "local", ok(1)), Step("b", "local", ok(2))], "completed"),
            "all skipped": ([skipped("a"), skipped("b")], "skipped"),
            "single skipped": ([skipped("a")], "skipped"),
            "completed and skipped": ([Step("a", "local", ok(1)), skipped("b")], "partial"),
            "failed only": ([Step("a", "local", boom)], "failed"),
            "failed and completed": ([Step("a", "local", boom), Step("b", "local", ok(1))], "failed"),
            "failed and skipped": ([Step("a", "local", boom), skipped("b")], "failed"),
            "failed then dependency-skipped": ([Step("a", "local", boom),
                                                Step("b", "local", ok(1), depends_on=("a",))], "failed"),
        }
        for label, (steps, expected) in cases.items():
            with self.subTest(label):
                result = execute_run(RECORD, steps, run_id="run-1", clock=make_clock())
                self.assertEqual(result["status"], expected)

    def test_all_skipped_run_has_no_completed_step_and_no_timestamps_for_steps(self):
        steps = [Step("a", "nvidia", ok(1), skip_reason="not_configured"),
                 Step("b", "tavily", ok(1), depends_on=("a",))]
        result = execute_run(RECORD, steps, run_id="run-1", clock=make_clock())
        self.assertEqual(result["status"], "skipped")
        self.assertEqual([s["status"] for s in result["steps"]], ["skipped", "skipped"])
        self.assertTrue(all(s["started_at"] is None and s["result"] is None for s in result["steps"]))

    def test_empty_steps_are_rejected_not_reported_as_skipped_or_completed(self):
        for empty in ([], ()):
            with self.subTest(empty=repr(empty)):
                with self.assertRaises(ValueError):
                    execute_run(RECORD, empty, run_id="run-1")
        # even for an invalid record, the empty steps are rejected first
        with self.assertRaises(ValueError):
            execute_run({}, [], run_id="run-1")

    def test_failure_and_skip_codes_with_a_trailing_newline_are_not_kept(self):
        def newline_code(context):
            raise StepFailure("rate_limited\n")

        result = execute_run(RECORD, [Step("a", "local", newline_code)], run_id="run-1")
        self.assertEqual(result["steps"][0]["error_code"], "step_failed")
        self.assertEqual(result["status"], "failed")
        with self.assertRaises(ValueError):
            execute_run(RECORD, [Step("a", "local", ok(1), skip_reason="off\n")], run_id="run-1")

    def test_a_step_cannot_change_a_dependency_result_stored_by_an_earlier_step(self):
        seen_by_third = {}

        def first(context):
            return {"items": [1, 2], "nested": {"k": "v"}}

        def second(context):
            context["results"]["first"]["items"].append(99)
            context["results"]["first"]["nested"]["k"] = "tampered"
            context["results"]["first"]["new"] = True
            return "second"

        def third(context):
            seen_by_third.update(copy.deepcopy(context["results"]))
            return "third"

        steps = [Step("first", "local", first), Step("second", "local", second, depends_on=("first",)),
                 Step("third", "local", third, depends_on=("first",))]
        result = execute_run(RECORD, steps, run_id="run-1", clock=make_clock())
        original = {"items": [1, 2], "nested": {"k": "v"}}
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"][0]["result"], original)
        self.assertEqual(seen_by_third["first"], original)

    def test_a_step_cannot_change_a_stored_result_by_keeping_a_reference_to_its_return_value(self):
        kept = {"list": [1]}

        def first(context):
            return kept

        def second(context):
            kept["list"].append(2)  # mutates the object the first step returned
            return None

        steps = [Step("first", "local", first), Step("second", "local", second)]
        result = execute_run(RECORD, steps, run_id="run-1")
        self.assertEqual(result["steps"][0]["result"], {"list": [1]})

    def test_depends_on_is_snapshotted_before_the_first_step_runs(self):
        order = []
        deps = ["a"]

        def a(context):
            order.append("a")
            raise StepFailure("broken")

        def b(context):
            order.append("b")
            return None

        def tamper(context):
            deps.clear()  # try to remove the dependency of `c` while the run is in progress
            order.append("tamper")
            return None

        c = Step("c", "local", lambda context: order.append("c"), depends_on=deps)
        steps = [Step("a", "local", a), Step("tamper", "local", tamper), Step("b", "local", b), c]
        result = execute_run(RECORD, steps, run_id="run-1")
        self.assertEqual(order, ["a", "tamper", "b"])
        self.assertEqual(by_name(result)["c"]["status"], "skipped")
        self.assertEqual(by_name(result)["c"]["depends_on"], ["a"])
        self.assertEqual(by_name(result)["c"]["skip_reason"], "dependency_not_completed")


class DeterminismTests(unittest.TestCase):
    def test_injected_clock_gives_deterministic_timestamps(self):
        steps = [Step("a", "local", ok(1)), Step("b", "local", boom), Step("c", "local", ok(3), depends_on=("b",))]
        first = execute_run(RECORD, steps, run_id="run-1", clock=make_clock())
        second = execute_run(RECORD, steps, run_id="run-1", clock=make_clock())
        self.assertEqual(first, second)
        steps_by_name = by_name(first)
        self.assertEqual(first["started_at"], "t0")
        self.assertEqual((steps_by_name["a"]["started_at"], steps_by_name["a"]["completed_at"]), ("t1", "t2"))
        self.assertEqual((steps_by_name["b"]["started_at"], steps_by_name["b"]["completed_at"]), ("t3", "t4"))
        self.assertEqual(first["completed_at"], "t5")

    def test_default_clock_gives_iso_text(self):
        result = execute_run(RECORD, [Step("a", "local", ok(1))], run_id="run-1")
        self.assertIsInstance(result["started_at"], str)
        self.assertIn("T", result["started_at"])

    def test_explicit_run_id_and_factory(self):
        steps = [Step("a", "local", ok(1))]
        self.assertEqual(execute_run(RECORD, steps, run_id="abc_123-X")["run_id"], "abc_123-X")
        ids = iter(["first-run", "second-run"])
        factory = lambda: next(ids)
        self.assertEqual(execute_run(RECORD, steps, run_id_factory=factory)["run_id"], "first-run")
        self.assertEqual(execute_run(RECORD, steps, run_id_factory=factory)["run_id"], "second-run")

    def test_default_run_ids_differ_and_are_not_derived_from_the_record(self):
        steps = [Step("a", "local", ok(1))]
        a, b = execute_run(RECORD, steps)["run_id"], execute_run(RECORD, steps)["run_id"]
        self.assertNotEqual(a, b)
        for text in (str(RECORD["sequence_number"]), RECORD["rover_id"], str(RECORD["photo_id"])):
            self.assertNotEqual(a, text)

    def test_bad_run_ids_are_rejected(self):
        steps = [Step("a", "local", ok(1))]
        for bad in ("", "has space", "x" * 65, 5, "../x"):
            with self.subTest(run_id=bad):
                with self.assertRaises(ValueError):
                    execute_run(RECORD, steps, run_id=bad)
        with self.assertRaises(ValueError):
            execute_run(RECORD, steps, run_id="a", run_id_factory=lambda: "b")
        with self.assertRaises(ValueError):
            execute_run(RECORD, steps, run_id_factory=lambda: "bad id")

    def test_same_record_twice_makes_two_independent_runs(self):
        steps = [Step("a", "local", ok(1))]
        ids = iter(["r1", "r2"])
        one = execute_run(RECORD, steps, run_id_factory=lambda: next(ids), record_id=3)
        two = execute_run(RECORD, steps, run_id_factory=lambda: next(ids), record_id=3)
        self.assertNotEqual(one["run_id"], two["run_id"])
        self.assertEqual(one["record_id"], two["record_id"])


class RecordIdentityTests(unittest.TestCase):
    def test_missing_record_id_is_null_and_unavailable(self):
        result = execute_run(RECORD, [Step("a", "local", ok(1))], run_id="run-1")
        self.assertIsNone(result["record_id"])
        self.assertEqual(result["record_id_scope"], "unavailable")

    def test_supplied_storage_id_is_labelled_local_store(self):
        result = execute_run(RECORD, [Step("a", "local", ok(1))], run_id="run-1", record_id=41)
        self.assertEqual((result["record_id"], result["record_id_scope"]), (41, "local_store"))

    def test_record_id_is_never_inferred_from_record_fields(self):
        record = copy.deepcopy(RECORD)
        record.update(sequence_number=99, photo_id="mock-photo-0-0005")
        result = execute_run(record, [Step("a", "local", ok(1))], run_id="run-1")
        self.assertIsNone(result["record_id"])
        self.assertEqual(result["record_id_scope"], "unavailable")
        self.assertNotIn(str(record["sequence_number"]), (result["run_id"],))

    def test_bad_record_ids_are_rejected(self):
        for bad in (True, False, 0, -1, 1.5, "7", [1]):
            with self.subTest(record_id=bad):
                with self.assertRaises(ValueError):
                    execute_run(RECORD, [Step("a", "local", ok(1))], run_id="run-1", record_id=bad)


class ValidationTests(unittest.TestCase):
    def test_invalid_record_returns_invalid_record_and_runs_nothing(self):
        called = []
        steps = [Step("a", "local", lambda c: called.append(1))]
        for bad in ({}, [], "text", None, dict(RECORD, rover_id="")):
            with self.subTest(record=repr(bad)[:30]):
                result = execute_run(bad, steps, run_id="run-1", clock=make_clock())
                self.assertEqual(result["status"], "invalid_record")
                self.assertEqual(result["steps"], [])
                self.assertTrue(result["errors"])
                self.assertEqual(set(result["errors"][0]), {"path", "code"})
                json.dumps(result)
        self.assertEqual(called, [])

    def test_invalid_record_error_does_not_echo_values(self):
        bad = dict(RECORD, rover_id="")
        bad["time"] = SECRET
        bad["extra"] = SECRET
        result = execute_run(bad, [Step("a", "local", ok(1))], run_id="run-1")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_malformed_steps_are_rejected(self):
        good = Step("a", "local", ok(1))
        cases = {
            "not a sequence": 5,
            "string": "abc",
            "bytes": b"abc",
            "bytearray": bytearray(b"abc"),
            "dict": {"a": good},
            "set": {good},
            "frozenset": frozenset([good]),
            "generator": (s for s in [good]),
            "iterator": iter([good]),
            "dict values": {"a": good}.values(),
            "None": None,
            "empty": [],
            "not a Step": [{"name": "a"}],
            "duplicate name": [good, Step("a", "local", ok(2))],
            "bad name": [Step("Bad Name", "local", ok(1))],
            "bad provider": [Step("a", "Not A Token", ok(1))],
            "not callable": [Step("a", "local", "nope")],
            "unknown dependency": [Step("a", "local", ok(1), depends_on=("zzz",))],
            "later dependency": [Step("a", "local", ok(1), depends_on=("b",)), Step("b", "local", ok(2))],
            "self dependency": [Step("a", "local", ok(1), depends_on=("a",))],
            "bad skip reason": [Step("a", "local", ok(1), skip_reason="Not OK")],
            "depends_on text": [good, Step("b", "local", ok(1), depends_on="a")],
        }
        for label, steps in cases.items():
            with self.subTest(label):
                with self.assertRaises((TypeError, ValueError)):
                    execute_run(RECORD, steps, run_id="run-1")

    def test_unsupported_containers_raise_type_error_and_run_no_step(self):
        called = []
        step = Step("a", "local", lambda c: called.append(1))
        for container in ({step}, frozenset([step]), {"a": step}, "abc", b"abc", (s for s in [step])):
            with self.subTest(container=type(container).__name__):
                with self.assertRaises(TypeError):
                    execute_run(RECORD, container, run_id="run-1")
        self.assertEqual(called, [])
        # a list and a tuple are accepted
        self.assertEqual(execute_run(RECORD, [step], run_id="run-1")["status"], "completed")
        self.assertEqual(execute_run(RECORD, (step,), run_id="run-1")["status"], "completed")

    def test_trailing_newline_is_rejected_for_ids_and_names(self):
        steps = [Step("a", "local", ok(1))]
        for bad in ("run-1\n", "\nrun-1", "run-1\r\n", "run\n-1"):
            with self.subTest(run_id=bad):
                with self.assertRaises(ValueError):
                    execute_run(RECORD, steps, run_id=bad)
        with self.assertRaises(ValueError):
            execute_run(RECORD, steps, run_id_factory=lambda: "run-1\n")
        for field, step in (("name", Step("a\n", "local", ok(1))),
                            ("provider", Step("a", "local\n", ok(1))),
                            ("name with leading newline", Step("\na", "local", ok(1)))):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    execute_run(RECORD, [step], run_id="run-1")
        with self.assertRaises(ValueError):  # a dependency name with a newline cannot match an earlier step
            execute_run(RECORD, [Step("a", "local", ok(1)), Step("b", "local", ok(1), depends_on=("a\n",))],
                        run_id="run-1")

    def test_bad_clock_is_rejected(self):
        with self.assertRaises(TypeError):
            execute_run(RECORD, [Step("a", "local", ok(1))], run_id="run-1", clock="now")
        with self.assertRaises(TypeError):
            execute_run(RECORD, [Step("a", "local", ok(1))], run_id="run-1", clock=lambda: 5)


class ArgumentCheckTests(unittest.TestCase):
    """`check_run_arguments` validates the run arguments without calling the run-id factory or the clock (F-07)."""

    BAD = (dict(record_id=0), dict(record_id=True), dict(record_id="7"), dict(record_id=1.5),
           dict(run_id=""), dict(run_id="has space"), dict(run_id="run-1\n"), dict(run_id=5),
           dict(run_id="x" * 65), dict(run_id="a", run_id_factory=lambda: "b"),
           dict(run_id_factory="not callable"), dict(run_id_factory=5), dict(clock="not callable"), dict(clock=5))

    def test_good_arguments_pass_and_the_factory_and_clock_are_never_called(self):
        calls = []
        factory = lambda: calls.append("factory") or "run-x"
        clock = lambda: calls.append("clock") or "t"
        for good in (dict(), dict(record_id=1), dict(record_id=7, run_id="run-1"), dict(run_id_factory=factory),
                     dict(clock=clock), dict(record_id=3, run_id_factory=factory, clock=clock)):
            self.assertIsNone(run_module.check_run_arguments(**good))
        self.assertEqual(calls, [])

    def test_bad_arguments_raise_the_same_error_as_execute_run(self):
        steps = [Step("a", "local", ok(1))]
        for bad in self.BAD:
            with self.subTest(bad=repr(bad)[:40]):
                with self.assertRaises((ValueError, TypeError)) as checked:
                    run_module.check_run_arguments(**bad)
                with self.assertRaises(type(checked.exception)) as ran:
                    execute_run(RECORD, steps, **bad)
                self.assertEqual(str(checked.exception), str(ran.exception))

    def test_the_order_is_record_id_then_run_id_then_clock(self):
        with self.assertRaises(ValueError) as raised:
            run_module.check_run_arguments(record_id=0, run_id="has space", clock=5)
        self.assertIn("record_id", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            run_module.check_run_arguments(run_id="has space", clock=5)
        self.assertIn("run_id", str(raised.exception))
        with self.assertRaises(TypeError):
            run_module.check_run_arguments(clock=5)

    def test_the_messages_never_contain_the_submitted_values(self):
        with self.assertRaises(ValueError) as raised:
            run_module.check_run_arguments(run_id="secret " + SECRET)  # the space makes it invalid
        self.assertNotIn(SECRET, str(raised.exception))


def result_of_size(size):
    """A JSON-compatible value whose serialised text (the runner's own `json.dumps`) is exactly `size` long."""
    value = {"t": "x" * (size - len(json.dumps({"t": ""})))}
    assert len(json.dumps(value)) == size
    return value


class ResultSizeTests(unittest.TestCase):
    """The per-step result limit (F-09). The size is the length of the serialised JSON text the runner itself
    produces (ASCII-escaped, so characters equal bytes): serialised size, not Python memory. The limit is
    provisional (see ARCHITECTURE_DECISIONS.md, OR-8). The per-run total is tested in RunTotalTests below."""

    LIMIT = run_module.MAX_STEP_RESULT_BYTES

    def run_one(self, value):
        return execute_run(RECORD, [Step("big", "local", lambda context: value)], run_id="run-1")

    def test_the_provisional_limit_reuses_the_existing_response_body_limit(self):
        from image_analysis import nvidia
        self.assertEqual(self.LIMIT, 4 * 1024 * 1024)
        self.assertEqual(self.LIMIT, nvidia._MAX_RESPONSE_BYTES)

    def test_below_and_exactly_at_the_limit_are_stored_unchanged(self):
        for size in (100, self.LIMIT - 1, self.LIMIT):
            with self.subTest(size=size):
                value = result_of_size(size)
                run = self.run_one(value)
                step = run["steps"][0]
                self.assertEqual((run["status"], step["status"], step["error_code"]), ("completed", "completed", None))
                self.assertEqual(step["result"], value)               # nothing truncated or changed

    def test_one_over_the_limit_fails_the_step_explicitly_and_keeps_nothing(self):
        run = self.run_one(result_of_size(self.LIMIT + 1))
        step = run["steps"][0]
        self.assertEqual((run["status"], step["status"], step["error_code"], step["result"]),
                         ("failed", "failed", "result_too_large", None))
        text = json.dumps(run)
        self.assertNotIn("x" * 1000, text)                           # no part of the oversized value is kept
        self.assertLess(len(text), 2000)
        self.assertEqual(run_module.ERROR_RESULT_TOO_LARGE, "result_too_large")

    def test_a_failed_oversized_step_follows_the_existing_dependency_rules(self):
        ran = []

        def independent(context):
            ran.append("independent")
            return 1

        def dependant(context):
            ran.append("dependant")
            return 2

        steps = [Step("big", "local", lambda context: result_of_size(self.LIMIT + 1)),
                 Step("free", "local", independent),
                 Step("after", "local", dependant, depends_on=("big",))]
        run = execute_run(RECORD, steps, run_id="run-1")
        by = {s["name"]: s for s in run["steps"]}
        self.assertEqual((by["big"]["status"], by["free"]["status"], by["after"]["status"]),
                         ("failed", "completed", "skipped"))
        self.assertEqual(by["after"]["skip_reason"], "dependency_not_completed")
        self.assertEqual((ran, run["status"]), (["independent"], "failed"))

    def test_the_size_is_the_ascii_escaped_text_so_non_ascii_text_counts_conservatively(self):
        # "é" is 2 bytes in UTF-8 but 6 characters ("é") in the runner's JSON text.
        fits = {"t": "é" * ((self.LIMIT - len(json.dumps({"t": ""}))) // 6)}
        self.assertLessEqual(len(json.dumps(fits)), self.LIMIT)
        self.assertEqual(self.run_one(fits)["steps"][0]["status"], "completed")
        over = {"t": "é" * (self.LIMIT // 6 + 1)}
        self.assertGreater(len(json.dumps(over)), self.LIMIT)
        self.assertLess(len(json.dumps(over, ensure_ascii=False).encode("utf-8")), self.LIMIT)  # smaller as UTF-8
        self.assertEqual(self.run_one(over)["steps"][0]["error_code"], "result_too_large")

    def test_nested_values_are_measured_as_a_whole(self):
        part = result_of_size(self.LIMIT // 2)["t"]
        fits = {"a": [part], "b": {"c": part[:1000]}}
        self.assertLessEqual(len(json.dumps(fits)), self.LIMIT)
        self.assertEqual(self.run_one(fits)["steps"][0]["status"], "completed")
        over = {"a": [part], "b": {"c": [part, part[:100]]}}
        self.assertGreater(len(json.dumps(over)), self.LIMIT)
        self.assertEqual(self.run_one(over)["steps"][0]["error_code"], "result_too_large")

    def test_a_value_json_cannot_hold_is_still_not_serializable_not_too_large(self):
        for value in ({"t": float("nan")}, {1, 2}, object()):
            with self.subTest(value=repr(value)[:20]):
                self.assertEqual(self.run_one(value)["steps"][0]["error_code"], "result_not_serializable")

    def test_the_limit_is_read_when_the_step_returns_so_it_can_be_checked_cheaply(self):
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 120):
            self.assertEqual(self.run_one(result_of_size(120))["steps"][0]["status"], "completed")
            self.assertEqual(self.run_one(result_of_size(121))["steps"][0]["error_code"], "result_too_large")



class RunTotalTests(unittest.TestCase):
    """The per-run cumulative limit (OR-9, owner-approved but provisional: 8 MiB). The total is the sum of the
    serialised lengths (the runner's own `json.dumps` text) of the results actually STORED by completed steps.
    A step that would take the total over the limit fails with `result_too_large`, stores nothing and adds
    nothing to the total. Failed and skipped steps count zero. It is serialised size, not memory."""

    def steps_of(self, sizes, **kw):
        return [Step("s%d" % n, "local", (lambda size: lambda context: result_of_size(size))(size), **kw)
                for n, size in enumerate(sizes)]

    def run_sizes(self, sizes):
        return execute_run(RECORD, self.steps_of(sizes), run_id="run-1")

    def codes(self, run):
        return [(s["status"], s["error_code"]) for s in run["steps"]]

    def test_the_real_policy_values_are_pinned(self):
        self.assertEqual(run_module.MAX_STEP_RESULT_BYTES, 4 * 1024 * 1024)
        self.assertEqual(run_module.MAX_RUN_RESULT_BYTES, 8 * 1024 * 1024)

    def test_with_the_real_limits_two_results_at_the_step_limit_total_exactly_the_run_limit(self):
        step_limit = run_module.MAX_STEP_RESULT_BYTES
        run = self.run_sizes([step_limit, step_limit])
        self.assertEqual((run["status"], self.codes(run)), ("completed", [("completed", None)] * 2))

    def test_with_the_real_limits_a_third_result_fails_even_if_tiny_and_the_first_two_stay(self):
        step_limit = run_module.MAX_STEP_RESULT_BYTES
        run = self.run_sizes([step_limit, step_limit, 100])
        self.assertEqual(self.codes(run), [("completed", None)] * 2 + [("failed", "result_too_large")])
        self.assertEqual((run["status"], run["steps"][2]["result"]), ("failed", None))
        self.assertEqual(len(run["steps"][0]["result"]["t"]) + len(run["steps"][1]["result"]["t"]),
                         2 * (step_limit - len(json.dumps({"t": ""}))))   # earlier results untouched

    def test_one_byte_over_the_step_limit_still_fails_with_the_real_limits(self):
        run = self.run_sizes([run_module.MAX_STEP_RESULT_BYTES + 1])
        self.assertEqual(self.codes(run), [("failed", "result_too_large")])

    def test_below_both_limits_everything_is_stored_unchanged(self):
        values = [result_of_size(100), {"a": [1, 2.5, None, "é"]}, "plain"]
        steps = [Step("s%d" % n, "local", (lambda v: lambda context: v)(v)) for n, v in enumerate(values)]
        run = execute_run(RECORD, steps, run_id="run-1")
        self.assertEqual((run["status"], [s["result"] for s in run["steps"]]), ("completed", values))

    def test_exactly_the_run_limit_is_allowed_and_one_byte_over_is_rejected(self):
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 100), \
                mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", 250):
            exact = self.run_sizes([100, 100, 50])
            self.assertEqual((exact["status"], self.codes(exact)), ("completed", [("completed", None)] * 3))
            over = self.run_sizes([100, 100, 51])
            self.assertEqual(self.codes(over), [("completed", None)] * 2 + [("failed", "result_too_large")])
            self.assertEqual((over["status"], over["steps"][2]["result"]), ("failed", None))

    def test_a_rejected_step_adds_nothing_so_a_later_smaller_independent_step_can_still_fit(self):
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 100), \
                mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", 250):
            run = self.run_sizes([100, 100, 100, 50])   # the third is rejected (300 > 250); 200 is still used
            self.assertEqual(self.codes(run), [("completed", None)] * 2 + [("failed", "result_too_large"),
                                                                           ("completed", None)])
            self.assertEqual(len(json.dumps(run["steps"][3]["result"])), 50)

    def test_a_later_independent_step_is_attempted_but_obeys_the_same_total(self):
        ran = []

        def late(context):
            ran.append("late")
            return result_of_size(60)

        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 100), \
                mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", 150):
            steps = self.steps_of([100, 100]) + [Step("late", "local", late)]
            run = execute_run(RECORD, steps, run_id="run-1")
        self.assertEqual(ran, ["late"])                                  # it was called ...
        self.assertEqual(self.codes(run), [("completed", None), ("failed", "result_too_large"),
                                           ("failed", "result_too_large")])  # ... and 100 + 60 > 150

    def test_a_dependant_of_a_step_rejected_by_the_total_is_skipped(self):
        ran = []
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 100), \
                mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", 150):
            steps = self.steps_of([100, 100]) + [Step("after", "local", lambda c: ran.append(1),
                                                      depends_on=("s1",))]
            run = execute_run(RECORD, steps, run_id="run-1")
        by = {s["name"]: s for s in run["steps"]}
        self.assertEqual((by["s1"]["status"], by["after"]["status"], by["after"]["skip_reason"]),
                         ("failed", "skipped", "dependency_not_completed"))
        self.assertEqual(ran, [])

    def test_failed_and_skipped_steps_contribute_zero(self):
        def fail(context):
            raise StepFailure("nope")

        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 100), \
                mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", 200):
            steps = [Step("bad", "local", fail), Step("off", "local", ok(1), skip_reason="off"),
                     Step("dep", "local", ok(1), depends_on=("bad",)),
                     Step("unser", "local", ok({1, 2})),
                     Step("a", "local", lambda c: result_of_size(100)),
                     Step("b", "local", lambda c: result_of_size(100))]
            run = execute_run(RECORD, steps, run_id="run-1")
        self.assertEqual([s["status"] for s in run["steps"]],
                         ["failed", "skipped", "skipped", "failed", "completed", "completed"])

    def test_the_total_is_per_run_not_remembered_between_runs(self):
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 100), \
                mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", 200):
            for _ in range(2):
                self.assertEqual(self.run_sizes([100, 100])["status"], "completed")

    def test_each_result_counts_once_using_the_normalised_stored_form(self):
        # a tuple becomes a list and an int key a string; the stored (normalised) text is what is counted
        value = {1: (1, 2)}
        size = len(json.dumps(value))
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 1000), \
                mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", 2 * size):
            steps = [Step("a", "local", ok(value)), Step("b", "local", ok(value)), Step("c", "local", ok(1))]
            run = execute_run(RECORD, steps, run_id="run-1")
        self.assertEqual(self.codes(run), [("completed", None)] * 2 + [("failed", "result_too_large")])

    def test_non_ascii_and_nested_values_use_the_ascii_escaped_measure(self):
        value = {"t": ["é", {"u": "é"}]}                                 # 6 characters per "é" in the text
        size = len(json.dumps(value))
        self.assertGreater(size, len(json.dumps(value, ensure_ascii=False).encode("utf-8")))
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 1000), \
                mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", 2 * size):
            steps = [Step("a", "local", ok(value)), Step("b", "local", ok(value)), Step("c", "local", ok(value))]
            run = execute_run(RECORD, steps, run_id="run-1")
        self.assertEqual(self.codes(run), [("completed", None)] * 2 + [("failed", "result_too_large")])

    def test_not_serializable_keeps_precedence_over_the_total(self):
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 100), \
                mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", 100):
            steps = self.steps_of([100]) + [Step("bad", "local", ok({1, 2}))]
            run = execute_run(RECORD, steps, run_id="run-1")   # the total is used up; a set is still "not serializable"
        self.assertEqual(self.codes(run), [("completed", None), ("failed", "result_not_serializable")])

    def test_nothing_of_a_rejected_result_leaks_into_the_run(self):
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", 400), \
                mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", 500):
            secret_value = {"t": "SECRET" + "y" * 300}
            steps = [Step("a", "local", lambda c: result_of_size(300)), Step("b", "local", ok(secret_value))]
            run = execute_run(RECORD, steps, run_id="run-1")
        self.assertEqual(run["steps"][1]["error_code"], "result_too_large")
        self.assertNotIn("SECRET", json.dumps(run))
        self.assertIsNone(run["steps"][1]["result"])

    def test_the_run_dict_shape_is_unchanged(self):
        run = self.run_sizes([10])
        self.assertEqual(set(run), {"version", "run_id", "record_id", "record_id_scope", "status", "started_at",
                                    "completed_at", "steps", "errors"})
        self.assertEqual(set(run["steps"][0]), {"name", "provider", "status", "depends_on", "started_at",
                                                "completed_at", "result", "error_code", "skip_reason"})


class OutputTests(unittest.TestCase):
    def test_result_serialises_with_the_standard_json_encoder(self):
        steps = [Step("a", "local", ok({"k": [1, 2.5, None, "x"]})), Step("b", "nvidia", boom),
                 Step("c", "local", ok(1), depends_on=("b",)), Step("d", "tavily", ok(1), skip_reason="off")]
        result = execute_run(RECORD, steps, run_id="run-1", record_id=2, clock=make_clock())
        text = json.dumps(result, allow_nan=False)
        self.assertEqual(json.loads(text), result)
        expected = {"version", "run_id", "record_id", "record_id_scope", "status", "started_at",
                    "completed_at", "steps", "errors"}
        self.assertEqual(set(result), expected)
        for step in result["steps"]:
            self.assertEqual(set(step), {"name", "provider", "status", "depends_on", "started_at",
                                         "completed_at", "result", "error_code", "skip_reason"})

    def test_result_does_not_contain_the_record_or_its_values(self):
        result = execute_run(RECORD, [Step("a", "local", ok(1))], run_id="run-1")
        text = json.dumps(result)
        self.assertNotIn(RECORD["rover_id"], text)
        self.assertNotIn("sensor_status", text)

    def test_returned_results_are_independent_copies(self):
        shared = {"list": [1]}
        result = execute_run(RECORD, [Step("a", "local", ok(shared))], run_id="run-1")
        shared["list"].append(2)
        self.assertEqual(result["steps"][0]["result"], {"list": [1]})


ALLOWED_IMPORTS = {"copy", "json", "re", "secrets", "dataclasses", "datetime", "record"}


class IsolationTests(unittest.TestCase):
    def test_module_imports_only_standard_library_and_the_record_validator(self):
        for path in (Path(run_module.__file__), Path(run_module.__file__).with_name("__init__.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertIn(name.split(".")[0], ALLOWED_IMPORTS, "%s imports %s" % (path.name, name))

    def test_no_provider_or_sdk_modules_are_named_in_the_source(self):
        source = Path(run_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        for forbidden in ("nodus", "image_analysis", "research", "demo_app", "requests", "urllib",
                          "socket", "http", "agri_pipeline", "second_opinion"):
            self.assertFalse(any(m == forbidden or m.startswith(forbidden + ".") for m in imported), forbidden)

    def test_a_run_makes_no_network_call_through_the_common_python_entry_points(self):
        """A tripwire, not proof of isolation: it fails if code in this process calls these two entry
        points during a run. It does not cover other libraries, other processes or the operating system."""
        def forbidden(*args, **kwargs):
            raise AssertionError("network entry point was called")

        steps = [Step("a", "local", ok(1)), Step("b", "local", ok(2), depends_on=("a",))]
        with mock.patch.object(socket.socket, "connect", forbidden), \
                mock.patch.object(urllib.request, "urlopen", forbidden):
            result = execute_run(RECORD, steps, run_id="run-1", clock=make_clock())
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
