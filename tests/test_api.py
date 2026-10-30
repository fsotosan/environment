"""HTTP API and dashboard tests (no background sampler)."""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from envmon.api.app import create_app
from envmon.config import Settings
from envmon.repository import Repository
from envmon.sensors.manager import SensorManager
from envmon.sensors.simulator import SimulatorSensor
from envmon.timeutil import now_ts, today

from .test_repository import sample


def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    start_background: bool = False,
) -> TestClient:
    manager = SensorManager(settings, [SimulatorSensor(settings)])
    app = create_app(
        settings,
        session_factory=session_factory,
        manager=manager,
        start_background=start_background,
    )
    return TestClient(app)


def test_config_endpoint(settings: Settings, session_factory) -> None:
    with client(settings, session_factory) as http:
        response = http.get("/api/config")

    assert response.status_code == 200
    body = response.json()
    assert body["station_name"] == "Test Office"
    assert body["simulated"] is True
    assert body["mode"] == "simulator"
    assert body["noise"]["approximate"] is True
    assert body["llm"] == {"enabled": False, "model": None}
    assert body["thresholds"]["temperature_c"] == {"min": 18.0, "max": 26.0}


def test_dashboard_and_security_headers(settings: Settings, session_factory) -> None:
    with client(settings, session_factory) as http:
        response = http.get("/")

    assert response.status_code == 200
    assert "Test Office" in response.text
    assert "SIMULATED DATA" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "script-src 'self' 'unsafe-eval'" in response.headers["content-security-policy"]


def test_current_series_and_export(
    settings: Settings, session_factory, repository: Repository
) -> None:
    ts = now_ts() - 60
    repository.insert_sample(sample(ts, temperature=23.0, noise=44.0))

    with client(settings, session_factory) as http:
        current = http.get("/api/current").json()
        series = http.get("/api/series", params={"hours": 1}).json()
        export = http.get("/api/export.csv", params={"hours": 1})

    assert current["sample"]["ts"] == ts
    assert current["sample"]["temperature_c"] == 23.0

    assert series["bucket_seconds"] >= 1
    assert len(series["points"]) == 1
    assert series["points"][0]["temperature_c"] == 23.0
    assert series["timezone"] == "UTC"

    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in export.headers["content-disposition"]
    assert export.text.splitlines()[0].startswith("ts,utc_time,temperature_c")
    assert str(ts) in export.text


def test_daily_and_summary_endpoints(
    settings: Settings, session_factory, repository: Repository
) -> None:
    day = today(dt.UTC)
    repository.insert_sample(sample(now_ts() - 30, temperature=22.0, noise=41.0))
    repository.aggregate_day(day, dt.UTC)

    with client(settings, session_factory) as http:
        daily = http.get("/api/daily", params={"days": 1}).json()
        detail = http.get(f"/api/daily/{day.isoformat()}").json()
        summary = http.get("/api/summary").json()

    assert daily["days"][0]["day"] == day.isoformat()
    assert daily["days"][0]["sample_count"] == 1
    assert detail["stat"]["sample_count"] == 1
    assert detail["bounds"]["start_ts"] < detail["bounds"]["end_ts"]

    assert summary["source"] == "rules"
    assert summary["model"] == "rules"
    assert summary["day"] == day.isoformat()
    assert summary["stats"]["sample_count"] == 1
    assert "approximate" in summary["summary"]


def test_status_and_health(settings: Settings, session_factory, repository: Repository) -> None:
    ts = now_ts() - 5
    repository.insert_sample(sample(ts))

    with client(settings, session_factory) as http:
        status = http.get("/api/status")
        health = http.get("/api/healthz")

    assert status.status_code == 200
    body = status.json()
    assert body["ok"] is True
    assert body["simulated"] is True
    assert body["latest"]["ts"] == ts
    assert body["database"]["samples"] == 1
    assert body["maintenance"]["enabled"] is True
    assert body["server_time"] >= ts

    assert health.status_code == 200
    assert health.json()["status"] == "ok"


def test_status_without_samples_is_not_ok(settings: Settings, session_factory) -> None:
    with client(settings, session_factory) as http:
        body = http.get("/api/status").json()
    assert body["ok"] is False
    assert body["sample_age_seconds"] is None


def test_invalid_queries_return_422(settings: Settings, session_factory) -> None:
    with client(settings, session_factory) as http:
        assert http.get("/api/series", params={"hours": 0}).status_code == 422
        assert http.get("/api/series", params={"start": "bad"}).status_code == 422
        assert (
            http.get(
                "/api/series",
                params={"start": "2026-01-02T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
            ).status_code
            == 422
        )
        assert http.get("/api/daily/bad-day").status_code == 422
        assert http.get("/api/daily", params={"days": 0}).status_code == 422


def test_export_can_be_disabled(settings: Settings, session_factory) -> None:
    disabled = settings.model_copy(
        update={"web": settings.web.model_copy(update={"allow_export": False})}
    )
    with client(disabled, session_factory) as http:
        response = http.get("/api/export.csv")
    assert response.status_code == 403
