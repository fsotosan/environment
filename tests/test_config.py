"""Configuration loading, precedence and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from envmon.config import DatabaseSettings, Settings


def test_defaults_are_safe(settings: Settings) -> None:
    assert settings.sensors.mode == "simulator"
    assert settings.database.is_sqlite
    assert settings.llm.enabled is False
    assert settings.sampling.interval_seconds == 30
    assert settings.thresholds.temperature_c.status(17.0) == "low"
    assert settings.thresholds.temperature_c.status(22.0) == "ok"
    assert settings.thresholds.temperature_c.status(30.0) == "high"
    assert settings.thresholds.temperature_c.status(None) == "unknown"


def test_yaml_file_is_loaded(monkeypatch, tmp_path) -> None:
    config = tmp_path / "station.yaml"
    config.write_text(
        """
station_name: YAML Office
timezone: Europe/Berlin
web:
  port: 9000
sensors:
  mode: hardware
  noise:
    sample_rate_hz: 4000
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENVMON_CONFIG", str(config))

    settings = Settings()

    assert settings.station_name == "YAML Office"
    assert settings.timezone == "Europe/Berlin"
    assert settings.web.port == 9000
    assert settings.sensors.mode == "hardware"
    assert settings.sensors.noise.sample_rate_hz == 4000
    # Nested YAML deep-merges with field defaults.
    assert settings.sensors.noise.duration_seconds == 1.0
    assert settings.web.host == "0.0.0.0"


def test_environment_overrides_yaml(monkeypatch, tmp_path) -> None:
    config = tmp_path / "station.yaml"
    config.write_text(
        """
station_name: YAML Office
database:
  url: sqlite:///yaml.db
web:
  port: 9000
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENVMON_CONFIG", str(config))
    monkeypatch.setenv("ENVMON_STATION_NAME", "Env Office")
    monkeypatch.setenv("ENVMON_DATABASE__URL", "sqlite:///env.db")
    monkeypatch.setenv("ENVMON_WEB__PORT", "9100")

    settings = Settings()

    assert settings.station_name == "Env Office"
    assert settings.database.url == "sqlite:///env.db"
    assert settings.web.port == 9100


def test_explicit_arguments_win_over_environment(monkeypatch) -> None:
    monkeypatch.setenv("ENVMON_STATION_NAME", "Env Office")
    settings = Settings(station_name="Explicit Office")
    assert settings.station_name == "Explicit Office"


def test_sqlite_path_handles_absolute_and_relative_urls(tmp_path) -> None:
    absolute = DatabaseSettings(url=f"sqlite:///{tmp_path / 'abs.db'}")
    assert absolute.sqlite_path == tmp_path / "abs.db"

    relative = DatabaseSettings(url="sqlite:///data/local.db")
    assert relative.sqlite_path is not None
    assert str(relative.sqlite_path) == "data/local.db"

    memory = DatabaseSettings(url="sqlite:///:memory:")
    assert memory.sqlite_path is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timezone": "Mars/Olympus_Mons"}, "unknown timezone"),
        ({"database": {"url": "mysql://db/envmon"}}, "database.url"),
        ({"log_level": "LOUD"}, "log_level"),
        ({"sensors": {"noise": {"sample_rate_hz": 8000, "duration_seconds": 10}}}, "80000"),
        ({"sensors": {"noise": {"highpass_hz": 200, "sample_rate_hz": 200}}}, "Nyquist"),
    ],
)
def test_invalid_settings_are_rejected(kwargs, message) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(**kwargs)
