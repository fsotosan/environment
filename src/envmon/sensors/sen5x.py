"""Sensirion SEN54/SEN55 particulate matter, RH/T and VOC/NOx sensor."""

from __future__ import annotations

import logging
import time

from envmon.config import Sen5xSettings

logger = logging.getLogger(__name__)


class SensorNotReady(RuntimeError):
    pass


class Sen5xSensor:
    name = "sen5x"

    def __init__(self, bus_number: int, settings: Sen5xSettings) -> None:
        self.bus_number = bus_number
        self.settings = settings
        self._transceiver = None
        self._device = None

    def _ensure_device(self):
        if self._device is not None:
            return self._device

        from sensirion_i2c_driver import I2cConnection, LinuxI2cTransceiver
        from sensirion_i2c_sen5x import Sen5xI2cDevice

        transceiver = LinuxI2cTransceiver(f"/dev/i2c-{self.bus_number}")
        try:
            connection = I2cConnection(transceiver)
            device = Sen5xI2cDevice(connection, slave_address=self.settings.address)
            if self.settings.fan_auto_cleaning_hours >= 0:
                device.set_fan_auto_cleaning_interval(
                    int(self.settings.fan_auto_cleaning_hours * 3600)
                )
            if self.settings.temperature_offset_c:
                device.set_temperature_offset_parameters(
                    float(self.settings.temperature_offset_c), 0.0, 0
                )
            device.start_measurement()
        except Exception:
            transceiver.close()
            raise

        try:
            logger.info("SEN5x ready: %s", device.get_product_name())
        except Exception:  # product name is informational only
            logger.debug("could not read SEN5x product name", exc_info=True)

        self._transceiver = transceiver
        self._device = device
        return device

    def read(self) -> dict[str, object]:
        device = self._ensure_device()
        try:
            ready = False
            for _ in range(3):
                if device.read_data_ready():
                    ready = True
                    break
                time.sleep(0.3)
            if not ready:
                raise SensorNotReady("SEN5x has no new data yet (warm-up or fan cleaning)")
            measured = device.read_measured_values()
        except Exception:
            # Force a clean reopen on the next cycle.
            self.close()
            raise

        values: dict[str, object] = {}
        for key, attribute in (
            ("pm1_0_ugm3", measured.mass_concentration_1p0),
            ("pm2_5_ugm3", measured.mass_concentration_2p5),
            ("pm4_0_ugm3", measured.mass_concentration_4p0),
            ("pm10_ugm3", measured.mass_concentration_10p0),
        ):
            if attribute.available:
                values[key] = float(attribute.physical)

        if measured.ambient_temperature.available:
            values["temperature_c"] = float(measured.ambient_temperature.degrees_celsius)
        if measured.ambient_humidity.available:
            values["humidity_pct"] = float(measured.ambient_humidity.percent_rh)
        if measured.voc_index.available:
            values["voc_index"] = float(measured.voc_index.scaled)
        if measured.nox_index.available:
            values["nox_index"] = float(measured.nox_index.scaled)

        if not values:
            raise SensorNotReady("SEN5x returned no available values")
        return values

    def close(self) -> None:
        if self._transceiver is not None:
            try:
                self._transceiver.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.debug("error closing SEN5x transceiver", exc_info=True)
        self._transceiver = None
        self._device = None
