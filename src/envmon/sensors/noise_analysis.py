"""Approximate environmental noise metrics from a polled Grove microphone.

The Grove Analog Microphone is a raw analog MEMS/electret module and the Grove
Base HAT ADC is not an audio ADC. Sampling is limited to roughly 1-3 kHz and
the ADC's anti-aliasing, gain flatness and timing jitter are unknown. Values
are useful for trends, not for certified sound-level measurements. The API
and dashboard label them as approximate.

The estimator:

1. converts ADC counts to volts,
2. removes the DC bias and optional low-frequency rumble,
3. applies a Hann window and an FFT-domain A-weighting filter,
4. integrates the weighted power to obtain an RMS voltage,
5. converts RMS volts to dB SPL using the configured microphone sensitivity,
6. applies the user calibration offset.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

REFERENCE_PRESSURE_PA = 20e-6


@dataclass(frozen=True)
class NoiseResult:
    """One short analysis block (typically about one second)."""

    db_a: float
    peak_db: float
    dbfs: float
    sample_rate_hz: int
    clipping_pct: float
    rms_volts: float
    sample_count: int

    @property
    def duration_seconds(self) -> float:
        if self.sample_rate_hz <= 0:
            return 0.0
        return self.sample_count / self.sample_rate_hz


@dataclass(frozen=True)
class NoiseIntervalStats:
    """Leq, maximum and statistical levels over one logging interval."""

    leq_db_a: float
    max_db_a: float
    l10_db_a: float
    l50_db_a: float
    l90_db_a: float
    peak_db: float
    dbfs: float
    sample_rate_hz: int
    clipping_pct: float
    sample_count: int
    block_count: int
    duration_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {
            "noise_leq_db_a": round(self.leq_db_a, 1),
            "noise_max_db_a": round(self.max_db_a, 1),
            "noise_l10_db_a": round(self.l10_db_a, 1),
            "noise_l50_db_a": round(self.l50_db_a, 1),
            "noise_l90_db_a": round(self.l90_db_a, 1),
            "noise_peak_db": round(self.peak_db, 1),
            "noise_dbfs": round(self.dbfs, 1),
            "noise_sample_rate_hz": self.sample_rate_hz,
            "noise_clipping_pct": round(self.clipping_pct, 2),
        }


def _weighted_percentile(values: np.ndarray, weights: np.ndarray, fraction: float) -> float:
    """Return the level at a cumulative time ``fraction`` (0 = quietest).

    The result is an observed block level rather than an interpolated value, so
    every reported percentile is a level the station actually measured.
    """
    order = np.argsort(values, kind="stable")
    ordered_values = values[order]
    cumulative = np.cumsum(weights[order])
    total = float(cumulative[-1])
    if total <= 0:
        return float(ordered_values[-1])
    target = max(0.0, min(1.0, fraction)) * total
    index = int(np.searchsorted(cumulative, target, side="left"))
    return float(ordered_values[min(index, ordered_values.size - 1)])


def summarize_noise_blocks(blocks: Sequence[NoiseResult]) -> NoiseIntervalStats:
    """Combine per-block LAeq values into interval Leq, max and L10/L50/L90.

    ``L10`` is the level exceeded for 10 % of the interval, ``L90`` for 90 %.
    Blocks are time-weighted so a slow ADC block counts for its real duration.
    """
    if not blocks:
        raise ValueError("no noise blocks to summarise")

    levels = np.asarray([block.db_a for block in blocks], dtype=np.float64)
    weights = np.asarray([max(block.duration_seconds, 1e-9) for block in blocks], dtype=np.float64)
    if not np.all(np.isfinite(levels)):
        raise ValueError("noise levels were not finite")

    # Energy average: 10*log10(sum(t_i * 10^(L_i/10)) / sum(t_i)).
    energy = float(np.sum(weights * np.power(10.0, levels / 10.0)) / np.sum(weights))
    leq = 10.0 * math.log10(max(energy, 1e-12))

    total_samples = sum(block.sample_count for block in blocks)
    total_duration = float(np.sum(weights))
    clipped = sum(block.clipping_pct * block.sample_count / 100.0 for block in blocks)
    sample_rate = total_samples / total_duration if total_duration > 0 else 0.0

    return NoiseIntervalStats(
        leq_db_a=leq,
        max_db_a=float(np.max(levels)),
        # L10 is exceeded 10 % of the time (90th percentile); L90 is the
        # background level exceeded 90 % of the time (10th percentile).
        l10_db_a=_weighted_percentile(levels, weights, 0.90),
        l50_db_a=_weighted_percentile(levels, weights, 0.50),
        l90_db_a=_weighted_percentile(levels, weights, 0.10),
        peak_db=max(block.peak_db for block in blocks),
        dbfs=max(block.dbfs for block in blocks),
        sample_rate_hz=round(sample_rate),
        clipping_pct=100.0 * clipped / total_samples if total_samples else 0.0,
        sample_count=total_samples,
        block_count=len(blocks),
        duration_seconds=total_duration,
    )


def a_weighting_db(frequencies: np.ndarray) -> np.ndarray:
    """IEC 61672 A-weighting curve in dB (0 dB at 1 kHz)."""
    f = np.asarray(frequencies, dtype=np.float64)
    result = np.full_like(f, -200.0, dtype=np.float64)
    mask = f > 0
    f2 = f[mask] ** 2
    numerator = (12194.0**2) * (f2**2)
    denominator = (f2 + 20.6**2) * np.sqrt((f2 + 107.7**2) * (f2 + 737.9**2)) * (f2 + 12194.0**2)
    result[mask] = 20.0 * np.log10(numerator / denominator) + 2.0
    return result


def estimate_noise(
    counts: list[int] | np.ndarray,
    *,
    sample_rate_hz: float,
    adc_reference_volts: float = 3.3,
    adc_max_count: int = 4095,
    sensitivity_mv_per_pa: float = 10.0,
    calibration_offset_db: float = 0.0,
    a_weighting: bool = True,
    highpass_hz: float = 20.0,
) -> NoiseResult:
    samples = np.asarray(counts, dtype=np.float64)
    if samples.ndim != 1 or samples.size < 32:
        raise ValueError("not enough samples for a noise estimate")
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")

    clipped = np.count_nonzero((samples <= 1) | (samples >= adc_max_count - 1))
    clipping_pct = 100.0 * clipped / samples.size

    volts = samples / float(adc_max_count) * adc_reference_volts
    centred = volts - float(np.mean(volts))
    n = centred.size

    window = np.hanning(n)
    spectrum = np.fft.rfft(centred * window)
    frequencies = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)
    power = np.abs(spectrum) ** 2

    weight_sq = np.ones_like(frequencies)
    if highpass_hz > 0:
        weight_sq[frequencies < highpass_hz] = 0.0
    if a_weighting:
        weight_sq *= 10.0 ** (a_weighting_db(frequencies) / 10.0)

    weighted = power * weight_sq
    # One-sided PSD reconstruction. Parseval: sum_full |X|^2 = N * sum |xw|^2.
    total = 2.0 * float(np.sum(weighted)) - float(weighted[0])
    if n % 2 == 0:
        total -= float(weighted[-1])
    mean_square = max(total, 0.0) / (n * float(np.sum(window**2)))
    rms_volts = math.sqrt(mean_square)

    reference_volts = (sensitivity_mv_per_pa / 1000.0) * REFERENCE_PRESSURE_PA
    if rms_volts > 1e-12 and reference_volts > 0:
        db_spl = 20.0 * math.log10(rms_volts / reference_volts) + calibration_offset_db
    else:
        db_spl = 0.0

    peak_volts = float(np.max(np.abs(centred)))
    if peak_volts > 1e-12 and reference_volts > 0:
        peak_db = 20.0 * math.log10(peak_volts / reference_volts) + calibration_offset_db
    else:
        peak_db = 0.0

    full_scale_peak = adc_reference_volts / 2.0
    dbfs = 20.0 * math.log10(max(peak_volts, 1e-12) / full_scale_peak)
    dbfs = min(dbfs, 0.0)

    return NoiseResult(
        db_a=db_spl,
        peak_db=peak_db,
        dbfs=dbfs,
        sample_rate_hz=round(sample_rate_hz),
        clipping_pct=clipping_pct,
        rms_volts=rms_volts,
        sample_count=int(n),
    )
