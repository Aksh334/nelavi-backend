"""Simple 2D skid-steering movement model.

Heading is in radians, counter-clockwise positive, 0 along +x. There are no
motor curves, wheel/soil contact or terrain; slip is a deterministic factor.
"""

import math
from dataclasses import dataclass


@dataclass
class Pose:
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0


def check_command(value):
    """Wheel commands are numbers from -1.0 (full reverse) to 1.0 (full forward)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("wheel command must be a number")
    if not math.isfinite(value) or not -1.0 <= value <= 1.0:
        raise ValueError("wheel command must be between -1.0 and 1.0")
    return float(value)


def normalize_angle(angle):
    return (angle + math.pi) % (2 * math.pi) - math.pi


def wheel_velocities(config, left_cmd, right_cmd, slip_noise):
    """Return (left, right) wheel speeds in m/s after slip."""
    factor = (1.0 - config.slip) * (1.0 + config.slip_jitter * slip_noise)
    return (
        left_cmd * config.max_speed_mps * factor,
        right_cmd * config.max_speed_mps * factor,
    )


def body_velocity(config, left_speed, right_speed):
    """Return (forward speed in m/s, turn rate in rad/s)."""
    return (
        (left_speed + right_speed) / 2.0,
        (right_speed - left_speed) / config.wheel_track_m,
    )


def advance_pose(pose, speed, turn_rate, dt):
    middle = pose.heading + turn_rate * dt / 2.0
    pose.x += speed * math.cos(middle) * dt
    pose.y += speed * math.sin(middle) * dt
    pose.heading = normalize_angle(pose.heading + turn_rate * dt)
