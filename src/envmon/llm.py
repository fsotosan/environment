"""Daily narrative summaries: deterministic rules plus optional external Ollama.

The LLM is a presentation layer only. It never controls sampling, retention or
alerts. If it is disabled or unavailable, a deterministic summary is returned
instead.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from envmon.config import Settings
from envmon.repository import Repository
from envmon.timeutil import day_bounds, now_ts, today

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1"


def _fmt(value: object, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{number:.{digits}f}{suffix}"


def _range(avg: object, low: object, high: object, suffix: str) -> str:
    return f"{_fmt(avg, suffix)} (min {_fmt(low, suffix)}, max {_fmt(high, suffix)})"


def rules_summary(day: dt.date, stats: dict[str, object], settings: Settings) -> str:
    count = int(stats.get("sample_count") or 0)
    if count == 0:
        return f"No samples were recorded on {day.isoformat()}."

    thresholds = settings.thresholds
    observations: list[str] = []
    observations.append(
        "Temperature averaged "
        + _range(
            stats.get("temperature_avg"),
            stats.get("temperature_min"),
            stats.get("temperature_max"),
            " °C",
        )
    )
    observations.append(
        "relative humidity "
        + _range(
            stats.get("humidity_avg"),
            stats.get("humidity_min"),
            stats.get("humidity_max"),
            " %",
        )
    )
    observations.append(
        "PM2.5 averaged "
        + _fmt(stats.get("pm2_5_avg"), " µg/m³")
        + " (max "
        + _fmt(stats.get("pm2_5_max"), " µg/m³")
        + ")"
    )
    observations.append(
        "PM10 averaged "
        + _fmt(stats.get("pm10_avg"), " µg/m³")
        + " (max "
        + _fmt(stats.get("pm10_max"), " µg/m³")
        + ")"
    )
    observations.append(f"VOC index averaged {_fmt(stats.get('voc_avg'), '', 0)}")
    if stats.get("nox_avg") is not None:
        observations.append(f"NOx index averaged {_fmt(stats.get('nox_avg'), '', 0)}")
    observations.append(
        "light averaged "
        + _fmt(stats.get("lux_avg"), " lx", 0)
        + " (min "
        + _fmt(stats.get("lux_min"), " lx", 0)
        + ", max "
        + _fmt(stats.get("lux_max"), " lx", 0)
        + ")"
    )
    observations.append(
        "sound level averaged "
        + _fmt(stats.get("noise_avg"), " dB(A)")
        + " (max "
        + _fmt(stats.get("noise_max"), " dB(A)")
        + ")"
    )

    flags: list[str] = []
    if thresholds.temperature_c.status(stats.get("temperature_avg")) != "ok":
        flags.append("average temperature outside the configured comfort range")
    if thresholds.humidity_pct.status(stats.get("humidity_avg")) != "ok":
        flags.append("average humidity outside the configured comfort range")
    if thresholds.pm2_5_ugm3.status(stats.get("pm2_5_max")) == "high":
        flags.append("PM2.5 peak above the configured attention level")
    if thresholds.pm10_ugm3.status(stats.get("pm10_max")) == "high":
        flags.append("PM10 peak above the configured attention level")
    if thresholds.noise_db_a.status(stats.get("noise_max")) == "high":
        flags.append("sound peak above the configured attention level")

    if flags:
        verdict = "Points to review: " + "; ".join(flags) + "."
    else:
        verdict = "Conditions stayed within the configured comfort and attention ranges."

    return (
        f"On {day.isoformat()}, {count:,} samples were recorded. "
        + "; ".join(observations)
        + ". "
        + verdict
        + " Sound values are approximate: the Grove microphone and Base HAT ADC are not a "
        "certified sound-level meter."
    )


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings.llm

    def chat(self, system: str, user: str) -> str:
        url = f"{self.settings.base_url}/api/chat"
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": self.settings.temperature},
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama returned invalid JSON") from exc

        content = (body.get("message") or {}).get("content")
        if not content or not str(content).strip():
            raise RuntimeError("Ollama returned an empty response")
        return str(content).strip()


class DailySummaryService:
    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository
        self._llm_cooldown_until = 0.0
        self._last_llm_error: str | None = None

    def _stats(self, day: dt.date) -> dict[str, object]:
        if day < today(self.settings.tzinfo):
            cached = self.repository.get_daily_stat(day)
            if cached:
                return cached
        start, end = day_bounds(day, self.settings.tzinfo)
        return self.repository.daily_extremes(start, end)

    def get(self, day: dt.date, *, force: bool = False) -> dict[str, Any]:
        stats = self._stats(day)
        rules = rules_summary(day, stats, self.settings)
        base: dict[str, Any] = {
            "day": day.isoformat(),
            "stats": stats,
            "generated_at": now_ts(),
        }

        if not self.settings.llm.enabled:
            return {**base, "summary": rules, "source": "rules", "model": "rules", "cached": False}

        cached = self.repository.get_llm_summary(day)
        cache_seconds = self.settings.llm.cache_hours * 3600
        if (
            not force
            and cached
            and cached.get("model") == self.settings.llm.model
            and int(cached.get("created_at") or 0) > now_ts() - cache_seconds
        ):
            return {
                **base,
                "summary": cached["summary"],
                "source": cached["source"],
                "model": cached["model"],
                "created_at": cached["created_at"],
                "cached": True,
            }

        if not force and time.monotonic() < self._llm_cooldown_until:
            return {
                **base,
                "summary": rules,
                "source": "rules",
                "model": "rules",
                "cached": False,
                "error": self._last_llm_error or "LLM temporarily unavailable",
            }

        prompt = self._prompt(day, stats, rules)
        system = (
            "You are a concise environmental data analyst for an indoor office station. "
            "Use only the supplied statistics. Never claim a safety emergency, medical "
            "diagnosis or certified sound measurement. Mention that noise values are "
            "approximate. Write 3-5 short sentences of plain prose. No markdown headings, "
            "no bullet lists, no invented numbers."
        )
        try:
            text = OllamaClient(self.settings).chat(system, prompt)
        except Exception as exc:
            self._last_llm_error = str(exc)[:300]
            self._llm_cooldown_until = time.monotonic() + 300
            logger.warning("Ollama summary unavailable: %s", exc)
            return {
                **base,
                "summary": rules,
                "source": "rules",
                "model": "rules",
                "cached": False,
                "error": self._last_llm_error,
            }

        created_at = now_ts()
        self.repository.save_llm_summary(
            day,
            model=self.settings.llm.model,
            source="ollama",
            summary=text,
            created_at=created_at,
        )
        self._last_llm_error = None
        self._llm_cooldown_until = 0.0
        return {
            **base,
            "summary": text,
            "source": "ollama",
            "model": self.settings.llm.model,
            "created_at": created_at,
            "cached": False,
        }

    def _prompt(self, day: dt.date, stats: dict[str, object], rules: str) -> str:
        payload = {
            "day": day.isoformat(),
            "station": self.settings.station_name,
            "stats": stats,
            "configured_attention_ranges": {
                "temperature_c": self.settings.thresholds.temperature_c.model_dump(),
                "humidity_pct": self.settings.thresholds.humidity_pct.model_dump(),
                "pm2_5_ugm3": self.settings.thresholds.pm2_5_ugm3.model_dump(),
                "pm10_ugm3": self.settings.thresholds.pm10_ugm3.model_dump(),
                "noise_db_a": self.settings.thresholds.noise_db_a.model_dump(),
            },
            "deterministic_draft": rules,
        }
        return (
            "Summarise this day for a dashboard. Use the deterministic draft only as a "
            "cross-check; do not copy it verbatim.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )
