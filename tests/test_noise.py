"""Tests for the approximate A-weighted noise estimator."""

from __future__ import annotations

import math

import numpy as np
import pytest

from envmon.config import NoiseSettings
from envmon.sensors.noise import NoiseSensor, _BlockBuffer
from envmon.sensors.noise_analysis import (
    REFERENCE_PRESSURE_PA,
    NoiseResult,
    a_weighting_db,
    estimate_noise,
    summarize_noise_blocks,
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


def _block(level: float, *, seconds: float = 1.0, clipping: float = 0.0) -> NoiseResult:
    rate = 2000
    return NoiseResult(
        db_a=level,
        peak_db=level + 10.0,
        dbfs=-40.0,
        sample_rate_hz=rate,
        clipping_pct=clipping,
        rms_volts=0.01,
        sample_count=int(seconds * rate),
    )


def test_summarize_blocks_energy_average_and_percentiles() -> None:
    stats = summarize_noise_blocks([_block(40.0), _block(50.0), _block(60.0)])

    expected_leq = 10.0 * math.log10((10.0**4 + 10.0**5 + 10.0**6) / 3.0)
    assert stats.leq_db_a == pytest.approx(expected_leq, abs=1e-6)
    assert stats.max_db_a == 60.0
    assert stats.l10_db_a >= stats.l50_db_a >= stats.l90_db_a
    assert stats.l10_db_a == 60.0
    assert stats.l50_db_a == 50.0
    assert stats.l90_db_a == 40.0
    assert stats.block_count == 3
    assert stats.duration_seconds == pytest.approx(3.0)


def test_summarize_blocks_weights_by_duration() -> None:
    blocks = [_block(40.0, seconds=1.0), _block(60.0, seconds=3.0)]
    stats = summarize_noise_blocks(blocks)

    expected_leq = 10.0 * math.log10((1.0 * 10.0**4 + 3.0 * 10.0**6) / 4.0)
    assert stats.leq_db_a == pytest.approx(expected_leq, abs=1e-6)
    # Three quarters of the interval is loud, so the 90th percentile is loud
    # and the 10th percentile is the quieter block.
    assert stats.l10_db_a == 60.0
    assert stats.l90_db_a == 40.0
    assert stats.sample_count == 8000
    assert stats.duration_seconds == pytest.approx(4.0)


def test_summarize_blocks_reports_peak_and_clipping() -> None:
    stats = summarize_noise_blocks([_block(40.0, clipping=0.0), _block(50.0, clipping=10.0)])
    assert stats.peak_db == 60.0
    assert stats.dbfs == -40.0
    assert stats.clipping_pct == pytest.approx(5.0)


def test_summarize_blocks_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="no noise blocks"):
        summarize_noise_blocks([])


def test_sensor_read_summarises_buffered_blocks(monkeypatch) -> None:
    sensor = NoiseSensor.__new__(NoiseSensor)
    sensor.settings = NoiseSettings(window_seconds=1.0)
    sensor._buffer = _BlockBuffer()
    for level in (40.0, 50.0, 60.0):
        sensor._buffer.add(_block(level))
    monkeypatch.setattr(sensor, "_ensure_worker", lambda: None)

    result = sensor.read()

    assert result["noise_leq_db_a"] == pytest.approx(55.7, abs=0.1)
    assert result["noise_max_db_a"] == 60.0
    assert result["noise_l10_db_a"] == 60.0
    assert result["noise_l50_db_a"] == 50.0
    assert result["noise_l90_db_a"] == 40.0
    # The buffer is drained so the next read only sees new blocks.
    assert sensor._buffer.drain(0)[0] == []
