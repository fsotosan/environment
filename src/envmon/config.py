"""Application configuration.

Precedence (highest first):

1. Explicit ``Settings(...)`` keyword arguments (used by tests).
2. ``ENVMON_*`` environment variables (``__`` for nesting).
3. ``.env`` file.
4. YAML file selected by ``ENVMON_CONFIG`` (default ``config/config.yaml``).
5. Field defaults.

Secret values are never written to the API responses or logs.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources import YamlConfigSettingsSource

CONFIG_ENV_VAR = "ENVMON_CONFIG"
DEFAULT_CONFIG_PATH = Path("config/config.yaml")
SUPPORTED_DB_SCHEMES = ("sqlite", "postgresql", "postgresql+psycopg")


class YamlSettingsSource(YamlConfigSettingsSource):
    """YAML source whose path can be changed with ``ENVMON_CONFIG``."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        raw = os.environ.get(CONFIG_ENV_VAR, "").strip()
        path = Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH
        self.path = path
        super().__init__(settings_cls, yaml_file=path, deep_merge=True)


class DatabaseSettings(BaseModel):
    """Database connection settings.

    The default is a local SQLite database with WAL enabled. Point ``url`` at
    PostgreSQL for a shared/long-term installation, e.g.
    ``postgresql+psycopg://envmon:secret@db.local:5432/envmon``.
    """

    url: str = "sqlite:///data/envmon.db"
    echo: bool = False
    busy_timeout_ms: int = Field(default=5000, ge=100, le=120_000)
    pool_size: int = Field(default=5, ge=1, le=100)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("database.url must not be empty")
        if not value.startswith(SUPPORTED_DB_SCHEMES):
            raise ValueError(
                "database.url must start with sqlite:///, postgresql:// or postgresql+psycopg://"
            )
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @property
    def sqlite_path(self) -> Path | None:
        if not self.is_sqlite:
            return None
        raw = self.url.removeprefix("sqlite:///").removeprefix("sqlite://")
        # sqlite:////absolute/path.db -> /absolute/path.db
        raw = raw.split("?", 1)[0]
        if not raw or raw == ":memory:":
            return None
        return Path(raw).expanduser()


class SamplingSettings(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=30, ge=1, le=86_400)
    # Retry faster when the previous read failed completely (all sensors down).
    failure_retry_seconds: int = Field(default=10, ge=1, le=3600)


class RetentionSettings(BaseModel):
    raw_days: int = Field(default=730, ge=1, le=36_500)
    aggregate_hour: int = Field(default=1, ge=0, le=23)
    backup_enabled: bool = True
    backup_hour: int = Field(default=3, ge=0, le=23)
    backup_dir: str = "backups"
    backup_keep: int = Field(default=14, ge=1, le=3650)
    backup_compress: bool = True
    vacuum_after_prune: bool = True
    maintenance_enabled: bool = True


class Sen5xSettings(BaseModel):
    enabled: bool = True
    address: int = Field(default=0x69, ge=0x03, le=0x77)
    temperature_offset_c: float = Field(default=0.0, ge=-20.0, le=20.0)
    fan_auto_cleaning_hours: int = Field(default=168, ge=0, le=8760)


class LightSettings(BaseModel):
    enabled: bool = True
    address: int = Field(default=0x29, ge=0x03, le=0x77)
    # 1 or 16. 16 is needed for typical indoor light levels.
    gain: Literal[1, 16] = 16
    # 13.7, 101 or 402 ms (TSL2561 integration times).
    integration_time_ms: Literal[14, 101, 402] = 402


class NoiseSettings(BaseModel):
    enabled: bool = True
    adc_address: int = Field(default=0x04, ge=0x03, le=0x77)
    adc_channel: int = Field(default=0, ge=0, le=3)
    # The Grove Base HAT ADC is polled over I2C; ~2 kHz is a realistic maximum.
    sample_rate_hz: int = Field(default=2000, ge=200, le=8000)
    # Length of each continuous-capture analysis block. The sampler still
    # writes one row per sampling.interval_seconds and summarises the blocks.
    window_seconds: float = Field(default=1.0, ge=0.2, le=10.0)
    sensitivity_mv_per_pa: float = Field(default=10.0, gt=0.0, le=1000.0)
    calibration_offset_db: float = Field(default=0.0, ge=-60.0, le=60.0)
    a_weighting: bool = True
    highpass_hz: float = Field(default=20.0, ge=0.0, le=200.0)
    adc_reference_volts: float = Field(default=3.3, gt=0.0, le=5.0)

    @model_validator(mode="after")
    def _check_budget(self) -> NoiseSettings:
        if self.sample_rate_hz * self.window_seconds >= 80_000:
            raise ValueError(
                "noise sample_rate_hz * window_seconds must stay below 80000; "
                "the Grove Base HAT ADC is slow and long captures block sampling"
            )
        if self.highpass_hz * 2 >= self.sample_rate_hz:
            raise ValueError("noise.highpass_hz must be below Nyquist")
        return self


class SensorSettings(BaseModel):
    mode: Literal["hardware", "simulator"] = "hardware"
    i2c_bus: int = Field(default=1, ge=0, le=20)
    sen5x: Sen5xSettings = Field(default_factory=Sen5xSettings)
    light: LightSettings = Field(default_factory=LightSettings)
    noise: NoiseSettings = Field(default_factory=NoiseSettings)


class WebSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    title: str = "Environment Station"
    refresh_seconds: int = Field(default=30, ge=5, le=3600)
    chart_default_hours: int = Field(default=24, ge=1, le=24 * 365)
    chart_max_points: int = Field(default=1500, ge=100, le=10_000)
    allow_export: bool = True
    # Set when the app is served under a reverse-proxy sub-path, e.g. "/envmon".
    root_path: str = ""


class MetricThreshold(BaseModel):
    """Comfort/attention range used only for dashboard colouring."""

    min: float | None = None
    max: float | None = None

    def status(self, value: float | None) -> str:
        if value is None:
            return "unknown"
        if self.max is not None and value > self.max:
            return "high"
        if self.min is not None and value < self.min:
            return "low"
        return "ok"


class ThresholdSettings(BaseModel):
    temperature_c: MetricThreshold = Field(
        default_factory=lambda: MetricThreshold(min=18.0, max=26.0)
    )
    humidity_pct: MetricThreshold = Field(
        default_factory=lambda: MetricThreshold(min=30.0, max=70.0)
    )
    pm2_5_ugm3: MetricThreshold = Field(default_factory=lambda: MetricThreshold(min=None, max=25.0))
    pm10_ugm3: MetricThreshold = Field(default_factory=lambda: MetricThreshold(min=None, max=50.0))
    voc_index: MetricThreshold = Field(default_factory=lambda: MetricThreshold(min=None, max=250.0))
    nox_index: MetricThreshold = Field(default_factory=lambda: MetricThreshold(min=None, max=250.0))
    lux: MetricThreshold = Field(default_factory=lambda: MetricThreshold(min=200.0, max=None))
    noise_leq_db_a: MetricThreshold = Field(
        default_factory=lambda: MetricThreshold(min=None, max=65.0)
    )


class LlmSettings(BaseModel):
    """Optional external Ollama configuration.

    The model is never used for alarms or safety decisions. Keep ``enabled``
    false unless an Ollama server is reachable from the station.
    """

    enabled: bool = False
    base_url: str = "http://192.168.1.10:11434"
    model: str = "llama3.1:8b"
    api_key: str | None = None
    timeout_seconds: float = Field(default=60.0, ge=5.0, le=600.0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    cache_hours: int = Field(default=6, ge=1, le=24 * 30)

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")


class Settings(BaseSettings):
    """Root settings object."""

    model_config = SettingsConfigDict(
        env_prefix="ENVMON_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    station_name: str = "Office"
    timezone: str = "UTC"
    log_level: str = "INFO"
    log_file: str | None = None

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    sampling: SamplingSettings = Field(default_factory=SamplingSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    sensors: SensorSettings = Field(default_factory=SensorSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    thresholds: ThresholdSettings = Field(default_factory=ThresholdSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlSettingsSource(settings_cls),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loading them on first use."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the settings cache (used by tests and the CLI)."""
    get_settings.cache_clear()
    return get_settings()
