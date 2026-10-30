"""Database engine, migrations and session helpers."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from envmon.config import DatabaseSettings, Settings

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = PACKAGE_DIR / "migrations"


def _prepare_sqlite_path(url: str) -> None:
    raw = url.removeprefix("sqlite:///").removeprefix("sqlite://")
    raw = raw.split("?", 1)[0]
    if not raw or raw == ":memory:":
        return
    path = Path(raw).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)


def _install_sqlite_pragmas(engine: Engine, busy_timeout_ms: int) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        # The stdlib sqlite3 connection supports execute directly. WAL is
        # persistent; the other pragmas are per connection.
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        finally:
            cursor.close()


def make_engine(database: DatabaseSettings | str, *, echo: bool = False) -> Engine:
    if isinstance(database, DatabaseSettings):
        url = database.url
        busy_timeout_ms = database.busy_timeout_ms
        echo = database.echo
    else:
        url = database
        busy_timeout_ms = 5000

    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        _prepare_sqlite_path(url)
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 5
        kwargs["pool_recycle"] = 1800

    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        _install_sqlite_pragmas(engine, busy_timeout_ms)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def _alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Alembic ConfigParser performs %-interpolation; escape percents in URLs.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def run_migrations(url: str) -> None:
    """Upgrade the database schema to the latest revision."""
    if url.startswith("sqlite"):
        _prepare_sqlite_path(url)
    logger.info("running database migrations")
    command.upgrade(_alembic_config(url), "head")


def get_current_revision(url: str) -> str | None:
    from alembic.runtime.migration import MigrationContext

    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def open_database(settings: Settings) -> tuple[Engine, sessionmaker[Session]]:
    """Create and migrate the engine/session factory used by the app."""
    engine = make_engine(settings.database)
    run_migrations(settings.database.url)
    return engine, make_session_factory(engine)


def sqlite_integrity_check(path: Path) -> tuple[bool, str]:
    """Run ``PRAGMA integrity_check`` against a SQLite file."""
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        message = str(row[0]) if row else "no result"
        return message.lower() == "ok", message
    except sqlite3.Error as exc:
        return False, str(exc)
