"""HTTP API routes."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from envmon import __version__
from envmon.config import Settings
from envmon.llm import DailySummaryService
from envmon.repository import Repository
from envmon.timeutil import day_bounds, now_ts, today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _repository(request: Request) -> Repository:
    return request.app.state.repository


def _parse_timestamp(value: str | None, tz: dt.tzinfo, default: int) -> int:
    if not value:
        return default
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return int(parsed.timestamp())


def _parse_day(value: str | None, tz: dt.tzinfo) -> dt.date:
    if not value:
        return today(tz)  # type: ignore[arg-type]
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid day: {value!r}") from exc


@router.get("/config")
def get_config(request: Request) -> dict[str, object]:
    settings = _settings(request)
    return {
        "version": __version__,
        "station_name": settings.station_name,
        "timezone": settings.timezone,
        "mode": settings.sensors.mode,
        "simulated": settings.sensors.mode == "simulator",
        "sampling": {
            "enabled": settings.sampling.enabled,
            "interval_seconds": settings.sampling.interval_seconds,
        },
        "web": {
            "title": settings.web.title,
            "refresh_seconds": settings.web.refresh_seconds,
            "chart_default_hours": settings.web.chart_default_hours,
            "chart_max_points": settings.web.chart_max_points,
            "allow_export": settings.web.allow_export,
        },
        "thresholds": settings.thresholds.model_dump(),
        "llm": {
            "enabled": settings.llm.enabled,
            "model": settings.llm.model if settings.llm.enabled else None,
        },
        "noise": {
            "approximate": True,
            "calibration_offset_db": settings.sensors.noise.calibration_offset_db,
        },
    }


@router.get("/current")
def get_current(request: Request) -> dict[str, object]:
    sample = _repository(request).latest_sample()
    return {"sample": sample, "server_time": now_ts()}


@router.get("/series")
def get_series(
    request: Request,
    hours: Annotated[int, Query(ge=1, le=24 * 3650)] = 24,
    start: str | None = None,
    end: str | None = None,
    max_points: Annotated[int | None, Query(ge=100, le=10_000)] = None,
) -> dict[str, object]:
    settings = _settings(request)
    now = now_ts() + 1
    end_ts = _parse_timestamp(end, settings.tzinfo, now)
    start_ts = _parse_timestamp(start, settings.tzinfo, end_ts - hours * 3600)
    if start_ts >= end_ts:
        raise HTTPException(status_code=422, detail="start must be before end")
    if end_ts - start_ts > 3650 * 86_400:
        raise HTTPException(status_code=422, detail="range is too large")
    result = _repository(request).series(
        start_ts,
        end_ts,
        max_points or settings.web.chart_max_points,
    )
    return {**result, "server_time": now_ts(), "timezone": settings.timezone}


@router.get("/daily")
def get_daily(
    request: Request,
    days: Annotated[int, Query(ge=1, le=3650)] = 30,
    end: str | None = None,
) -> dict[str, object]:
    settings = _settings(request)
    end_day = _parse_day(end, settings.tzinfo)
    rows = _repository(request).daily_stats(days, end_day=end_day)
    return {
        "days": rows,
        "end_day": end_day.isoformat(),
        "timezone": settings.timezone,
    }


@router.get("/summary")
async def get_summary(
    request: Request,
    day: str | None = None,
    refresh: bool = False,
) -> dict[str, object]:
    settings = _settings(request)
    summary_service: DailySummaryService = request.app.state.summary_service
    target = _parse_day(day, settings.tzinfo)
    return await asyncio.to_thread(summary_service.get, target, force=refresh)


@router.get("/export.csv")
def export_csv(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    hours: Annotated[int | None, Query(ge=1, le=24 * 3650)] = None,
) -> StreamingResponse:
    settings = _settings(request)
    if not settings.web.allow_export:
        raise HTTPException(status_code=403, detail="CSV export is disabled")
    now = now_ts() + 1
    end_ts = _parse_timestamp(end, settings.tzinfo, now)
    start_ts = _parse_timestamp(start, settings.tzinfo, end_ts - (hours or 24) * 3600)
    if start_ts >= end_ts:
        raise HTTPException(status_code=422, detail="start must be before end")

    repository = _repository(request)
    filename = (
        f"envmon-{dt.datetime.fromtimestamp(start_ts, tz=settings.tzinfo):%Y%m%d-%H%M}"
        f"-{dt.datetime.fromtimestamp(end_ts, tz=settings.tzinfo):%Y%m%d-%H%M}.csv"
    )
    return StreamingResponse(
        repository.iter_csv(start_ts, end_ts),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/status")
def get_status(request: Request) -> dict[str, object]:
    settings = _settings(request)
    repository = _repository(request)
    sampler = request.app.state.sampler
    manager = request.app.state.manager

    latest = repository.latest_sample()
    now = now_ts()
    sample_age = now - int(latest["ts"]) if latest else None
    max_age = max(settings.sampling.interval_seconds * 2 + 15, 90)
    db_stats = repository.database_stats(settings.database.sqlite_path)
    sampler_snapshot = (
        sampler.snapshot()
        if sampler
        else {
            "running": False,
            "sensors": manager.snapshot(),
            "last_success_ts": latest["ts"] if latest else None,
            "data_age_seconds": sample_age,
        }
    )
    sensors = sampler_snapshot.get("sensors", {})
    sensor_list = sensors.get("sensors", []) if isinstance(sensors, dict) else []
    degraded = any(sensor.get("ok") is False for sensor in sensor_list)
    recent_errors = [sensor.get("last_error") for sensor in sensor_list if sensor.get("last_error")]
    maintenance = request.app.state.maintenance
    return {
        "version": __version__,
        "station_name": settings.station_name,
        "timezone": settings.timezone,
        "mode": settings.sensors.mode,
        "simulated": settings.sensors.mode == "simulator",
        "ok": bool(latest) and sample_age is not None and sample_age <= max_age,
        "degraded": degraded,
        "sample_age_seconds": sample_age,
        "latest": latest,
        "sampler": sampler_snapshot,
        "sensors": sensor_list,
        "recent_errors": recent_errors,
        "database": db_stats,
        "maintenance": {
            "enabled": settings.retention.maintenance_enabled,
            "last_run": maintenance.last_run if maintenance else {},
            "last_result": maintenance.last_result if maintenance else {},
        },
        "llm": {
            "enabled": settings.llm.enabled,
            "model": settings.llm.model if settings.llm.enabled else None,
        },
        "server_time": now,
    }


@router.get("/healthz", include_in_schema=False)
def healthz(request: Request) -> JSONResponse:
    try:
        _repository(request).ping()
    except Exception as exc:
        logger.exception("health check failed")
        return JSONResponse(
            {"status": "error", "detail": str(exc)[:200], "time": now_ts()},
            status_code=503,
        )
    return JSONResponse({"status": "ok", "time": now_ts(), "version": __version__})


@router.get("/daily/{day}")
def get_day(request: Request, day: str) -> dict[str, object]:
    settings = _settings(request)
    target = _parse_day(day, settings.tzinfo)
    stat = _repository(request).get_daily_stat(target)
    start, end = day_bounds(target, settings.tzinfo)
    return {
        "day": target.isoformat(),
        "stat": stat,
        "bounds": {"start_ts": start, "end_ts": end},
    }
