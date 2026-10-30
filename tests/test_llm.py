"""Deterministic summaries and optional external LLM fallback."""

from __future__ import annotations

import datetime as dt

import pytest

import envmon.llm as llm
from envmon.config import Settings
from envmon.llm import DailySummaryService, rules_summary
from envmon.repository import Repository
from envmon.timeutil import now_ts, today

from .test_repository import sample


def test_rules_summary_reports_all_metrics(settings: Settings) -> None:
    stats = {
        "sample_count": 1440,
        "temperature_avg": 22.1,
        "temperature_min": 20.5,
        "temperature_max": 24.0,
        "humidity_avg": 46.0,
        "humidity_min": 40.0,
        "humidity_max": 52.0,
        "pm2_5_avg": 12.0,
        "pm2_5_max": 30.0,
        "pm10_avg": 20.0,
        "pm10_max": 44.0,
        "voc_avg": 110.0,
        "nox_avg": 8.0,
        "lux_avg": 450.0,
        "lux_min": 20.0,
        "lux_max": 900.0,
        "noise_leq_avg": 42.0,
        "noise_max": 70.0,
    }

    text = rules_summary(dt.date(2026, 10, 30), stats, settings)

    assert "2026-10-30" in text
    assert "1,440 samples" in text
    assert "22.1 °C" in text
    assert "PM2.5" in text
    assert "70.0 dB(A)" in text
    assert "Points to review" in text
    assert "approximately" in text or "approximate" in text


def test_rules_summary_without_samples(settings: Settings) -> None:
    text = rules_summary(dt.date(2026, 10, 30), {"sample_count": 0}, settings)
    assert text == "No samples were recorded on 2026-10-30."


def test_summary_uses_rules_when_llm_disabled(settings: Settings, repository: Repository) -> None:
    repository.insert_sample(sample(now_ts(), temperature=27.0, noise=44.0))
    result = DailySummaryService(settings, repository).get(today(dt.UTC))

    assert result["source"] == "rules"
    assert result["model"] == "rules"
    assert result["cached"] is False
    assert result["stats"]["sample_count"] == 1
    assert "approximately" in result["summary"] or "approximate" in result["summary"]


def test_summary_falls_back_and_cools_down(
    settings: Settings, repository: Repository, monkeypatch
) -> None:
    configured = settings.model_copy(
        update={
            "llm": settings.llm.model_copy(
                update={"enabled": True, "base_url": "http://127.0.0.1:1", "model": "test:1b"}
            )
        }
    )
    calls = 0

    def failing_chat(self, system: str, user: str) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("connection refused")

    monkeypatch.setattr(llm.OllamaClient, "chat", failing_chat)
    service = DailySummaryService(configured, repository)

    first = service.get(today(dt.UTC))
    second = service.get(today(dt.UTC))

    assert first["source"] == "rules"
    assert first["error"] == "connection refused"
    assert second["source"] == "rules"
    assert second["error"] == "connection refused"
    assert calls == 1  # the five-minute cooldown prevents a second request


def test_summary_uses_cached_llm_result(
    settings: Settings, repository: Repository, monkeypatch
) -> None:
    configured = settings.model_copy(
        update={
            "llm": settings.llm.model_copy(
                update={"enabled": True, "model": "test:1b", "cache_hours": 6}
            )
        }
    )
    day = today(dt.UTC)
    repository.save_llm_summary(
        day,
        model="test:1b",
        source="ollama",
        summary="A cached narrative.",
        created_at=now_ts(),
    )

    def unexpected_chat(self, system: str, user: str) -> str:
        raise AssertionError("the cache should be used")

    monkeypatch.setattr(llm.OllamaClient, "chat", unexpected_chat)
    result = DailySummaryService(configured, repository).get(day)

    assert result["source"] == "ollama"
    assert result["summary"] == "A cached narrative."
    assert result["cached"] is True


def test_summary_ignores_cache_for_other_model_or_force(
    settings: Settings, repository: Repository, monkeypatch
) -> None:
    configured = settings.model_copy(
        update={
            "llm": settings.llm.model_copy(
                update={"enabled": True, "model": "new:1b", "cache_hours": 6}
            )
        }
    )
    day = today(dt.UTC)
    repository.save_llm_summary(
        day, model="old:1b", source="ollama", summary="Old model.", created_at=now_ts()
    )

    def fake_chat(self, system: str, user: str) -> str:
        return "Fresh summary."

    monkeypatch.setattr(llm.OllamaClient, "chat", fake_chat)
    service = DailySummaryService(configured, repository)

    result = service.get(day)
    assert result["source"] == "ollama"
    assert result["summary"] == "Fresh summary."

    forced = service.get(day, force=True)
    assert forced["source"] == "ollama"
    assert forced["summary"] == "Fresh summary."


def test_prompt_forbids_invented_claims(settings: Settings, repository: Repository) -> None:
    service = DailySummaryService(settings, repository)
    prompt = service._prompt(dt.date(2026, 10, 30), {"sample_count": 0}, "draft")
    assert "deterministic_draft" in prompt
    assert "2026-10-30" in prompt


@pytest.mark.parametrize("value", [None, "n/a"])
def test_format_handles_missing_values(value) -> None:
    assert llm._fmt(value) == "n/a"
