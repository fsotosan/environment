"""SQLite backup, retention and scheduled aggregation."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from envmon.config import Settings
from envmon.repository import Repository

logger = logging.getLogger(__name__)


def backup_database(settings: Settings) -> Path | None:
    """Create a consistent SQLite backup, optionally gzip-compressed.

    PostgreSQL installations should use ``pg_dump`` instead; this function
    returns ``None`` for non-SQLite URLs.
    """
    if not settings.database.is_sqlite:
        logger.info("database is not SQLite; use pg_dump for backups")
        return None
    source_path = settings.database.sqlite_path
    if source_path is None or not source_path.exists():
        logger.warning("SQLite database %s does not exist", source_path)
        return None

    backup_dir = Path(settings.retention.backup_dir).expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(tz=settings.tzinfo).strftime("%Y%m%d-%H%M%S")
    if settings.retention.backup_compress:
        destination = backup_dir / f"envmon-{stamp}.db.gz"
    else:
        destination = backup_dir / f"envmon-{stamp}.db"

    fd, temp_name = tempfile.mkstemp(prefix="envmon-backup-", suffix=".db", dir=backup_dir)
    os.close(fd)
    Path(temp_name).unlink(missing_ok=True)
    tmp_path = Path(temp_name)
    try:
        with sqlite3.connect(source_path) as source, sqlite3.connect(tmp_path) as target:
            source.backup(target)
        if settings.retention.backup_compress:
            with tmp_path.open("rb") as raw, gzip.open(destination, "wb", compresslevel=6) as gz:
                shutil.copyfileobj(raw, gz, length=1024 * 1024)
            tmp_path.unlink(missing_ok=True)
        else:
            tmp_path.replace(destination)
    finally:
        tmp_path.unlink(missing_ok=True)

    _rotate_backups(backup_dir, settings.retention.backup_keep)
    logger.info("database backup written to %s", destination)
    return destination


def _rotate_backups(backup_dir: Path, keep: int) -> None:
    candidates = sorted(
        list(backup_dir.glob("envmon-*.db")) + list(backup_dir.glob("envmon-*.db.gz")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old in candidates[keep:]:
        try:
            old.unlink()
            logger.info("removed old backup %s", old)
        except OSError as exc:
            logger.warning("could not remove old backup %s: %s", old, exc)


def aggregate_missing_days(
    repository: Repository, settings: Settings, *, max_days: int = 31
) -> list[str]:
    """Aggregate recent days that have raw data but no daily row."""
    today = dt.datetime.now(tz=settings.tzinfo).date()
    oldest, _ = repository.sample_bounds()
    if oldest is None:
        return []
    oldest_day = dt.datetime.fromtimestamp(oldest, tz=settings.tzinfo).date()
    start = max(oldest_day, today - dt.timedelta(days=max_days))
    days: list[str] = []
    day = start
    while day < today:
        if repository.get_daily_stat(day) is None:
            result = repository.aggregate_day(day, settings.tzinfo)
            if result:
                days.append(day.isoformat())
        day += dt.timedelta(days=1)
    return days


def run_retention(repository: Repository, settings: Settings) -> dict[str, object]:
    cutoff = int(dt.datetime.now(tz=dt.UTC).timestamp()) - settings.retention.raw_days * 86_400
    deleted = repository.prune_before(cutoff)
    if settings.retention.vacuum_after_prune and deleted:
        repository.vacuum()
    return {"deleted_samples": deleted, "cutoff_ts": cutoff}


class MaintenanceScheduler:
    """Runs daily aggregation, pruning and backups inside the server process."""

    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_run: dict[str, str] = {}
        self.last_result: dict[str, object] = {}

    async def start(self) -> None:
        if not self.settings.retention.maintenance_enabled:
            logger.info("maintenance scheduler disabled")
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="envmon-maintenance")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run(self) -> None:
        # Startup catch-up is cheap and fixes gaps after downtime.
        try:
            days = await asyncio.to_thread(
                aggregate_missing_days, self.repository, self.settings, max_days=7
            )
            if days:
                logger.info("aggregated missing days: %s", ", ".join(days))
        except Exception:
            logger.exception("startup aggregation failed")

        while not self._stop.is_set():
            try:
                await self.run_pending()
            except Exception:
                logger.exception("maintenance cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=300)
            except TimeoutError:
                continue

    async def run_pending(self) -> None:
        now = dt.datetime.now(tz=self.settings.tzinfo)
        today_key = now.date().isoformat()

        aggregating_key = f"aggregate:{today_key}"
        if (
            now.hour >= self.settings.retention.aggregate_hour
            and aggregating_key not in self.last_run
        ):
            yesterday = now.date() - dt.timedelta(days=1)
            result = await asyncio.to_thread(
                self.repository.aggregate_day, yesterday, self.settings.tzinfo
            )
            self.last_run[aggregating_key] = now.isoformat(timespec="minutes")
            self.last_result["aggregate"] = result or {"day": yesterday.isoformat(), "samples": 0}

        backup_key = f"backup:{today_key}"
        if (
            self.settings.retention.backup_enabled
            and now.hour >= self.settings.retention.backup_hour
            and backup_key not in self.last_run
        ):
            self.last_result["retention"] = await asyncio.to_thread(
                run_retention, self.repository, self.settings
            )
            path = await asyncio.to_thread(backup_database, self.settings)
            self.last_result["backup"] = str(path) if path else None
            self.last_run[backup_key] = now.isoformat(timespec="minutes")
