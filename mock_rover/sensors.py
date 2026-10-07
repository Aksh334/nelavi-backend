"""Deterministic synthetic soil, air and light models.

Noise comes from a hash of (seed, name, coordinates), never from a stateful
random generator, so the same inputs always give the same readings regardless
of call order. The values are for software testing only and are not claimed
to be physically or agriculturally accurate.
"""

import hashlib

AIR_NAMES = ("temperature", "humidity", "pressure")


def unit_noise(seed, *keys):
    """Return a deterministic number in [-1, 1) for the given seed and keys."""
    digest = hashlib.sha256(repr((seed,) + keys).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**63 - 1.0


class SoilField:
    """Soil values that vary with rover position plus deterministic noise."""

    def __init__(self, config):
        self._seed = config.seed
        self._params = config.soil_field

    def sample(self, x, y):
        ix, iy = round(x * 1000), round(y * 1000)
        values = {}
        for name, base, gx, gy, amplitude in self._params:
            noise = unit_noise(self._seed, "soil", name, ix, iy)
            values[name] = base + gx * x + gy * y + amplitude * noise
        return values


class AirModel:
    """Air (BME280-like) and light (BH1750-like) values that change slowly with
    simulated time."""

    def __init__(self, config):
        self._seed = config.seed
        self._base = config.air_base
        self._noise = config.air_noise
        self._light_base = config.light_base
        self._light_noise = config.light_noise

    def air(self, sim_time):
        bucket = int(sim_time // 60)
        return {
            name: base + amplitude * unit_noise(self._seed, "air", name, bucket)
            for name, base, amplitude in zip(AIR_NAMES, self._base, self._noise)
        }

    def light(self, sim_time):
        bucket = int(sim_time // 60)
        return self._light_base + self._light_noise * unit_noise(
            self._seed, "light", bucket
        )
