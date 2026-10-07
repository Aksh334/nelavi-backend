import copy
import dataclasses
import importlib
import unittest
from dataclasses import replace
from unittest import mock

from energy.energy import (
    KIND_BATTERY_NET,
    KIND_ELECTRICAL,
    UNIT_ENERGY,
    CostResult,
    EnergyResult,
    average_power_w,
    battery_interval_from_telemetry,
    battery_net_energy,
    energy_cost,
    energy_from_power,
)
from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover

BAD_NUMBERS = (-1.0, float("nan"), float("inf"), float("-inf"), True, False, "5", None, [1])


def make_rover(**overrides):
    return MockRover(replace(RoverConfig(), **overrides))


class TestEnergyFromPower(unittest.TestCase):
    def test_zero_power_and_zero_duration_give_zero_energy(self):
        self.assertEqual(energy_from_power(0, 3600).quantity, 0.0)
        self.assertEqual(energy_from_power(100, 0).quantity, 0.0)
        self.assertEqual(energy_from_power(0, 0).quantity, 0.0)

    def test_normal_values(self):
        self.assertAlmostEqual(energy_from_power(100, 3600).quantity, 100.0)
        self.assertAlmostEqual(energy_from_power(60, 1800).quantity, 30.0)
        self.assertAlmostEqual(energy_from_power(1500, 7200).quantity, 3000.0)
        self.assertAlmostEqual(energy_from_power(2.5, 36).quantity, 0.025)

    def test_integer_and_float_inputs_agree(self):
        self.assertEqual(energy_from_power(100, 3600), energy_from_power(100.0, 3600.0))

    def test_result_states_unit_duration_source_and_assumptions(self):
        result = energy_from_power(50, 120)
        self.assertIsInstance(result, EnergyResult)
        self.assertEqual(result.unit, UNIT_ENERGY)
        self.assertEqual(result.unit, "Wh")
        self.assertEqual(result.duration_s, 120.0)
        self.assertEqual(result.kind, KIND_ELECTRICAL)
        self.assertIn("supplied by the caller", result.source)
        self.assertTrue(result.assumptions)
        self.assertFalse(hasattr(result, "confidence"))

    def test_large_values_are_finite(self):
        result = energy_from_power(1e6, 1e6)
        self.assertAlmostEqual(result.quantity, 1e6 * 1e6 / 3600.0)

    def test_invalid_values_are_rejected(self):
        for bad in BAD_NUMBERS:
            with self.assertRaises(ValueError, msg=("power", bad)):
                energy_from_power(bad, 10)
            with self.assertRaises(ValueError, msg=("duration", bad)):
                energy_from_power(10, bad)


class TestBatteryNetEnergy(unittest.TestCase):
    def test_discharge_is_negative(self):
        result = battery_net_energy(100, 50, 28, 3600)
        self.assertAlmostEqual(result.quantity, -14.0)
        self.assertEqual(result.unit, "Wh")
        self.assertEqual(result.kind, KIND_BATTERY_NET)

    def test_charge_is_positive(self):
        self.assertAlmostEqual(battery_net_energy(50, 100, 28, 3600).quantity, 14.0)

    def test_no_change_is_zero(self):
        self.assertEqual(battery_net_energy(70, 70, 28, 600).quantity, 0.0)

    def test_boundary_percentages(self):
        self.assertAlmostEqual(battery_net_energy(0, 100, 28, 1).quantity, 28.0)
        self.assertAlmostEqual(battery_net_energy(100, 0, 28, 1).quantity, -28.0)

    def test_quantity_scales_with_capacity(self):
        self.assertAlmostEqual(battery_net_energy(100, 50, 56, 1).quantity, -28.0)
        self.assertAlmostEqual(battery_net_energy(100, 50, 0.5, 1).quantity, -0.25)

    def test_result_states_its_limits(self):
        result = battery_net_energy(100, 50, 28, 3600)
        text = " ".join(result.assumptions)
        self.assertIn("NET", text)
        self.assertIn("not machine electrical consumption", text)
        self.assertIn("28", text)  # the supplied capacity is stated
        self.assertEqual(result.duration_s, 3600.0)

    def test_invalid_values_are_rejected(self):
        for bad in BAD_NUMBERS + (100.1, 101):
            with self.assertRaises(ValueError, msg=("start", bad)):
                battery_net_energy(bad, 50, 28, 1)
            with self.assertRaises(ValueError, msg=("end", bad)):
                battery_net_energy(50, bad, 28, 1)
        for bad in BAD_NUMBERS + (0, 0.0):
            with self.assertRaises(ValueError, msg=("capacity", bad)):
                battery_net_energy(50, 40, bad, 1)
        for bad in BAD_NUMBERS:
            with self.assertRaises(ValueError, msg=("duration", bad)):
                battery_net_energy(50, 40, 28, bad)

    def test_there_is_no_default_capacity(self):
        with self.assertRaises(TypeError):
            battery_net_energy(100, 50, duration_s=1)


class TestAveragePower(unittest.TestCase):
    def test_average_power_from_supplied_power(self):
        self.assertAlmostEqual(average_power_w(energy_from_power(80, 1800)), 80.0)

    def test_average_net_power_of_a_discharge_is_negative(self):
        self.assertAlmostEqual(average_power_w(battery_net_energy(100, 50, 28, 3600)), -14.0)
        self.assertAlmostEqual(average_power_w(battery_net_energy(100, 75, 28, 1800)), -14.0)

    def test_zero_energy_over_a_positive_duration_is_zero_power(self):
        self.assertEqual(average_power_w(battery_net_energy(70, 70, 28, 60)), 0.0)

    def test_zero_duration_is_unavailable_not_zero(self):
        for result in (energy_from_power(100, 0), battery_net_energy(100, 50, 28, 0)):
            with self.assertRaises(ValueError):
                average_power_w(result)

    def test_requires_an_energy_result(self):
        for bad in (None, 5, {"quantity": 1}):
            with self.assertRaises(TypeError):
                average_power_w(bad)


class TestEnergyCost(unittest.TestCase):
    def test_cost_is_kwh_times_the_supplied_tariff(self):
        cost = energy_cost(energy_from_power(1000, 3600), 0.2, "EUR")
        self.assertIsInstance(cost, CostResult)
        self.assertAlmostEqual(cost.energy_kwh, 1.0)
        self.assertAlmostEqual(cost.amount, 0.2)
        self.assertEqual((cost.currency, cost.tariff_per_kwh), ("EUR", 0.2))

    def test_other_quantities(self):
        self.assertAlmostEqual(energy_cost(energy_from_power(500, 3600), 0.5, "X").amount, 0.25)
        self.assertAlmostEqual(energy_cost(energy_from_power(2000, 1800), 12.0, "Y").amount, 12.0)

    def test_zero_energy_and_zero_tariff_give_zero_cost(self):
        self.assertEqual(energy_cost(energy_from_power(0, 3600), 0.3, "EUR").amount, 0.0)
        self.assertEqual(energy_cost(energy_from_power(1000, 3600), 0, "EUR").amount, 0.0)

    def test_tariff_is_configurable_not_fixed(self):
        energy = energy_from_power(1000, 3600)
        self.assertNotEqual(
            energy_cost(energy, 0.1, "EUR").amount, energy_cost(energy, 0.4, "EUR").amount
        )

    def test_there_is_no_default_tariff_or_currency(self):
        energy = energy_from_power(1000, 3600)
        with self.assertRaises(TypeError):
            energy_cost(energy)
        with self.assertRaises(TypeError):
            energy_cost(energy, 0.2)

    def test_invalid_tariff_and_currency_are_rejected(self):
        energy = energy_from_power(1000, 3600)
        for bad in BAD_NUMBERS:
            with self.assertRaises(ValueError, msg=bad):
                energy_cost(energy, bad, "EUR")
        for bad in ("", None, 5, ["EUR"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                energy_cost(energy, 0.2, bad)

    def test_battery_net_energy_is_not_billable(self):
        with self.assertRaises(ValueError) as caught:
            energy_cost(battery_net_energy(100, 50, 28, 3600), 0.2, "EUR")
        self.assertIn("not billable", str(caught.exception))

    def test_requires_an_energy_result(self):
        for bad in (None, 5, 1000.0):
            with self.assertRaises(TypeError):
                energy_cost(bad, 0.2, "EUR")


class TestImmutabilityAndDeterminism(unittest.TestCase):
    def test_results_are_immutable(self):
        result = energy_from_power(10, 10)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.quantity = 1.0
        cost = energy_cost(result, 1.0, "EUR")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cost.amount = 1.0

    def test_repeated_calculation_is_identical(self):
        self.assertEqual(energy_from_power(123.4, 567.8), energy_from_power(123.4, 567.8))
        self.assertEqual(battery_net_energy(91, 33, 28, 99), battery_net_energy(91, 33, 28, 99))
        energy = energy_from_power(250, 4000)
        self.assertEqual(energy_cost(energy, 0.17, "EUR"), energy_cost(energy, 0.17, "EUR"))

    def test_no_wall_clock_is_used(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("the wall clock was used")

        with mock.patch("time.time", forbidden), \
                mock.patch("time.time_ns", forbidden), \
                mock.patch("time.monotonic", forbidden), \
                mock.patch("time.perf_counter", forbidden), \
                mock.patch("time.sleep", forbidden):
            first = energy_from_power(100, 3600)
            second = battery_net_energy(100, 50, 28, 3600)
            average_power_w(second)
            energy_cost(first, 0.2, "EUR")
            battery_interval_from_telemetry(
                {"sim_time": 0.0, "battery": {"pct": 100.0}},
                {"sim_time": 10.0, "battery": {"pct": 99.0}},
                28,
            )
        self.assertEqual(first, energy_from_power(100, 3600))

    def test_module_imports_no_clock_random_network_storage_or_other_package(self):
        module = importlib.import_module("energy.energy")
        for banned in (
            "time", "datetime", "random", "socket", "threading", "asyncio", "sqlite3",
            "os", "json", "mock_rover", "storage", "record", "machine_state", "calibration",
        ):
            self.assertFalse(hasattr(module, banned), banned)


class TestMockRoverIntegration(unittest.TestCase):
    """Built on the existing Mock Rover battery model: sim_time and battery.pct only."""

    CAPACITY_WH = 28.0  # supplied by the test; the simulator has no capacity

    def interval(self, rover, action):
        before = rover.telemetry()
        action(rover)
        return battery_interval_from_telemetry(before, rover.telemetry(), self.CAPACITY_WH)

    def test_idle_interval(self):
        result = self.interval(make_rover(), lambda r: r.advance(3600.0))
        self.assertAlmostEqual(result.quantity, -0.01 * self.CAPACITY_WH, places=6)  # 1 %/h
        self.assertAlmostEqual(result.duration_s, 3600.0)
        self.assertEqual(result.kind, KIND_BATTERY_NET)

    def test_driving_interval(self):
        def drive(rover):
            rover.drive(1.0, 1.0)
            rover.advance(3600.0)

        result = self.interval(make_rover(), drive)
        self.assertAlmostEqual(result.quantity, -14.0, places=6)  # 50 %/h of 28 Wh
        self.assertAlmostEqual(average_power_w(result), -14.0, places=6)

    def test_driving_drains_more_energy_than_idling_over_the_same_time(self):
        idle = self.interval(make_rover(), lambda r: r.advance(600.0))

        def drive(rover):
            rover.drive(1.0, 1.0)
            rover.advance(600.0)

        driving = self.interval(make_rover(), drive)
        self.assertLess(driving.quantity, idle.quantity)

    def test_testing_activity_uses_its_own_drain_rate(self):
        def measure(rover):
            rover.insert_probe(depth_ok=True)
            rover.take_measurement()  # 1 simulated second of testing activity

        result = self.interval(make_rover(), measure)
        self.assertAlmostEqual(result.duration_s, 1.0)
        self.assertAlmostEqual(result.quantity, -(10.0 / 3600.0) / 100.0 * self.CAPACITY_WH, places=9)

    def test_solar_charging_gives_positive_net_energy(self):
        rover = make_rover(battery_start_pct=50.0)
        rover.set_solar(30.0)  # percent of capacity per hour
        result = self.interval(rover, lambda r: r.advance(3600.0))
        self.assertAlmostEqual(result.quantity, 0.29 * self.CAPACITY_WH, places=6)
        self.assertGreater(average_power_w(result), 0.0)

    def test_full_battery_cannot_charge_further(self):
        rover = make_rover()
        rover.set_solar(500.0)
        result = self.interval(rover, lambda r: r.advance(3600.0))
        self.assertEqual(result.quantity, 0.0)

    def test_zero_elapsed_simulated_time_is_zero_energy_and_unavailable_power(self):
        rover = make_rover()
        result = self.interval(rover, lambda r: None)
        self.assertEqual((result.quantity, result.duration_s), (0.0, 0.0))
        with self.assertRaises(ValueError):
            average_power_w(result)

    def test_same_scenario_gives_the_same_energy(self):
        def run():
            rover = make_rover(seed=3)
            return self.interval(rover, lambda r: (r.drive(1.0, 1.0), r.advance(120.0)))

        self.assertEqual(run(), run())

    def test_telemetry_snapshots_are_not_modified(self):
        rover = make_rover()
        before = rover.telemetry()
        rover.advance(60.0)
        after = rover.telemetry()
        snapshots = (copy.deepcopy(before), copy.deepcopy(after))
        battery_interval_from_telemetry(before, after, self.CAPACITY_WH)
        self.assertEqual((before, after), snapshots)

    def test_missing_or_invalid_snapshot_data_fails_clearly(self):
        good = {"sim_time": 1.0, "battery": {"pct": 50.0}}
        for bad in (
            {},
            {"sim_time": 1.0},
            {"battery": {"pct": 50.0}},
            {"sim_time": 1.0, "battery": {}},
            {"sim_time": 1.0, "battery": []},
            {"sim_time": "1", "battery": {"pct": 50.0}},
            {"sim_time": 1.0, "battery": {"pct": None}},
            {"sim_time": -1.0, "battery": {"pct": 50.0}},
        ):
            with self.assertRaises(ValueError, msg=bad):
                battery_interval_from_telemetry(bad, good, 28)
            with self.assertRaises(ValueError, msg=bad):
                battery_interval_from_telemetry(good, bad, 28)
        for bad in (None, [], "snapshot"):
            with self.assertRaises(TypeError):
                battery_interval_from_telemetry(bad, good, 28)

    def test_a_later_before_snapshot_is_rejected(self):
        early = {"sim_time": 1.0, "battery": {"pct": 50.0}}
        late = {"sim_time": 9.0, "battery": {"pct": 50.0}}
        with self.assertRaises(ValueError):
            battery_interval_from_telemetry(late, early, 28)

    def test_capacity_is_required_and_validated(self):
        snapshot = {"sim_time": 0.0, "battery": {"pct": 50.0}}
        with self.assertRaises(TypeError):
            battery_interval_from_telemetry(snapshot, snapshot)
        for bad in (0, -1, float("nan"), True, "28"):
            with self.assertRaises(ValueError, msg=bad):
                battery_interval_from_telemetry(snapshot, snapshot, bad)


if __name__ == "__main__":
    unittest.main()
