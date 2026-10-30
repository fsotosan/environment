"""Backups, retention and scheduled aggregation."""

from __future__ import annotations

import datetime as dt
import gzip
import os
import sqlite3

import pytest

from envmon.config import Settings
from envmon.maintenance import (
    _rotate_backups,
    aggregate_missing_days,
    backup_database,
    run_retention,
)
from envmon.repository import Repository
from envmon.timeutil import now_ts

from .test_repository import sample


def settings_with(settings: Settings, **retention) -> Settings:
    return settings.model_copy(
        update={"retention": settings.retention.model_copy(update=retention)}
    )


@pytest.mark.parametrize("compress", [True, False])
def test_backup_database(
    settings: Settings, repository: Repository, tmp_path, compress: bool
) -> None:
    repository.insert_sample(sample(now_ts()))
    configured = settings_with(settings, backup_compress=compress)

    path = backup_database(configured)

    assert path is not None
    assert path.exists()
    assert path.parent == tmp_path / "backups"
    assert path.suffix == (".gz" if compress else ".db")

    if compress:
        unpacked = tmp_path / "unpacked.db"
        with gzip.open(path, "rb") as source, unpacked.open("wb") as target:
            target.write(source.read())
        backup_path = unpacked
    else:
        backup_path = path

    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_backup_returns_none_for_missing_or_external_database(
    settings: Settings,
) -> None:
    missing = settings.model_copy(
        update={"database": settings.database.model_copy(update={"url": "sqlite:///nope.db"})}
    )
    assert backup_database(missing) is None

    postgres = settings.model_copy(
        update={
            "database": settings.database.model_copy(
                update={"url": "postgresql+psycopg://envmon:secret@db:5432/envmon"}
            )
        }
    )
    assert backup_database(postgres) is None


def test_backup_rotation_keeps_newest(tmp_path) -> None:
    files = []
    for index, name in enumerate(["envmon-old.db", "envmon-mid.db.gz", "envmon-new.db"]):
        path = tmp_path / name
        path.write_bytes(b"x")
        os.utime(path, (100 + index * 100, 100 + index * 100))
        files.append(path)

    _rotate_backups(tmp_path, keep=2)

    assert not files[0].exists()
    assert files[1].exists()
    assert files[2].exists()


def test_run_retention_deletes_only_old_samples(settings: Settings, repository: Repository) -> None:
    old = now_ts() - 3 * 86_400
    fresh = now_ts() - 60
    repository.insert_sample(sample(old))
    repository.insert_sample(sample(fresh))

    result = run_retention(repository, settings_with(settings, raw_days=1))

    assert result["deleted_samples"] == 1
    assert result["cutoff_ts"] > old
    assert repository.sample_count() == 1
    assert repository.latest_sample()["ts"] == fresh


def test_aggregate_missing_days(settings: Settings, repository: Repository) -> None:
    yesterday = dt.datetime.now(tz=dt.UTC).date() - dt.timedelta(days=1)
    noon = dt.datetime.combine(yesterday, dt.time(12, 0), tzinfo=dt.UTC)
    repository.insert_sample(sample(int(noon.timestamp())))

    aggregated = aggregate_missing_days(repository, settings)

    assert aggregated == [yesterday.isoformat()]
    assert repository.get_daily_stat(yesterday)["sample_count"] == 1
    # Second run is a no-op.
    assert aggregate_missing_days(repository, settings) == []


def test_aggregate_missing_days_without_data(settings: Settings, repository: Repository) -> None:
    assert aggregate_missing_days(repository, settings) == []
