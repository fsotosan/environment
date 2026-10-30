"""Deterministic synthetic readings for demos, tests and UI development.

Simulator mode is explicit (``sensors.mode: simulator``). The station never
silently substitutes simulated values for a failed hardware sensor.
"""

from __future__ import annotations

import math
import random

from envmon.config import Settings


class SimulatorSensor:
    name = "simulator"

    def __init__(self, settings: Settings) -> None:
        self.tz = settings.tzinfo
        self._random = random.Random(0xE7A)

    def read(self) -> dict[str, object]:
        import time

        ts = int(time.time())
        day_phase = 2 * math.pi * (ts % 86_400) / 86_400
        slow = math.sin(day_phase)
        wobble = math.sin(ts / 1_700)

        temperature = 22.4 + 1.1 * slow + 0.25 * wobble + self._random.gauss(0, 0.08)
        humidity = 48.0 - 4.5 * slow + self._random.gauss(0, 0.8)
        pm25 = max(1.0, 12.0 + 4.0 * math.sin(ts / 2_700) + self._random.gauss(0, 1.4))
        pm10 = max(pm25, 21.0 + 6.0 * math.sin(ts / 3_100) + self._random.gauss(0, 2.0))
        voc = max(1.0, 108.0 + 22.0 * math.sin(ts / 5_400) + self._random.gauss(0, 4.0))
        nox = max(1.0, 8.0 + 3.0 * math.sin(ts / 6_300) + self._random.gauss(0, 1.2))

        hour = int(time.strftime("%H", time.localtime(ts)))
        if 7 <= hour < 19:
            sunlight = math.sin(math.pi * (hour - 7) / 12)
            lux = max(30.0, 320.0 + 520.0 * sunlight + self._random.gauss(0, 25.0))
        else:
            lux = max(5.0, 25.0 + self._random.gauss(0, 6.0))

        # A short meeting/coffee-machine bump every ~20 minutes makes the demo
        # charts more interesting without looking like an alarm.
        bump = 7.0 if (ts // 60) % 20 in (0, 1) else 0.0
        noise = 41.0 + 3.0 * math.sin(ts / 900) + bump + self._random.gauss(0, 1.5)
        # Plausible interval statistics: the maximum is above L10, which is
        # above the median, which is above the background L90.
        noise_max = noise + 8.0 + self._random.random() * 4.0
        noise_l10 = noise + 3.0 + self._random.random() * 2.0
        noise_l50 = noise + self._random.gauss(0, 0.5)
        noise_l90 = noise - 3.0 - self._random.random() * 2.0

        return {
            "temperature_c": round(temperature, 2),
            "humidity_pct": round(max(1.0, min(99.0, humidity)), 2),
            "pm1_0_ugm3": round(pm25 * 0.72, 2),
            "pm2_5_ugm3": round(pm25, 2),
            "pm4_0_ugm3": round((pm25 + pm10) / 2, 2),
            "pm10_ugm3": round(pm10, 2),
            "voc_index": round(voc, 1),
            "nox_index": round(nox, 1),
            "lux": round(lux, 1),
            "noise_leq_db_a": round(noise, 1),
            "noise_max_db_a": round(noise_max, 1),
            "noise_l10_db_a": round(noise_l10, 1),
            "noise_l50_db_a": round(noise_l50, 1),
            "noise_l90_db_a": round(noise_l90, 1),
            "noise_peak_db": round(noise + 11.0 + self._random.random() * 5.0, 1),
            "noise_dbfs": round(-38.0 + self._random.gauss(0, 1.0), 1),
            "noise_sample_rate_hz": 2000,
            "noise_clipping_pct": 0.0,
        }

    def close(self) -> None:
        return None
