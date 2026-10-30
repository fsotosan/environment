"""Shared pytest fixtures.

Tests never touch real hardware or the default ``data/envmon.db``. Every test
gets an isolated temporary directory, the ``ENVMON_*`` environment is cleared,
and databases are created with the real migrations.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from envmon.config import Settings
from envmon.db import make_engine, make_session_factory, run_migrations
from envmon.repository import Repository


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for key in list(os.environ):
        if key.startswith("ENVMON_"):
            monkeypatch.delenv(key, raising=False)
    # Keep relative paths (config/config.yaml, .env, data/, backups/) inside the
    # test's temporary directory.
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        station_name="Test Office",
        timezone="UTC",
        database={"url": f"sqlite:///{tmp_path / 'envmon-test.db'}"},
        sensors={"mode": "simulator"},
        retention={
            "backup_dir": str(tmp_path / "backups"),
            "backup_compress": False,
            "vacuum_after_prune": False,
        },
        web={"allow_export": True},
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    run_migrations(settings.database.url)
    engine = make_engine(settings.database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture
def repository(session_factory: sessionmaker[Session]) -> Repository:
    return Repository(session_factory)
