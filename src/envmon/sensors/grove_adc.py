"""Grove Base HAT analog input (STM32/MM32 ADC over I2C).

The Base HAT exposes a 12-bit ADC through a small I2C protocol:
write the channel selector ``0x10 + channel`` then read a 16-bit word.
This is *not* an audio interface; it is a slow polled ADC. The noise sensor
documents the resulting limitations.
"""

from __future__ import annotations

import contextlib
import time

from smbus2 import SMBus


class GroveAdc:
    def __init__(self, bus_number: int, address: int = 0x04) -> None:
        self.bus_number = bus_number
        self.address = address
        self._bus: SMBus | None = None

    def _ensure_bus(self) -> SMBus:
        if self._bus is None:
            self._bus = SMBus(self.bus_number)
        return self._bus

    def read_raw(self, channel: int) -> int:
        bus = self._ensure_bus()
        command = 0x10 + channel
        bus.write_byte(self.address, command)
        value = bus.read_word_data(self.address, command)
        return int(value) & 0x0FFF

    def read_many(self, channel: int, count: int) -> list[int]:
        """Read ``count`` samples as fast as the I2C bus allows.

        No artificial pacing is applied: the actual sample rate is measured by
        the caller. On a 100 kHz bus this is typically 1-2 kHz.
        """
        bus = self._ensure_bus()
        command = 0x10 + channel
        values: list[int] = [0] * count
        write = bus.write_byte
        read = bus.read_word_data
        address = self.address
        for index in range(count):
            write(address, command)
            values[index] = int(read(address, command)) & 0x0FFF
        return values

    def close(self) -> None:
        if self._bus is not None:
            try:
                self._bus.close()
            finally:
                self._bus = None

    def __enter__(self) -> GroveAdc:
        self._ensure_bus()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()


def timed_read_many(adc: GroveAdc, channel: int, count: int) -> tuple[list[int], float]:
    started = time.perf_counter()
    samples = adc.read_many(channel, count)
    elapsed = max(time.perf_counter() - started, 1e-9)
    return samples, elapsed
