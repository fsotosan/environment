"""Command-line interface for the environment monitoring station."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import sys
from pathlib import Path

from envmon import __version__
from envmon.config import (
    CONFIG_ENV_VAR,
    DEFAULT_CONFIG_PATH,
    Settings,
    reload_settings,
)
from envmon.db import (
    get_current_revision,
    make_engine,
    open_database,
    run_migrations,
    sqlite_integrity_check,
)
from envmon.logging import configure_logging
from envmon.maintenance import backup_database, run_retention
from envmon.repository import Repository
from envmon.sensors.manager import SensorManager
from envmon.timeutil import now_ts, today


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="envmon",
        description="24/7 indoor environment monitoring station",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", metavar="PATH", help=f"YAML config file (or ${CONFIG_ENV_VAR})")
    parser.add_argument("--log-level", help="override log level")
    parser.add_argument(
        "--mode",
        choices=["hardware", "simulator"],
        help="override sensors.mode for this invocation",
    )
    parser.add_argument("--version", action="version", version=f"envmon {__version__}")

    # Allow common options on either side of the subcommand. Defaults are
    # SUPPRESS so a subparser value does not overwrite an earlier global one.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"YAML config file (or ${CONFIG_ENV_VAR})",
    )
    common.add_argument("--log-level", default=argparse.SUPPRESS, help="override log level")
    common.add_argument(
        "--mode",
        choices=["hardware", "simulator"],
        default=argparse.SUPPRESS,
        help="override sensors.mode for this invocation",
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = sub.add_parser("run", parents=[common], help="run the collector and web dashboard")
    run.add_argument("--host", help="bind address (default: config web.host)")
    run.add_argument("--port", type=int, help="bind port (default: config web.port)")

    sub.add_parser("init-db", parents=[common], help="create or upgrade the database schema")

    doctor = sub.add_parser(
        "doctor", parents=[common], help="check configuration, database, disk and sensors"
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.add_argument("--skip-sensors", action="store_true", help="do not read the sensors")

    sample = sub.add_parser("sample-once", parents=[common], help="take one reading and store it")
    sample.add_argument("--json", action="store_true", help="print the reading as JSON")

    aggregate = sub.add_parser("aggregate", parents=[common], help="recompute daily statistics")
    aggregate.add_argument("--day", help="single day (YYYY-MM-DD)")
    aggregate.add_argument(
        "--days",
        type=int,
        default=1,
        help="recompute this many previous days (excluding today)",
    )

    prune = sub.add_parser(
        "prune", parents=[common], help="delete raw samples older than retention.raw_days"
    )
    prune.add_argument("--dry-run", action="store_true", help="report without deleting")

    sub.add_parser(
        "backup", parents=[common], help="write a consistent SQLite backup and rotate old files"
    )

    export = sub.add_parser("export", parents=[common], help="export raw samples as CSV")
    export.add_argument("--start", help="ISO start timestamp (default: 24h ago)")
    export.add_argument("--end", help="ISO end timestamp (default: now)")
    export.add_argument("--hours", type=int, default=24, help="used when --start is omitted")
    export.add_argument("-o", "--output", default="-", help="output file or - for stdout")

    summarize = sub.add_parser(
        "summarize", parents=[common], help="print a daily summary (rules or Ollama)"
    )
    summarize.add_argument("--day", help="day (YYYY-MM-DD), default today")
    summarize.add_argument("--force", action="store_true", help="bypass the LLM cache")
    summarize.add_argument("--json", action="store_true", help="print full JSON response")

    sub.add_parser("version", parents=[common], help="print version and environment details")
    return parser


def _parse_ts(value: str | None, settings: Settings, default: int) -> int:
    if not value:
        return default
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=settings.tzinfo)
    return int(parsed.timestamp())


def _parse_day(value: str | None, settings: Settings) -> dt.date:
    return dt.date.fromisoformat(value) if value else today(settings.tzinfo)


def _repository(settings: Settings) -> Repository:
    _engine, session_factory = open_database(settings)
    return Repository(session_factory)


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from envmon.api.app import create_app

    app = create_app(settings)
    uvicorn.run(
        app,
        host=args.host or settings.web.host,
        port=args.port or settings.web.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        timeout_keep_alive=30,
    )
    return 0


def cmd_init_db(_args: argparse.Namespace, settings: Settings) -> int:
    run_migrations(settings.database.url)
    revision = get_current_revision(settings.database.url)
    print(f"database ready: {settings.database.url}")
    print(f"schema revision: {revision}")
    return 0


def cmd_sample_once(args: argparse.Namespace, settings: Settings) -> int:
    repository = _repository(settings)
    manager = SensorManager(settings)
    try:
        reading = manager.read()
    finally:
        manager.close()
    record = reading.as_record()
    written = repository.insert_sample(record) if reading.has_data else False
    if args.json:
        print(json.dumps({"written": written, "reading": record}, indent=2, default=str))
    else:
        if not reading.has_data:
            print("no sensor data:", "; ".join(reading.errors) or "all values unavailable")
            return 1
        print(f"saved sample ts={record['ts']} mode={record['mode']} written={written}")
        for key in (
            "temperature_c",
            "humidity_pct",
            "pm2_5_ugm3",
            "pm10_ugm3",
            "voc_index",
            "nox_index",
            "lux",
            "noise_db_a",
        ):
            if record.get(key) is not None:
                print(f"  {key}: {record[key]}")
        if reading.errors:
            print("  partial errors: " + "; ".join(reading.errors))
    return 0 if reading.has_data else 1


def cmd_aggregate(args: argparse.Namespace, settings: Settings) -> int:
    repository = _repository(settings)
    if args.day:
        days = [dt.date.fromisoformat(args.day)]
    else:
        anchor = today(settings.tzinfo)
        days = [anchor - dt.timedelta(days=offset) for offset in range(1, args.days + 1)]
    for day in days:
        result = repository.aggregate_day(day, settings.tzinfo)
        if result:
            print(
                f"{day.isoformat()}: {result['sample_count']} samples, "
                f"temp avg {result['temperature_avg']} °C"
            )
        else:
            print(f"{day.isoformat()}: no raw samples, skipped")
    return 0


def cmd_prune(args: argparse.Namespace, settings: Settings) -> int:
    repository = _repository(settings)
    cutoff = now_ts() - settings.retention.raw_days * 86_400
    if args.dry_run:
        count = repository.count_before(cutoff)
        print(
            f"would delete {count} samples older than "
            f"{dt.datetime.fromtimestamp(cutoff, dt.UTC).isoformat()}"
        )
        return 0
    result = run_retention(repository, settings)
    print(f"deleted {result['deleted_samples']} samples older than cutoff {result['cutoff_ts']}")
    return 0


def cmd_backup(_args: argparse.Namespace, settings: Settings) -> int:
    path = backup_database(settings)
    if path is None:
        print("nothing to back up (non-SQLite database or missing file)")
        return 1
    print(path)
    return 0


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    repository = _repository(settings)
    end = _parse_ts(args.end, settings, now_ts() + 1)
    start = _parse_ts(args.start, settings, end - args.hours * 3600)
    if start >= end:
        print("error: start must be before end", file=sys.stderr)
        return 2
    lines = repository.iter_csv(start, end)
    if args.output == "-":
        for chunk in lines:
            sys.stdout.write(chunk)
    else:
        path = Path(args.output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            for chunk in lines:
                handle.write(chunk)
        print(f"wrote {path}", file=sys.stderr)
    return 0


def cmd_summarize(args: argparse.Namespace, settings: Settings) -> int:
    from envmon.llm import DailySummaryService

    repository = _repository(settings)
    service = DailySummaryService(settings, repository)
    day = _parse_day(args.day, settings)
    result = service.get(day, force=args.force)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(result["summary"])
        print()
        print(f"[source: {result['source']} · model: {result.get('model')}]")
        if result.get("error"):
            print(f"[LLM error: {result['error']}]")
    return 0


def _check_ollama(settings: Settings) -> dict[str, object]:
    import urllib.error
    import urllib.request

    url = f"{settings.llm.base_url}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "detail": str(exc)[:200]}
    models = [str(model.get("name")) for model in body.get("models", [])]
    return {"ok": True, "models": models, "model_present": settings.llm.model in models}


def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    checks: list[dict[str, object]] = []
    critical = False

    def add(name: str, ok: bool, detail: str, level: str = "info") -> None:
        nonlocal critical
        checks.append({"name": name, "ok": ok, "detail": detail, "level": level})
        if not ok and level == "critical":
            critical = True

    config_path = Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH).expanduser()
    add(
        "config file",
        config_path.exists(),
        f"{config_path} ({'found' if config_path.exists() else 'missing, using defaults'})",
        "warning" if not config_path.exists() else "info",
    )
    add(
        "python",
        sys.version_info >= (3, 11),
        f"{platform.python_version()} on {platform.machine()} ({platform.system()})",
        "critical" if sys.version_info < (3, 11) else "info",
    )
    add("timezone", True, settings.timezone)
    add("sensor mode", True, settings.sensors.mode)

    engine = None
    try:
        engine = make_engine(settings.database)
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        add("database connection", True, "SQLite" if settings.database.is_sqlite else "PostgreSQL")
        from sqlalchemy import inspect

        tables = inspect(engine).get_table_names()
        schema_ok = "samples" in tables and "daily_stats" in tables
        add(
            "database schema",
            schema_ok,
            f"{len(tables)} tables" + ("" if schema_ok else " — run `envmon init-db`"),
            "critical" if not schema_ok else "info",
        )
    except Exception as exc:
        add("database connection", False, str(exc)[:300], "critical")

    if settings.database.is_sqlite and settings.database.sqlite_path:
        path = settings.database.sqlite_path
        if path.exists():
            ok, message = sqlite_integrity_check(path)
            add("sqlite integrity", ok, message, "critical" if not ok else "info")
            add("database size", True, f"{path.stat().st_size / 1024 / 1024:.1f} MB")
        else:
            add("database file", False, f"{path} not created yet — run `envmon init-db`", "warning")

    if engine is not None:
        engine.dispose()

    i2c_devices = sorted(Path("/dev").glob("i2c-*"))
    add(
        "i2c devices",
        bool(i2c_devices),
        ", ".join(str(path) for path in i2c_devices) or "none found",
        "warning" if not i2c_devices else "info",
    )
    add(
        "i2c permissions",
        all(os.access(device, os.R_OK | os.W_OK) for device in i2c_devices)
        if i2c_devices
        else False,
        "read/write"
        if i2c_devices and all(os.access(d, os.R_OK | os.W_OK) for d in i2c_devices)
        else "check group i2c and udev rules",
        "warning",
    )

    data_dir = settings.database.sqlite_path.parent if settings.database.sqlite_path else Path(".")
    try:
        usage = shutil.disk_usage(data_dir if data_dir.exists() else Path("."))
        add(
            "disk space",
            usage.free > 1_000_000_000,
            f"{usage.free / 1024 / 1024 / 1024:.1f} GB free",
            "warning" if usage.free <= 1_000_000_000 else "info",
        )
    except OSError as exc:
        add("disk space", False, str(exc), "warning")

    sensor_results: list[dict[str, object]] = []
    if not args.skip_sensors:
        try:
            manager = SensorManager(settings)
            try:
                reading = manager.read()
                snapshot = manager.snapshot()
                sensor_results = snapshot.get("sensors", [])
                add(
                    "sensor read",
                    reading.has_data,
                    (
                        f"{len(reading.values)} metrics in {snapshot.get('last_read_duration_s')}s"
                        if reading.has_data
                        else "; ".join(reading.errors) or "no data"
                    ),
                    "warning" if not reading.has_data else "info",
                )
            finally:
                manager.close()
        except Exception as exc:
            add("sensor read", False, str(exc)[:300], "warning")

    if settings.llm.enabled:
        result = _check_ollama(settings)
        detail = result.get("detail") or (
            f"model {settings.llm.model} "
            + ("available" if result.get("model_present") else "not found")
        )
        add("ollama", bool(result.get("ok")), detail, "warning")
    else:
        add("ollama", True, "disabled (optional external service)")

    payload = {
        "version": __version__,
        "ok": not critical,
        "checks": checks,
        "sensors": sensor_results,
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"envmon doctor {__version__}")
        symbols = {True: "OK  ", False: "FAIL"}
        for check in checks:
            print(f"[{symbols[bool(check['ok'])]}] {check['name']}: {check['detail']}")
        if sensor_results:
            print("sensors:")
            for sensor in sensor_results:
                state = "ok" if sensor.get("ok") else "error"
                detail = sensor.get("last_error") or state
                print(f"  - {sensor.get('name')}: {detail}")
        print("\nresult:", "healthy" if not critical else "critical problems found")
    return 0 if not critical else 1


def cmd_version(_args: argparse.Namespace, settings: Settings) -> int:
    print(f"envmon {__version__}")
    print(f"python {platform.python_version()} on {platform.machine()} ({platform.system()})")
    print(
        f"config file: {Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH).expanduser()}"
    )
    print(f"database: {settings.database.url}")
    print(f"sensor mode: {settings.sensors.mode}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.config:
        config_path = Path(args.config).expanduser()
        if not config_path.exists():
            print(f"error: config file not found: {config_path}", file=sys.stderr)
            return 2
        os.environ[CONFIG_ENV_VAR] = str(config_path)
    if args.mode:
        os.environ["ENVMON_SENSORS__MODE"] = args.mode
    if args.log_level:
        os.environ["ENVMON_LOG_LEVEL"] = args.log_level.upper()

    try:
        settings = reload_settings()
    except Exception as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level, settings.log_file)

    handlers = {
        "run": cmd_run,
        "init-db": cmd_init_db,
        "doctor": cmd_doctor,
        "sample-once": cmd_sample_once,
        "aggregate": cmd_aggregate,
        "prune": cmd_prune,
        "backup": cmd_backup,
        "export": cmd_export,
        "summarize": cmd_summarize,
        "version": cmd_version,
    }
    try:
        return handlers[args.command](args, settings)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        if settings.log_level == "DEBUG":
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
