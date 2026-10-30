"""Grove Analog Microphone with continuous capture and interval statistics.

The ADC is polled continuously in a background thread. Each analysis block is
turned into an LAeq value; when the sampler asks for a reading, the accumulated
blocks are summarised into interval Leq, maximum and L10/L50/L90. The sampler
still writes one row per ``sampling.interval_seconds`` (30 s by default).
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque

from envmon.config import NoiseSettings
from envmon.sensors.grove_adc import GroveAdc, timed_read_many
from envmon.sensors.noise_analysis import (
    NoiseIntervalStats,
    NoiseResult,
    estimate_noise,
    summarize_noise_blocks,
)

logger = logging.getLogger(__name__)

# Keep at most two hours of one-second blocks if nobody drains the buffer. The
# sampler normally drains every 30 s; the bound prevents a stalled reader from
# growing memory without limit.
MAX_BUFFERED_BLOCKS = 7200


class _BlockBuffer:
    """Thread-safe hand-off between the capture thread and ``read()``."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._blocks: deque[NoiseResult] = deque(maxlen=MAX_BUFFERED_BLOCKS)
        self._error: str | None = None

    def add(self, block: NoiseResult) -> None:
        with self._condition:
            self._blocks.append(block)
            self._condition.notify_all()

    def fail(self, message: str) -> None:
        with self._condition:
            self._error = message
            self._condition.notify_all()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def drain(self, timeout: float) -> tuple[list[NoiseResult], str | None]:
        with self._condition:
            if not self._blocks and self._error is None and timeout > 0:
                self._condition.wait(timeout=timeout)
            blocks = list(self._blocks)
            self._blocks.clear()
            error = self._error
            self._error = None
            return blocks, error


class NoiseSensor:
    name = "noise"

    def __init__(self, bus_number: int, settings: NoiseSettings) -> None:
        self.settings = settings
        self._adc = GroveAdc(bus_number, settings.adc_address)
        self._buffer = _BlockBuffer()
        self._stop = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Start from the configured target, then adapt to the measured rate so
        # each analysis block stays close to ``window_seconds``.
        self._target_rate = float(settings.sample_rate_hz)

    # --------------------------------------------------------------- lifecycle

    def _ensure_worker(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._capture_loop,
                name="envmon-noise",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            self._buffer.wake()
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(5.0, self.settings.window_seconds * 4 + 2.0))
        self._adc.close()

    # ------------------------------------------------------------------ capture

    def _capture_block(self) -> NoiseResult:
        count = max(128, int(self._target_rate * self.settings.window_seconds))
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
        if not math.isfinite(result.db_a) or not math.isfinite(result.peak_db):
            raise RuntimeError("noise estimate was not finite")
        # Adapt the next block to the measured throughput. The configured rate
        # is only a target on a polled I2C ADC.
        self._target_rate = actual_rate
        return result

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._buffer.add(self._capture_block())
            except Exception as exc:
                message = str(exc).strip() or exc.__class__.__name__
                logger.warning("noise capture failed: %s", message)
                self._buffer.fail(message[:300])
                try:
                    self._adc.close()
                except Exception:  # pragma: no cover - cleanup only
                    logger.debug("error closing noise ADC", exc_info=True)
                return

    # --------------------------------------------------------------------- read

    def read(self) -> dict[str, object]:
        self._ensure_worker()
        # Drain everything accumulated since the last read. In steady state the
        # capture worker is faster than the sampler, so this returns the whole
        # interval's blocks almost immediately; on the first interval (or after
        # a restart) we wait for the worker to produce at least one block.
        timeout = max(5.0, self.settings.window_seconds * 8 + 2.0)
        blocks, error = self._buffer.drain(timeout)

        if not blocks:
            raise RuntimeError(error or "no noise blocks collected yet")

        stats: NoiseIntervalStats = summarize_noise_blocks(blocks)
        if error:
            # The capture thread stopped after this batch. Return the data we
            # have; the next read will surface the failure and reopen the ADC.
            logger.warning("noise capture stopped after partial interval: %s", error)
        logger.debug(
            "noise interval: Leq %.1f dB(A), max %.1f, L10 %.1f, L50 %.1f, L90 %.1f, "
            "%.2fs, %s blocks, %.0f Hz, clipping %.2f%%",
            stats.leq_db_a,
            stats.max_db_a,
            stats.l10_db_a,
            stats.l50_db_a,
            stats.l90_db_a,
            stats.duration_seconds,
            stats.block_count,
            stats.sample_rate_hz,
            stats.clipping_pct,
        )
        if stats.clipping_pct > 5.0:
            logger.warning(
                "microphone ADC is clipping %.1f%% of samples; reduce the input level",
                stats.clipping_pct,
            )
        return stats.as_dict()
