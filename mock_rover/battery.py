"""Simple battery model: a percentage, a linear voltage, and a net charge rate.

No electrical chemistry is modelled and the numbers do not predict real
battery behaviour.
"""

MODES = ("idle", "driving", "testing")


class BatteryModel:
    def __init__(self, config):
        self._config = config
        self.pct = min(config.battery_start_pct, config.battery_max_pct)
        self.solar_pct_per_hour = config.solar_pct_per_hour
        self.mode = "idle"
        self.charging = False

    @property
    def voltage_v(self):
        c = self._config
        span = c.battery_max_voltage_v - c.battery_min_voltage_v
        return c.battery_min_voltage_v + span * (self.pct / 100.0)

    def drain_rate(self, mode):
        if mode not in MODES:
            raise ValueError("unknown battery mode: %r" % (mode,))
        return self._config.drain_pct_per_hour[MODES.index(mode)]

    def set_solar(self, pct_per_hour):
        if pct_per_hour < 0:
            raise ValueError("solar input must not be negative")
        self.solar_pct_per_hour = float(pct_per_hour)

    def step(self, dt_s, mode):
        drain = self.drain_rate(mode)
        net = self.solar_pct_per_hour - drain
        self.pct += net * dt_s / 3600.0
        self.pct = max(0.0, min(self.pct, self._config.battery_max_pct))
        self.mode = mode
        self.charging = net > 0 and self.pct < self._config.battery_max_pct
