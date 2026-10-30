"""Tests for the approximate A-weighted noise estimator."""

from __future__ import annotations

import math

import numpy as np
import pytest

from envmon.sensors.noise_analysis import (
    REFERENCE_PRESSURE_PA,
    a_weighting_db,
    estimate_noise,
)


def _sine(
    frequency: float,
    *,
    amplitude: float = 800.0,
    sample_rate: int = 2000,
    seconds: float = 1.0,
    offset: float = 2048.0,
) -> np.ndarray:
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    return np.clip(np.round(offset + amplitude * np.sin(2 * math.pi * frequency * t)), 0, 4095)


def test_a_weighting_reference_points() -> None:
    values = a_weighting_db(np.array([0.0, 20.0, 100.0, 1000.0, 10_000.0]))
    assert values[0] == -200.0
    assert abs(values[3]) < 0.2  # 0 dB at 1 kHz
    assert values[1] < -30.0
    assert values[2] < -15.0
    assert -5.0 < values[4] < 0.0


def test_sine_estimate_matches_rms_conversion() -> None:
    samples = _sine(440.0, amplitude=800.0)
    result = estimate_noise(samples, sample_rate_hz=2000, a_weighting=False, highpass_hz=0.0)

    rms_volts = (800.0 / math.sqrt(2.0)) / 4095.0 * 3.3
    reference_volts = (10.0 / 1000.0) * REFERENCE_PRESSURE_PA
    expected_db = 20.0 * math.log10(rms_volts / reference_volts)

    assert result.db_a == pytest.approx(expected_db, abs=0.3)
    assert result.peak_db > result.db_a
    assert result.clipping_pct == 0.0
    assert result.sample_rate_hz == 2000
    assert result.sample_count == 2000


def test_calibration_offset_and_a_weighting_are_applied() -> None:
    samples = _sine(440.0)
    plain = estimate_noise(samples, sample_rate_hz=2000, a_weighting=True, highpass_hz=20.0)
    shifted = estimate_noise(
        samples,
        sample_rate_hz=2000,
        a_weighting=True,
        highpass_hz=20.0,
        calibration_offset_db=7.5,
    )

    assert shifted.db_a == pytest.approx(plain.db_a + 7.5, abs=0.05)
    assert shifted.peak_db == pytest.approx(plain.peak_db + 7.5, abs=0.05)


def test_low_frequency_tone_is_attenuated_by_a_weighting() -> None:
    low = _sine(50.0)
    high = _sine(440.0)
    low_result = estimate_noise(low, sample_rate_hz=2000)
    high_result = estimate_noise(high, sample_rate_hz=2000)
    assert low_result.db_a < high_result.db_a - 20.0


def test_clipping_is_reported() -> None:
    samples = np.full(500, 4095)
    result = estimate_noise(samples, sample_rate_hz=2000)
    assert result.clipping_pct == 100.0


def test_silence_is_finite_and_zero() -> None:
    result = estimate_noise(np.full(256, 2048), sample_rate_hz=2000)
    assert result.db_a == 0.0
    assert result.peak_db == 0.0
    assert math.isfinite(result.dbfs)


@pytest.mark.parametrize(
    ("samples", "rate", "message"),
    [
        ([1, 2, 3], 2000, "not enough samples"),
        (np.arange(64), 0, "sample_rate_hz"),
        (np.zeros((64, 2), dtype=int), 2000, "not enough samples"),
    ],
)
def test_invalid_inputs_raise(samples, rate, message) -> None:
    with pytest.raises(ValueError, match=message):
        estimate_noise(samples, sample_rate_hz=rate)
