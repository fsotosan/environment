# HTTP API

Base URL: `http://<station>:8080`. The interactive OpenAPI UI is served at
`/api/docs` and the schema at `/api/openapi.json`.

All responses are JSON unless noted. Timestamps in JSON are UTC epoch seconds
unless the field name ends in `_time`/`_at` and is documented as ISO. The API is
read-only (the only write is the cached LLM summary, triggered internally). There
is no authentication; run the station on a trusted network or behind an
authenticated reverse proxy.

Error responses use FastAPI's standard shape:

```json
{"detail": "invalid day: '2026-13-01'"}
```

| Status | Meaning |
|---|---|
| `200` | Success |
| `403` | Feature disabled by configuration (CSV export) |
| `422` | Invalid query parameter or range |
| `503` | Health check cannot reach the database |

---

## `GET /api/config`

Safe, public configuration for the dashboard. Secrets (database password, Ollama
API key) are never included.

```json
{
  "version": "0.1.0",
  "station_name": "Office",
  "timezone": "Europe/Madrid",
  "mode": "hardware",
  "simulated": false,
  "sampling": {"enabled": true, "interval_seconds": 30},
  "web": {
    "title": "Environment Station",
    "refresh_seconds": 30,
    "chart_default_hours": 24,
    "chart_max_points": 1500,
    "allow_export": true
  },
  "thresholds": {
    "temperature_c": {"min": 18.0, "max": 26.0},
    "humidity_pct": {"min": 30.0, "max": 70.0},
    "pm2_5_ugm3": {"min": null, "max": 25.0},
    "pm10_ugm3": {"min": null, "max": 50.0},
    "voc_index": {"min": null, "max": 250.0},
    "nox_index": {"min": null, "max": 250.0},
    "lux": {"min": 200.0, "max": null},
    "noise_db_a": {"min": null, "max": 65.0}
  },
  "llm": {"enabled": false, "model": null},
  "noise": {"approximate": true, "calibration_offset_db": 0.0}
}
```

`simulated` is true exactly when `sensors.mode` is `simulator`. Clients must show a
simulated-data warning when it is true.

---

## `GET /api/current`

Latest stored sample, or `null` before the first sample.

```json
{
  "sample": {
    "ts": 1793363516,
    "temperature_c": 22.1,
    "humidity_pct": 48.7,
    "pm1_0_ugm3": 11.0,
    "pm2_5_ugm3": 15.3,
    "pm4_0_ugm3": 15.3,
    "pm10_ugm3": 22.9,
    "voc_index": 113.8,
    "nox_index": 10.7,
    "lux": 838.8,
    "noise_db_a": 43.4,
    "noise_peak_db": 56.0,
    "noise_dbfs": -39.1,
    "noise_sample_rate_hz": 2000,
    "noise_clipping_pct": 0.0,
    "mode": "simulator",
    "errors": []
  },
  "server_time": 1793363520
}
```

`errors` contains per-sensor failures for a partial sample. A sample is only
stored when at least one metric is available.

---

## `GET /api/series`

Bucketed history for charts.

| Query | Type | Default | Description |
|---|---|---|---|
| `hours` | int 1–87600 | `24` | Window ending now (ignored when `end` is set) |
| `start` | ISO 8601 | derived | Inclusive start; naive values use `timezone` |
| `end` | ISO 8601 | now | Exclusive end; naive values use `timezone` |
| `max_points` | int 100–10000 | `web.chart_max_points` | Maximum returned buckets |

Response:

```json
{
  "start": 1793277120,
  "end": 1793363520,
  "bucket_seconds": 60,
  "points": [
    {
      "ts": 1793363460,
      "count": 2,
      "temperature_c": 22.1,
      "temperature_min": 21.9,
      "temperature_max": 22.3,
      "humidity_pct": 48.6,
      "humidity_min": 48.1,
      "humidity_max": 49.0,
      "pm1_0_ugm3": 11.0,
      "pm2_5_ugm3": 15.2,
      "pm2_5_max": 16.0,
      "pm4_0_ugm3": 15.2,
      "pm10_ugm3": 22.8,
      "pm10_max": 24.0,
      "voc_index": 113.8,
      "voc_max": 118.0,
      "nox_index": 10.7,
      "nox_max": 11.0,
      "lux": 838.8,
      "lux_min": 830.0,
      "lux_max": 847.0,
      "noise_db_a": 43.4,
      "noise_max": 47.0
    }
  ],
  "server_time": 1793363521,
  "timezone": "Europe/Madrid"
}
```

`ts` is the bucket start. Empty windows return an empty `points` array. A start
greater than or equal to the end, a range over 3650 days or unparsable ISO values
return `422`.

Example:

```bash
curl -s 'http://localhost:8080/api/series?hours=6&max_points=300'
```

---

## `GET /api/daily`

Daily aggregates written by the maintenance scheduler.

| Query | Type | Default | Description |
|---|---|---|---|
| `days` | int 1–3650 | `30` | Number of days ending at `end` |
| `end` | date `YYYY-MM-DD` | today (station TZ) | Last day included |

```json
{
  "days": [
    {
      "day": "2026-10-29",
      "sample_count": 2880,
      "temperature_avg": 22.1,
      "temperature_min": 20.5,
      "temperature_max": 24.0,
      "humidity_avg": 46.2,
      "humidity_min": 40.1,
      "humidity_max": 52.3,
      "pm2_5_avg": 12.0,
      "pm2_5_max": 30.0,
      "pm10_avg": 20.0,
      "pm10_max": 44.0,
      "voc_avg": 110.0,
      "voc_max": 150.0,
      "nox_avg": 8.0,
      "nox_max": 12.0,
      "lux_avg": 450.0,
      "lux_min": 20.0,
      "lux_max": 900.0,
      "noise_avg": 42.0,
      "noise_max": 70.0
    }
  ],
  "end_day": "2026-10-30",
  "timezone": "Europe/Madrid"
}
```

Today may be absent until the next day's aggregation (use `/api/series` for live
data). Days without any samples are omitted.

---

## `GET /api/daily/{day}`

One day's aggregate plus its UTC bounds.

```json
{
  "day": "2026-10-29",
  "stat": {"day": "2026-10-29", "sample_count": 2880, "...": "..."},
  "bounds": {"start_ts": 1793232000, "end_ts": 1793318400}
}
```

`stat` is `null` if that day has not been aggregated. Invalid dates return `422`.

---

## `GET /api/summary`

Narrative summary for one day.

| Query | Type | Default | Description |
|---|---|---|---|
| `day` | date | today (station TZ) | Day to summarise |
| `refresh` | bool | `false` | Bypass the cached LLM response |

```json
{
  "day": "2026-10-29",
  "stats": {"sample_count": 2880, "temperature_avg": 22.1, "...": "..."},
  "generated_at": 1793363521,
  "summary": "On 2026-10-29, 2,880 samples were recorded. ...",
  "source": "rules",
  "model": "rules",
  "cached": false
}
```

- `source` is `rules` or `ollama`.
- Without LLM configuration the deterministic rules text is always returned.
- If Ollama is enabled but unreachable, the response still succeeds with
  `source: "rules"` and an `error` field; subsequent requests use a five-minute
  cooldown.
- A cached response has `cached: true` and may include `created_at`.
- Sound values in the text are explicitly described as approximate.

`refresh=true` asks for a new LLM call; it does not bypass configuration or the
failure cooldown.

---

## `GET /api/export.csv`

Raw samples as CSV.

| Query | Type | Default | Description |
|---|---|---|---|
| `start` | ISO 8601 | derived | Inclusive start |
| `end` | ISO 8601 | now | Exclusive end |
| `hours` | int 1–87600 | `24` | Used when `start` is omitted |

The response is `text/csv; charset=utf-8` with a download filename. The header is:

```
ts,utc_time,temperature_c,humidity_pct,pm1_0_ugm3,pm2_5_ugm3,pm4_0_ugm3,
pm10_ugm3,voc_index,nox_index,lux,noise_db_a,noise_peak_db,noise_dbfs,
noise_sample_rate_hz,noise_clipping_pct,mode,errors
```

`utc_time` is ISO 8601 UTC; `errors` joins multiple errors with `; `. Returns
`403` when `web.allow_export` is false and `422` for an invalid range.

---

## `GET /api/status`

Operational status for the dashboard and monitoring integrations.

```json
{
  "version": "0.1.0",
  "station_name": "Office",
  "timezone": "Europe/Madrid",
  "mode": "hardware",
  "simulated": false,
  "ok": true,
  "degraded": false,
  "sample_age_seconds": 12,
  "latest": {"ts": 1793363516, "...": "..."},
  "sampler": {
    "running": true,
    "started_at": 1793350000,
    "uptime_seconds": 13521,
    "last_sample_ts": 1793363516,
    "last_success_ts": 1793363516,
    "last_attempt_ts": 1793363516,
    "data_age_seconds": 5,
    "last_error": null,
    "consecutive_failures": 0,
    "samples_written": 450,
    "samples_skipped": 0,
    "interval_seconds": 30,
    "sensors": {
      "mode": "hardware",
      "last_read_ts": 1793363516,
      "last_read_duration_s": 1.2,
      "sensors": [
        {
          "name": "sen5x",
          "ok": true,
          "last_ok_ts": 1793363516,
          "last_error": null,
          "consecutive_errors": 0,
          "total_errors": 0
        }
      ]
    }
  },
  "sensors": ["..."],
  "recent_errors": [],
  "database": {
    "samples": 1296000,
    "oldest_ts": 1730000000,
    "newest_ts": 1793363516,
    "db_size_bytes": 250000000
  },
  "maintenance": {
    "enabled": true,
    "last_run": {"aggregate:2026-10-30": "2026-10-30T01:00"},
    "last_result": {"aggregate": {"day": "2026-10-29", "sample_count": 2880}}
  },
  "llm": {"enabled": false, "model": null},
  "server_time": 1793363521
}
```

- `ok` requires a stored sample no older than
  `max(interval_seconds * 2 + 15, 90)` seconds.
- `degraded` is true when any configured sensor currently reports `ok: false`.
- `sample_age_seconds` is `null` before the first sample.
- Sensor status is process-local and resets on restart.

---

## `GET /api/healthz`

Liveness check, excluded from the OpenAPI schema.

```json
{"status": "ok", "time": 1793363521, "version": "0.1.0"}
```

Returns `503` with `status: "error"` if the database cannot be queried. It does not
check sensor freshness; use `/api/status` for that.

---

## Dashboard

`GET /` returns the server-rendered dashboard (HTML). It uses the same `/api`
endpoints and has no separate API. `GET /favicon.svg`, `/static/*` and
`/api/docs` are also served by the app.

## Notes for clients

- Poll `/api/current` and `/api/status` at `web.refresh_seconds` or slower.
- Prefer `/api/series` to raw samples for charts; the server buckets to
  `max_points`.
- Treat all noise values as approximate. Do not build safety or compliance logic
  on them.
- Epoch timestamps are UTC; convert with the `timezone` field for display.
- The API does not send CORS headers. Put a reverse proxy in front if a browser
  client on another origin needs access.
