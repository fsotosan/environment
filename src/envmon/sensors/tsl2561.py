"""Grove Digital Light Sensor (TSL2561, I2C address 0x29 by default)."""

from __future__ import annotations

import logging
import time

from envmon.config import LightSettings

logger = logging.getLogger(__name__)

_COMMAND = 0x80
_CONTROL = 0x00
_TIMING = 0x01
_DATA0 = 0x0C
_DATA1 = 0x0E

_INTEGRATION_CODE = {14: 0, 101: 1, 402: 2}
_INTEGRATION_MS = {0: 13.7, 1: 101.0, 2: 402.0}


class Tsl2561Sensor:
    name = "tsl2561"

    def __init__(self, bus_number: int, settings: LightSettings) -> None:
        self.bus_number = bus_number
        self.settings = settings
        self._bus = None
        self._gain = 16.0 if settings.gain == 16 else 1.0
        self._integration_ms = _INTEGRATION_MS[_INTEGRATION_CODE[settings.integration_time_ms]]

    def _ensure_bus(self):
        if self._bus is not None:
            return self._bus
        from smbus2 import SMBus

        bus = SMBus(self.bus_number)
        try:
            bus.write_byte(self.settings.address, _COMMAND | _CONTROL)
            bus.write_byte(self.settings.address, 0x03)  # power on
            timing = _INTEGRATION_CODE[self.settings.integration_time_ms]
            if self.settings.gain == 16:
                timing |= 0x10
            bus.write_byte(self.settings.address, _COMMAND | _TIMING)
            bus.write_byte(self.settings.address, timing)
            # Let the first conversion complete before reading.
            time.sleep(min(0.5, self._integration_ms / 1000.0 + 0.05))
        except Exception:
            bus.close()
            raise
        self._bus = bus
        logger.info(
            "TSL2561 ready (gain=%sx, integration=%sms)",
            self.settings.gain,
            self.settings.integration_time_ms,
        )
        return bus

    def _read_word(self, register: int) -> int:
        bus = self._ensure_bus()
        data = bus.read_i2c_block_data(self.settings.address, _COMMAND | register, 2)
        return int(data[0]) | (int(data[1]) << 8)

    @staticmethod
    def calculate_lux(channel0: int, channel1: int) -> float:
        """Datasheet formula for the T/CS package (visible + IR channels)."""
        if channel0 <= 0 or channel1 <= 0:
            return 0.0
        ratio = channel1 / channel0
        if ratio <= 0.50:
            lux = 0.0304 * channel0 - 0.062 * channel0 * (ratio**1.4)
        elif ratio <= 0.61:
            lux = 0.0224 * channel0 - 0.031 * channel1
        elif ratio <= 0.80:
            lux = 0.0128 * channel0 - 0.0153 * channel1
        elif ratio <= 1.30:
            lux = 0.00146 * channel0 - 0.00112 * channel1
        else:
            lux = 0.0
        return max(0.0, float(lux))

    def read(self) -> dict[str, object]:
        try:
            channel0 = min(self._read_word(_DATA0), 0xFFFF)
            channel1 = min(self._read_word(_DATA1), 0xFFFF)
        except Exception:
            self.close()
            raise

        # Normalise to the datasheet's 402 ms / 1x reference before applying
        # the piecewise formula, otherwise 16x gain over-reports by 16x.
        scale = (402.0 / self._integration_ms) / self._gain
        normalised0 = channel0 * scale
        normalised1 = channel1 * scale
        return {"lux": round(self.calculate_lux(normalised0, normalised1), 1)}

    def close(self) -> None:
        if self._bus is not None:
            try:
                try:
                    self._bus.write_byte(self.settings.address, _COMMAND | _CONTROL)
                    self._bus.write_byte(self.settings.address, 0x00)
                finally:
                    self._bus.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.debug("error closing TSL2561", exc_info=True)
        self._bus = None
