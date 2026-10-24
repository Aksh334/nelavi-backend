"""Offline tests for the provider-neutral task-runner boundary, its deterministic fake and the thin Nodus adapter
(Milestone N-1).

Nothing here installs, imports or contacts the Nodus SDK. The "SDK" is a hand-written stand-in object that follows
only the calls the vendor documentation shows (a function handle with spawn(...), a call with get(timeout=...)).
A socket tripwire fails any test that opens a socket. The stand-in proves the adapter's own logic, not how the real
service behaves.
"""

import ast
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover
from orchestration import nodus_adapter, task_runner
from orchestration import run as run_module
from orchestration.nodus_adapter import NodusTaskRunner
from orchestration.run import Step, StepFailure, execute_run
from orchestration.task_runner import (
    ERROR_CODES,
    PAYLOAD_KEYS,
    SUMMARY_KEYS,
    TASK_SUMMARIZE_RECORD,
    FakeTaskRunner,
    TaskResult,
    expected_summary,
    minimal_payload,
    run_record_summary,
    task_step,
    validate_summary,
)
from record.validation import validate_record

CANARY = "canary-not-a-real-key-1234567890"


class NoSockets(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)


def make_record(fault=False):
    rover = MockRover(RoverConfig())
    rover.startup()
    rover.insert_probe()
    if fault:
        rover.inject_fault("moisture")
    rover.take_measurement()
    return rover.build_record(use_capture=False)


def steps_of(run):
    return {entry["name"]: entry for entry in run["steps"]}


class FakeCall:
    def __init__(self, handle, payload):
        self.handle, self.payload = handle, payload

    def get(self, timeout=None):
        self.handle.get_timeouts.append(timeout)
        if self.handle.raises is not None:
            raise self.handle.raises
        if self.handle.output is not None:
            return self.handle.output
        return expected_summary(self.payload)


class FakeHandle:
    """A stand-in for a Nodus function handle. Records what it was given; never touches a network."""

    def __init__(self, output=None, raises=None, spawn_raises=None):
        self.output, self.raises, self.spawn_raises = output, raises, spawn_raises
        self.spawned, self.get_timeouts = [], []

    def spawn(self, payload):
        self.spawned.append(payload)
        if self.spawn_raises is not None:
            raise self.spawn_raises
        return FakeCall(self, payload)


def error_class(name, base=Exception, module="nodus.errors._codes"):
    """A stand-in exception class. By default it claims to live in the SDK's error module, like the real ones."""
    return type(name, (base,), {"__module__": module})


class PayloadAndSummaryTests(NoSockets):
    def test_the_payload_holds_only_the_minimum_sections_and_no_identity_or_photo_fields(self):
        record = make_record()
        payload = minimal_payload(validate_record(record).record)
        self.assertEqual(tuple(payload), PAYLOAD_KEYS)
        text = json.dumps(payload)
        for secret_field in (record["rover_id"], "photo_id", "leaf_label", "confidence", "versions", "firmware",
                             "sequence_number", record["time"]):
            self.assertNotIn(str(secret_field), text)

    def test_the_payload_is_a_copy(self):
        valid = validate_record(make_record()).record
        payload = minimal_payload(valid)
        payload["soil"]["moisture"] = -1
        self.assertNotEqual(valid["soil"]["moisture"], -1)

    def test_the_expected_summary_counts_missing_readings_and_faulty_sensors(self):
        payload = minimal_payload(validate_record(make_record(fault=True)).record)
        summary = expected_summary(payload)
        self.assertEqual(tuple(summary), SUMMARY_KEYS)
        self.assertEqual(summary["readings_present"] + summary["readings_missing"], 9)
        self.assertEqual((summary["readings_missing"], summary["sensors_fault"], summary["faulty_sensors"]),
                         (1, 1, ["moisture"]))
        self.assertEqual(summary["sensors_ok"], 4)
        self.assertTrue(validate_summary(summary, payload))

    def test_validation_rejects_the_wrong_shape_wrong_types_and_inconsistent_numbers(self):
        payload = minimal_payload(validate_record(make_record()).record)
        good = expected_summary(payload)
        self.assertTrue(validate_summary(good, payload))
        bad = [None, [], "text", 5, {}, dict(good, extra=1), {k: v for k, v in good.items() if k != "sensors_ok"},
               dict(good, readings_present=True), dict(good, readings_present=-1), dict(good, readings_present=2.0),
               dict(good, readings_missing=good["readings_missing"] + 1),        # totals no longer match
               dict(good, sensors_ok=99), dict(good, sensors_fault=1),           # claims a fault that is not listed
               dict(good, faulty_sensors=["not_a_sensor"], sensors_fault=1),
               dict(good, faulty_sensors="moisture"), dict(good, faulty_sensors=[1]),
               dict(good, faulty_sensors=["air", "air"], sensors_fault=2),
               dict(good, readings_present=10 ** 9)]
        for output in bad:
            with self.subTest(output=str(output)[:50]):
                self.assertFalse(validate_summary(output, payload))

    def test_a_huge_list_is_rejected_before_anything_is_stored(self):
        payload = minimal_payload(validate_record(make_record()).record)
        output = dict(expected_summary(payload), faulty_sensors=["x"] * 1_000_000)
        self.assertFalse(validate_summary(output, payload))


class FakeRunnerChainTests(NoSockets):
    def test_the_chain_validates_then_summarises_in_order_with_the_fake(self):
        runner = FakeTaskRunner()
        run = run_record_summary(make_record(), runner, run_id="run-1")
        self.assertEqual([e["name"] for e in run["steps"]], ["prepare_payload", "summarize_record"])
        self.assertEqual((run["status"], [e["status"] for e in run["steps"]]), ("completed", ["completed"] * 2))
        self.assertEqual(steps_of(run)["summarize_record"]["depends_on"], ["prepare_payload"])
        result = steps_of(run)["summarize_record"]["result"]
        self.assertEqual((result["task"], result["fake"]), (TASK_SUMMARIZE_RECORD, True))
        self.assertIn("fake", result["runner"].lower())
        self.assertEqual(tuple(result["summary"]), SUMMARY_KEYS)
        self.assertEqual(len(runner.calls), 1)
        task, payload = runner.calls[0]
        self.assertEqual((task, tuple(payload)), (TASK_SUMMARIZE_RECORD, PAYLOAD_KEYS))

    def test_the_payload_given_to_the_runner_is_the_one_the_first_step_stored(self):
        runner = FakeTaskRunner()
        run = run_record_summary(make_record(), runner, run_id="run-1")
        self.assertEqual(runner.calls[0][1], steps_of(run)["prepare_payload"]["result"])

    def test_an_invalid_record_runs_nothing_and_never_reaches_the_runner(self):
        runner = FakeTaskRunner()
        record = make_record()
        del record["soil"]
        run = run_record_summary(record, runner, run_id="run-1")
        self.assertEqual((run["status"], run["steps"]), ("invalid_record", []))
        self.assertEqual(runner.calls, [])

    def test_a_failed_first_step_skips_the_runner_step(self):
        runner = FakeTaskRunner()

        def broken(context):
            raise StepFailure("prepare_failed")

        steps = [Step("prepare_payload", "local", broken),
                 task_step("summarize_record", runner, TASK_SUMMARIZE_RECORD, "prepare_payload")]
        run = execute_run(make_record(), steps, run_id="run-1")
        by = steps_of(run)
        self.assertEqual((by["prepare_payload"]["status"], by["summarize_record"]["status"],
                          by["summarize_record"]["skip_reason"]), ("failed", "skipped", "dependency_not_completed"))
        self.assertEqual(runner.calls, [])

    def test_each_fake_failure_mode_gives_a_closed_code_and_stores_nothing(self):
        expected = {"fail": "task_failed", "timeout": "task_timeout", "malformed": "task_response_malformed",
                    "wrong_counts": "task_response_malformed", "rejected": "task_rejected",
                    "limit": "task_limit_reached"}
        for mode, code in expected.items():
            with self.subTest(mode=mode):
                run = run_record_summary(make_record(), FakeTaskRunner(mode), run_id="run-1")
                step = steps_of(run)["summarize_record"]
                self.assertEqual((run["status"], step["status"], step["error_code"], step["result"]),
                                 ("failed", "failed", code, None))
                self.assertIn(code, ERROR_CODES)

    def test_a_runner_that_raises_leaks_nothing(self):
        run = run_record_summary(make_record(), FakeTaskRunner("raise"), run_id="run-1")
        step = steps_of(run)["summarize_record"]
        self.assertEqual((step["status"], step["error_code"]), ("failed", "step_failed"))
        self.assertNotIn("exploded", json.dumps(run))

    def test_something_that_is_not_a_task_result_is_malformed(self):
        class Odd:
            def run(self, task, payload):
                return {"status": "ok", "output": {}}

        run = run_record_summary(make_record(), Odd(), run_id="run-1")
        self.assertEqual(steps_of(run)["summarize_record"]["error_code"], "task_response_malformed")

    def test_an_unknown_error_code_from_a_runner_becomes_task_failed(self):
        class Odd:
            def run(self, task, payload):
                return TaskResult("error", "totally_made_up " + CANARY, None, "odd", False)

        run = run_record_summary(make_record(), Odd(), run_id="run-1")
        self.assertEqual(steps_of(run)["summarize_record"]["error_code"], "task_failed")
        self.assertNotIn(CANARY, json.dumps(run))

    def test_a_wrong_task_name_is_rejected_by_the_fake(self):
        result = FakeTaskRunner().run("something_else", {})
        self.assertEqual((result.status, result.error_code, result.output), ("error", "task_rejected", None))

    def test_the_chain_refuses_an_object_that_is_not_a_runner(self):
        with self.assertRaises(TypeError):
            run_record_summary(make_record(), object())

    def test_the_fake_rejects_an_unknown_mode(self):
        with self.assertRaises(ValueError):
            FakeTaskRunner("explode")

    def test_the_two_size_limits_still_apply_to_a_runner_step(self):
        payload_size = None
        run = run_record_summary(make_record(), FakeTaskRunner(), run_id="run-1")
        payload_size = len(json.dumps(steps_of(run)["prepare_payload"]["result"]))
        with mock.patch.object(run_module, "MAX_STEP_RESULT_BYTES", payload_size - 1):
            run = run_record_summary(make_record(), FakeTaskRunner(), run_id="run-1")
        self.assertEqual(steps_of(run)["prepare_payload"]["error_code"], "result_too_large")
        self.assertEqual(steps_of(run)["summarize_record"]["status"], "skipped")
        self.assertEqual(run_module.MAX_STEP_RESULT_BYTES, 4 * 1024 * 1024)
        self.assertEqual(run_module.MAX_RUN_RESULT_BYTES, 8 * 1024 * 1024)


class NodusAdapterTests(NoSockets):
    def runner(self, handle, **kwargs):
        return NodusTaskRunner(lambda: handle, **kwargs)

    def test_a_successful_call_goes_through_spawn_and_get_with_the_timeout(self):
        handle = FakeHandle()
        run = run_record_summary(make_record(), self.runner(handle, timeout_seconds=12), run_id="run-1")
        step = steps_of(run)["summarize_record"]
        self.assertEqual((run["status"], step["status"]), ("completed", "completed"))
        self.assertEqual(step["result"]["fake"], False)
        self.assertEqual(step["result"]["runner"], NodusTaskRunner.LABEL)
        self.assertEqual(tuple(handle.spawned[0]), PAYLOAD_KEYS)
        self.assertEqual(handle.get_timeouts, [12])

    def test_the_handle_factory_is_called_once_per_task_and_not_at_construction(self):
        calls = []
        runner = NodusTaskRunner(lambda: calls.append(1) or FakeHandle())
        self.assertEqual(calls, [])
        run_record_summary(make_record(), runner, run_id="run-1")
        self.assertEqual(calls, [1])

    def test_documented_service_error_names_map_to_closed_codes(self):
        cases = {"InsufficientCredits": "task_limit_reached", "QuotaExceeded": "task_limit_reached",
                 "NotFound": "task_rejected", "Invalid": "task_rejected", "RemoteError": "task_failed",
                 "NodusError": "task_failed", "SomethingElse": "task_failed"}   # more names: the hardening tests
        for name, code in cases.items():
            with self.subTest(error=name):
                handle = FakeHandle(raises=error_class(name)("detail " + CANARY))
                run = run_record_summary(make_record(), self.runner(handle), run_id="run-1")
                step = steps_of(run)["summarize_record"]
                self.assertEqual((step["status"], step["error_code"], step["result"]), ("failed", code, None))
                self.assertNotIn(CANARY, json.dumps(run))
                self.assertNotIn("detail", json.dumps(run))

    def test_a_subclass_of_a_documented_error_maps_like_its_parent(self):
        child = error_class("BillingTrouble", error_class("InsufficientCredits"))
        handle = FakeHandle(raises=child("x"))
        run = run_record_summary(make_record(), self.runner(handle), run_id="run-1")
        self.assertEqual(steps_of(run)["summarize_record"]["error_code"], "task_limit_reached")

    def test_a_timeout_error_is_reported_as_a_timeout_by_a_generic_rule_not_a_documented_one(self):
        handle = FakeHandle(raises=TimeoutError("slow " + CANARY))
        run = run_record_summary(make_record(), self.runner(handle), run_id="run-1")
        self.assertEqual(steps_of(run)["summarize_record"]["error_code"], "task_timeout")
        self.assertNotIn(CANARY, json.dumps(run))

    def test_an_error_while_starting_the_call_is_reported_the_same_way(self):
        handle = FakeHandle(spawn_raises=error_class("Invalid")("bad " + CANARY))
        run = run_record_summary(make_record(), self.runner(handle), run_id="run-1")
        self.assertEqual(steps_of(run)["summarize_record"]["error_code"], "task_rejected")
        self.assertNotIn(CANARY, json.dumps(run))

    def test_a_missing_or_unusable_handle_is_reported_as_unavailable(self):
        def broken_factory():
            raise ImportError("no module named nodus " + CANARY)

        for factory in (broken_factory, lambda: None, lambda: object()):
            with self.subTest(factory=repr(factory)[:30]):
                run = run_record_summary(make_record(), NodusTaskRunner(factory), run_id="run-1")
                step = steps_of(run)["summarize_record"]
                self.assertEqual((step["status"], step["error_code"]), ("failed", "task_runner_unavailable"))
                self.assertNotIn(CANARY, json.dumps(run))

    def test_malformed_or_inconsistent_service_output_fails_safely(self):
        payload = minimal_payload(validate_record(make_record()).record)
        good = expected_summary(payload)
        outputs = ["a string", [1, 2], {"unexpected": 1}, dict(good, readings_present=True),
                   dict(good, faulty_sensors=["x"] * 100_000), {1, 2}, object()]
        for output in outputs:
            with self.subTest(output=str(output)[:40]):
                run = run_record_summary(make_record(), self.runner(FakeHandle(output=output)), run_id="run-1")
                step = steps_of(run)["summarize_record"]
                self.assertEqual((step["status"], step["error_code"], step["result"]),
                                 ("failed", "task_response_malformed", None))

    def test_the_wrong_task_name_is_rejected_without_calling_the_service(self):
        handle = FakeHandle()
        result = self.runner(handle).run("other_task", {})
        self.assertEqual((result.status, result.error_code), ("error", "task_rejected"))
        self.assertEqual(handle.spawned, [])

    def test_construction_checks_its_arguments_without_echoing_them(self):
        for factory in (None, 5, "nodus"):
            with self.assertRaises(TypeError):
                NodusTaskRunner(factory)
        for timeout in (0, -1, True, "5", None, float("nan"), 10 ** 9):
            with self.subTest(timeout=repr(timeout)):
                with self.assertRaises(ValueError) as raised:
                    NodusTaskRunner(lambda: None, timeout_seconds=timeout)
                self.assertNotIn(repr(timeout) + "x", str(raised.exception))

    def test_the_adapter_never_imports_the_sdk_and_loads_without_it(self):
        source = Path(nodus_adapter.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                self.assertNotEqual(name.split(".")[0], "nodus", name)
        if importlib.util.find_spec("nodus") is None:
            self.assertNotIn("nodus", sys.modules)

    def test_the_adapter_reads_no_environment_and_opens_no_connection(self):
        banned_imports = {"os", "socket", "ssl", "urllib", "http", "subprocess", "requests", "asyncio", "threading"}
        banned_names = {"environ", "getenv", "open", "system", "popen"}
        for module in (nodus_adapter, task_runner):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], banned_imports, module.__name__)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module.split(".")[0], banned_imports, module.__name__)
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, banned_names, module.__name__)
                elif isinstance(node, ast.Name):
                    self.assertNotIn(node.id, banned_names, module.__name__)

    def test_the_documented_facts_are_recorded_as_names_and_ranges_only(self):
        self.assertEqual(nodus_adapter.SDK_DISTRIBUTION, "nodus-compute")
        self.assertEqual(nodus_adapter.SDK_IMPORT_NAME, "nodus")
        self.assertTrue(all(n.startswith("NODUS_") for n in nodus_adapter.AUTH_ENV_VAR_NAMES))
        self.assertIn("NODUS_API_KEY", nodus_adapter.AUTH_ENV_VAR_NAMES)
        for name in nodus_adapter.AUTH_ENV_VAR_NAMES:
            self.assertNotIn("=", name)
        self.assertIsInstance(nodus_adapter.sdk_installed(), bool)

    def test_python_support_follows_the_documented_range_and_excludes_this_runtime_if_newer(self):
        check = nodus_adapter.python_is_documented
        for version, expected in (((3, 9, 18), False), ((3, 10, 0), True), ((3, 12, 5), True), ((3, 13, 9), True),
                                  ((3, 14, 0), False), ((3, 14, 6), False)):
            with self.subTest(version=version):
                self.assertEqual(check(version), expected)
        self.assertEqual(check(), check(tuple(sys.version_info[:3])))
        if sys.version_info[:2] >= (3, 14):
            self.assertFalse(check())     # on this machine's Python the SDK is outside its documented range


def two_fault_record():
    rover = MockRover(RoverConfig())
    rover.startup()
    rover.insert_probe()
    rover.inject_fault("moisture")
    rover.inject_fault("npk")
    rover.take_measurement()
    return rover.build_record(use_capture=False)


class HardeningTests(NoSockets):
    """N-2: defects found by reading the N-1 code, each with a test that failed before the fix."""

    def payload_and_good(self, record=None):
        payload = minimal_payload(validate_record(record or make_record()).record)
        return payload, expected_summary(payload)

    # ---- the summary must equal what the payload implies, not merely look consistent
    def test_a_healthy_sensor_named_faulty_is_rejected(self):
        payload, good = self.payload_and_good()
        lie = dict(good, sensors_fault=1, faulty_sensors=["air"], sensors_ok=good["sensors_ok"] - 1)
        self.assertFalse(validate_summary(lie, payload))

    def test_a_wrong_but_self_consistent_ok_count_is_rejected(self):
        payload, good = self.payload_and_good()
        self.assertFalse(validate_summary(dict(good, sensors_ok=good["sensors_ok"] - 1), payload))

    def test_wrong_present_and_missing_counts_that_still_add_up_are_rejected(self):
        payload, good = self.payload_and_good()
        shifted = dict(good, readings_present=good["readings_present"] - 1, readings_missing=1)
        self.assertFalse(validate_summary(shifted, payload))

    def test_a_missed_fault_is_rejected(self):
        payload, good = self.payload_and_good(make_record(fault=True))
        hidden = dict(good, sensors_fault=0, faulty_sensors=[], sensors_ok=good["sensors_ok"])
        self.assertFalse(validate_summary(hidden, payload))

    def test_the_faulty_list_must_be_in_the_documented_sorted_order(self):
        payload, good = self.payload_and_good(two_fault_record())
        self.assertEqual(good["faulty_sensors"], ["moisture", "npk"])
        self.assertTrue(validate_summary(good, payload))
        self.assertFalse(validate_summary(dict(good, faulty_sensors=["npk", "moisture"]), payload))

    def test_the_exact_expected_summary_is_still_accepted_for_every_scenario_shape(self):
        for record in (make_record(), make_record(fault=True), two_fault_record()):
            payload, good = self.payload_and_good(record)
            self.assertTrue(validate_summary(good, payload))

    def test_a_boolean_or_float_is_not_accepted_as_a_count_even_when_it_equals_the_number(self):
        payload, good = self.payload_and_good(make_record(fault=True))
        self.assertFalse(validate_summary(dict(good, sensors_fault=True), payload))     # True == 1
        self.assertFalse(validate_summary(dict(good, sensors_ok=float(good["sensors_ok"])), payload))

    def test_validation_never_raises_on_a_hostile_output_object(self):
        class Hostile(dict):
            def __iter__(self):
                raise RuntimeError("secret detail " + CANARY)

        payload, good = self.payload_and_good()
        self.assertFalse(validate_summary(Hostile(good), payload))
        run = run_record_summary(make_record(), self.runner_returning(Hostile(good)), run_id="run-1")
        step = steps_of(run)["summarize_record"]
        self.assertEqual((step["status"], step["error_code"]), ("failed", "task_response_malformed"))
        self.assertNotIn(CANARY, json.dumps(run))

    def runner_returning(self, output, label="odd-runner", fake=False):
        class Returning:
            def run(self, task, payload):
                return TaskResult("ok", None, output, label, fake)
        return Returning()

    # ---- the answer is checked against the payload the FIRST step produced, not one the runner could edit
    def test_a_runner_that_edits_its_payload_cannot_make_a_wrong_answer_pass(self):
        class Tampering:
            def run(self, task, payload):
                payload["sensor_status"]["npk"] = "fault"
                return TaskResult("ok", None, expected_summary(payload), "tamper", False)

        run = run_record_summary(make_record(), Tampering(), run_id="run-1")
        step = steps_of(run)["summarize_record"]
        self.assertEqual((step["status"], step["error_code"]), ("failed", "task_response_malformed"))
        self.assertEqual(steps_of(run)["prepare_payload"]["result"]["sensor_status"]["npk"], "ok")

    def test_the_stored_first_step_result_is_not_changed_by_the_runner(self):
        class Tampering:
            def run(self, task, payload):
                payload["soil"].clear()
                return error_for_test()

        def error_for_test():
            return TaskResult("error", "task_failed", None, "tamper", False)

        run = run_record_summary(make_record(), Tampering(), run_id="run-1")
        self.assertTrue(steps_of(run)["prepare_payload"]["result"]["soil"])

    # ---- the runner's label is stored, so it must be short, printable text or nothing
    def test_an_unsafe_runner_label_is_not_stored(self):
        payload, good = self.payload_and_good()
        for label in ("x" * 1000, "line\nbreak", "tab\there", "", "   ", None, 5, "bell\a"):
            with self.subTest(label=repr(label)[:20]):
                run = run_record_summary(make_record(), self.runner_returning(good, label=label), run_id="run-1")
                step = steps_of(run)["summarize_record"]
                self.assertEqual(step["status"], "completed")
                self.assertIsNone(step["result"]["runner"])
        run = run_record_summary(make_record(), self.runner_returning(good, label="a fine label"), run_id="run-1")
        self.assertEqual(steps_of(run)["summarize_record"]["result"]["runner"], "a fine label")

    def test_the_fake_flag_is_true_only_when_the_runner_says_exactly_true(self):
        payload, good = self.payload_and_good()
        for flag, expected in ((True, True), (False, False), (1, False), ("yes", False), (None, False)):
            run = run_record_summary(make_record(), self.runner_returning(good, fake=flag), run_id="run-1")
            self.assertIs(steps_of(run)["summarize_record"]["result"]["fake"], expected)

    # ---- construction arguments fail with ValueError and never echo the value
    def test_absurd_timeouts_are_a_value_error_not_an_overflow(self):
        for timeout in (10 ** 400, -(10 ** 400), 1e308, float("inf"), float("-inf"), float("nan"), 0, -0.0, 3601):
            with self.subTest(timeout=repr(timeout)[:20]):
                with self.assertRaises(ValueError) as raised:
                    NodusTaskRunner(lambda: None, timeout_seconds=timeout)
                self.assertNotIn("1000000", str(raised.exception))
        for timeout in (0.001, 1, 59.5, 3600):
            NodusTaskRunner(lambda: None, timeout_seconds=timeout)

    def test_a_secret_like_argument_is_never_echoed_by_a_construction_error(self):
        for bad in (CANARY, [CANARY], {"k": CANARY}, object()):
            for kwargs, factory in (({}, bad), ({"timeout_seconds": bad}, lambda: None)):
                try:
                    NodusTaskRunner(factory, **kwargs)
                except (TypeError, ValueError) as error:
                    self.assertNotIn(CANARY, str(error))
                else:
                    self.fail("construction should have been refused")

    def test_the_fake_runner_refuses_a_bad_mode_without_echoing_it(self):
        with self.assertRaises(ValueError) as raised:
            FakeTaskRunner("mode-" + CANARY)
        self.assertNotIn(CANARY, str(raised.exception))
        with self.assertRaises(TypeError) as raised:
            run_record_summary(make_record(), CANARY)
        self.assertNotIn(CANARY, str(raised.exception))

    # ---- invalid Records never reach the runner
    def test_every_kind_of_invalid_record_leaves_the_runner_untouched(self):
        bad_nested = make_record()
        bad_nested["soil"]["extra_reading"] = 1.0                       # an unknown nested field
        not_finite = make_record()
        not_finite["soil"]["moisture"] = float("nan")
        wrong_type = make_record()
        wrong_type["air"]["humidity"] = "wet"
        for record in (None, [], "text", 5, {}, bad_nested, not_finite, wrong_type):
            with self.subTest(record=repr(record)[:30]):
                runner = FakeTaskRunner()
                run = run_record_summary(record, runner, run_id="run-1")
                self.assertEqual((run["status"], run["steps"]), ("invalid_record", []))
                self.assertEqual(runner.calls, [])

    def test_the_payload_sections_hold_exactly_the_approved_keys(self):
        from record.validation import AIR_KEYS, BATTERY_KEYS, SOIL_KEYS, STATUS_KEYS
        payload = minimal_payload(validate_record(make_record()).record)
        self.assertEqual(set(payload["soil"]), set(SOIL_KEYS))
        self.assertEqual(set(payload["air"]), set(AIR_KEYS))
        self.assertEqual(set(payload["battery"]), set(BATTERY_KEYS))
        self.assertEqual(set(payload["sensor_status"]), set(STATUS_KEYS))

    # ---- dependency behaviour
    def test_a_skipped_first_step_skips_the_runner_step_too(self):
        runner = FakeTaskRunner()
        steps = [Step("prepare_payload", "local", lambda context: None, skip_reason="not_wanted"),
                 task_step("summarize_record", runner, TASK_SUMMARIZE_RECORD, "prepare_payload")]
        run = execute_run(make_record(), steps, run_id="run-1")
        by = steps_of(run)
        self.assertEqual((by["prepare_payload"]["status"], by["summarize_record"]["status"],
                          by["summarize_record"]["skip_reason"], run["status"]),
                         ("skipped", "skipped", "dependency_not_completed", "skipped"))
        self.assertEqual(runner.calls, [])

    def test_an_unexpected_error_in_the_first_step_leaks_nothing_and_skips_the_runner(self):
        runner = FakeTaskRunner()
        with mock.patch.object(task_runner, "minimal_payload", side_effect=RuntimeError("path C:/secret " + CANARY)):
            run = run_record_summary(make_record(), runner, run_id="run-1")
        by = steps_of(run)
        self.assertEqual((by["prepare_payload"]["error_code"], by["summarize_record"]["status"]),
                         ("step_failed", "skipped"))
        self.assertNotIn(CANARY, json.dumps(run))
        self.assertNotIn("secret", json.dumps(run))
        self.assertEqual(runner.calls, [])

    # ---- size limits through this workflow
    def test_the_per_run_total_applies_to_the_second_step(self):
        run = run_record_summary(make_record(), FakeTaskRunner(), run_id="run-1")
        first = len(json.dumps(steps_of(run)["prepare_payload"]["result"]))
        with mock.patch.object(run_module, "MAX_RUN_RESULT_BYTES", first):          # exactly the first step's size
            run = run_record_summary(make_record(), FakeTaskRunner(), run_id="run-1")
        by = steps_of(run)
        self.assertEqual((by["prepare_payload"]["status"], by["summarize_record"]["status"],
                          by["summarize_record"]["error_code"], by["summarize_record"]["result"]),
                         ("completed", "failed", "result_too_large", None))
        self.assertEqual(run["status"], "failed")

    def test_a_huge_label_or_answer_cannot_reach_the_size_limit_because_it_is_bounded_first(self):
        payload, good = self.payload_and_good()
        run = run_record_summary(make_record(), self.runner_returning(good, label="x" * (5 * 1024 * 1024)),
                                 run_id="run-1")
        step = steps_of(run)["summarize_record"]
        self.assertEqual(step["status"], "completed")
        self.assertLess(len(json.dumps(run)), 20000)
        self.assertIsNone(step["result"]["runner"])

    # ---- determinism
    def test_the_same_inputs_give_identical_runs_with_the_fake_and_with_the_mocked_handle(self):
        def clock_factory():
            ticks = iter("t%02d" % n for n in range(100))
            return lambda: next(ticks)

        record = make_record(fault=True)
        for make_runner in (lambda: FakeTaskRunner(), lambda: NodusTaskRunner(lambda: FakeHandle())):
            first = run_record_summary(record, make_runner(), run_id="run-1", clock=clock_factory())
            second = run_record_summary(record, make_runner(), run_id="run-1", clock=clock_factory())
            self.assertEqual(first, second)

    # ---- the adapter and the workflow stay out of the demo and the other modules
    def test_no_production_module_imports_the_new_modules(self):
        root = Path(run_module.__file__).resolve().parents[1]
        own = {"task_runner.py", "nodus_adapter.py", "integrated.py"}   # integrated.py is the one intended composer
        for path in root.rglob("*.py"):
            parts = path.relative_to(root).parts
            if parts[0] in ("tests", ".checkpoints", ".browser_evidence") or path.name in own:
                continue
            text = path.read_text(encoding="utf-8")
            if parts != ("demo_app", "service.py"):   # IW-3: only the demo service uses the fake runner, nothing else
                self.assertNotIn("task_runner", text, str(path))
            if parts != ("scripts", "run_demo.py"):   # IW-4: the launcher is the one place that builds the real adapter
                self.assertNotIn("nodus_adapter", text, str(path))


REMOTE_ERROR = error_class("RemoteError", module="nodus.errors._base")


def remote_chained(error):
    """What the SDK does for an exception raised INSIDE the remote function: re-raise it chained from RemoteError."""
    try:
        try:
            raise REMOTE_ERROR("remote traceback " + CANARY)
        except Exception as remote:
            raise error from remote
    except Exception as raised:
        return raised


class AdapterErrorMappingTests(NoSockets):
    """N-3: the adapter's mapping, written against the installed SDK 1.0.23 source (see test_nodus_sdk_contract.py,
    which checks the same names against the real classes when the SDK is importable)."""

    def run_with(self, handle):
        runner = NodusTaskRunner(lambda: handle, timeout_seconds=5)
        run = run_record_summary(make_record(), runner, run_id="run-1")
        return runner, run, steps_of(run)["summarize_record"]

    def code_for(self, error, where="get"):
        handle = FakeHandle(spawn_raises=error) if where == "spawn" else FakeHandle(raises=error)
        _, run, step = self.run_with(handle)
        self.assertNotIn(CANARY, json.dumps(run))
        self.assertNotIn("traceback", json.dumps(run))
        return step["error_code"]

    LIMIT = ("InsufficientCredits", "BudgetExceeded", "QuotaExceeded", "PaymentMethodRequired", "ArrearsOutstanding",
             "PaymentDisputed", "TrialLimitExceeded")
    UNAVAILABLE = ("TooManyRequests", "Unavailable", "CapacityUnavailable", "AdmissionsPaused", "InternalError",
                   "PaymentVerificationUnavailable", "PaymentsNotConfigured", "ComputeSuspended", "RequestInProgress",
                   "APIConnectionError")
    REJECTED = ("NotFound", "FunctionNotFound", "Invalid", "BadRequest", "Unsupported", "UnsupportedMediaType",
                "Unauthorized", "Forbidden", "RequestEntityTooLarge", "MethodNotAllowed")

    def test_budget_payment_and_arrears_errors_map_to_the_limit_code(self):
        for name in self.LIMIT:
            with self.subTest(name=name):
                self.assertEqual(self.code_for(error_class(name)("m " + CANARY)), "task_limit_reached")
                self.assertEqual(self.code_for(error_class(name)("m " + CANARY), "spawn"), "task_limit_reached")

    def test_throttling_unavailable_capacity_and_connection_errors_map_to_the_unavailable_code(self):
        for name in self.UNAVAILABLE:
            with self.subTest(name=name):
                self.assertEqual(self.code_for(error_class(name)("m " + CANARY)), "task_runner_unavailable")

    def test_refusals_map_to_the_rejected_code(self):
        for name in self.REJECTED:
            with self.subTest(name=name):
                self.assertEqual(self.code_for(error_class(name)("m " + CANARY)), "task_rejected")

    def test_a_client_side_api_timeout_is_a_timeout_not_a_connection_error_although_it_is_one_by_inheritance(self):
        connection = error_class("APIConnectionError")
        timeout = error_class("APITimeoutError", connection, "nodus.errors._base")
        self.assertEqual(self.code_for(timeout("t " + CANARY)), "task_timeout")
        self.assertEqual(self.code_for(timeout("t"), "spawn"), "task_timeout")

    def test_a_function_call_that_hit_its_remote_deadline_is_a_timeout_and_other_endings_are_failures(self):
        failed = error_class("FunctionCallFailed", error_class("NodusError", module="nodus.errors._base"),
                             "nodus.errors._base")
        for reason, code in (("DeadlineExceeded", "task_timeout"), ("OutOfMemory", "task_failed"),
                             (None, "task_failed"), (5, "task_failed"), (["DeadlineExceeded"], "task_failed")):
            with self.subTest(reason=repr(reason)):
                error = failed("call")
                error.reason = reason
                self.assertEqual(self.code_for(error), code)

    def test_unknown_sdk_errors_and_ordinary_exceptions_are_a_generic_failure(self):
        for error in (error_class("BrandNewFutureError")("x " + CANARY), RuntimeError("y " + CANARY),
                      ValueError("z"), KeyError("k"), OSError("o")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.code_for(error), "task_failed")

    def test_a_class_that_only_borrows_an_sdk_name_is_not_trusted(self):
        for name in self.LIMIT + self.UNAVAILABLE + self.REJECTED + ("APITimeoutError",):
            with self.subTest(name=name):
                spoof = error_class(name, module="my_remote_function")
                self.assertEqual(self.code_for(spoof("x " + CANARY)), "task_failed")

    # ---- timeouts: a local wait timeout is a timeout; the function's own TimeoutError is not
    def test_a_local_wait_timeout_is_a_timeout(self):
        self.assertEqual(self.code_for(TimeoutError("waited " + CANARY)), "task_timeout")

    def test_the_python_310_asyncio_timeout_is_recognised_without_inheriting_from_the_builtin(self):
        old_style = type("TimeoutError", (Exception,), {"__module__": "asyncio.exceptions"})
        self.assertFalse(issubclass(old_style, TimeoutError))
        self.assertEqual(self.code_for(old_style()), "task_timeout")

    def test_a_timeout_error_raised_inside_the_remote_function_is_a_failure_not_a_timeout(self):
        error = remote_chained(TimeoutError("inside the function " + CANARY))
        self.assertIsInstance(error, TimeoutError)
        handle = FakeHandle(raises=error)
        runner, run, step = self.run_with(handle)
        self.assertEqual(step["error_code"], "task_failed")
        self.assertIsNone(runner.last_cancel)                       # a finished call is not cancelled
        self.assertNotIn(CANARY, json.dumps(run))

    def test_any_remote_exception_is_a_failure_even_when_it_borrows_an_sdk_error_name(self):
        for error in (remote_chained(error_class("InsufficientCredits")("m")),
                      remote_chained(error_class("Unavailable")("m")), remote_chained(ValueError("m"))):
            self.assertEqual(self.code_for(error), "task_failed")

    def test_a_user_defined_class_named_timeout_error_is_not_a_timeout(self):
        impostor = type("TimeoutError", (Exception,), {"__module__": "my_remote_function"})
        self.assertEqual(self.code_for(impostor("x")), "task_failed")

    # ---- cancellation is best effort and never changes the outcome
    def test_a_local_wait_timeout_asks_the_call_to_cancel_once(self):
        cancelled = []

        class Call(FakeCall):
            def cancel(self, *args, **kwargs):
                cancelled.append((args, kwargs))

        class Handle(FakeHandle):
            def spawn(self, payload):
                self.spawned.append(payload)
                return Call(self, payload)

        runner, _, step = self.run_with(Handle(raises=TimeoutError("slow")))
        self.assertEqual((step["status"], step["error_code"]), ("failed", "task_timeout"))
        self.assertEqual(cancelled, [((), {})])
        self.assertEqual(runner.last_cancel, "requested")

    def test_a_failing_cancel_keeps_the_original_timeout_outcome_and_leaks_nothing(self):
        class Call(FakeCall):
            def cancel(self):
                raise RuntimeError("cancel exploded " + CANARY)

        class Handle(FakeHandle):
            def spawn(self, payload):
                return Call(self, payload)

        runner, run, step = self.run_with(Handle(raises=TimeoutError("slow")))
        self.assertEqual(step["error_code"], "task_timeout")
        self.assertEqual(runner.last_cancel, "failed")
        self.assertNotIn(CANARY, json.dumps(run))
        self.assertNotIn("exploded", json.dumps(run))

    def test_a_call_without_cancel_is_still_a_timeout_and_the_adapter_says_cancel_was_unsupported(self):
        runner, _, step = self.run_with(FakeHandle(raises=TimeoutError("slow")))
        self.assertEqual((step["error_code"], runner.last_cancel), ("task_timeout", "unsupported"))

    def test_cancel_is_not_attempted_for_success_other_failures_or_a_timeout_before_a_call_exists(self):
        cancels = []

        class Call(FakeCall):
            def cancel(self):
                cancels.append(1)

        class Handle(FakeHandle):
            def spawn(self, payload):
                if self.spawn_raises is not None:
                    raise self.spawn_raises
                return Call(self, payload)

        for handle in (Handle(), Handle(raises=RuntimeError("x")), Handle(raises=error_class("Invalid")("x")),
                       Handle(spawn_raises=TimeoutError("spawn timed out")),
                       Handle(raises=remote_chained(TimeoutError("inside")))):
            runner, _, _ = self.run_with(handle)
            self.assertIsNone(runner.last_cancel)
        self.assertEqual(cancels, [])

    def test_cancel_state_is_reset_for_each_task(self):
        runner = NodusTaskRunner(lambda: FakeHandle(raises=TimeoutError("slow")))
        runner.run(task_runner.TASK_SUMMARIZE_RECORD, {})
        self.assertEqual(runner.last_cancel, "unsupported")
        runner = NodusTaskRunner(lambda: FakeHandle())
        runner.last_cancel = "requested"
        runner.run(task_runner.TASK_SUMMARIZE_RECORD, {"sensor_status": {}, "soil": {}, "air": {}, "battery": {}})
        self.assertIsNone(runner.last_cancel)

    def test_cancel_makes_no_claim_about_remote_execution_or_billing(self):
        text = Path(nodus_adapter.__file__).read_text(encoding="utf-8")
        self.assertIn("best-effort", text)
        self.assertIn("does not show that remote execution or billing stopped", text)

    # ---- spawn timing is not bounded by the wait timeout
    def test_the_wait_timeout_does_not_bound_the_time_spawn_may_take(self):
        virtual_seconds = []

        class SlowSpawnHandle(FakeHandle):
            def spawn(self, payload):
                virtual_seconds.append(400)        # the SDK's transport may retry for up to 300 s before returning
                return FakeCall(self, payload)

        runner = NodusTaskRunner(lambda: SlowSpawnHandle(), timeout_seconds=5)
        run = run_record_summary(make_record(), runner, run_id="run-1")
        self.assertEqual(steps_of(run)["summarize_record"]["status"], "completed")   # not cut off at 5 seconds
        self.assertGreater(sum(virtual_seconds), 5)

    def test_the_documentation_says_so_and_offers_no_total_time_guarantee(self):
        text = Path(nodus_adapter.__file__).read_text(encoding="utf-8")
        self.assertIn("`timeout_seconds` bounds only the wait for the result", text)
        self.assertIn("`spawn` itself can block", text)
        self.assertIn("no total-time guarantee", text)


class ExistingBehaviourUnchangedTests(unittest.TestCase):
    def test_the_runner_module_does_not_know_the_new_modules_and_keeps_its_api(self):
        source = Path(run_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("task_runner", source)
        self.assertNotIn("nodus", source.lower())
        self.assertEqual(run_module.VERSION, "1")

    def test_the_demo_package_does_not_import_the_new_modules(self):
        demo = Path(run_module.__file__).resolve().parents[1] / "demo_app"
        for path in demo.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.name != "service.py":   # IW-3: the service builds the labelled fake runner; the Nodus adapter stays out
                self.assertNotIn("task_runner", text, path.name)
            self.assertNotIn("nodus_adapter", text, path.name)


if __name__ == "__main__":
    unittest.main()
