# Environment Monitoring Station

A self-hosted, 24/7 indoor environment monitor for a Raspberry Pi Compute Module 5
(CM5) and Grove sensors. It samples temperature, humidity, particulate matter, air
quality, illuminance and approximate sound level, stores everything locally, and
serves a live dashboard and JSON API.

Built for the **Raspberry Pi Development Kit (RPI CM5)** with:

- Grove All-in-one Environmental Sensor **SEN54 / SEN55**
- Grove **Digital Light Sensor** (TSL2561)
- Grove **Analog Microphone**
- Grove **Base HAT for Raspberry Pi**

> **Noise is approximate.** The Grove microphone is a raw analog module and the
> Base HAT ADC is not an audio ADC. Readings are useful for trends and relative
> changes, not for certified sound-level measurements. See
> [docs/calibration.md](docs/calibration.md).

---

## What you get

- **Live dashboard** at `http://<station>:8080/` with current cards, 6 h / 24 h /
  7 d / 30 d charts, station health and a daily summary.
- **Local-first storage**: SQLite with WAL by default; PostgreSQL/TimescaleDB
  supported through one config value.
- **Current + historical data**: raw samples (30 s by default), daily aggregates,
  retention, vacuum, compressed daily backups and CSV export.
- **JSON API** with OpenAPI docs at `/api/docs`.
- **Explicit simulator mode** for demos and development. The dashboard shows a
  loud `SIMULATED DATA` banner; hardware failures never silently fall back to
  simulated values.
- **Optional external Ollama summary**. Off by default. The target Pi does not
  host the model.
- **Native systemd deployment** as the primary path; Docker/Compose available.
- **No cloud dependency** and no required outbound network traffic.

---

## Architecture

```
 Grove sensors                         Raspberry Pi CM5
┌──────────────────┐                 ┌──────────────────────────────────────┐
│ SEN54/55  I²C    │                 │ envmon process                       │
│ TSL2561   I²C    │──I²C bus 1─────▶│  SensorManager ──▶ Sampler           │
│ Microphone  A0   │──ADC───────────▶│       │                │             │
└──────────────────┘                 │       ▼                ▼             │
                                     │  SEN5x/TSL/Noise   SQLite / Postgres │
                                     │                        │             │
                                     │  FastAPI + Jinja2 ◀────┘             │
                                     │  dashboard, JSON API, CSV            │
                                     └──────────────────┬───────────────────┘
                                                        │ optional, external
                                                        ▼
                                                Ollama on another host
```

- The collector, web server and maintenance scheduler run in **one process** so
  sensor health is visible in the API. systemd restarts it on failure.
- Timestamps are stored as UTC epoch seconds, so time bucketing works the same
  on SQLite and PostgreSQL. Daily aggregates use the configured station
  timezone.
- SQL schema migrations are managed by Alembic and run automatically on start.

More detail: [docs/architecture.md](docs/architecture.md).

---

## Quick start (simulator, no hardware)

```bash
git clone https://github.com/fsotosan/environment.git
cd environment

python3 -m venv .venv
.venv/bin/pip install -e .

cp config/config.example.yaml config/config.yaml
# Option A: edit config/config.yaml and set sensors.mode: simulator
# Option B: leave hardware mode and override for this run:
.venv/bin/envmon init-db
.venv/bin/envmon run --mode simulator
```

Open <http://localhost:8080/>. The simulator produces plausible office data and
a simulated-data banner.

Useful first commands:

```bash
.venv/bin/envmon doctor --skip-sensors
.venv/bin/envmon sample-once --json
.venv/bin/envmon version
```

---

## Hardware setup

### Wiring

| Sensor | Grove port | I²C address | Notes |
|---|---|---|---|
| SEN54 / SEN55 | I²C port on the Base HAT | `0x69` | 3.3 V / 5 V sensor; PM, RH/T, VOC; SEN55 adds NOx |
| TSL2561 light | I²C port on the Base HAT | `0x29` | Addresses do not conflict |
| Analog microphone | Analog **A0** | Base HAT ADC `0x04` or `0x08` | Raw analog output, biased around mid-rail |
| Base HAT | 40-pin header | `0x04` (STM32) / `0x08` (MM32) | 3.3 V logic; provides the ADC |

The Base HAT has **three** Grove I²C ports, all wired straight to the Pi's I²C
pins (the HAT itself acts as the hub), so the SEN5x and TSL2561 can both be
plugged in directly — no separate Grove I²C Hub is needed. Their addresses
(`0x69` and `0x29`) sit on the same bus as the ADC (`0x04`/`0x08`) without
conflicting. Do not connect 5 V-only Grove devices to the Pi's 3.3 V I²C pins.

### Raspberry Pi OS preparation

```bash
sudo apt update
sudo apt install -y python3-venv i2c-tools
sudo raspi-config          # Interface Options → I2C → enable
sudo usermod -aG i2c "$USER"
# log out and back in (or reboot) for the group change
i2cdetect -y 1
```

Expected devices on bus 1: `29` (TSL2561), `69` (SEN5x), and `04` or `08`
(Base HAT ADC). If `i2cdetect` shows nothing, stop and fix wiring/permissions
before debugging the app.

The CM5 has no battery-backed RTC by default. Fit the CR2032 backup battery or
ensure NTP is working, otherwise timestamps after a power cut will be wrong.

### Install the app natively (systemd)

From a clone of this repository:

```bash
sudo ./deploy/install.sh
sudoedit /etc/envmon/config.yaml     # timezone, ports, thresholds, sensors
sudo systemctl status envmon
journalctl -u envmon -f
```

The installer creates:

| Path | Purpose |
|---|---|
| `/opt/envmon` | application + virtualenv |
| `/etc/envmon/config.yaml` | configuration (created once, never overwritten) |
| `/var/lib/envmon` | SQLite database |
| `/var/backups/envmon` | compressed daily backups |
| `/var/log/envmon` | optional log file directory |

Manual equivalent:

```bash
sudo useradd --system --home /opt/envmon --shell /usr/sbin/nologin envmon
sudo mkdir -p /opt/envmon /etc/envmon /var/lib/envmon /var/backups/envmon
sudo cp -a pyproject.toml README.md LICENSE src /opt/envmon/
sudo python3 -m venv /opt/envmon/.venv
sudo /opt/envmon/.venv/bin/pip install /opt/envmon
sudo cp config/config.example.yaml /etc/envmon/config.yaml
sudo cp deploy/envmon.service /etc/systemd/system/envmon.service
sudo systemctl daemon-reload
sudo systemctl enable --now envmon
```

The service unit overrides the database and backup paths to `/var/lib/envmon`
and `/var/backups/envmon`, so the read-only system hardening works.

### Verify hardware mode

```bash
sudo -u envmon ENVMON_CONFIG=/etc/envmon/config.yaml /opt/envmon/.venv/bin/envmon doctor
sudo -u envmon ENVMON_CONFIG=/etc/envmon/config.yaml /opt/envmon/.venv/bin/envmon sample-once
```

`doctor` reports configuration, database schema, I²C devices and permissions,
disk space, each sensor and (if enabled) the Ollama endpoint. Sensor failures
are warnings; configuration/database failures are critical.

---

## Configuration

Settings are read from, in increasing priority:

1. built-in defaults in `src/envmon/config.py`
2. YAML file at `ENVMON_CONFIG` (default `config/config.yaml`)
3. `.env` file
4. `ENVMON_*` environment variables, with `__` for nesting

Copy [`config/config.example.yaml`](config/config.example.yaml) and edit it. A
minimal production config:

```yaml
station_name: Office
timezone: Europe/Madrid

database:
  url: sqlite:///data/envmon.db

sampling:
  interval_seconds: 30

sensors:
  mode: hardware

web:
  host: 0.0.0.0
  port: 8080
```

Common overrides:

```bash
# Use PostgreSQL instead of SQLite
export ENVMON_DATABASE__URL='postgresql+psycopg://envmon:secret@db.local:5432/envmon'

# Demo mode for this process only
export ENVMON_SENSORS__MODE=simulator

# Change the web port
export ENVMON_WEB__PORT=8080
```

Important settings:

| Key | Default | Description |
|---|---|---|
| `station_name` | `Office` | Shown in the dashboard |
| `timezone` | `UTC` | Daily aggregate boundaries |
| `database.url` | `sqlite:///data/envmon.db` | SQLite or `postgresql+psycopg://…` |
| `sampling.interval_seconds` | `30` | Raw sample interval |
| `sensors.mode` | `hardware` | `hardware` or `simulator` |
| `sensors.sen5x.temperature_offset_c` | `0.0` | Compensate enclosure self-heating |
| `sensors.light.gain` | `16` | Lower to `1` if lux saturates |
| `sensors.noise.window_seconds` | `1.0` | Continuous-capture analysis block length |
| `sensors.noise.calibration_offset_db` | `0.0` | Calibrate against a sound-level meter |
| `retention.raw_days` | `730` | Raw data retention |
| `retention.backup_enabled` | `true` | Daily SQLite backup |
| `web.allow_export` | `true` | Allow CSV download |
| `llm.enabled` | `false` | External Ollama summaries |

The full reference with comments is in `config/config.example.yaml`. Secrets
should come from environment variables or `.env`, not from a committed YAML
file.

---

## Dashboard and API

- Dashboard: `http://<station>:8080/`
- OpenAPI UI: `http://<station>:8080/api/docs`
- Health check: `http://<station>:8080/api/healthz`
- Prometheus-style metrics are intentionally not included; the JSON API is the
  integration point.

Main endpoints:

| Method | Path | Description |
|---|---|---|
| GET | `/api/config` | Safe public configuration (no secrets) |
| GET | `/api/current` | Latest sample |
| GET | `/api/series?hours=24` | Bucketed history for charts |
| GET | `/api/daily?days=30` | Daily aggregates |
| GET | `/api/summary?day=YYYY-MM-DD` | Rules or Ollama daily summary |
| GET | `/api/status` | Sampler, sensors, database and maintenance status |
| GET | `/api/export.csv?hours=720` | Raw CSV export |
| GET | `/api/healthz` | Liveness/DB check |

Examples:

```bash
curl -s http://localhost:8080/api/current | jq
curl -s 'http://localhost:8080/api/series?hours=24' | jq '.points[-1]'
curl -s 'http://localhost:8080/api/export.csv?hours=24' -o samples.csv
```

Full documentation: [docs/api.md](docs/api.md).

### Dashboard data

The dashboard is server-rendered HTML plus a small vanilla-JS client and a
self-hosted Apache ECharts bundle. It has no CDN dependency and works on a
LAN without internet access. Auto-refresh defaults to 30 s; the interval is
configurable.

The dashboard shows a **SIMULATED DATA** banner whenever
`sensors.mode: simulator`, and the status pill distinguishes `Live`,
`Degraded` (partial sensor failure), `No recent data`, and `Simulated`.

---

## Data, retention and backups

Default policy:

- raw samples every 30 s, retained **2 years**;
- daily statistics kept **forever**;
- every day at 01:00 local time, yesterday is aggregated;
- at 03:00 local time, raw data is pruned and a compressed SQLite backup is
  written to `backups/`; the last **14** backups are kept.

Maintenance runs inside the server process and catches up on startup. Manual
commands:

```bash
envmon aggregate --days 7          # rebuild the last 7 daily rows
envmon aggregate --day 2026-10-29  # rebuild one day
envmon prune --dry-run
envmon prune
envmon backup
envmon export --hours 24 -o samples.csv
```

SQLite backups use the `sqlite3` backup API, so they are consistent while the
station is writing. PostgreSQL users should schedule `pg_dump` instead; the
built-in backup command refuses to pretend it can back up Postgres.

Restore a compressed SQLite backup:

```bash
sudo systemctl stop envmon
gunzip -c /var/backups/envmon/envmon-20261030-030000.db.gz > /tmp/envmon.db
sudo install -o envmon -g envmon -m 0640 /tmp/envmon.db /var/lib/envmon/envmon.db
sudo systemctl start envmon
```

---

## External Ollama (optional)

The Pi should **not** run Ollama. Point it at an existing Ollama server on
another machine:

```yaml
llm:
  enabled: true
  base_url: http://192.168.1.10:11434
  model: llama3.1:8b
  timeout_seconds: 60
  cache_hours: 6
```

```bash
curl http://192.168.1.10:11434/api/tags
ollama pull llama3.1:8b        # run on the Ollama host, not the Pi
envmon doctor                  # checks reachability and model presence
envmon summarize --force
```

The generated summary is cached per day in the database. If Ollama is
unreachable, the dashboard transparently falls back to a deterministic summary
built from the same statistics. The LLM is a presentation layer only: it never
controls sampling, retention or alerts, and it is instructed not to claim
emergencies or certified sound measurements.

---

## Docker (optional)

The base Compose file works without any hardware access, so it is suitable for
the simulator and for PostgreSQL deployments. Use the hardware overlay only on a
Pi where `/dev/i2c-1` exists.

```bash
cp config/config.example.yaml config/config.yaml
# set sensors.mode: hardware or simulator

# Simulator / demo (no device access)
docker compose up -d --build
docker compose logs -f envmon

# Hardware: add the I²C device and the host I²C group
export I2C_GID=$(stat -c '%g' /dev/i2c-1)
docker compose -f docker-compose.yml -f docker-compose.hardware.yml up -d --build
```

The base Compose file persists the database in the `envmon-data` volume and
backups in `envmon-backups`. The hardware overlay only adds `/dev/i2c-1` and the
host group, so the container still runs as a non-root user. Set
`ENVMON_I2C_DEVICE` if your bus is not `/dev/i2c-1`. Do not run two containers
against the same SQLite file.

For PostgreSQL + Grafana:

```bash
docker compose --profile postgres up -d postgres
export ENVMON_DATABASE__URL='postgresql+psycopg://envmon:change-me@postgres:5432/envmon'
docker compose up -d --build envmon
docker compose --profile grafana up -d grafana
```

Grafana provisioning is in `deploy/grafana/`. Change the default passwords
before exposing anything beyond localhost.

---

## Noise measurement caveats

- The Base HAT ADC is polled over I²C. Expect roughly **1–2 kHz** effective
  sampling, not 44.1 kHz. The measured rate is stored with each sample.
- Noise is captured continuously in a background thread. One row is written per
  `sampling.interval_seconds` (30 s by default) containing the interval **Leq**
  (energy average), **max**, **L10**, **L50** and **L90**. L10 is the level
  exceeded for 10 % of the interval, L50 the median and L90 the background
  level exceeded for 90 % of the interval.
- The ADC's anti-aliasing and the microphone's frequency response are not
  characterised. A-weighting is applied digitally and the result is labelled
  approximate.
- The default sensitivity (`10 mV/Pa`) is only an estimate. Calibrate
  `calibration_offset_db` against a real sound-level meter. A 94 dB / 1 kHz
  acoustic calibrator is ideal; a phone SPL meter is better than nothing.
- Clipping is recorded as `noise_clipping_pct`. If it is consistently above a
  few percent, the microphone input is too hot.
- Do not use these values for workplace safety, legal compliance or hearing
  conservation decisions.

Calibration procedure: [docs/calibration.md](docs/calibration.md).

---

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest

# Run with synthetic data and autoreload-friendly logs
.venv/bin/envmon run --mode simulator
```

Or use the convenience targets:

```bash
make check    # lint, format check and tests
make smoke    # init-db, sample-once and doctor against a temp SQLite file
make run      # simulator server
```

Tests use temporary SQLite databases and the simulator; no Raspberry Pi or
sensor hardware is required. GitHub Actions runs the same checks on Python 3.11
and 3.12 (`.github/workflows/ci.yml`).

Useful commands:

```bash
.venv/bin/envmon init-db
.venv/bin/envmon doctor --skip-sensors
.venv/bin/envmon sample-once --json
.venv/bin/envmon summarize --json
.venv/bin/envmon aggregate --days 3
.venv/bin/envmon backup
```

Project layout:

```
src/envmon/
  config.py          settings, YAML + env sources, validation
  models.py          SQLAlchemy models
  repository.py      all SQL and CSV export
  sampler.py         async sampling loop
  maintenance.py     aggregation, retention, backups
  llm.py             deterministic + Ollama summaries
  sensors/           SEN5x, TSL2561, ADC, noise analysis, simulator
  api/               FastAPI app, routes, templates, static assets
  migrations/        Alembic revisions
  cli.py             envmon command
tests/               pytest suite (simulator only)
deploy/              systemd unit, installer, Grafana provisioning
```

Architecture and contribution guidance: [AGENTS.md](AGENTS.md) and
[docs/architecture.md](docs/architecture.md).

---

## Security

The dashboard has **no authentication**. Run it on a trusted LAN, or put it
behind a VPN/reverse proxy with authentication. Do not expose port 8080
directly to the internet. The API is read-only except for the summary cache,
but exported CSV contains your environmental history.

Hardening already applied in the systemd unit: dedicated user, `ProtectSystem`,
`ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, and read/write access limited to
the data and backup directories.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `No such file or directory: /dev/i2c-1` | Enable I²C in `raspi-config`, reboot, check `i2cdetect -y 1` |
| `Permission denied` on I²C | `sudo usermod -aG i2c envmon`, re-login, restart the service |
| `i2cdetect` shows no `69` | Check the SEN5x cable and power, and that it is in an I²C port (not an analog/digital port) |
| `SEN5x has no new data yet` | Normal for ~1 s after start, or during automatic fan cleaning; retry |
| Light readings pinned or zero | Lower `sensors.light.gain` to `1`; check for a saturated lux value |
| Sound level always very high/low | Calibrate `calibration_offset_db`; verify the mic is on A0 and the ADC address is right |
| `ADC sampled at only … Hz` | Check I²C bus speed (100 kHz), cable length and power supply |
| `database is locked` | Only one sampler process may write to a SQLite database; stop duplicates |
| Dashboard shows “No recent data” | `systemctl status envmon`, `journalctl -u envmon`, then `envmon doctor` |
| Timestamps jump after power loss | Fit the CM5 RTC battery or fix NTP |
| Ollama summary never appears | `llm.enabled`, `base_url`, firewall, model name; check `envmon doctor` |

---

## License

MIT. See [LICENSE](LICENSE).

Apache ECharts is bundled under the Apache License 2.0; see
`src/envmon/api/static/vendor/LICENSE.echarts`.
