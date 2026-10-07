import importlib
import unittest
from dataclasses import replace
from unittest import mock

from mock_rover.config import RoverConfig
from mock_rover.scenarios import SCENARIOS, get_scenario, run_scenario
from mock_rover.simulator import MockRover
from record.validation import validate_record


def make_rover(**overrides):
    return MockRover(replace(RoverConfig(), **overrides))


def run(name, **overrides):
    rover = make_rover(**overrides)
    results = run_scenario(rover, get_scenario(name))
    return rover, results


def records_in(results):
    return [value for step, value in results if step == "build_record"]


class TestScenarioMechanism(unittest.TestCase):
    def test_all_required_scenarios_exist(self):
        for name in (
            "normal_operation", "startup", "driving", "obstacle", "probe_measurement",
            "sensor_fault", "communication_dropout", "camera_capture", "shutdown",
        ):
            self.assertIn(name, SCENARIOS)

    def test_every_scenario_runs(self):
        for name in SCENARIOS:
            rover, results = run(name)
            self.assertEqual(len(results), len(get_scenario(name)), name)

    def test_unknown_scenario_and_step_are_rejected(self):
        with self.assertRaises(ValueError):
            get_scenario("nope")
        with self.assertRaises(ValueError):
            run_scenario(make_rover(), (("__init__", {}),))
        with self.assertRaises(ValueError):
            run_scenario(make_rover(), (("telemetry", {}),))


class TestScenarioOutcomes(unittest.TestCase):
    def test_startup_homes_the_camera(self):
        rover, _ = run("startup")
        self.assertTrue(rover.started)
        self.assertTrue(rover.camera.homed)

    def test_driving_moves_and_turns_then_stops(self):
        rover, _ = run("driving")
        self.assertNotEqual(rover.pose.heading, 0.0)
        self.assertEqual(rover.speed, 0.0)
        self.assertGreater(rover.battery.pct, 0.0)
        self.assertLess(rover.battery.pct, 100.0)

    def test_obstacle_scenario_stops_at_the_threshold(self):
        rover, _ = run("obstacle")
        self.assertTrue(rover.obstacle_detected)
        self.assertLessEqual(rover.obstacle_distance_m, 0.25)
        self.assertGreater(rover.pose.x, 0.0)
        self.assertLess(rover.pose.x, 1.0)

    def test_probe_measurement_gives_a_valid_record(self):
        _, results = run("probe_measurement")
        records = records_in(results)
        self.assertEqual(len(records), 1)
        self.assertTrue(validate_record(records[0]).valid)
        self.assertEqual(set(records[0]["sensor_status"].values()), {"ok"})

    def test_sensor_fault_scenario_gives_a_faulty_but_valid_record(self):
        _, results = run("sensor_fault")
        record = records_in(results)[0]
        self.assertEqual(record["sensor_status"]["moisture"], "fault")
        self.assertIsNone(record["soil"]["moisture"])
        self.assertTrue(validate_record(record).valid)

    def test_communication_dropout_scenario(self):
        rover, results = run("communication_dropout")
        transmitted = [value for step, value in results if step == "transmit_telemetry"]
        self.assertIsNotNone(transmitted[0])
        self.assertIsNone(transmitted[1])
        self.assertIsNotNone(transmitted[2])
        self.assertEqual(rover.dropped, 1)

    def test_camera_capture_scenario(self):
        rover, results = run("camera_capture")
        capture = [value for step, value in results if step == "capture_image"][0]
        self.assertTrue(capture.led_on)
        self.assertEqual((capture.pan_deg, capture.tilt_deg), (90.0, -10.0))
        self.assertFalse(rover.camera.led_on)

    def test_normal_operation_gives_a_valid_record_with_leaf_data(self):
        _, results = run("normal_operation")
        record = records_in(results)[0]
        self.assertTrue(validate_record(record).valid)
        self.assertIsNotNone(record["leaf_label"])
        self.assertIsNotNone(record["photo_id"])

    def test_shutdown_stops_the_rover(self):
        rover, _ = run("shutdown")
        self.assertFalse(rover.started)
        self.assertEqual((rover.left_cmd, rover.right_cmd), (0.0, 0.0))


class TestScenarioDeterminism(unittest.TestCase):
    def test_same_seed_and_scenario_give_the_same_results(self):
        for name in SCENARIOS:
            _, first = run(name, seed=21)
            _, second = run(name, seed=21)
            self.assertEqual(first, second, name)

    def test_same_seed_gives_the_same_final_telemetry(self):
        for name in SCENARIOS:
            a, _ = run(name, seed=21)
            b, _ = run(name, seed=21)
            self.assertEqual(a.telemetry(), b.telemetry(), name)

    def test_different_seeds_change_the_measured_values(self):
        _, one = run("probe_measurement", seed=1)
        _, two = run("probe_measurement", seed=2)
        self.assertNotEqual(records_in(one), records_in(two))

    def test_simulated_scenarios_do_not_depend_on_wall_clock(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("the simulator used the wall clock or slept")

        def run_all():
            outcomes = {}
            for name in SCENARIOS:
                rover, results = run(name, seed=3)
                outcomes[name] = (results, rover.telemetry())
            return outcomes

        # Any call to a wall-clock or sleep function raises. The scenarios must
        # still run to completion and give the same results as an unpatched run.
        with mock.patch("time.time", forbidden), \
                mock.patch("time.time_ns", forbidden), \
                mock.patch("time.monotonic", forbidden), \
                mock.patch("time.perf_counter", forbidden), \
                mock.patch("time.sleep", forbidden):
            patched = run_all()
        self.assertEqual(patched, run_all())

        # Simulated minutes pass instantly: sim_time moves, the wall clock is not used.
        rover, _ = run("normal_operation", seed=3)
        self.assertGreater(rover.sim_time, 10.0)

        # The simulator modules do not import time-, randomness- or network-related modules.
        for module_name in (
            "config", "movement", "sensors", "camera", "battery", "simulator", "scenarios",
        ):
            module = importlib.import_module("mock_rover." + module_name)
            for banned in ("time", "datetime", "random", "socket", "threading", "asyncio"):
                self.assertFalse(hasattr(module, banned), (module_name, banned))


if __name__ == "__main__":
    unittest.main()
