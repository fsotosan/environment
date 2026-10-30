"""Repository queries, aggregation, retention, CSV and LLM cache."""

from __future__ import annotations

import csv
import datetime as dt
import io

from envmon.repository import CSV_COLUMNS, Repository
from envmon.timeutil import day_bounds, now_ts


def sample(
    ts: int,
    *,
    temperature: float = 21.5,
    humidity: float = 45.0,
    pm25: float = 12.0,
    lux: float = 500.0,
    noise: float = 42.0,
    mode: str = "simulator",
    errors: list[str] | None = None,
) -> dict[str, object]:
    return {
        "ts": ts,
        "temperature_c": temperature,
        "humidity_pct": humidity,
        "pm1_0_ugm3": pm25 * 0.7,
        "pm2_5_ugm3": pm25,
        "pm4_0_ugm3": pm25,
        "pm10_ugm3": pm25 * 1.5,
        "voc_index": 110.0,
        "nox_index": 8.0,
        "lux": lux,
        "noise_leq_db_a": noise,
        "noise_max_db_a": noise + 6,
        "noise_l10_db_a": noise + 3,
        "noise_l50_db_a": noise,
        "noise_l90_db_a": noise - 3,
        "noise_peak_db": noise + 10,
        "noise_dbfs": -38.0,
        "noise_sample_rate_hz": 2000,
        "noise_clipping_pct": 0.0,
        "mode": mode,
        "errors": errors or [],
    }


def test_insert_and_latest_sample(repository: Repository) -> None:
    base = 1_700_000_000
    assert repository.insert_sample(sample(base)) is True
    assert repository.insert_sample(sample(base, temperature=99.0)) is False

    assert repository.sample_count() == 1
    latest = repository.latest_sample()
    assert latest is not None
    assert latest["ts"] == base
    assert latest["temperature_c"] == 21.5
    assert repository.sample_bounds() == (base, base)


def test_invalid_values_are_dropped(repository: Repository) -> None:
    record = sample(1_700_000_100)
    record["temperature_c"] = float("nan")
    record["humidity_pct"] = "not a number"
    assert repository.insert_sample(record) is True
    latest = repository.latest_sample()
    assert latest is not None
    assert latest["temperature_c"] is None
    assert latest["humidity_pct"] is None


def test_series_buckets_and_averages(repository: Repository) -> None:
    base = 1_700_000_000
    repository.insert_sample(sample(base + 1, temperature=20.0, pm25=10.0))
    repository.insert_sample(sample(base + 2, temperature=22.0, pm25=20.0))
    repository.insert_sample(sample(base + 60, temperature=26.0, pm25=30.0))

    result = repository.series(base, base + 120, max_points=2)

    assert result["bucket_seconds"] == 60
    points = result["points"]
    assert len(points) == 2
    assert points[0]["count"] == 2
    assert points[0]["temperature_c"] == 21.0
    assert points[0]["temperature_min"] == 20.0
    assert points[0]["temperature_max"] == 22.0
    assert points[0]["pm2_5_ugm3"] == 15.0
    assert points[0]["noise_leq_db_a"] == 42.0
    assert points[0]["noise_max"] == 48.0
    assert points[0]["noise_l90_db_a"] == 39.0
    assert points[1]["temperature_c"] == 26.0


def test_series_empty_or_invalid_range(repository: Repository) -> None:
    assert repository.series(100, 100)["points"] == []
    assert repository.series(200, 100)["points"] == []
    result = repository.series(1_700_000_000, 1_700_000_100)
    assert result["points"] == []


def test_aggregate_day_and_daily_queries(repository: Repository) -> None:
    day = dt.date(2026, 10, 30)
    start, end = day_bounds(day, dt.UTC)
    repository.insert_sample(sample(start + 60, temperature=20.0, humidity=40.0, noise=40.0))
    repository.insert_sample(sample(start + 120, temperature=24.0, humidity=50.0, noise=50.0))
    # Outside the day boundary: must not be included.
    repository.insert_sample(sample(end + 60, temperature=30.0))

    result = repository.aggregate_day(day, dt.UTC)

    assert result is not None
    assert result["sample_count"] == 2
    assert result["temperature_avg"] == 22.0
    assert result["temperature_min"] == 20.0
    assert result["temperature_max"] == 24.0
    assert result["noise_leq_avg"] == 45.0
    assert result["noise_max"] == 56.0

    assert repository.get_daily_stat(day)["sample_count"] == 2
    assert repository.daily_stats(days=1, end_day=day)[0]["day"] == "2026-10-30"
    assert repository.daily_stats(days=1, end_day=day - dt.timedelta(days=1)) == []


def test_aggregate_day_without_samples(repository: Repository) -> None:
    assert repository.aggregate_day(dt.date(2020, 1, 1), dt.UTC) is None


def test_prune_and_count(repository: Repository) -> None:
    old, recent = 1_500_000_000, now_ts()
    repository.insert_sample(sample(old))
    repository.insert_sample(sample(recent))

    assert repository.count_before(old + 1) == 1
    assert repository.prune_before(old + 1) == 1
    assert repository.sample_count() == 1
    assert repository.latest_sample()["ts"] == recent


def test_csv_export(repository: Repository) -> None:
    base = 1_700_000_000
    repository.insert_sample(sample(base, errors=["light: offline"]))
    repository.insert_sample(sample(base + 100))

    text = "".join(repository.iter_csv(base, base + 200))
    rows = list(csv.reader(io.StringIO(text)))

    assert rows[0] == CSV_COLUMNS
    assert len(rows) == 3
    assert rows[1][0] == str(base)
    assert rows[1][CSV_COLUMNS.index("errors")] == "light: offline"
    assert rows[2][0] == str(base + 100)

    assert "".join(repository.iter_csv(base + 500, base + 600)).count("\n") == 1


def test_llm_summary_cache(repository: Repository) -> None:
    day = dt.date(2026, 10, 29)
    assert repository.get_llm_summary(day) is None

    repository.save_llm_summary(
        day, model="llama3.1:8b", source="ollama", summary="Quiet day.", created_at=123
    )
    cached = repository.get_llm_summary(day)
    assert cached is not None
    assert cached["summary"] == "Quiet day."
    assert cached["created_at"] == 123

    repository.save_llm_summary(
        day, model="llama3.1:8b", source="ollama", summary="Updated.", created_at=456
    )
    assert repository.get_llm_summary(day)["summary"] == "Updated."


def test_database_stats(repository: Repository, settings) -> None:
    repository.insert_sample(sample(1_700_000_000))
    stats = repository.database_stats(settings.database.sqlite_path)
    assert stats["samples"] == 1
    assert stats["oldest_ts"] == 1_700_000_000
    assert stats["db_size_bytes"] > 0
