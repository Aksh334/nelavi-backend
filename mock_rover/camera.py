"""Simulated camera arm: pan, tilt, home, LED and dataset-backed captures.

The physical home direction is not specified by the frozen sources, so the
simulator uses its own configurable home angle. Tilt is an abstract angle with
a configurable range (simulator convention: negative is down). Captures return
fixture references; no real image is produced.
"""

from dataclasses import dataclass


class CameraNotHomed(Exception):
    pass


@dataclass(frozen=True)
class ImageCapture:
    photo_id: str
    fixture: str
    label: str
    confidence: float
    led_on: bool
    pan_deg: float
    tilt_deg: float
    sim_time: float


class CameraModel:
    def __init__(self, config):
        self._config = config
        self.pan_deg = None  # unknown until homed
        self.tilt_deg = config.tilt_initial_deg
        self.homed = False
        self.led_on = False
        self.last_move_duration_s = 0.0
        self.capture_count = 0
        self.force_unclear = False

    def home(self):
        """Home the pan axis. Returns the simulated duration in seconds."""
        c = self._config
        if self.pan_deg is None:
            distance = c.pan_max_deg - c.pan_min_deg  # worst-case sweep
        else:
            distance = abs(self.pan_deg - c.pan_home_deg)
        self.pan_deg = c.pan_home_deg
        self.homed = True
        self.last_move_duration_s = distance / c.pan_speed_deg_s
        return self.last_move_duration_s

    def move_to(self, pan_deg=None, tilt_deg=None):
        """Move pan and/or tilt together. Returns the simulated duration."""
        c = self._config
        durations = [0.0]
        if pan_deg is not None:
            if not self.homed:
                raise CameraNotHomed("the pan axis must be homed before it moves")
            if not c.pan_min_deg <= pan_deg <= c.pan_max_deg:
                raise ValueError("pan angle outside the configured range")
            durations.append(abs(pan_deg - self.pan_deg) / c.pan_speed_deg_s)
        if tilt_deg is not None:
            if not c.tilt_min_deg <= tilt_deg <= c.tilt_max_deg:
                raise ValueError("tilt angle outside the configured range")
            durations.append(abs(tilt_deg - self.tilt_deg) / c.tilt_speed_deg_s)
        if pan_deg is not None:
            self.pan_deg = float(pan_deg)
        if tilt_deg is not None:
            self.tilt_deg = float(tilt_deg)
        self.last_move_duration_s = max(durations)
        return self.last_move_duration_s

    def set_led(self, on):
        self.led_on = bool(on)

    def capture(self, sim_time):
        c = self._config
        index = self.capture_count
        fixture, label, confidence = c.image_fixtures[index % len(c.image_fixtures)]
        if self.force_unclear:
            label, confidence = "unclear", c.unclear_confidence
        self.capture_count += 1
        return ImageCapture(
            photo_id="mock-photo-%d-%04d" % (c.seed, index + 1),
            fixture=fixture,
            label=label,
            confidence=confidence,
            led_on=self.led_on,
            pan_deg=self.pan_deg,
            tilt_deg=self.tilt_deg,
            sim_time=sim_time,
        )
