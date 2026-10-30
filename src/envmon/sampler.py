"""Background sampling loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from envmon.config import Settings
from envmon.repository import Repository
from envmon.sensors.base import Reading
from envmon.sensors.manager import SensorManager

logger = logging.getLogger(__name__)


class Sampler:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        manager: SensorManager | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.manager = manager or SensorManager(settings)
        self.started_at = int(time.time())
        self.last_sample_ts: int | None = None
        self.last_success_ts: int | None = None
        self.last_attempt_ts: int | None = None
        self.last_error: str | None = None
        self.consecutive_failures = 0
        self.samples_written = 0
        self.samples_skipped = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ status

    def snapshot(self) -> dict[str, object]:
        now = int(time.time())
        latest = self.last_success_ts
        return {
            "running": self._task is not None and not self._task.done(),
            "started_at": self.started_at,
            "uptime_seconds": max(0, now - self.started_at),
            "last_sample_ts": self.last_sample_ts,
            "last_success_ts": self.last_success_ts,
            "last_attempt_ts": self.last_attempt_ts,
            "data_age_seconds": (now - latest) if latest else None,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "samples_written": self.samples_written,
            "samples_skipped": self.samples_skipped,
            "interval_seconds": self.settings.sampling.interval_seconds,
            "sensors": self.manager.snapshot(),
        }

    # ------------------------------------------------------------------- loop

    async def run(self) -> None:
        interval = self.settings.sampling.interval_seconds
        retry = self.settings.sampling.failure_retry_seconds
        next_run = time.monotonic()
        logger.info(
            "sampler started (mode=%s, interval=%ss)",
            self.settings.sensors.mode,
            interval,
        )
        while not self._stop.is_set():
            delay = next_run - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    break
                except TimeoutError:
                    pass

            started = time.monotonic()
            reading = await self.sample_once()
            delay = retry if reading is None or not reading.has_data else interval
            next_run = started + max(1.0, delay)
            if next_run < time.monotonic():
                next_run = time.monotonic()

    async def sample_once(self) -> Reading | None:
        self.last_attempt_ts = int(time.time())
        try:
            reading = await asyncio.to_thread(self.manager.read)
        except Exception as exc:  # defensive: manager should never raise
            logger.exception("sensor manager crashed")
            self._fail(f"sensor manager: {exc}")
            return None

        if not reading.has_data:
            self._fail("; ".join(reading.errors) or "no sensor data")
            return None

        record = reading.as_record()
        try:
            written = await asyncio.to_thread(self.repository.insert_sample, record)
        except Exception as exc:
            logger.exception("could not write sample")
            self._fail(f"database: {exc}")
            return None

        if written:
            self.samples_written += 1
        else:
            self.samples_skipped += 1
        self.last_sample_ts = reading.ts
        self.last_success_ts = reading.ts
        self.consecutive_failures = 0
        self.last_error = (
            "; ".join(reading.errors) if reading.errors else None
        )  # partial failure remains visible in status
        if self.samples_written <= 3 or self.samples_written % 120 == 0:
            logger.info(
                "sample ts=%s temp=%s pm2.5=%s lux=%s noise=%s",
                reading.ts,
                record.get("temperature_c"),
                record.get("pm2_5_ugm3"),
                record.get("lux"),
                record.get("noise_leq_db_a"),
            )
        if reading.errors:
            logger.warning("partial sample: %s", self.last_error)
        return reading

    def _fail(self, message: str) -> None:
        self.consecutive_failures += 1
        self.last_error = message[:500]
        # Log first failure and then every 10th (~5 minutes at a 30s retry).
        if self.consecutive_failures == 1 or self.consecutive_failures % 10 == 0:
            logger.error("sampling failed (%sx): %s", self.consecutive_failures, self.last_error)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="envmon-sampler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.manager.close()
