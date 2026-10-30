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
from dataclasses import dataclass

import numpy as np

REFERENCE_PRESSURE_PA = 20e-6


@dataclass(frozen=True)
class NoiseResult:
    db_a: float
    peak_db: float
    dbfs: float
    sample_rate_hz: int
    clipping_pct: float
    rms_volts: float
    sample_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "noise_db_a": round(self.db_a, 1),
            "noise_peak_db": round(self.peak_db, 1),
            "noise_dbfs": round(self.dbfs, 1),
            "noise_sample_rate_hz": self.sample_rate_hz,
            "noise_clipping_pct": round(self.clipping_pct, 2),
        }


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
