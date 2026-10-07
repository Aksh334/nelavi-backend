import copy
import dataclasses
import importlib
import itertools
import unittest
from dataclasses import replace
from unittest import mock

from machine_state.state import (
    FAULT,
    IDLE,
    MAINTENANCE,
    NOT_INFERABLE_FROM_TELEMETRY,
    OFF,
    OVERLOADED,
    RUNNING,
    STARTING,
    STATES,
    UNKNOWN,
    MachineEvidence,
    StateResult,
    evaluate,
    evaluate_telemetry,
    evidence_from_telemetry,
)
from mock_rover.config import RoverConfig
from mock_rover.scenarios import get_scenario, run_scenario
from mock_rover.simulator import MockRover


def state_of(**kwargs):
    return evaluate(MachineEvidence(**kwargs)).state


def make_rover(**overrides):
    return MockRover(replace(RoverConfig(), **overrides))


TRI = (None, True, False)


class TestVocabulary(unittest.TestCase):
    def test_the_state_vocabulary_is_exactly_the_eight_owner_specified_states(self):
        self.assertEqual(
            STATES,
            ("unknown", "off", "starting", "running", "idle", "overloaded", "fault", "maintenance"),
        )

    def test_states_that_cannot_be_inferred_are_documented(self):
        self.assertEqual(set(NOT_INFERABLE_FROM_TELEMETRY), {OVERLOADED, FAULT, MAINTENANCE})
        for reason in NOT_INFERABLE_FROM_TELEMETRY.values():
            self.assertTrue(reason)

    def test_overloaded_is_never_produced(self):
        for started, homed, moving in itertools.product(TRI, TRI, TRI):
            for reasons in ((), ("x",)):
                for declared in (False, True):
                    result = evaluate(
                        MachineEvidence(started, homed, moving, reasons, declared)
                    )
                    self.assertNotEqual(result.state, OVERLOADED)
                    self.assertIn(result.state, STATES)


class TestEachState(unittest.TestCase):
    def test_unknown_when_nothing_is_reported(self):
        result = evaluate(MachineEvidence())
        self.assertEqual(result.state, UNKNOWN)
        self.assertIn("no evidence", result.reason)

    def test_unknown_when_started_but_movement_is_not_reported(self):
        result = evaluate(MachineEvidence(started=True, homed=True))
        self.assertEqual(result.state, UNKNOWN)
        self.assertIn("movement is not reported", result.reason)

    def test_unknown_when_started_and_stationary_but_startup_completion_is_not_reported(self):
        result = evaluate(MachineEvidence(started=True, moving=False))
        self.assertEqual(result.state, UNKNOWN)
        self.assertIn("startup completion is not reported", result.reason)

    def test_unknown_when_evidence_is_contradictory(self):
        result = evaluate(MachineEvidence(started=False, moving=True))
        self.assertEqual(result.state, UNKNOWN)
        self.assertIn("contradictory", result.reason)

    def test_off_when_the_rover_reports_it_is_not_started(self):
        for homed, moving in itertools.product((None, True, False), (None, False)):
            result = evaluate(MachineEvidence(started=False, homed=homed, moving=moving))
            self.assertEqual(result.state, OFF, (homed, moving))
        self.assertIn("not started", result.reason)

    def test_starting_when_started_but_homing_is_not_complete(self):
        result = evaluate(MachineEvidence(started=True, homed=False, moving=False))
        self.assertEqual(result.state, STARTING)
        self.assertIn("homing is not complete", result.reason)

    def test_running_when_moving(self):
        for homed in (None, True):
            result = evaluate(MachineEvidence(started=True, homed=homed, moving=True))
            self.assertEqual(result.state, RUNNING, homed)
        self.assertIn("moving or turning", result.reason)

    def test_idle_when_started_ready_and_stationary(self):
        result = evaluate(MachineEvidence(started=True, homed=True, moving=False))
        self.assertEqual(result.state, IDLE)
        self.assertIn("stationary", result.reason)

    def test_fault_only_from_an_explicit_fault_signal(self):
        result = evaluate(MachineEvidence(started=True, homed=True, moving=True,
                                          fault_reasons=("drive stalled", "relay open")))
        self.assertEqual(result.state, FAULT)
        self.assertIn("drive stalled", result.reason)
        self.assertIn("relay open", result.reason)

    def test_maintenance_only_when_explicitly_declared(self):
        result = evaluate(MachineEvidence(started=True, homed=True, moving=False,
                                          maintenance_declared=True))
        self.assertEqual(result.state, MAINTENANCE)
        self.assertEqual(state_of(started=True, homed=True, moving=False), IDLE)


class TestPrecedence(unittest.TestCase):
    def test_fault_beats_everything(self):
        for kwargs in (
            {"started": False},
            {"started": True, "homed": True, "moving": True},
            {"started": None},
            {"started": True, "homed": True, "moving": False, "maintenance_declared": True},
            {"started": False, "moving": True},
        ):
            self.assertEqual(state_of(fault_reasons=("f",), **kwargs), FAULT, kwargs)

    def test_maintenance_beats_the_operating_states(self):
        for kwargs in (
            {"started": False},
            {"started": None},
            {"started": True, "homed": False},
            {"started": True, "homed": True, "moving": True},
            {"started": True, "homed": True, "moving": False},
            {"started": False, "moving": True},
        ):
            self.assertEqual(state_of(maintenance_declared=True, **kwargs), MAINTENANCE, kwargs)

    def test_starting_beats_running(self):
        self.assertEqual(state_of(started=True, homed=False, moving=True), STARTING)

    def test_not_started_beats_a_stationary_ready_reading(self):
        self.assertEqual(state_of(started=False, homed=True, moving=False), OFF)

    def test_unreported_started_is_unknown_even_if_moving(self):
        self.assertEqual(state_of(started=None, homed=True, moving=True), UNKNOWN)

    def test_every_combination_has_exactly_one_state(self):
        for started, homed, moving in itertools.product(TRI, TRI, TRI):
            for reasons in ((), ("x",)):
                for declared in (False, True):
                    result = evaluate(MachineEvidence(started, homed, moving, reasons, declared))
                    expected_top = FAULT if reasons else MAINTENANCE if declared else None
                    if expected_top:
                        self.assertEqual(result.state, expected_top)
                    self.assertTrue(result.reason)


class TestResultAndTransitions(unittest.TestCase):
    def test_result_has_state_reason_and_evidence(self):
        evidence = MachineEvidence(started=True, homed=True, moving=True)
        result = evaluate(evidence)
        self.assertIsInstance(result, StateResult)
        self.assertEqual((result.state, result.evidence), (RUNNING, evidence))
        self.assertEqual((result.previous_state, result.changed), (None, False))
        self.assertFalse(hasattr(result, "confidence"))

    def test_transition_information(self):
        evidence = MachineEvidence(started=True, homed=True, moving=True)
        changed = evaluate(evidence, previous_state=IDLE)
        self.assertEqual((changed.previous_state, changed.changed), (IDLE, True))
        same = evaluate(evidence, previous_state=RUNNING)
        self.assertEqual((same.previous_state, same.changed), (RUNNING, False))

    def test_no_transition_is_refused(self):
        evidence = MachineEvidence(started=False)
        for previous in STATES:
            self.assertEqual(evaluate(evidence, previous_state=previous).state, OFF)

    def test_invalid_previous_state_is_rejected(self):
        evidence = MachineEvidence()
        for bad in ("flying", "", 5, ["off"]):
            with self.assertRaises(ValueError):
                evaluate(evidence, previous_state=bad)

    def test_result_and_evidence_are_immutable(self):
        result = evaluate(MachineEvidence(started=True, homed=True, moving=False))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.state = "off"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.evidence.started = False


class TestInputValidation(unittest.TestCase):
    def test_tristate_fields_must_be_bool_or_none(self):
        for field in ("started", "homed", "moving"):
            for bad in ("yes", 1, 0, 1.0, [], {}):
                with self.assertRaises(ValueError, msg=(field, bad)):
                    MachineEvidence(**{field: bad})

    def test_maintenance_declared_must_be_a_bool(self):
        for bad in (None, 1, "yes"):
            with self.assertRaises(ValueError):
                MachineEvidence(maintenance_declared=bad)

    def test_fault_reasons_must_be_a_sequence_of_non_empty_strings(self):
        for bad in ("stalled", b"x", None, 5, {"a"}, [""], [1], ["ok", ""]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                MachineEvidence(fault_reasons=bad)

    def test_fault_reasons_list_is_copied_into_a_tuple(self):
        reasons = ["a", "b"]
        evidence = MachineEvidence(fault_reasons=reasons)
        reasons.append("c")
        self.assertEqual(evidence.fault_reasons, ("a", "b"))

    def test_evaluate_requires_machine_evidence(self):
        for bad in (None, {}, {"started": True}):
            with self.assertRaises(TypeError):
                evaluate(bad)


class TestDeterminism(unittest.TestCase):
    def test_repeated_evaluation_is_identical(self):
        for started, homed, moving in itertools.product(TRI, TRI, TRI):
            evidence = MachineEvidence(started, homed, moving)
            self.assertEqual(evaluate(evidence), evaluate(evidence))
            self.assertEqual(evaluate(evidence), evaluate(MachineEvidence(started, homed, moving)))

    def test_evaluation_does_not_use_the_wall_clock(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("the wall clock was used")

        with mock.patch("time.time", forbidden), \
                mock.patch("time.time_ns", forbidden), \
                mock.patch("time.monotonic", forbidden), \
                mock.patch("time.perf_counter", forbidden), \
                mock.patch("time.sleep", forbidden):
            results = [
                evaluate(MachineEvidence(s, h, m))
                for s, h, m in itertools.product(TRI, TRI, TRI)
            ]
        self.assertEqual(
            results, [evaluate(MachineEvidence(s, h, m)) for s, h, m in itertools.product(TRI, TRI, TRI)]
        )

    def test_module_imports_no_clock_random_network_or_storage_modules(self):
        module = importlib.import_module("machine_state.state")
        for banned in (
            "time", "datetime", "random", "socket", "threading", "asyncio",
            "sqlite3", "os", "json", "mock_rover", "storage", "record",
        ):
            self.assertFalse(hasattr(module, banned), banned)


class TestTelemetryAdapter(unittest.TestCase):
    def test_adapter_uses_only_started_homed_and_running(self):
        telemetry = {"started": True, "running": False, "camera": {"homed": True}}
        evidence = evidence_from_telemetry(telemetry)
        self.assertEqual(
            (evidence.started, evidence.homed, evidence.moving, evidence.fault_reasons,
             evidence.maintenance_declared),
            (True, True, False, (), False),
        )

    def test_missing_telemetry_keys_mean_not_reported(self):
        self.assertEqual(evaluate_telemetry({}).state, UNKNOWN)
        self.assertEqual(evaluate_telemetry({"started": False}).state, OFF)
        self.assertEqual(evaluate_telemetry({"started": True, "running": True}).state, RUNNING)

    def test_adapter_ignores_sensor_faults_obstacle_battery_probe_and_link(self):
        telemetry = {
            "started": True,
            "running": False,
            "camera": {"homed": True},
            "sensor_status": {"npk": "fault", "air": "fault", "light": "fault"},
            "obstacle": {"detected": True, "forward_inhibited": True},
            "battery": {"pct": 0.0, "voltage_v": 9.0, "charging": False, "mode": "idle"},
            "probe_state": "inserted_depth_not_ok",
            "link": {"connected": False},
        }
        self.assertEqual(evaluate_telemetry(telemetry).state, IDLE)

    def test_adapter_rejects_bad_shapes_and_types(self):
        for bad in (None, [], "telemetry"):
            with self.assertRaises(TypeError):
                evidence_from_telemetry(bad)
        for telemetry in (
            {"started": "yes"},
            {"started": True, "running": 1},
            {"started": True, "camera": []},
            {"started": True, "camera": {"homed": "yes"}},
        ):
            with self.assertRaises(ValueError, msg=telemetry):
                evidence_from_telemetry(telemetry)

    def test_adapter_does_not_modify_the_telemetry(self):
        telemetry = make_rover().telemetry()
        snapshot = copy.deepcopy(telemetry)
        evaluate_telemetry(telemetry)
        self.assertEqual(telemetry, snapshot)

    def test_evaluate_telemetry_reports_transition_information(self):
        telemetry = {"started": False}
        result = evaluate_telemetry(telemetry, previous_state=IDLE)
        self.assertEqual((result.state, result.previous_state, result.changed), (OFF, IDLE, True))


class TestMockRoverIntegration(unittest.TestCase):
    """Only the evidence the Mock Rover really provides: started, homed, moving."""

    def test_a_new_rover_reports_off(self):
        self.assertEqual(evaluate_telemetry(make_rover().telemetry()).state, OFF)

    def test_after_startup_the_rover_is_idle(self):
        rover = make_rover()
        rover.startup()
        self.assertEqual(evaluate_telemetry(rover.telemetry()).state, IDLE)

    def test_driving_in_place_turning_and_stopping(self):
        rover = make_rover()
        rover.startup()
        rover.drive(1.0, 1.0)
        rover.advance(1.0)
        self.assertEqual(evaluate_telemetry(rover.telemetry()).state, RUNNING)
        rover.stop()
        self.assertEqual(evaluate_telemetry(rover.telemetry()).state, IDLE)
        rover.drive(-1.0, 1.0)
        rover.advance(0.5)
        self.assertEqual(evaluate_telemetry(rover.telemetry()).state, RUNNING)

    def test_state_follows_actual_motion_not_commands(self):
        rover = make_rover()
        rover.startup()
        rover.set_obstacle(0.1)  # inside the stop distance
        rover.drive(1.0, 1.0)
        rover.advance(1.0)
        self.assertTrue(rover.forward_inhibited)
        self.assertEqual(evaluate_telemetry(rover.telemetry()).state, IDLE)

    def test_shutdown_returns_to_off(self):
        rover = make_rover()
        rover.startup()
        rover.drive(1.0, 1.0)
        rover.advance(1.0)
        rover.shutdown()
        self.assertEqual(evaluate_telemetry(rover.telemetry()).state, OFF)

    def test_sensor_faults_do_not_make_the_rover_faulty(self):
        rover = make_rover()
        rover.startup()
        for sensor in ("npk", "moisture", "soil_temperature", "air", "battery", "light"):
            rover.inject_fault(sensor)
        self.assertEqual(evaluate_telemetry(rover.telemetry()).state, IDLE)

    def test_empty_battery_obstacle_and_link_loss_do_not_change_the_state(self):
        rover = make_rover(battery_start_pct=0.0)
        rover.startup()
        rover.disconnect()
        rover.set_obstacle(0.05)
        self.assertEqual(evaluate_telemetry(rover.telemetry()).state, IDLE)

    def test_fault_overloaded_and_maintenance_are_unreachable_from_the_mock_rover(self):
        seen = set()
        for name in ("startup", "driving", "obstacle", "probe_measurement", "sensor_fault",
                     "communication_dropout", "camera_capture", "normal_operation", "shutdown"):
            rover = make_rover()
            for step_name, kwargs in get_scenario(name):
                run_scenario(rover, ((step_name, kwargs),))
                seen.add(evaluate_telemetry(rover.telemetry()).state)
        self.assertEqual(seen & {FAULT, OVERLOADED, MAINTENANCE}, set())
        self.assertEqual(seen, {OFF, IDLE, RUNNING})

    def test_state_sequence_is_deterministic_with_transition_information(self):
        def run_once():
            rover = make_rover(seed=5)
            history, previous = [], None
            for action in (
                lambda: None,
                rover.startup,
                lambda: (rover.drive(1.0, 1.0), rover.advance(1.0)),
                rover.stop,
                rover.shutdown,
            ):
                action()
                result = evaluate_telemetry(rover.telemetry(), previous_state=previous)
                history.append((result.state, result.changed))
                previous = result.state
            return history

        first, second = run_once(), run_once()
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            [(OFF, False), (IDLE, True), (RUNNING, True), (IDLE, True), (OFF, True)],
        )


if __name__ == "__main__":
    unittest.main()
