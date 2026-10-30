"""Sensor manager: runs each sensor, isolates failures and tracks health."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable

from envmon.config import Settings
from envmon.sensors.base import Reading, Sensor, SensorStatus, now_ts
from envmon.sensors.noise import NoiseSensor
from envmon.sensors.sen5x import Sen5xSensor
from envmon.sensors.simulator import SimulatorSensor
from envmon.sensors.tsl2561 import Tsl2561Sensor

logger = logging.getLogger(__name__)


class BrokenSensor:
    """Placeholder for a configured sensor that could not be constructed."""

    def __init__(self, name: str, error: str) -> None:
        self.name = name
        self.error = error

    def read(self) -> dict[str, object]:
        raise RuntimeError(self.error)

    def close(self) -> None:
        return None


def build_sensors(settings: Settings) -> list[Sensor]:
    sensors_config = settings.sensors
    if sensors_config.mode == "simulator":
        return [SimulatorSensor(settings)]

    sensors: list[Sensor] = []
    if sensors_config.sen5x.enabled:
        sensors.append(
            Sen5xSensor(sensors_config.i2c_bus, sensors_config.sen5x),
        )
    if sensors_config.light.enabled:
        sensors.append(
            Tsl2561Sensor(sensors_config.i2c_bus, sensors_config.light),
        )
    if sensors_config.noise.enabled:
        sensors.append(
            NoiseSensor(sensors_config.i2c_bus, sensors_config.noise),
        )
    return sensors


class SensorManager:
    def __init__(self, settings: Settings, sensors: Iterable[Sensor] | None = None) -> None:
        self.settings = settings
        self.mode = settings.sensors.mode
        self.sensors = list(sensors) if sensors is not None else build_sensors(settings)
        self.statuses = {sensor.name: SensorStatus(name=sensor.name) for sensor in self.sensors}
        self.last_read_ts: int | None = None
        self.last_read_duration_s: float | None = None
        self._lock = threading.Lock()

    def read(self) -> Reading:
        started = time.perf_counter()
        values: dict[str, object] = {}
        errors: list[str] = []

        with self._lock:
            if not self.sensors:
                errors.append("no sensors are enabled")
            for sensor in self.sensors:
                status = self.statuses[sensor.name]
                try:
                    result = sensor.read() or {}
                except Exception as exc:
                    message = str(exc).strip() or exc.__class__.__name__
                    text = f"{sensor.name}: {message}"
                    errors.append(text[:300])
                    status.ok = False
                    status.last_error = message[:300]
                    status.consecutive_errors += 1
                    status.total_errors += 1
                    logger.warning("sensor %s failed: %s", sensor.name, message)
                    try:
                        sensor.close()
                    except Exception:  # pragma: no cover - cleanup only
                        logger.debug("error closing %s", sensor.name, exc_info=True)
                    continue

                values.update(result)
                status.ok = True
                status.last_error = None
                status.consecutive_errors = 0
                status.last_ok_ts = now_ts()

            self.last_read_ts = now_ts()
            self.last_read_duration_s = round(time.perf_counter() - started, 3)

        return Reading(ts=self.last_read_ts, mode=self.mode, values=values, errors=errors)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "mode": self.mode,
                "last_read_ts": self.last_read_ts,
                "last_read_duration_s": self.last_read_duration_s,
                "sensors": [self.statuses[name].as_dict() for name in sorted(self.statuses)],
            }

    def close(self) -> None:
        with self._lock:
            for sensor in self.sensors:
                try:
                    sensor.close()
                except Exception:  # pragma: no cover - cleanup only
                    logger.debug("error closing %s", sensor.name, exc_info=True)
