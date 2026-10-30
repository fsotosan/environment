"""Time helpers. Storage is always UTC epoch seconds; days use the station TZ."""

from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo


def now_ts() -> int:
    return int(time.time())


def day_bounds(day: dt.date, tz: ZoneInfo) -> tuple[int, int]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def local_date(ts: int, tz: ZoneInfo) -> dt.date:
    return dt.datetime.fromtimestamp(ts, tz=tz).date()


def today(tz: ZoneInfo) -> dt.date:
    return dt.datetime.now(tz=tz).date()


def parse_day(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def utc_iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat().replace("+00:00", "Z")


def local_iso(ts: int, tz: ZoneInfo) -> str:
    return dt.datetime.fromtimestamp(ts, tz=tz).isoformat()


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
