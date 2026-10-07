"""Small deterministic scenarios for the Nelavi mock rover.

A scenario is a tuple of (method name, keyword arguments) steps run against a
MockRover. Only the whitelisted methods below can be called.
"""

ALLOWED_STEPS = (
    "startup",
    "shutdown",
    "drive",
    "stop",
    "advance",
    "set_obstacle",
    "home_camera",
    "move_camera",
    "set_led",
    "set_force_unclear",
    "capture_image",
    "insert_probe",
    "retract_probe",
    "take_measurement",
    "inject_fault",
    "clear_fault",
    "disconnect",
    "reconnect",
    "transmit_telemetry",
    "build_record",
    "set_solar",
)

SCENARIOS = {
    "startup": (
        ("startup", {}),
        ("advance", {"dt": 1.0}),
    ),
    "driving": (
        ("startup", {}),
        ("drive", {"left": 1.0, "right": 1.0}),
        ("advance", {"dt": 4.0}),
        ("drive", {"left": -1.0, "right": -1.0}),
        ("advance", {"dt": 2.0}),
        ("drive", {"left": -1.0, "right": 1.0}),
        ("advance", {"dt": 1.0}),
        ("stop", {}),
    ),
    "obstacle": (
        ("startup", {}),
        ("set_obstacle", {"distance_m": 1.0}),
        ("drive", {"left": 1.0, "right": 1.0}),
        ("advance", {"dt": 10.0}),
        ("stop", {}),
    ),
    "probe_measurement": (
        ("startup", {}),
        ("insert_probe", {"depth_ok": True}),
        ("take_measurement", {}),
        ("build_record", {}),
    ),
    "sensor_fault": (
        ("startup", {}),
        ("inject_fault", {"sensor": "moisture"}),
        ("insert_probe", {"depth_ok": True}),
        ("take_measurement", {}),
        ("build_record", {}),
    ),
    "communication_dropout": (
        ("startup", {}),
        ("transmit_telemetry", {}),
        ("disconnect", {}),
        ("advance", {"dt": 1.0}),
        ("transmit_telemetry", {}),
        ("reconnect", {}),
        ("transmit_telemetry", {}),
    ),
    "camera_capture": (
        ("startup", {}),
        ("set_led", {"on": True}),
        ("move_camera", {"pan": 90.0, "tilt": -10.0}),
        ("capture_image", {}),
        ("set_led", {"on": False}),
    ),
    "normal_operation": (
        ("startup", {}),
        ("drive", {"left": 1.0, "right": 1.0}),
        ("advance", {"dt": 10.0}),
        ("stop", {}),
        ("insert_probe", {"depth_ok": True}),
        ("take_measurement", {}),
        ("set_led", {"on": True}),
        ("capture_image", {}),
        ("build_record", {}),
        ("set_led", {"on": False}),
        ("retract_probe", {}),
    ),
    "shutdown": (
        ("startup", {}),
        ("drive", {"left": 1.0, "right": 1.0}),
        ("advance", {"dt": 1.0}),
        ("shutdown", {}),
    ),
}


def get_scenario(name):
    if name not in SCENARIOS:
        raise ValueError("unknown scenario: %r" % (name,))
    return SCENARIOS[name]


def run_scenario(rover, steps):
    """Run steps against a rover. Returns a list of (step name, result)."""
    results = []
    for name, kwargs in steps:
        if name not in ALLOWED_STEPS:
            raise ValueError("step not allowed in a scenario: %r" % (name,))
        results.append((name, getattr(rover, name)(**kwargs)))
    return results
