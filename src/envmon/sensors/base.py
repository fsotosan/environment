"""Sensor interfaces and shared result types."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class Sensor(Protocol):
    name: str

    def read(self) -> dict[str, object]:
        """Return one or more metric values. Raise on unrecoverable failure."""
        ...

    def close(self) -> None: ...


@dataclass
class SensorStatus:
    name: str
    ok: bool | None = None
    last_ok_ts: int | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    total_errors: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "last_ok_ts": self.last_ok_ts,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
            "total_errors": self.total_errors,
        }


@dataclass
class Reading:
    ts: int
    mode: str
    values: dict[str, object] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return any(value is not None for value in self.values.values())

    def as_record(self) -> dict[str, object]:
        record = dict(self.values)
        record.update({"ts": self.ts, "mode": self.mode, "errors": list(self.errors)})
        return record


def now_ts() -> int:
    return int(time.time())
