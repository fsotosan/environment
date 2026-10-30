"""Sampler and SensorManager behaviour."""

from __future__ import annotations

import pytest

from envmon.config import Settings
from envmon.repository import Repository
from envmon.sampler import Sampler
from envmon.sensors.manager import SensorManager
from envmon.sensors.simulator import SimulatorSensor


class GoodSensor:
    name = "good"

    def __init__(self) -> None:
        self.closed = False

    def read(self) -> dict[str, object]:
        return {"temperature_c": 21.0, "lux": 400.0}

    def close(self) -> None:
        self.closed = True


class BadSensor:
    name = "bad"

    def __init__(self) -> None:
        self.closed = False

    def read(self) -> dict[str, object]:
        raise RuntimeError("I2C NACK")

    def close(self) -> None:
        self.closed = True


def test_manager_isolates_sensor_failures(settings: Settings) -> None:
    good, bad = GoodSensor(), BadSensor()
    manager = SensorManager(settings, [good, bad])

    reading = manager.read()

    assert reading.has_data is True
    assert reading.values == {"temperature_c": 21.0, "lux": 400.0}
    assert reading.errors == ["bad: I2C NACK"]

    statuses = {item["name"]: item for item in manager.snapshot()["sensors"]}
    assert statuses["good"]["ok"] is True
    assert statuses["bad"]["ok"] is False
    assert statuses["bad"]["last_error"] == "I2C NACK"
    assert statuses["bad"]["consecutive_errors"] == 1
    assert bad.closed is True  # failed sensors are closed for a clean reopen

    manager.read()
    assert manager.statuses["bad"].consecutive_errors == 2
    manager.close()
    assert good.closed is True


def test_manager_without_sensors_reports_error(settings: Settings) -> None:
    manager = SensorManager(settings, [])
    reading = manager.read()
    assert reading.has_data is False
    assert reading.errors == ["no sensors are enabled"]


async def test_sampler_writes_and_skips_duplicates(
    settings: Settings, repository: Repository, monkeypatch
) -> None:
    monkeypatch.setattr("envmon.sensors.manager.now_ts", lambda: 1_700_000_000)
    manager = SensorManager(settings, [SimulatorSensor(settings)])
    sampler = Sampler(settings, repository, manager=manager)

    first = await sampler.sample_once()
    second = await sampler.sample_once()

    assert first is not None and first.has_data
    assert second is not None
    assert repository.sample_count() == 1
    assert sampler.samples_written == 1
    assert sampler.samples_skipped == 1
    assert sampler.last_success_ts == 1_700_000_000
    assert sampler.consecutive_failures == 0
    assert sampler.last_error is None

    snapshot = sampler.snapshot()
    assert snapshot["samples_written"] == 1
    assert snapshot["samples_skipped"] == 1
    assert snapshot["sensors"]["mode"] == "simulator"

    await sampler.stop()
    assert sampler.snapshot()["running"] is False


async def test_sampler_reports_total_failure(settings: Settings, repository: Repository) -> None:
    sampler = Sampler(settings, repository, manager=SensorManager(settings, [BadSensor()]))

    reading = await sampler.sample_once()

    assert reading is None
    assert repository.sample_count() == 0
    assert sampler.consecutive_failures == 1
    assert sampler.last_error == "bad: I2C NACK"
    await sampler.stop()


async def test_sampler_records_database_error(
    settings: Settings, repository: Repository, monkeypatch
) -> None:
    sampler = Sampler(settings, repository, manager=SensorManager(settings, [GoodSensor()]))

    def boom(_record):
        raise RuntimeError("disk full")

    monkeypatch.setattr(repository, "insert_sample", boom)
    reading = await sampler.sample_once()

    assert reading is None
    assert sampler.consecutive_failures == 1
    assert sampler.last_error == "database: disk full"
    await sampler.stop()


@pytest.mark.parametrize("enabled", [True, False])
async def test_sampler_start_stop(
    settings: Settings, repository: Repository, monkeypatch, enabled: bool
) -> None:
    settings = settings.model_copy(
        update={"sampling": settings.sampling.model_copy(update={"enabled": enabled})}
    )
    manager = SensorManager(settings, [SimulatorSensor(settings)])
    sampler = Sampler(settings, repository, manager=manager)

    async def no_op() -> None:
        return None

    monkeypatch.setattr(sampler, "run", no_op)
    await sampler.start()
    assert sampler.snapshot()["running"] is True
    await sampler.stop()
    assert sampler.snapshot()["running"] is False
