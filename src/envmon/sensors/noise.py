"""Grove Analog Microphone noise sensor (approximate LAeq)."""

from __future__ import annotations

import logging
import math

from envmon.config import NoiseSettings
from envmon.sensors.grove_adc import GroveAdc, timed_read_many
from envmon.sensors.noise_analysis import estimate_noise

logger = logging.getLogger(__name__)


class NoiseSensor:
    name = "noise"

    def __init__(self, bus_number: int, settings: NoiseSettings) -> None:
        self.settings = settings
        self._adc = GroveAdc(bus_number, settings.adc_address)

    def read(self) -> dict[str, object]:
        count = max(128, int(self.settings.sample_rate_hz * self.settings.duration_seconds))
        samples, elapsed = timed_read_many(self._adc, self.settings.adc_channel, count)
        actual_rate = len(samples) / elapsed
        if actual_rate < 200:
            raise RuntimeError(
                f"ADC sampled at only {actual_rate:.0f} Hz; check the I2C bus speed "
                "and power supply (100 kHz is recommended)"
            )
        if actual_rate / 2 <= self.settings.highpass_hz:
            raise RuntimeError("actual ADC sample rate is too low for the configured high-pass")

        result = estimate_noise(
            samples,
            sample_rate_hz=actual_rate,
            adc_reference_volts=self.settings.adc_reference_volts,
            sensitivity_mv_per_pa=self.settings.sensitivity_mv_per_pa,
            calibration_offset_db=self.settings.calibration_offset_db,
            a_weighting=self.settings.a_weighting,
            highpass_hz=self.settings.highpass_hz,
        )
        logger.debug(
            "noise: %.1f dB(A), peak %.1f dB, %.0f Hz, clipping %.2f%%",
            result.db_a,
            result.peak_db,
            actual_rate,
            result.clipping_pct,
        )
        data = result.as_dict()
        # Report the measured rate, not the configured target rate.
        data["noise_sample_rate_hz"] = round(actual_rate)
        if not math.isfinite(float(data["noise_db_a"])):
            raise RuntimeError("noise estimate was not finite")
        if result.clipping_pct > 5.0:
            logger.warning(
                "microphone ADC is clipping %.1f%% of samples; reduce the input level",
                result.clipping_pct,
            )
        return data

    def close(self) -> None:
        self._adc.close()
