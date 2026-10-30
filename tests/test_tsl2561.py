"""TSL2561 lux conversion."""

from __future__ import annotations

import pytest

from envmon.sensors.tsl2561 import Tsl2561Sensor


@pytest.mark.parametrize("channel0", [0, -1])
def test_missing_channel_returns_zero(channel0: int) -> None:
    assert Tsl2561Sensor.calculate_lux(channel0, 0) == 0.0


def test_lux_branch_low_ratio() -> None:
    channel0, channel1 = 10_000, 2_000  # ratio 0.2
    expected = 0.0304 * channel0 - 0.062 * channel0 * (0.2**1.4)
    assert Tsl2561Sensor.calculate_lux(channel0, channel1) == pytest.approx(expected)


def test_lux_branch_mid_ratios() -> None:
    assert Tsl2561Sensor.calculate_lux(10_000, 5_500) == pytest.approx(
        0.0224 * 10_000 - 0.031 * 5_500
    )
    assert Tsl2561Sensor.calculate_lux(10_000, 7_000) == pytest.approx(
        0.0128 * 10_000 - 0.0153 * 7_000
    )
    assert Tsl2561Sensor.calculate_lux(10_000, 9_000) == pytest.approx(
        0.00146 * 10_000 - 0.00112 * 9_000
    )


def test_lux_saturates_if_infrared_dominates() -> None:
    assert Tsl2561Sensor.calculate_lux(1_000, 2_000) == 0.0
