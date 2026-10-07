import unittest
from dataclasses import replace

from mock_rover.config import RoverConfig
from mock_rover.sensors import SoilField, unit_noise
from mock_rover.simulator import (
    PROBE_BAD_DEPTH,
    PROBE_OK,
    PROBE_RETRACTED,
    MockRover,
    ProbeNotReady,
)


def make_rover(**overrides):
    return MockRover(replace(RoverConfig(), **overrides))


class TestNoise(unittest.TestCase):
    def test_noise_is_deterministic_and_bounded(self):
        for keys in (("a",), ("soil", "nitrogen", 1, 2), (5, 6, 7)):
            first = unit_noise(1, *keys)
            self.assertEqual(first, unit_noise(1, *keys))
            self.assertGreaterEqual(first, -1.0)
            self.assertLess(first, 1.0)

    def test_noise_depends_on_seed_and_keys(self):
        self.assertNotEqual(unit_noise(1, "x"), unit_noise(2, "x"))
        self.assertNotEqual(unit_noise(1, "x"), unit_noise(1, "y"))


class TestProbe(unittest.TestCase):
    def test_no_measurement_before_insertion(self):
        rover = make_rover()
        self.assertEqual(rover.probe_state, PROBE_RETRACTED)
        with self.assertRaises(ProbeNotReady):
            rover.take_measurement()
        self.assertIsNone(rover.last_measurement)

    def test_successful_insertion_enables_measurement(self):
        rover = make_rover()
        self.assertTrue(rover.insert_probe(depth_ok=True))
        self.assertEqual(rover.probe_state, PROBE_OK)
        measurement = rover.take_measurement()
        self.assertEqual(
            set(measurement["soil"]),
            {"nitrogen", "phosphorus", "potassium", "moisture", "temperature"},
        )
        self.assertEqual(set(measurement["sensor_status"].values()), {"ok"})

    def test_failed_insertion_gives_no_measurement(self):
        rover = make_rover()
        self.assertFalse(rover.insert_probe(depth_ok=False))
        self.assertEqual(rover.probe_state, PROBE_BAD_DEPTH)
        with self.assertRaises(ProbeNotReady):
            rover.take_measurement()

    def test_retracting_the_probe_disables_measurement(self):
        rover = make_rover()
        rover.insert_probe(depth_ok=True)
        rover.retract_probe()
        with self.assertRaises(ProbeNotReady):
            rover.take_measurement()

    def test_measurement_is_deterministic_for_a_seed(self):
        readings = []
        for _ in range(2):
            rover = make_rover(seed=11)
            rover.insert_probe(depth_ok=True)
            readings.append(rover.take_measurement()["soil"])
        self.assertEqual(readings[0], readings[1])

    def test_different_seeds_give_different_readings(self):
        values = []
        for seed in (1, 2):
            rover = make_rover(seed=seed)
            rover.insert_probe(depth_ok=True)
            values.append(rover.take_measurement()["soil"])
        self.assertNotEqual(values[0], values[1])

    def test_repeated_reading_at_one_position_is_identical(self):
        rover = make_rover(seed=5)
        rover.insert_probe(depth_ok=True)
        first = rover.take_measurement()["soil"]
        second = rover.take_measurement()["soil"]
        self.assertEqual(first, second)

    def test_readings_vary_with_position(self):
        rover = make_rover(seed=5)
        rover.insert_probe(depth_ok=True)
        here = rover.take_measurement()["soil"]
        rover.drive(1.0, 1.0)
        rover.advance(2.0)
        rover.stop()
        there = rover.take_measurement()["soil"]
        self.assertNotEqual(here, there)

    def test_soil_field_is_repeatable_per_position(self):
        field = SoilField(RoverConfig(seed=2))
        self.assertEqual(field.sample(1.0, 2.0), field.sample(1.0, 2.0))
        self.assertNotEqual(field.sample(1.0, 2.0), field.sample(3.0, 2.0))

    def test_measurement_takes_simulated_time(self):
        rover = make_rover(measurement_duration_s=2.5)
        rover.insert_probe(depth_ok=True)
        rover.take_measurement()
        self.assertAlmostEqual(rover.sim_time, 2.5)


class TestSensorFaults(unittest.TestCase):
    def measure_with_fault(self, sensor):
        rover = make_rover()
        rover.inject_fault(sensor)
        rover.insert_probe(depth_ok=True)
        return rover.take_measurement()

    def test_npk_fault_nulls_the_three_nutrients(self):
        m = self.measure_with_fault("npk")
        self.assertEqual(m["sensor_status"]["npk"], "fault")
        for key in ("nitrogen", "phosphorus", "potassium"):
            self.assertIsNone(m["soil"][key])
        self.assertIsNotNone(m["soil"]["moisture"])

    def test_moisture_and_soil_temperature_faults(self):
        m = self.measure_with_fault("moisture")
        self.assertIsNone(m["soil"]["moisture"])
        self.assertEqual(m["sensor_status"]["moisture"], "fault")
        m = self.measure_with_fault("soil_temperature")
        self.assertIsNone(m["soil"]["temperature"])
        self.assertEqual(m["sensor_status"]["soil_temperature"], "fault")

    def test_air_and_battery_faults(self):
        m = self.measure_with_fault("air")
        self.assertEqual(set(m["air"].values()), {None})
        m = self.measure_with_fault("battery")
        self.assertIsNone(m["battery"]["voltage_v"])

    def test_fault_can_be_cleared(self):
        rover = make_rover()
        rover.inject_fault("moisture")
        rover.clear_fault("moisture")
        rover.insert_probe(depth_ok=True)
        self.assertEqual(rover.take_measurement()["sensor_status"]["moisture"], "ok")

    def test_unknown_sensor_is_rejected(self):
        rover = make_rover()
        with self.assertRaises(ValueError):
            rover.inject_fault("camera")
        with self.assertRaises(ValueError):
            rover.clear_fault("nothing")


class TestAirAndLight(unittest.TestCase):
    def test_air_readings_exist(self):
        air = make_rover().read_air()
        self.assertEqual(set(air), {"temperature", "humidity", "pressure"})
        self.assertTrue(all(isinstance(v, float) for v in air.values()))

    def test_light_reading_exists(self):
        self.assertIsInstance(make_rover().read_light(), float)

    def test_air_and_light_are_deterministic_for_a_seed(self):
        a, b = make_rover(seed=9), make_rover(seed=9)
        for _ in range(3):
            self.assertEqual(a.read_air(), b.read_air())
            self.assertEqual(a.read_light(), b.read_light())
            a.advance(61.0)
            b.advance(61.0)

    def test_air_varies_over_simulated_time(self):
        rover = make_rover(seed=9)
        first = rover.read_air()
        rover.advance(120.0)
        self.assertNotEqual(first, rover.read_air())

    def test_light_fault_nulls_light_and_does_not_touch_air(self):
        rover = make_rover()
        rover.inject_fault("light")
        self.assertIsNone(rover.read_light())
        self.assertIsNotNone(rover.read_air()["temperature"])
        self.assertEqual(rover.telemetry()["sensor_status"]["light"], "fault")

    def test_light_is_telemetry_only_not_a_record_field(self):
        rover = make_rover()
        rover.insert_probe(depth_ok=True)
        measurement = rover.take_measurement()
        self.assertIn("light", measurement)
        record = rover.build_record()
        self.assertNotIn("light", record)
        self.assertNotIn("light", record["air"])
        self.assertNotIn("light", record["sensor_status"])


if __name__ == "__main__":
    unittest.main()
