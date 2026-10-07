"""Configuration for the Nelavi mock rover simulator.

Every value here is a simulator setting, not a frozen requirement. Reference
figures from the physical model (speeds, track width, sensor range) are
defaults only and can be overridden per scenario. Several defaults are
placeholders chosen for software testing and are not claims about the real
rover (battery drain rates, soil field values, image fixtures, version
strings).
"""

from dataclasses import dataclass

# (fixture name, label, confidence). Placeholder dataset references; the
# labels other than "unclear" are test placeholders, not a class list.
DEFAULT_IMAGE_FIXTURES = (
    ("fixture-leaf-001", "mock_healthy", 92.0),
    ("fixture-leaf-002", "mock_disease", 81.5),
    ("fixture-leaf-003", "unclear", 58.0),
)

# (name, base value, gradient per metre in x, gradient per metre in y,
# noise amplitude). Synthetic values for software testing only.
DEFAULT_SOIL_FIELD = (
    ("nitrogen", 40.0, 2.0, -1.0, 1.5),
    ("phosphorus", 25.0, -0.5, 1.0, 1.0),
    ("potassium", 150.0, 3.0, 2.0, 5.0),
    ("moisture", 30.0, 0.5, -0.5, 1.0),
    ("temperature", 22.0, 0.1, 0.1, 0.3),
)


@dataclass(frozen=True)
class RoverConfig:
    # --- identity and determinism -------------------------------------
    seed: int = 0
    rover_id: str = "nelavi-mock-1"
    firmware_version: str = "mock-firmware"
    model_version: str = "mock-model"
    rules_version: str = "mock-rules"
    calibration_version: str = "mock-calibration"
    initial_sequence: int = 0
    time_step_s: float = 0.1  # largest internal simulation sub-step

    # --- movement (skid steering, simple 2D model) --------------------
    wheel_track_m: float = 0.270
    max_speed_mps: float = 0.56  # reference: unloaded top speed
    slip: float = 0.0  # fraction of speed lost, 0.0 to 1.0
    slip_jitter: float = 0.0  # deterministic variation of the slip factor

    # --- obstacle sensor (forward facing) -----------------------------
    obstacle_stop_distance_m: float = 0.25
    obstacle_stop_inclusive: bool = True  # simulator choice, see ARCHITECTURE_DECISIONS.md
    obstacle_min_range_m: float = 0.02  # reference range, not a requirement
    obstacle_max_range_m: float = 4.0  # reference range, not a requirement

    # --- camera --------------------------------------------------------
    pan_min_deg: float = 0.0
    pan_max_deg: float = 350.0
    pan_home_deg: float = 0.0  # simulator-defined home position
    tilt_min_deg: float = -60.0  # simulator convention: negative is down
    tilt_max_deg: float = 45.0
    tilt_initial_deg: float = 0.0
    pan_speed_deg_s: float = 30.0
    tilt_speed_deg_s: float = 30.0
    capture_duration_s: float = 2.0
    unclear_confidence: float = 55.0
    image_fixtures: tuple = DEFAULT_IMAGE_FIXTURES

    # --- soil probe, air and light -------------------------------------
    measurement_duration_s: float = 1.0
    soil_field: tuple = DEFAULT_SOIL_FIELD
    air_base: tuple = (25.0, 60.0, 1013.0)  # temperature, humidity, pressure
    air_noise: tuple = (0.5, 2.0, 1.0)
    light_base: float = 20000.0
    light_noise: float = 500.0

    # --- battery (3S reference range) ----------------------------------
    battery_start_pct: float = 100.0
    battery_max_pct: float = 100.0
    battery_min_voltage_v: float = 9.0
    battery_max_voltage_v: float = 12.6
    # percent of capacity per hour for (idle, driving, testing)
    drain_pct_per_hour: tuple = (1.0, 50.0, 10.0)
    solar_pct_per_hour: float = 0.0

    # --- connectivity ----------------------------------------------------
    link_delay_s: float = 0.0
    dropout_windows: tuple = ()  # (start_s, end_s) simulated-time windows
