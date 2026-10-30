# AGENTS.md

Guidance for coding agents and contributors working in this repository.

## Project summary

`envmon` is a single-process Python 3.11+ application that reads Grove sensors on a
Raspberry Pi (CM5 reference platform), stores samples in SQLite or PostgreSQL, and
serves a FastAPI dashboard plus a JSON API. The collector, web server and daily
maintenance scheduler share one process; systemd restarts that process if it dies.

Read these first:

- `README.md` — user-facing setup, wiring, deployment and troubleshooting.
- `docs/architecture.md` — components, data flow, schema and concurrency model.
- `docs/api.md` — HTTP contract.
- `docs/calibration.md` — sensor and noise calibration caveats.
- `config/config.example.yaml` — every setting with comments.

## Development commands

Use the project virtualenv. Do not install into the system Python.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

.venv/bin/ruff check .
.venv/bin/ruff format .
.venv/bin/pytest
.venv/bin/envmon run --mode simulator
```

`pytest` uses temporary SQLite databases, temporary working directories and the
simulator. It must never require a Raspberry Pi, `/dev/i2c-*`, network access or an
Ollama server. Keep it that way.

Useful smoke test without starting a long-running server:

```bash
ENVMON_DATABASE__URL=sqlite:////tmp/opencode/envmon-smoke.db \
  .venv/bin/envmon init-db
ENVMON_DATABASE__URL=sqlite:////tmp/opencode/envmon-smoke.db \
  .venv/bin/envmon sample-once --mode simulator --json
ENVMON_DATABASE__URL=sqlite:////tmp/opencode/envmon-smoke.db \
  .venv/bin/envmon doctor --skip-sensors --mode simulator
```

## Layout

```
src/envmon/
  config.py          Pydantic settings (YAML + .env + ENVMON_* env)
  models.py          SQLAlchemy ORM models
  repository.py      every SQL query, CSV export and LLM cache access
  db.py              engine creation, SQLite PRAGMAs, Alembic runner
  sampler.py         async sampling loop and failure backoff
  maintenance.py     aggregation, retention, SQLite backup and scheduling
  llm.py             deterministic daily summary + optional external Ollama
  logging.py         process-wide logging setup
  timeutil.py        UTC epoch and station-timezone helpers
  sensors/
    base.py          Sensor protocol, Reading, SensorStatus
    manager.py       builds sensors, isolates failures, tracks health
    simulator.py     explicit synthetic data (never an automatic fallback)
    sen5x.py         Sensirion SEN54/SEN55 over I²C
    tsl2561.py       Grove digital light sensor
    grove_adc.py     Grove Base HAT ADC over I²C
    noise.py         microphone capture + estimate
    noise_analysis.py FFT/A-weighting estimator
  api/
    app.py           app factory, lifespan, middleware, dashboard route
    routes.py        /api endpoints
    templates/       Jinja2 dashboard
    static/          CSS, vanilla JS, self-hosted ECharts
  migrations/        Alembic environment and revisions
  cli.py             `envmon` command
tests/               pytest suite (simulator only)
config/              example configuration
deploy/              systemd unit, installer, Grafana provisioning
docs/                architecture, API and calibration notes
```

## Invariants — do not break these

1. **Simulator mode is opt-in.** `sensors.mode` defaults to `hardware`. Never fall
   back to simulated values when hardware fails. The dashboard must keep the
   `SIMULATED DATA` banner whenever mode is `simulator`.
2. **Noise is approximate.** Keep the "approximate" wording in API docs, the
   dashboard, summaries and README. Never present values as certified or suitable
   for safety/legal decisions.
3. **The LLM is optional and display-only.** It must never control sampling,
   retention or alerts. Ollama is external; do not add a model runtime to the Pi
   image or Compose stack. Always fall back to the deterministic rules summary.
4. **All SQL lives in `repository.py`.** Do not scatter queries through routes,
   the sampler or the CLI. Routes stay thin.
5. **Timestamps are UTC epoch seconds** (`BIGINT`). Daily boundaries use the
   configured station timezone via `timeutil.day_bounds`. Series buckets must use
   integer arithmetic that works on both SQLite and PostgreSQL.
6. **One writer per SQLite file.** The sampler and maintenance run in one process;
   the CLI can read while the service runs, but do not start two `envmon run`
   processes against the same database.
7. **Secrets stay out of responses and logs.** `/api/config` exposes only safe
   fields. The Ollama API key is read from settings and never serialized.
8. **Hardware imports stay lazy.** `smbus2` and Sensirion imports must remain
   inside functions/constructors that hardware mode needs, so the simulator and
   tests work on a normal workstation.
9. **Schema changes require a migration.** Update `models.py`, add an Alembic
   revision under `src/envmon/migrations/versions/`, and test `init-db` on a fresh
   SQLite file. Never rely on `create_all()`.
10. **Keep the dependency surface small.** ECharts is vendored; the dashboard must
    work offline on a LAN. Do not introduce a CDN or a Node build step.

## Configuration rules

- Precedence: explicit kwargs > `ENVMON_*` environment > `.env` > YAML at
  `ENVMON_CONFIG` (default `config/config.yaml`) > defaults.
- Nested environment variables use `__`, e.g.
  `ENVMON_SENSORS__NOISE__SAMPLE_RATE_HZ=2000`.
- Common CLI options (`--config`, `--log-level`, `--mode`) work before **and** after
  the subcommand. Keep the `parents=[common]` / `argparse.SUPPRESS` pattern in
  `cli.py`.
- New settings need validation, a default in `config.py`, a commented entry in
  `config/config.example.yaml`, and a row in the README table when user-facing.
- Relative paths (`data/`, `backups/`) are relative to the process working
  directory. The systemd unit sets an explicit working directory and absolute
  paths.

## Testing expectations

Every behavior change needs a test. Existing coverage includes:

- config precedence and validation (`tests/test_config.py`)
- noise DSP and A-weighting (`tests/test_noise.py`)
- TSL2561 lux formula (`tests/test_tsl2561.py`)
- repository/SQL/CSV/LLM cache (`tests/test_repository.py`)
- sampler and sensor error isolation (`tests/test_sampler.py`)
- API/dashboard with `TestClient` (`tests/test_api.py`)
- backup, retention and aggregation (`tests/test_maintenance.py`)
- rules and Ollama fallback (`tests/test_llm.py`)
- CLI parser and lightweight commands (`tests/test_cli.py`)

Guidelines:

- Use the `settings`, `engine`, `session_factory` and `repository` fixtures from
  `tests/conftest.py`.
- `create_app(..., session_factory=..., start_background=False)` avoids starting
  background tasks in API tests.
- Patch `envmon.llm.OllamaClient.chat`; never make a real network call.
- Never read real sensors in tests. Use `SimulatorSensor` or small fake sensors.
- Run `ruff check`, `ruff format --check` and `pytest` before finishing.

## Adding a sensor

1. Implement the `Sensor` protocol: a `name` attribute, `read() -> dict[str, object]`
   returning metric values, and `close()`.
2. Keep imports of hardware libraries lazy and close the bus on every error path.
3. Add explicit settings under `SensorSettings` with validation.
4. Add the sensor in `sensors/manager.py` only when its `enabled` flag is set.
5. Add the metric to `models.Sample`, `CSV_COLUMNS`, `repository` queries (series,
   daily extremes, daily stats), the Alembic schema, the API/dashboard and the
   rules summary as appropriate.
6. Add tests for the conversion logic with synthetic input, plus a manager failure
   isolation case.
7. Update `README.md`, `docs/api.md` and `config/config.example.yaml`.

SQLAlchemy stores nullable metrics as `NULL` when a sensor is absent. Never write
sentinel values such as `-1` or `0` for "no reading".

## Database and migration workflow

```bash
# after changing models.py
.venv/bin/alembic -c alembic.ini revision --autogenerate -m "add metric"
# review the generated file; autogenerate is not authoritative
.venv/bin/envmon init-db
.venv/bin/pytest
```

- The app runs migrations on startup; `envmon init-db` is for setup and recovery.
- SQLite uses WAL, `synchronous=NORMAL`, `foreign_keys=ON` and a busy timeout.
- PostgreSQL users should use `pg_dump`; `envmon backup` intentionally returns
  non-zero for non-SQLite databases.
- Daily stats are recomputed with `envmon aggregate`; raw retention never deletes
  daily rows.
- `backup_database()` uses the SQLite backup API, then optionally gzip-compresses
  and rotates by modification time. Keep it consistent under concurrent writes.

## Frontend rules

- Server-rendered Jinja2 plus vanilla JS; no framework and no build step.
- Self-hosted ECharts lives in `static/vendor/`. The CSP allows `'unsafe-eval'`
  only because the ECharts UMD bundle uses `Function()`. Do not add other inline
  scripts or loosen the CSP further.
- Escape user/data-derived text; do not inject HTML from API values.
- Keep the simulator banner and the health/degraded indicators working.
- Respect `web.allow_export`, `web.chart_max_points` and `web.refresh_seconds`.
- Test both empty-database and populated states.

## Security and deployment

- No authentication is built in. Document reverse-proxy/VPN use; do not claim the
  dashboard is safe to expose to the internet.
- Keep security headers, gzip, the dedicated system user and systemd hardening.
- The Docker image runs as a non-root user and needs `/dev/i2c-1` plus the host I²C
  group for hardware mode. Compose is optional; native systemd is the primary path.
- For PostgreSQL, schema changes must remain compatible with the Alembic migration
  chain. TimescaleDB is optional; the schema does not require it.

## Style

- Python 3.11+ syntax, `from __future__ import annotations`, full type hints on
  public functions.
- Ruff: line length 100, import sorting, `E/F/I/UP/B/SIM/C4/PIE/RUF`. Run the
  formatter rather than hand-wrapping.
- Logging: module-level `logger = logging.getLogger(__name__)`. Log failures with
  context, avoid logging secrets, and do not log on every sample at INFO.
- Public modules and non-obvious functions get short docstrings explaining *why*,
  not restating the code.
- Comments should record hardware constraints and safety caveats (for example why
  the ADC cannot be treated as an audio interface).

## Release checklist

1. `ruff check . && ruff format --check . && pytest`
2. Simulator smoke test: `envmon init-db`, `sample-once`, `doctor --skip-sensors`
3. Fresh-database migration test
4. Hardware check on the Pi: `i2cdetect -y 1`, `envmon doctor`, dashboard
5. Update README/docs/config example for new settings or endpoints
6. Bump `__version__` in `src/envmon/__init__.py` and `pyproject.toml` together
7. Commit with a focused message; tag releases as `vX.Y.Z`
