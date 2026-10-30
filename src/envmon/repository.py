"""Data access layer.

All SQL lives here so the sampler, web API and CLI share one implementation.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import math
import os
from collections.abc import Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from envmon.models import DailyStat, LlmSummary, Sample
from envmon.timeutil import day_bounds, now_ts

logger = logging.getLogger(__name__)

CSV_COLUMNS = [
    "ts",
    "utc_time",
    "temperature_c",
    "humidity_pct",
    "pm1_0_ugm3",
    "pm2_5_ugm3",
    "pm4_0_ugm3",
    "pm10_ugm3",
    "voc_index",
    "nox_index",
    "lux",
    "noise_db_a",
    "noise_peak_db",
    "noise_dbfs",
    "noise_sample_rate_hz",
    "noise_clipping_pct",
    "mode",
    "errors",
]


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: object, digits: int = 2) -> float | None:
    number = _finite(value)
    return round(number, digits) if number is not None else None


class Repository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    # ------------------------------------------------------------------ writes

    def insert_sample(self, values: dict[str, object]) -> bool:
        """Insert one sample. Returns False when the timestamp already exists."""
        ts = int(values["ts"])  # type: ignore[arg-type]
        payload = {
            "ts": ts,
            "temperature_c": _round(values.get("temperature_c"), 3),
            "humidity_pct": _round(values.get("humidity_pct"), 3),
            "pm1_0_ugm3": _round(values.get("pm1_0_ugm3")),
            "pm2_5_ugm3": _round(values.get("pm2_5_ugm3")),
            "pm4_0_ugm3": _round(values.get("pm4_0_ugm3")),
            "pm10_ugm3": _round(values.get("pm10_ugm3")),
            "voc_index": _round(values.get("voc_index"), 1),
            "nox_index": _round(values.get("nox_index"), 1),
            "lux": _round(values.get("lux"), 1),
            "noise_db_a": _round(values.get("noise_db_a"), 1),
            "noise_peak_db": _round(values.get("noise_peak_db"), 1),
            "noise_dbfs": _round(values.get("noise_dbfs"), 1),
            "noise_sample_rate_hz": (
                int(values["noise_sample_rate_hz"])
                if values.get("noise_sample_rate_hz") is not None
                else None
            ),
            "noise_clipping_pct": _round(values.get("noise_clipping_pct"), 2),
            "mode": str(values.get("mode") or "hardware"),
            "errors": [str(error) for error in values.get("errors") or []],  # type: ignore[union-attr]
        }
        with self.session_factory() as session:
            session.add(Sample(**payload))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                logger.debug("sample for ts=%s already exists", ts)
                return False
        return True

    # ------------------------------------------------------------------- reads

    def latest_sample(self) -> dict[str, object] | None:
        with self.session_factory() as session:
            row = session.execute(
                select(Sample).order_by(Sample.ts.desc()).limit(1)
            ).scalar_one_or_none()
            return row.as_dict() if row else None

    def sample_bounds(self) -> tuple[int | None, int | None]:
        with self.session_factory() as session:
            row = session.execute(select(func.min(Sample.ts), func.max(Sample.ts))).one()
            return row[0], row[1]

    def sample_count(self) -> int:
        with self.session_factory() as session:
            return int(session.execute(select(func.count(Sample.id))).scalar_one())

    def count_before(self, cutoff_ts: int) -> int:
        with self.session_factory() as session:
            return int(
                session.execute(
                    select(func.count(Sample.id)).where(Sample.ts < cutoff_ts)
                ).scalar_one()
            )

    def ping(self) -> None:
        with self.session_factory() as session:
            session.execute(select(1))

    def series(self, start: int, end: int, max_points: int = 1500) -> dict[str, object]:
        """Return evenly bucketed aggregates between two UTC timestamps."""
        if end <= start:
            return {"start": start, "end": end, "bucket_seconds": 1, "points": []}

        interval = end - start
        bucket = max(1, math.ceil(interval / max(1, max_points)))
        # Integer bucket start. Plain ``ts / bucket * bucket`` can become
        # floating-point division on some dialects and then no longer groups.
        bucket_expr = (Sample.ts - (Sample.ts % bucket)).label("bucket")

        columns = [
            bucket_expr,
            func.count(Sample.id).label("count"),
            func.avg(Sample.temperature_c),
            func.min(Sample.temperature_c),
            func.max(Sample.temperature_c),
            func.avg(Sample.humidity_pct),
            func.min(Sample.humidity_pct),
            func.max(Sample.humidity_pct),
            func.avg(Sample.pm1_0_ugm3),
            func.avg(Sample.pm2_5_ugm3),
            func.max(Sample.pm2_5_ugm3),
            func.avg(Sample.pm4_0_ugm3),
            func.avg(Sample.pm10_ugm3),
            func.max(Sample.pm10_ugm3),
            func.avg(Sample.voc_index),
            func.max(Sample.voc_index),
            func.avg(Sample.nox_index),
            func.max(Sample.nox_index),
            func.avg(Sample.lux),
            func.min(Sample.lux),
            func.max(Sample.lux),
            func.avg(Sample.noise_db_a),
            func.max(Sample.noise_db_a),
        ]
        statement = (
            select(*columns)
            .where(Sample.ts >= start, Sample.ts < end)
            .group_by(bucket_expr)
            .order_by(bucket_expr)
        )

        points: list[dict[str, object]] = []
        with self.session_factory() as session:
            for row in session.execute(statement):
                points.append(
                    {
                        "ts": int(row[0]),
                        "count": int(row[1]),
                        "temperature_c": _round(row[2]),
                        "temperature_min": _round(row[3]),
                        "temperature_max": _round(row[4]),
                        "humidity_pct": _round(row[5]),
                        "humidity_min": _round(row[6]),
                        "humidity_max": _round(row[7]),
                        "pm1_0_ugm3": _round(row[8]),
                        "pm2_5_ugm3": _round(row[9]),
                        "pm2_5_max": _round(row[10]),
                        "pm4_0_ugm3": _round(row[11]),
                        "pm10_ugm3": _round(row[12]),
                        "pm10_max": _round(row[13]),
                        "voc_index": _round(row[14], 1),
                        "voc_max": _round(row[15], 1),
                        "nox_index": _round(row[16], 1),
                        "nox_max": _round(row[17], 1),
                        "lux": _round(row[18], 1),
                        "lux_min": _round(row[19], 1),
                        "lux_max": _round(row[20], 1),
                        "noise_db_a": _round(row[21], 1),
                        "noise_max": _round(row[22], 1),
                    }
                )

        return {
            "start": start,
            "end": end,
            "bucket_seconds": bucket,
            "points": points,
        }

    def daily_stats(
        self, days: int = 30, end_day: dt.date | None = None
    ) -> list[dict[str, object]]:
        from envmon.timeutil import today as _today

        end_day = end_day or _today(dt.UTC)
        first = end_day - dt.timedelta(days=max(0, days - 1))
        statement = (
            select(DailyStat)
            .where(DailyStat.day >= first, DailyStat.day <= end_day)
            .order_by(DailyStat.day)
        )
        with self.session_factory() as session:
            return [row.as_dict() for row in session.execute(statement).scalars()]

    def get_daily_stat(self, day: dt.date) -> dict[str, object] | None:
        with self.session_factory() as session:
            row = session.get(DailyStat, day)
            return row.as_dict() if row else None

    def daily_extremes(self, start: int, end: int) -> dict[str, object]:
        """Aggregate raw samples between two timestamps."""
        statement = select(
            func.count(Sample.id),
            func.avg(Sample.temperature_c),
            func.min(Sample.temperature_c),
            func.max(Sample.temperature_c),
            func.avg(Sample.humidity_pct),
            func.min(Sample.humidity_pct),
            func.max(Sample.humidity_pct),
            func.avg(Sample.pm2_5_ugm3),
            func.max(Sample.pm2_5_ugm3),
            func.avg(Sample.pm10_ugm3),
            func.max(Sample.pm10_ugm3),
            func.avg(Sample.voc_index),
            func.max(Sample.voc_index),
            func.avg(Sample.nox_index),
            func.max(Sample.nox_index),
            func.avg(Sample.lux),
            func.min(Sample.lux),
            func.max(Sample.lux),
            func.avg(Sample.noise_db_a),
            func.max(Sample.noise_db_a),
        ).where(Sample.ts >= start, Sample.ts < end)
        with self.session_factory() as session:
            row = session.execute(statement).one()
        count = int(row[0] or 0)
        return {
            "sample_count": count,
            "temperature_avg": _round(row[1]),
            "temperature_min": _round(row[2]),
            "temperature_max": _round(row[3]),
            "humidity_avg": _round(row[4]),
            "humidity_min": _round(row[5]),
            "humidity_max": _round(row[6]),
            "pm2_5_avg": _round(row[7]),
            "pm2_5_max": _round(row[8]),
            "pm10_avg": _round(row[9]),
            "pm10_max": _round(row[10]),
            "voc_avg": _round(row[11], 1),
            "voc_max": _round(row[12], 1),
            "nox_avg": _round(row[13], 1),
            "nox_max": _round(row[14], 1),
            "lux_avg": _round(row[15], 1),
            "lux_min": _round(row[16], 1),
            "lux_max": _round(row[17], 1),
            "noise_avg": _round(row[18], 1),
            "noise_max": _round(row[19], 1),
        }

    # --------------------------------------------------------------- aggregate

    def aggregate_day(self, day: dt.date, tz: ZoneInfo) -> dict[str, object] | None:
        start, end = day_bounds(day, tz)
        values = self.daily_extremes(start, end)
        if not values["sample_count"]:
            return None
        stat = DailyStat(
            day=day,
            updated_at=now_ts(),
            **values,
        )
        with self.session_factory() as session:
            session.merge(stat)
            session.commit()
        logger.info("aggregated %s (%s samples)", day, values["sample_count"])
        return stat.as_dict()

    # --------------------------------------------------------------- retention

    def prune_before(self, cutoff_ts: int) -> int:
        with self.session_factory() as session:
            result = session.execute(delete(Sample).where(Sample.ts < cutoff_ts))
            session.commit()
            deleted = int(result.rowcount or 0)
        if deleted:
            logger.info("pruned %s raw samples older than %s", deleted, cutoff_ts)
        return deleted

    def vacuum(self) -> None:
        with self.session_factory() as session:
            engine = session.get_bind()
        if engine.dialect.name == "sqlite":
            # VACUUM cannot run inside a transaction.
            with engine.connect() as connection:
                connection.exec_driver_sql("VACUUM")
            logger.info("sqlite vacuum complete")

    # --------------------------------------------------------------------- llm

    def get_llm_summary(self, day: dt.date) -> dict[str, object] | None:
        with self.session_factory() as session:
            row = session.get(LlmSummary, day)
            if not row:
                return None
            return {
                "day": row.day.isoformat(),
                "model": row.model,
                "source": row.source,
                "summary": row.summary,
                "created_at": row.created_at,
            }

    def save_llm_summary(
        self,
        day: dt.date,
        *,
        model: str,
        source: str,
        summary: str,
        created_at: int | None = None,
    ) -> None:
        with self.session_factory() as session:
            session.merge(
                LlmSummary(
                    day=day,
                    model=model,
                    source=source,
                    summary=summary,
                    created_at=created_at or now_ts(),
                )
            )
            session.commit()

    # ------------------------------------------------------------------ export

    def iter_csv(self, start: int, end: int) -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        statement = (
            select(Sample)
            .where(Sample.ts >= start, Sample.ts < end)
            .order_by(Sample.ts)
            .execution_options(yield_per=1000)
        )
        with self.session_factory() as session:
            result = session.execute(statement).scalars()
            for sample in result:
                writer.writerow(
                    [
                        sample.ts,
                        dt.datetime.fromtimestamp(sample.ts, tz=dt.UTC).isoformat(),
                        sample.temperature_c,
                        sample.humidity_pct,
                        sample.pm1_0_ugm3,
                        sample.pm2_5_ugm3,
                        sample.pm4_0_ugm3,
                        sample.pm10_ugm3,
                        sample.voc_index,
                        sample.nox_index,
                        sample.lux,
                        sample.noise_db_a,
                        sample.noise_peak_db,
                        sample.noise_dbfs,
                        sample.noise_sample_rate_hz,
                        sample.noise_clipping_pct,
                        sample.mode,
                        "; ".join(sample.errors or []),
                    ]
                )
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)

    def database_stats(self, sqlite_path: Path | None = None) -> dict[str, object]:
        oldest, newest = self.sample_bounds()
        stats: dict[str, object] = {
            "samples": self.sample_count(),
            "oldest_ts": oldest,
            "newest_ts": newest,
        }
        if sqlite_path is not None and sqlite_path.exists():
            stats["db_size_bytes"] = os.path.getsize(sqlite_path)
        return stats
