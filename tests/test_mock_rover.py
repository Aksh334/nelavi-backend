import math
import unittest
from dataclasses import replace

from mock_rover.config import RoverConfig
from mock_rover.simulator import (
    MockRover,
    NoMeasurementError,
    ProbeNotReady,
)
from record.validation import TOP_LEVEL_KEYS, validate_record


def make_rover(**overrides):
    return MockRover(replace(RoverConfig(), **overrides))


class TestMovement(unittest.TestCase):
    def test_initial_state_is_stationary(self):
        rover = make_rover()
        self.assertEqual((rover.pose.x, rover.pose.y, rover.pose.heading), (0.0, 0.0, 0.0))
        self.assertEqual(rover.speed, 0.0)
        self.assertEqual(rover.sim_time, 0.0)

    def test_stays_still_without_commands(self):
        rover = make_rover()
        rover.advance(5.0)
        self.assertEqual((rover.pose.x, rover.pose.y, rover.pose.heading), (0.0, 0.0, 0.0))
        self.assertEqual(rover.speed, 0.0)
        self.assertAlmostEqual(rover.sim_time, 5.0)

    def test_forward_movement(self):
        rover = make_rover()
        rover.drive(1.0, 1.0)
        rover.advance(2.0)
        self.assertAlmostEqual(rover.pose.x, 0.56 * 2.0, places=6)
        self.assertAlmostEqual(rover.pose.y, 0.0, places=9)
        self.assertAlmostEqual(rover.speed, 0.56)

    def test_reverse_movement(self):
        rover = make_rover()
        rover.drive(-1.0, -1.0)
        rover.advance(1.0)
        self.assertAlmostEqual(rover.pose.x, -0.56, places=6)
        self.assertAlmostEqual(rover.speed, -0.56)

    def test_turning_while_moving_changes_heading_and_position(self):
        rover = make_rover()
        rover.drive(0.5, 1.0)
        rover.advance(1.0)
        self.assertGreater(rover.pose.heading, 0.0)
        self.assertGreater(rover.pose.x, 0.0)
        self.assertGreater(rover.pose.y, 0.0)

    def test_in_place_turn_changes_heading_only(self):
        rover = make_rover()
        rover.drive(-1.0, 1.0)
        rover.advance(0.1)
        expected = 2 * 0.56 / 0.270 * 0.1
        self.assertAlmostEqual(rover.pose.heading, expected, places=6)
        self.assertEqual((rover.pose.x, rover.pose.y), (0.0, 0.0))
        self.assertEqual(rover.speed, 0.0)

    def test_running_state_follows_motion(self):
        rover = make_rover()
        self.assertFalse(rover.running)
        rover.drive(1.0, 1.0)
        rover.advance(0.5)
        self.assertTrue(rover.running)
        self.assertTrue(rover.telemetry()["running"])
        rover.stop()
        self.assertFalse(rover.running)
        rover.drive(-1.0, 1.0)
        rover.advance(0.2)
        self.assertTrue(rover.running)  # turning in place counts as running

    def test_stop_halts_motion(self):
        rover = make_rover()
        rover.drive(1.0, 1.0)
        rover.advance(1.0)
        rover.stop()
        x = rover.pose.x
        rover.advance(2.0)
        self.assertEqual(rover.pose.x, x)
        self.assertEqual(rover.speed, 0.0)

    def test_slip_reduces_speed_deterministically(self):
        rover = make_rover(slip=0.25)
        rover.drive(1.0, 1.0)
        rover.advance(1.0)
        self.assertAlmostEqual(rover.pose.x, 0.56 * 0.75, places=6)

    def test_slip_jitter_is_repeatable_for_a_seed(self):
        positions = []
        for _ in range(2):
            rover = make_rover(seed=7, slip=0.1, slip_jitter=0.5)
            rover.drive(1.0, 1.0)
            rover.advance(3.0)
            positions.append(rover.pose.x)
        self.assertEqual(positions[0], positions[1])

    def test_invalid_commands_and_durations_are_rejected(self):
        rover = make_rover()
        for bad in (1.5, -1.5, float("nan"), True, "1"):
            with self.assertRaises(ValueError):
                rover.drive(bad, 0.0)
        for bad in (-1.0, float("inf"), float("nan"), True):
            with self.assertRaises(ValueError):
                rover.advance(bad)


class TestObstacle(unittest.TestCase):
    def test_no_obstacle_reads_max_range(self):
        rover = make_rover()
        self.assertEqual(rover.obstacle_reading(), 4.0)
        self.assertFalse(rover.obstacle_detected)

    def test_distance_can_be_set_and_read(self):
        rover = make_rover()
        rover.set_obstacle(1.5)
        self.assertEqual(rover.obstacle_reading(), 1.5)
        self.assertFalse(rover.obstacle_detected)

    def test_reading_is_limited_to_the_configured_range(self):
        rover = make_rover()
        rover.set_obstacle(0.0)
        self.assertEqual(rover.obstacle_reading(), 0.02)
        rover.set_obstacle(10.0)
        self.assertEqual(rover.obstacle_reading(), 4.0)

    def test_stop_threshold_is_configurable(self):
        rover = make_rover(obstacle_stop_distance_m=0.5)
        rover.set_obstacle(0.4)
        self.assertTrue(rover.obstacle_detected)
        rover.set_obstacle(0.6)
        self.assertFalse(rover.obstacle_detected)

    def test_threshold_comparison_is_configurable(self):
        inclusive = make_rover(obstacle_stop_distance_m=0.25, obstacle_stop_inclusive=True)
        exclusive = make_rover(obstacle_stop_distance_m=0.25, obstacle_stop_inclusive=False)
        inclusive.set_obstacle(0.25)
        exclusive.set_obstacle(0.25)
        self.assertTrue(inclusive.obstacle_detected)
        self.assertFalse(exclusive.obstacle_detected)
        exclusive.set_obstacle(0.24)
        self.assertTrue(exclusive.obstacle_detected)

    def test_default_configuration_treats_exactly_the_stop_distance_as_an_obstacle(self):
        rover = MockRover()  # default configuration, nothing overridden
        self.assertEqual(rover.config.obstacle_stop_distance_m, 0.25)
        rover.set_obstacle(0.25)
        self.assertTrue(rover.obstacle_detected)
        rover.set_obstacle(0.2500001)
        self.assertFalse(rover.obstacle_detected)
        rover.set_obstacle(0.25)
        rover.drive(1.0, 1.0)
        rover.advance(0.5)
        self.assertTrue(rover.forward_inhibited)
        self.assertEqual(rover.pose.x, 0.0)

    def test_arc_turn_loses_its_forward_component_but_keeps_rotating(self):
        rover = MockRover()
        rover.set_obstacle(0.2)  # inside the default stop distance
        rover.drive(0.5, 1.0)  # an arc turn: forward component and rotation
        rover.advance(0.5)
        self.assertTrue(rover.forward_inhibited)
        self.assertEqual(rover.speed, 0.0)
        self.assertEqual((rover.pose.x, rover.pose.y), (0.0, 0.0))
        self.assertEqual(rover.obstacle_distance_m, 0.2)
        expected_turn_rate = (1.0 - 0.5) * 0.56 / 0.270
        self.assertAlmostEqual(rover.turn_rate, expected_turn_rate, places=9)
        self.assertAlmostEqual(rover.pose.heading, expected_turn_rate * 0.5, places=9)
        self.assertTrue(rover.running)

    def test_obstacle_inhibits_forward_motion(self):
        rover = make_rover()
        rover.set_obstacle(0.2)
        rover.drive(1.0, 1.0)
        rover.advance(1.0)
        self.assertEqual(rover.pose.x, 0.0)
        self.assertEqual(rover.speed, 0.0)
        self.assertTrue(rover.forward_inhibited)

    def test_reverse_and_in_place_turning_are_not_inhibited(self):
        rover = make_rover()
        rover.set_obstacle(0.2)
        rover.drive(-1.0, -1.0)
        rover.advance(0.5)
        self.assertLess(rover.pose.x, 0.0)
        self.assertGreater(rover.obstacle_distance_m, 0.2)
        rover.drive(-1.0, 1.0)
        rover.advance(0.1)
        self.assertNotEqual(rover.pose.heading, 0.0)

    def test_distance_shrinks_while_approaching_and_rover_stops_at_threshold(self):
        rover = make_rover()
        rover.set_obstacle(1.0)
        rover.drive(1.0, 1.0)
        rover.advance(0.5)
        self.assertLess(rover.obstacle_distance_m, 1.0)
        rover.advance(5.0)
        self.assertTrue(rover.obstacle_detected)
        self.assertLessEqual(rover.obstacle_distance_m, 0.25)
        self.assertGreater(rover.obstacle_distance_m, 0.25 - 0.56 * 0.1 - 1e-9)
        self.assertEqual(rover.speed, 0.0)
        self.assertTrue(rover.forward_inhibited)

    def test_invalid_distances_are_rejected(self):
        rover = make_rover()
        for bad in (-1.0, float("nan"), float("inf"), True, "1"):
            with self.assertRaises(ValueError):
                rover.set_obstacle(bad)


class TestConnectivity(unittest.TestCase):
    def test_connected_by_default_and_delivers(self):
        rover = make_rover()
        out = rover.transmit_telemetry()
        self.assertIsNotNone(out)
        self.assertTrue(rover.telemetry()["link"]["connected"])
        self.assertEqual(len(rover.deliver_due()), 1)

    def test_disconnect_drops_and_reconnect_restores(self):
        rover = make_rover()
        rover.disconnect()
        self.assertFalse(rover.telemetry()["link"]["connected"])
        self.assertIsNone(rover.transmit_telemetry())
        self.assertEqual(rover.dropped, 1)
        rover.reconnect()
        self.assertIsNotNone(rover.transmit_telemetry())
        self.assertEqual(rover.dropped, 1)

    def test_configurable_delay(self):
        rover = make_rover(link_delay_s=0.5)
        out = rover.transmit_telemetry()
        self.assertAlmostEqual(out.deliver_at, 0.5)
        self.assertEqual(rover.deliver_due(), [])
        rover.advance(0.5)
        self.assertEqual(len(rover.deliver_due()), 1)

    def test_scheduled_dropout_window(self):
        rover = make_rover(dropout_windows=((2.0, 4.0),))
        rover.advance(1.0)
        self.assertIsNotNone(rover.transmit_telemetry())
        rover.advance(2.0)  # t = 3.0, inside the window
        self.assertIsNone(rover.transmit_telemetry())
        rover.advance(1.0)  # t = 4.0, the window end is exclusive
        self.assertIsNotNone(rover.transmit_telemetry())
        self.assertEqual(rover.dropped, 1)

    def test_dropout_behavior_is_deterministic(self):
        results = []
        for _ in range(2):
            rover = make_rover(dropout_windows=((1.0, 2.0), (5.0, 6.0)))
            outcome = []
            for _ in range(8):
                outcome.append(rover.transmit_telemetry() is not None)
                rover.advance(1.0)
            results.append(outcome)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][1], False)
        self.assertEqual(results[0][5], False)


def measured_rover(**overrides):
    rover = make_rover(**overrides)
    rover.startup()
    rover.insert_probe(depth_ok=True)
    rover.take_measurement()
    return rover


class TestRecords(unittest.TestCase):
    def test_generated_record_is_valid_and_has_only_canonical_fields(self):
        rover = measured_rover()
        record = rover.build_record()
        self.assertTrue(validate_record(record).valid)
        self.assertEqual(set(record), set(TOP_LEVEL_KEYS))
        self.assertNotIn("light", record)
        self.assertNotIn("light", record["air"])

    def test_record_without_measurement_is_refused(self):
        rover = make_rover()
        with self.assertRaises(NoMeasurementError):
            rover.build_record()

    def test_sequence_number_is_deterministic_and_increments(self):
        rover = measured_rover(initial_sequence=10)
        numbers = [rover.build_record()["sequence_number"] for _ in range(3)]
        self.assertEqual(numbers, [10, 11, 12])

    def test_record_uses_configured_identity_and_versions(self):
        rover = measured_rover(rover_id="r-test", firmware_version="f1")
        record = rover.build_record()
        self.assertEqual(record["rover_id"], "r-test")
        self.assertEqual(record["versions"]["firmware"], "f1")
        self.assertTrue(record["time"].startswith("sim:"))

    def test_record_without_capture_has_all_null_leaf_fields(self):
        record = measured_rover().build_record()
        self.assertEqual(
            (record["leaf_label"], record["confidence"], record["photo_id"]),
            (None, None, None),
        )

    def test_record_with_capture_has_populated_leaf_fields(self):
        rover = measured_rover()
        capture = rover.capture_image()
        record = rover.build_record()
        self.assertEqual(record["leaf_label"], capture.label)
        self.assertEqual(record["confidence"], capture.confidence)
        self.assertEqual(record["photo_id"], capture.photo_id)
        self.assertTrue(validate_record(record).valid)

    def test_unclear_switch_produces_a_valid_unclear_record(self):
        rover = measured_rover()
        rover.set_force_unclear(True)
        rover.capture_image()
        record = rover.build_record()
        self.assertEqual(record["leaf_label"], "unclear")
        self.assertTrue(validate_record(record).valid)

    def test_sensor_fault_record_follows_record_core_rules(self):
        rover = make_rover()
        rover.startup()
        rover.inject_fault("moisture")
        rover.inject_fault("air")
        rover.insert_probe(depth_ok=True)
        rover.take_measurement()
        record = rover.build_record()
        self.assertEqual(record["sensor_status"]["moisture"], "fault")
        self.assertIsNone(record["soil"]["moisture"])
        self.assertEqual(record["sensor_status"]["air"], "fault")
        self.assertEqual(set(record["air"].values()), {None})
        self.assertEqual(record["sensor_status"]["npk"], "ok")
        self.assertIsNotNone(record["soil"]["nitrogen"])
        self.assertTrue(validate_record(record).valid)

    def test_every_sensor_fault_still_gives_a_valid_record(self):
        rover = make_rover()
        for sensor in ("npk", "moisture", "soil_temperature", "air", "battery"):
            rover.inject_fault(sensor)
        rover.insert_probe(depth_ok=True)
        rover.take_measurement()
        self.assertTrue(validate_record(rover.build_record()).valid)

    def test_record_requires_a_probe_measurement(self):
        rover = make_rover()
        with self.assertRaises(ProbeNotReady):
            rover.take_measurement()
        with self.assertRaises(NoMeasurementError):
            rover.build_record()

    def test_same_seed_gives_the_same_records(self):
        records = []
        for _ in range(2):
            rover = measured_rover(seed=3)
            rover.capture_image()
            records.append(rover.build_record())
        self.assertEqual(records[0], records[1])


class TestTelemetry(unittest.TestCase):
    def test_telemetry_snapshot_has_expected_sections(self):
        rover = make_rover()
        snapshot = rover.telemetry()
        for key in (
            "sim_time", "pose", "speed_mps", "obstacle", "camera", "probe_state",
            "air", "light", "battery", "sensor_status", "link",
        ):
            self.assertIn(key, snapshot)
        self.assertIn("light", snapshot["sensor_status"])
        self.assertTrue(all(math.isfinite(v) for v in snapshot["air"].values()))

    def test_telemetry_is_a_snapshot_copy(self):
        rover = make_rover()
        snapshot = rover.telemetry()
        snapshot["pose"]["x"] = 99.0
        self.assertEqual(rover.pose.x, 0.0)


if __name__ == "__main__":
    unittest.main()
