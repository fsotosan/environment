"""SQLAlchemy ORM models.

Timestamps are stored as UTC epoch seconds (``BIGINT``) on purpose: integer
division then works identically for SQLite and PostgreSQL when bucketing
time-series data.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, BigInteger, Date, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)

    temperature_c: Mapped[float | None] = mapped_column(Float)
    humidity_pct: Mapped[float | None] = mapped_column(Float)
    pm1_0_ugm3: Mapped[float | None] = mapped_column(Float)
    pm2_5_ugm3: Mapped[float | None] = mapped_column(Float)
    pm4_0_ugm3: Mapped[float | None] = mapped_column(Float)
    pm10_ugm3: Mapped[float | None] = mapped_column(Float)
    voc_index: Mapped[float | None] = mapped_column(Float)
    nox_index: Mapped[float | None] = mapped_column(Float)

    lux: Mapped[float | None] = mapped_column(Float)

    noise_db_a: Mapped[float | None] = mapped_column(Float)
    noise_peak_db: Mapped[float | None] = mapped_column(Float)
    noise_dbfs: Mapped[float | None] = mapped_column(Float)
    noise_sample_rate_hz: Mapped[int | None] = mapped_column(Integer)
    noise_clipping_pct: Mapped[float | None] = mapped_column(Float)

    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="hardware")
    errors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "temperature_c": self.temperature_c,
            "humidity_pct": self.humidity_pct,
            "pm1_0_ugm3": self.pm1_0_ugm3,
            "pm2_5_ugm3": self.pm2_5_ugm3,
            "pm4_0_ugm3": self.pm4_0_ugm3,
            "pm10_ugm3": self.pm10_ugm3,
            "voc_index": self.voc_index,
            "nox_index": self.nox_index,
            "lux": self.lux,
            "noise_db_a": self.noise_db_a,
            "noise_peak_db": self.noise_peak_db,
            "noise_dbfs": self.noise_dbfs,
            "noise_sample_rate_hz": self.noise_sample_rate_hz,
            "noise_clipping_pct": self.noise_clipping_pct,
            "mode": self.mode,
            "errors": list(self.errors or []),
        }


class DailyStat(Base):
    __tablename__ = "daily_stats"

    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    temperature_avg: Mapped[float | None] = mapped_column(Float)
    temperature_min: Mapped[float | None] = mapped_column(Float)
    temperature_max: Mapped[float | None] = mapped_column(Float)
    humidity_avg: Mapped[float | None] = mapped_column(Float)
    humidity_min: Mapped[float | None] = mapped_column(Float)
    humidity_max: Mapped[float | None] = mapped_column(Float)
    pm2_5_avg: Mapped[float | None] = mapped_column(Float)
    pm2_5_max: Mapped[float | None] = mapped_column(Float)
    pm10_avg: Mapped[float | None] = mapped_column(Float)
    pm10_max: Mapped[float | None] = mapped_column(Float)
    voc_avg: Mapped[float | None] = mapped_column(Float)
    voc_max: Mapped[float | None] = mapped_column(Float)
    nox_avg: Mapped[float | None] = mapped_column(Float)
    nox_max: Mapped[float | None] = mapped_column(Float)
    lux_avg: Mapped[float | None] = mapped_column(Float)
    lux_min: Mapped[float | None] = mapped_column(Float)
    lux_max: Mapped[float | None] = mapped_column(Float)
    noise_avg: Mapped[float | None] = mapped_column(Float)
    noise_max: Mapped[float | None] = mapped_column(Float)

    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    def as_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "sample_count": self.sample_count,
            "temperature_avg": self.temperature_avg,
            "temperature_min": self.temperature_min,
            "temperature_max": self.temperature_max,
            "humidity_avg": self.humidity_avg,
            "humidity_min": self.humidity_min,
            "humidity_max": self.humidity_max,
            "pm2_5_avg": self.pm2_5_avg,
            "pm2_5_max": self.pm2_5_max,
            "pm10_avg": self.pm10_avg,
            "pm10_max": self.pm10_max,
            "voc_avg": self.voc_avg,
            "voc_max": self.voc_max,
            "nox_avg": self.nox_avg,
            "nox_max": self.nox_max,
            "lux_avg": self.lux_avg,
            "lux_min": self.lux_min,
            "lux_max": self.lux_max,
            "noise_avg": self.noise_avg,
            "noise_max": self.noise_max,
        }


class LlmSummary(Base):
    """Cached natural-language daily summary from an external LLM."""

    __tablename__ = "llm_summaries"

    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
