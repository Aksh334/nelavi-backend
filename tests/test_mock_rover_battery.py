import unittest
from dataclasses import replace

from mock_rover.battery import BatteryModel
from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover


def make_config(**overrides):
    return replace(RoverConfig(), **overrides)


class TestBatteryModel(unittest.TestCase):
    def test_full_battery_is_at_the_reference_maximum(self):
        battery = BatteryModel(make_config())
        self.assertEqual(battery.pct, 100.0)
        self.assertAlmostEqual(battery.voltage_v, 12.6)
        self.assertFalse(battery.charging)

    def test_empty_battery_is_at_the_reference_minimum(self):
        battery = BatteryModel(make_config(battery_start_pct=0.0))
        self.assertAlmostEqual(battery.voltage_v, 9.0)

    def test_voltage_is_between_the_reference_limits(self):
        for pct in (0.0, 25.0, 50.0, 75.0, 100.0):
            battery = BatteryModel(make_config(battery_start_pct=pct))
            self.assertGreaterEqual(battery.voltage_v, 9.0 - 1e-9)
            self.assertLessEqual(battery.voltage_v, 12.6 + 1e-9)

    def test_drain_depends_on_mode(self):
        drops = {}
        for mode in ("idle", "testing", "driving"):
            battery = BatteryModel(make_config())
            battery.step(3600.0, mode)
            drops[mode] = 100.0 - battery.pct
        self.assertAlmostEqual(drops["idle"], 1.0)
        self.assertAlmostEqual(drops["testing"], 10.0)
        self.assertAlmostEqual(drops["driving"], 50.0)
        self.assertLess(drops["idle"], drops["testing"])
        self.assertLess(drops["testing"], drops["driving"])

    def test_voltage_falls_as_the_battery_drains(self):
        battery = BatteryModel(make_config())
        before = battery.voltage_v
        battery.step(3600.0, "driving")
        self.assertLess(battery.voltage_v, before)

    def test_solar_charging_increases_the_charge(self):
        battery = BatteryModel(make_config(battery_start_pct=50.0))
        battery.set_solar(20.0)
        battery.step(3600.0, "idle")  # net +19 percent per hour
        self.assertAlmostEqual(battery.pct, 69.0)
        self.assertTrue(battery.charging)

    def test_charge_does_not_exceed_the_configured_maximum(self):
        battery = BatteryModel(make_config(battery_start_pct=99.0, battery_max_pct=100.0))
        battery.set_solar(500.0)
        battery.step(3600.0, "idle")
        self.assertEqual(battery.pct, 100.0)
        self.assertFalse(battery.charging)

    def test_a_lower_configured_maximum_is_respected(self):
        battery = BatteryModel(make_config(battery_start_pct=80.0, battery_max_pct=90.0))
        battery.set_solar(500.0)
        battery.step(3600.0, "idle")
        self.assertEqual(battery.pct, 90.0)

    def test_charge_does_not_go_below_zero(self):
        battery = BatteryModel(make_config(battery_start_pct=1.0))
        battery.step(7200.0, "driving")
        self.assertEqual(battery.pct, 0.0)

    def test_solar_below_the_drain_is_not_charging(self):
        battery = BatteryModel(make_config(battery_start_pct=50.0))
        battery.set_solar(30.0)
        battery.step(60.0, "driving")
        self.assertFalse(battery.charging)

    def test_invalid_mode_and_solar_are_rejected(self):
        battery = BatteryModel(make_config())
        with self.assertRaises(ValueError):
            battery.step(1.0, "flying")
        with self.assertRaises(ValueError):
            battery.set_solar(-1.0)


class TestBatteryInRover(unittest.TestCase):
    def test_driving_drains_faster_than_idling(self):
        idle = MockRover(make_config())
        idle.advance(600.0)
        driving = MockRover(make_config())
        driving.drive(1.0, 1.0)
        driving.advance(600.0)
        self.assertLess(driving.battery.pct, idle.battery.pct)
        self.assertEqual(idle.battery.mode, "idle")
        self.assertEqual(driving.battery.mode, "driving")

    def test_measurement_uses_testing_mode(self):
        rover = MockRover(make_config())
        rover.insert_probe(depth_ok=True)
        rover.take_measurement()
        self.assertEqual(rover.battery.mode, "testing")

    def test_solar_input_charges_the_rover_battery(self):
        rover = MockRover(make_config(battery_start_pct=50.0))
        rover.set_solar(30.0)
        rover.advance(3600.0)
        self.assertGreater(rover.battery.pct, 50.0)
        self.assertTrue(rover.battery.charging)

    def test_telemetry_reports_the_battery(self):
        battery = MockRover(make_config()).telemetry()["battery"]
        self.assertEqual(set(battery), {"pct", "voltage_v", "charging", "mode"})


if __name__ == "__main__":
    unittest.main()
