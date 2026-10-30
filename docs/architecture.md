# Architecture

## Goals

- Run unattended for months on a Raspberry Pi CM5.
- Keep all data local by default and work without internet access.
- Make hardware failure visible instead of silently substituting data.
- Stay small enough to audit: one Python package, one process, no Node build step.
- Allow PostgreSQL/TimescaleDB and an external Ollama server without making either
  mandatory.

Non-goals for v1: multi-station federation, alerts/webhooks, CO₂ measurement,
certified acoustic measurement and a mobile app.

## Runtime topology

```
                    Raspberry Pi CM5
┌─────────────────────────────────────────────────────────────┐
│ envmon process                                              │
│                                                             │
│  ┌──────────────┐   asyncio.to_thread   ┌────────────────┐  │
│  │ Sampler loop │──────────────────────▶│ SensorManager  │  │
│  │  every 30 s  │                       │  SEN5x         │  │
│  └──────┬───────┘                       │  TSL2561       │  │
│         │ insert_sample                 │  Noise/ADC     │  │
│         ▼                               │  or Simulator  │  │
│  ┌──────────────┐                       └────────────────┘  │
│  │ Repository   │                                           │
│  └──────┬───────┘                                           │
│         │ SQLAlchemy                                        │
│         ▼                                                   │
│  ┌──────────────┐      ┌─────────────────────────────┐     │
│  │ SQLite (WAL) │◀────▶│ MaintenanceScheduler        │     │
│  │ or PostgreSQL│      │ aggregate / prune / backup  │     │
│  └──────┬───────┘      └─────────────────────────────┘     │
│         │                                                   │
│         ▼                                                   │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ FastAPI + Uvicorn                                    │  │
│  │  /               Jinja2 dashboard + ECharts (local)  │  │
│  │  /api/*          JSON, CSV, OpenAPI                  │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                 │ optional HTTPS, external network
                 ▼
          Ollama on another host (disabled by default)
```

The collector, API and maintenance scheduler share a process. This keeps sensor
health, sampler state and maintenance results visible through `/api/status`.
systemd is responsible for restarting the process; Docker is an optional
alternative.

## Startup sequence

1. `envmon` loads configuration (defaults → YAML → `.env` → `ENVMON_*`).
2. `envmon run` builds the FastAPI app.
3. The lifespan handler opens the database and runs Alembic migrations
   (`open_database()`), then constructs the repository, sensor manager, sampler,
   summary service and maintenance scheduler.
4. If sampling is enabled, the sampler task starts immediately and takes its first
   reading without waiting for a full interval.
5. If maintenance is enabled, the scheduler performs a startup catch-up aggregation
   for recent days, then runs a five-minute check loop.
6. Uvicorn serves the dashboard and API.
7. On shutdown, maintenance stops, the sampler stops and the sensor buses and
   engine are closed.

`create_app(..., start_background=False)` is used by tests to skip steps 4–5.

## Sampling path

`Sampler.run()` is an asyncio loop. Each iteration:

1. waits until the next scheduled time (interruptible by shutdown);
2. calls `SensorManager.read()` through `asyncio.to_thread`, so a blocking I²C
   read cannot block the event loop;
3. writes the resulting `Reading` through `Repository.insert_sample()` in a worker
   thread;
4. schedules the next attempt at the normal interval after a complete reading, or
   at `sampling.failure_retry_seconds` after a total failure.

`SensorManager` holds a lock so concurrent reads cannot interleave on the I²C bus.
Each configured sensor is read independently:

- a successful read updates its values and resets its error counters;
- a failed read records the error, increments counters and closes the sensor so
  the next attempt can reopen the bus;
- one failed sensor does not discard values from the others.

A reading with no values at all is not stored. A partial reading is stored with
its `errors` list, and `/api/status` reports `degraded: true`.

The simulator is a single sensor selected only by explicit
`sensors.mode: simulator`. It is deterministic per process and generates plausible
daily cycles, including a periodic noise bump. It is never used as a fallback.

## Sensor details

| Component | Bus | Address | Notes |
|---|---|---|---|
| SEN54 / SEN55 | I²C 1 | `0x69` | PM1/2.5/4/10, temperature, humidity, VOC index; SEN55 adds NOx |
| TSL2561 | I²C 1 | `0x29` | Two-channel light sensor, lux via datasheet formula |
| Grove Base HAT ADC | I²C 1 | `0x04` or `0x08` | 12-bit polled ADC; microphone on A0 by default |
| Grove microphone | Analog A0 | — | Biased analog output; no audio interface |

The Base HAT exposes a single Grove I²C connector, so SEN5x and TSL2561 need a
Grove I²C Hub. The addresses do not conflict.

### Noise estimator

`noise_analysis.estimate_noise()` treats the ADC samples as an audio waveform:

1. convert counts to volts and remove the DC mean;
2. apply a Hann window and an FFT;
3. zero bins below `highpass_hz`;
4. multiply power by the IEC 61672 A-weighting curve (optional);
5. reconstruct one-sided power with Parseval's theorem;
6. convert RMS volts to dB SPL using `sensitivity_mv_per_pa` and 20 µPa;
7. add `calibration_offset_db`; also report peak dB, dBFS and clipping percentage.

The I²C-polled ADC typically reaches 1–2 kHz, so the bandwidth is far below a
sound-level meter. Values are for trends only. The measured sample rate is stored
with every sample. If the measured rate is below 200 Hz or cannot support the
configured high-pass, the sensor fails loudly rather than returning nonsense.

## Persistence

### Schema

`samples` (one row per reading, primary key `id`, unique `ts`):

- `ts` — UTC epoch seconds (`BIGINT`, unique index);
- temperature, humidity, PM1/2.5/4/10, VOC index, NOx index, lux;
- noise: `noise_db_a`, `noise_peak_db`, `noise_dbfs`,
  `noise_sample_rate_hz`, `noise_clipping_pct`;
- `mode` — `hardware` or `simulator`;
- `errors` — JSON array of per-sensor error strings.

`daily_stats` (primary key `day`): min/avg/max or avg/max for each metric over the
station-local day, plus `sample_count` and `updated_at`.

`llm_summaries` (primary key `day`): cached model, source, text and creation time.

Migrations live in `src/envmon/migrations/versions/` and run automatically at
startup. No `create_all()` is used in production.

### SQLite defaults

- WAL journal, `synchronous=NORMAL`, `foreign_keys=ON`, memory temp store and a
  configurable busy timeout (5 s by default).
- One process writes; readers can continue during writes thanks to WAL.
- The default database is `data/envmon.db`; systemd moves it to
  `/var/lib/envmon/envmon.db`.

At a 30 s interval a station produces 2,880 samples/day (~1.05 M/year). Two years
of raw data is roughly 2.1 M rows, comfortably within SQLite's range on a CM5.
Daily rows remain small indefinitely.

### PostgreSQL

Set `database.url` to `postgresql+psycopg://…`. The same SQL is used; epoch-second
integer arithmetic avoids dialect-specific date functions. TimescaleDB is optional:
the schema works as ordinary PostgreSQL tables and can be converted to a
hypertable later if desired. The built-in backup command only handles SQLite;
use `pg_dump` for PostgreSQL.

## Series bucketing

`Repository.series(start, end, max_points)` returns at most `max_points` points:

```
bucket_seconds = ceil((end - start) / max_points)
bucket_start   = ts - (ts % bucket_seconds)
```

Buckets are computed from the Unix epoch, so they are stable across requests. Each
point contains the bucket start, sample count and averages/minima/maxima for the
metrics used by the charts. Integer modulo arithmetic behaves the same on SQLite
and PostgreSQL; plain `/ *` can be floating-point division on some dialects and was
deliberately avoided.

## Daily summaries

`DailySummaryService` always builds a deterministic summary from the day's
statistics. When `llm.enabled` is true it may replace the narrative with an Ollama
response:

- responses are cached per day and model for `llm.cache_hours`;
- a failed request triggers a five-minute in-process cooldown and an immediate
  fallback to the rules text;
- the prompt forbids invented numbers, emergency claims and certified sound
  measurement claims;
- the LLM never participates in sampling, retention or alert decisions.

Ollama is an external service. The Pi does not host a model runtime.

## Maintenance

`MaintenanceScheduler` wakes every five minutes and, once per local day after the
configured hour:

- aggregates yesterday into `daily_stats` (plus a startup catch-up for recent
  missing days);
- prunes raw samples older than `retention.raw_days`;
- vacuums SQLite after a real deletion;
- writes a consistent SQLite backup via the stdlib backup API, compresses it and
  rotates old files.

Manual equivalents are exposed as `envmon aggregate`, `prune`, `backup` and
`export`. Daily aggregates are retained even when raw samples are pruned.

## Web layer

- FastAPI with Uvicorn; sync route handlers run in the threadpool and use
  short-lived SQLAlchemy sessions from a shared `sessionmaker`.
- Jinja2 renders the dashboard shell; `static/js/app.js` fetches the JSON API and
  draws charts with the vendored ECharts 5 bundle.
- Security headers are applied to every response. CSP permits `'unsafe-eval'` only
  for the self-hosted ECharts UMD bundle and inline styles for the dashboard.
- GZip is enabled for responses over 1 KB.
- `/api/healthz` checks the database and returns 503 on failure.
- No authentication is built in; deploy behind a trusted LAN, VPN or authenticated
  reverse proxy.

See `docs/api.md` for the endpoint contract.

## Failure handling summary

| Failure | Behavior |
|---|---|
| One sensor read fails | Other values stored; status degraded; sensor closed for retry |
| All sensors fail | No row written; retry sooner; error counted and logged |
| Database write fails | Sample dropped for that attempt; error logged; next attempt retries |
| Database unavailable at startup | Process exits; systemd restarts it |
| Ollama unavailable | Rules summary returned; error exposed; cooldown |
| Disk full / backup failure | Logged; sampling continues until the database itself fails |
| Power loss | WAL recovery; fit the CM5 RTC battery or rely on NTP for time |

## Extension points

- **New sensor**: implement the `Sensor` protocol, add settings and wire it in
  `sensors/manager.py`; add columns/migration and update the summary/dashboard.
- **Alerts**: consume `/api/status` or add a separate notifier reading the
  repository; keep alerting out of the request path.
- **Multi-station**: add a `station_id` column and scope every query/index; this is
  deliberately out of scope for v1.
- **CO₂**: a Sensirion SCD41 on the same I²C bus is the natural next metric.
