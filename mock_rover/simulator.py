"""Nelavi mock rover: deterministic simulator state, telemetry and Records.

Simulated time only (no wall clock, no sleeping, no network). Canonical
Records are built here but validated by the existing Record Core
(record.validation); this module defines no competing schema.

Nothing here is the real rover's safety logic or a protocol implementation.
The telemetry snapshot is simulator-internal, not a WebSocket message.
"""

import math
from dataclasses import dataclass

from mock_rover.battery import BatteryModel
from mock_rover.camera import CameraModel
from mock_rover.config import RoverConfig
from mock_rover.movement import (
    Pose,
    advance_pose,
    body_velocity,
    check_command,
    wheel_velocities,
)
from mock_rover.sensors import AirModel, SoilField, unit_noise
from record.validation import SENSOR_READINGS, STATUS_KEYS, validate_record

PROBE_RETRACTED = "retracted"
PROBE_OK = "inserted_depth_ok"
PROBE_BAD_DEPTH = "inserted_depth_not_ok"

FAULT_SENSORS = STATUS_KEYS + ("light",)  # light is telemetry only


class MockRoverError(Exception):
    pass


class ProbeNotReady(MockRoverError):
    pass


class NoMeasurementError(MockRoverError):
    pass


class RecordGenerationError(MockRoverError):
    def __init__(self, errors):
        super().__init__("generated record failed validation: %r" % (errors,))
        self.errors = errors


@dataclass(frozen=True)
class Outgoing:
    deliver_at: float
    payload: dict


class Link:
    """Connected/disconnected state with a delay and scheduled dropouts."""

    def __init__(self, config):
        self.delay_s = config.link_delay_s
        self.windows = config.dropout_windows
        self.manually_connected = True

    def is_connected(self, sim_time):
        if not self.manually_connected:
            return False
        return not any(start <= sim_time < end for start, end in self.windows)


class MockRover:
    def __init__(self, config=None):
        self.config = config if config is not None else RoverConfig()
        c = self.config
        self.sim_time = 0.0
        self._step = 0
        self._sequence = c.initial_sequence
        self.started = False

        self.pose = Pose()
        self.left_cmd = 0.0
        self.right_cmd = 0.0
        self.speed = 0.0
        self.turn_rate = 0.0
        self.forward_inhibited = False
        self.obstacle_distance_m = None

        self.camera = CameraModel(c)
        self.battery = BatteryModel(c)
        self._soil = SoilField(c)
        self._air = AirModel(c)
        self.probe_state = PROBE_RETRACTED
        self.faults = set()
        self.last_measurement = None
        self.last_capture = None

        self.link = Link(c)
        self.outbox = []
        self.dropped = 0

    # ------------------------------------------------------------ time
    def advance(self, dt, activity=None):
        """Advance simulated time by dt seconds with the current commands."""
        if isinstance(dt, bool) or not isinstance(dt, (int, float)):
            raise ValueError("dt must be a number")
        if not math.isfinite(dt) or dt < 0:
            raise ValueError("dt must be finite and not negative")
        if dt == 0:
            return
        c = self.config
        count = max(1, math.ceil(dt / c.time_step_s))
        sub_dt = dt / count
        start = self.sim_time
        for _ in range(count):
            self._advance_step(sub_dt, activity)
        # Set the time once, so repeated sub-steps cannot accumulate float drift.
        self.sim_time = start + dt

    def _advance_step(self, dt, activity):
        c = self.config
        slip_noise = unit_noise(c.seed, "slip", self._step)
        left, right = wheel_velocities(c, self.left_cmd, self.right_cmd, slip_noise)
        speed, turn_rate = body_velocity(c, left, right)
        self.forward_inhibited = False
        if speed > 0 and self.obstacle_detected:
            speed = 0.0  # simulator-only: the forward-facing sensor inhibits forward motion
            self.forward_inhibited = True
        advance_pose(self.pose, speed, turn_rate, dt)
        if self.obstacle_distance_m is not None:
            self.obstacle_distance_m = max(0.0, self.obstacle_distance_m - speed * dt)
        self.speed = speed
        self.turn_rate = turn_rate
        mode = activity or ("driving" if (speed != 0.0 or turn_rate != 0.0) else "idle")
        self.battery.step(dt, mode)
        self._step += 1

    # -------------------------------------------------------- lifecycle
    def startup(self):
        """Start the simulated rover and home the camera pan axis."""
        self.started = True
        return self.home_camera()

    def shutdown(self):
        self.stop()
        self.camera.set_led(False)
        self.probe_state = PROBE_RETRACTED
        self.started = False

    # --------------------------------------------------------- movement
    def drive(self, left, right):
        """Set wheel commands from -1.0 (reverse) to 1.0 (forward)."""
        self.left_cmd = check_command(left)
        self.right_cmd = check_command(right)

    def stop(self):
        self.left_cmd = 0.0
        self.right_cmd = 0.0
        self.speed = 0.0
        self.turn_rate = 0.0

    @property
    def running(self):
        """True while the rover is moving or turning, False when stopped."""
        return self.speed != 0.0 or self.turn_rate != 0.0

    # --------------------------------------------------------- obstacle
    def set_obstacle(self, distance_m):
        """Set the distance ahead in metres, or None for no obstacle."""
        if distance_m is not None:
            if isinstance(distance_m, bool) or not isinstance(distance_m, (int, float)):
                raise ValueError("distance must be a number or None")
            if not math.isfinite(distance_m) or distance_m < 0:
                raise ValueError("distance must be finite and not negative")
            distance_m = float(distance_m)
        self.obstacle_distance_m = distance_m

    @property
    def obstacle_detected(self):
        d = self.obstacle_distance_m
        if d is None:
            return False
        threshold = self.config.obstacle_stop_distance_m
        if self.config.obstacle_stop_inclusive:
            return d <= threshold
        return d < threshold

    def obstacle_reading(self):
        c = self.config
        if self.obstacle_distance_m is None:
            return c.obstacle_max_range_m
        return min(max(self.obstacle_distance_m, c.obstacle_min_range_m), c.obstacle_max_range_m)

    # ----------------------------------------------------------- camera
    def home_camera(self):
        duration = self.camera.home()
        self.advance(duration, "testing")
        return duration

    def move_camera(self, pan=None, tilt=None):
        duration = self.camera.move_to(pan_deg=pan, tilt_deg=tilt)
        self.advance(duration, "testing")
        return duration

    def set_led(self, on):
        self.camera.set_led(on)

    def set_force_unclear(self, on):
        self.camera.force_unclear = bool(on)

    def capture_image(self):
        self.advance(self.config.capture_duration_s, "testing")
        self.last_capture = self.camera.capture(self.sim_time)
        return self.last_capture

    def clear_capture(self):
        self.last_capture = None

    # ------------------------------------------------------------ probe
    def insert_probe(self, depth_ok=True):
        """Operator inserts the probe. depth_ok=False is a failed insertion."""
        self.probe_state = PROBE_OK if depth_ok else PROBE_BAD_DEPTH
        return self.probe_state == PROBE_OK

    def retract_probe(self):
        self.probe_state = PROBE_RETRACTED

    # ----------------------------------------------------------- faults
    def inject_fault(self, sensor):
        if sensor not in FAULT_SENSORS:
            raise ValueError("unknown sensor: %r" % (sensor,))
        self.faults.add(sensor)

    def clear_fault(self, sensor):
        if sensor not in FAULT_SENSORS:
            raise ValueError("unknown sensor: %r" % (sensor,))
        self.faults.discard(sensor)

    # --------------------------------------------------------- readings
    def read_air(self):
        values = self._air.air(self.sim_time)
        if "air" in self.faults:
            return {name: None for name in values}
        return values

    def read_light(self):
        if "light" in self.faults:
            return None
        return self._air.light(self.sim_time)

    def set_solar(self, pct_per_hour):
        self.battery.set_solar(pct_per_hour)

    def take_measurement(self):
        """Read soil, air and battery. Requires the probe inserted at depth."""
        if self.probe_state != PROBE_OK:
            raise ProbeNotReady("probe state is %r; insert it with depth OK first" % self.probe_state)
        self.advance(self.config.measurement_duration_s, "testing")
        sections = {
            "soil": self._soil.sample(self.pose.x, self.pose.y),
            "air": self._air.air(self.sim_time),
            "battery": {"voltage_v": self.battery.voltage_v},
        }
        status = {}
        for sensor in STATUS_KEYS:
            faulty = sensor in self.faults
            status[sensor] = "fault" if faulty else "ok"
            if faulty:
                for section, key in SENSOR_READINGS[sensor]:
                    sections[section][key] = None
        self.last_measurement = {
            "sim_time": self.sim_time,
            "soil": sections["soil"],
            "air": sections["air"],
            "battery": sections["battery"],
            "sensor_status": status,
            "light": self.read_light(),  # telemetry only; not part of the Record Core
        }
        return self.last_measurement

    # ------------------------------------------------------- telemetry
    def telemetry(self):
        """A simulator-internal snapshot of the rover state."""
        status = {s: ("fault" if s in self.faults else "ok") for s in FAULT_SENSORS}
        return {
            "sim_time": self.sim_time,
            "started": self.started,
            "running": self.running,
            "pose": {"x": self.pose.x, "y": self.pose.y, "heading": self.pose.heading},
            "speed_mps": self.speed,
            "turn_rate_rad_s": self.turn_rate,
            "commands": {"left": self.left_cmd, "right": self.right_cmd},
            "obstacle": {
                "distance_m": self.obstacle_reading(),
                "detected": self.obstacle_detected,
                "forward_inhibited": self.forward_inhibited,
            },
            "camera": {
                "pan_deg": self.camera.pan_deg,
                "tilt_deg": self.camera.tilt_deg,
                "homed": self.camera.homed,
                "led_on": self.camera.led_on,
                "last_move_duration_s": self.camera.last_move_duration_s,
            },
            "probe_state": self.probe_state,
            "air": self.read_air(),
            "light": self.read_light(),
            "battery": {
                "pct": self.battery.pct,
                "voltage_v": self.battery.voltage_v,
                "charging": self.battery.charging,
                "mode": self.battery.mode,
            },
            "sensor_status": status,
            "link": {
                "connected": self.link.is_connected(self.sim_time),
                "delay_s": self.link.delay_s,
            },
        }

    # ------------------------------------------------------ connectivity
    def disconnect(self):
        self.link.manually_connected = False

    def reconnect(self):
        self.link.manually_connected = True

    def transmit_telemetry(self):
        """Queue a telemetry snapshot; it is dropped while the link is down."""
        if not self.link.is_connected(self.sim_time):
            self.dropped += 1
            return None
        outgoing = Outgoing(self.sim_time + self.link.delay_s, self.telemetry())
        self.outbox.append(outgoing)
        return outgoing

    def deliver_due(self):
        """Return and remove payloads whose delivery time has arrived."""
        due = [o for o in self.outbox if o.deliver_at <= self.sim_time]
        self.outbox = [o for o in self.outbox if o.deliver_at > self.sim_time]
        return [o.payload for o in due]

    # ----------------------------------------------------------- records
    def build_record(self, use_capture=True):
        """Build a canonical Record, validated by the Record Core."""
        m = self.last_measurement
        if m is None:
            raise NoMeasurementError("take a measurement before building a record")
        c = self.config
        capture = self.last_capture if use_capture else None
        record = {
            "time": "sim:%.3f" % self.sim_time,
            "sequence_number": self._sequence,
            "rover_id": c.rover_id,
            "battery": dict(m["battery"]),
            "soil": dict(m["soil"]),
            "air": dict(m["air"]),
            "sensor_status": dict(m["sensor_status"]),
            "leaf_label": capture.label if capture else None,
            "confidence": capture.confidence if capture else None,
            "photo_id": capture.photo_id if capture else None,
            "versions": {
                "firmware": c.firmware_version,
                "model": c.model_version,
                "rules": c.rules_version,
                "calibration": c.calibration_version,
            },
        }
        result = validate_record(record)
        if not result.valid:
            raise RecordGenerationError(result.errors)
        self._sequence += 1
        return result.record
