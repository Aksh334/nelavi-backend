"""Nelavi energy monitoring: a few explicit, unit-labelled calculations.

Pure and deterministic: no wall clock, no randomness, no network, no database,
no hardware. Elapsed time is only ever a supplied duration or the Mock Rover's
simulated `sim_time`. Nothing here supplies electrical or production data.

Two different things are kept apart on purpose:

* electrical energy from supplied power (`energy_from_power`): the caller
  supplies a real or assumed power in watts and a duration. Nothing in the
  current project measures power, so no source feeds this yet.
* rover battery NET energy change (`battery_net_energy`): an estimate from the
  Mock Rover's battery percentage and a caller-supplied capacity. It is net of
  any solar charging, it is rover battery telemetry, and it is NOT machine
  electrical consumption and NOT billable energy.

Units: power W, duration s, energy Wh, tariff currency per kWh. The only
constants are the unit conversions 3600 s per hour and 1000 Wh per kWh. There
is no default capacity, tariff or currency.

Deferred (not implemented): baselines and deviation, peak demand, energy per
unit or per kg, utilization, idle and startup energy, tariff schedules.
"""

import math
from dataclasses import dataclass

SECONDS_PER_HOUR = 3600.0
WH_PER_KWH = 1000.0

UNIT_ENERGY = "Wh"
KIND_ELECTRICAL = "electrical_energy_from_supplied_power"
KIND_BATTERY_NET = "rover_battery_net_energy_estimate"


@dataclass(frozen=True)
class EnergyResult:
    quantity: float  # in `unit`; for KIND_BATTERY_NET positive = net charge
    unit: str
    duration_s: float
    kind: str
    source: str  # the evidence the quantity was computed from
    assumptions: tuple  # human-readable assumptions behind the number


@dataclass(frozen=True)
class CostResult:
    amount: float
    currency: str
    energy_kwh: float
    tariff_per_kwh: float
    assumptions: tuple


def _number(name, value, minimum=None, maximum=None, exclusive_minimum=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % name)
    if not math.isfinite(value):
        raise ValueError("%s must be finite" % name)
    if minimum is not None:
        too_low = value <= minimum if exclusive_minimum else value < minimum
        if too_low:
            raise ValueError("%s is below the allowed minimum" % name)
    if maximum is not None and value > maximum:
        raise ValueError("%s is above the allowed maximum" % name)
    return float(value)


def energy_from_power(power_w, duration_s):
    """Energy in Wh from a supplied constant power in W over a duration in s.

    energy_wh = power_w * duration_s / 3600. Power must not be negative:
    generation and export are not modelled.
    """
    power = _number("power_w", power_w, minimum=0.0)
    duration = _number("duration_s", duration_s, minimum=0.0)
    return EnergyResult(
        quantity=power * duration / SECONDS_PER_HOUR,
        unit=UNIT_ENERGY,
        duration_s=duration,
        kind=KIND_ELECTRICAL,
        source="power_w and duration_s supplied by the caller",
        assumptions=(
            "power is constant over the whole duration",
            "power_w is supplied by the caller; no measurement is made here",
        ),
    )


def battery_net_energy(pct_start, pct_end, capacity_wh, duration_s):
    """Net change of rover battery stored energy in Wh (positive = net charge).

    quantity = (pct_end - pct_start) / 100 * capacity_wh. This is an estimate:
    energy is taken as linear in the reported charge percentage, the capacity is
    supplied by the caller, and the result is net of any solar charging.
    """
    start = _number("pct_start", pct_start, minimum=0.0, maximum=100.0)
    end = _number("pct_end", pct_end, minimum=0.0, maximum=100.0)
    capacity = _number("capacity_wh", capacity_wh, minimum=0.0, exclusive_minimum=True)
    duration = _number("duration_s", duration_s, minimum=0.0)
    return EnergyResult(
        quantity=(end - start) / 100.0 * capacity,
        unit=UNIT_ENERGY,
        duration_s=duration,
        kind=KIND_BATTERY_NET,
        source="rover battery charge percentages (Mock Rover battery telemetry)",
        assumptions=(
            "stored energy is linear in the reported charge percentage",
            "capacity_wh is supplied by the caller (%s Wh)" % capacity,
            "the quantity is NET of any charging; it is not gross consumption",
            "rover battery energy is not machine electrical consumption",
        ),
    )


def average_power_w(result):
    """Average power in W: result.quantity (Wh) over result.duration_s.

    The duration must be greater than zero; there is no silent zero. For a
    KIND_BATTERY_NET result the value is the average NET power (negative while
    the battery discharges).
    """
    if not isinstance(result, EnergyResult):
        raise TypeError("result must be an EnergyResult")
    if result.duration_s <= 0:
        raise ValueError("average power is unavailable for a zero duration")
    return result.quantity / (result.duration_s / SECONDS_PER_HOUR)


def energy_cost(result, tariff_per_kwh, currency):
    """Cost of electrical energy: kWh x tariff.

    The tariff (currency per kWh) and the currency are always supplied by the
    caller; there is no default. Only KIND_ELECTRICAL results are accepted:
    rover battery net energy is not billable consumption.
    """
    if not isinstance(result, EnergyResult):
        raise TypeError("result must be an EnergyResult")
    if result.kind != KIND_ELECTRICAL:
        raise ValueError(
            "cost is defined only for electrical energy from supplied power; "
            "rover battery net energy is not billable consumption"
        )
    tariff = _number("tariff_per_kwh", tariff_per_kwh, minimum=0.0)
    if not isinstance(currency, str) or currency == "":
        raise ValueError("currency must be a non-empty string")
    energy_kwh = result.quantity / WH_PER_KWH
    return CostResult(
        amount=energy_kwh * tariff,
        currency=currency,
        energy_kwh=energy_kwh,
        tariff_per_kwh=tariff,
        assumptions=(
            "a single flat tariff applies to the whole energy",
            "the tariff and currency are supplied by the caller",
        ),
    )


def battery_interval_from_telemetry(before, after, capacity_wh):
    """Net battery energy change between two Mock Rover telemetry snapshots.

    Uses only `sim_time` and `battery.pct` from each snapshot. The duration is
    the difference of the simulated times, so no wall clock is involved. The
    snapshots are not modified.
    """
    start_time, start_pct = _snapshot_fields(before, "before")
    end_time, end_pct = _snapshot_fields(after, "after")
    if end_time < start_time:
        raise ValueError("the 'after' snapshot is earlier than the 'before' snapshot")
    return battery_net_energy(start_pct, end_pct, capacity_wh, end_time - start_time)


def _snapshot_fields(snapshot, label):
    if not isinstance(snapshot, dict):
        raise TypeError("%s snapshot must be a dict" % label)
    battery = snapshot.get("battery")
    if not isinstance(battery, dict):
        raise ValueError("%s snapshot has no battery section" % label)
    for key, section in (("sim_time", snapshot), ("pct", battery)):
        if key not in section:
            raise ValueError("%s snapshot is missing %s" % (label, key))
    return (
        _number("%s sim_time" % label, snapshot["sim_time"], minimum=0.0),
        _number("%s battery pct" % label, battery["pct"]),
    )
