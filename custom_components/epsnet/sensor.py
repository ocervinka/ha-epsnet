"""Sensor platform for EPSNET."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import EpsnetPollMetricsSnapshot
from .const import DOMAIN
from .entity import EpsnetEntity

# (metric_key, unit, device_class). The metric key doubles as the translation
# key (entity.sensor.<key>.name in translations/*.json). Duration metrics report
# ms; the block count is a plain measurement with no unit/device_class.
_MS = "ms"
_DURATION = "duration"
_POLL_METRIC_SENSORS = (
    ("average_read_duration_ms", _MS, _DURATION),
    ("minimum_read_duration_ms", _MS, _DURATION),
    ("maximum_read_duration_ms", _MS, _DURATION),
    ("average_cycle_duration_ms", _MS, _DURATION),
    ("minimum_cycle_duration_ms", _MS, _DURATION),
    ("maximum_cycle_duration_ms", _MS, _DURATION),
    ("average_block_duration_ms", _MS, _DURATION),
    ("minimum_block_duration_ms", _MS, _DURATION),
    ("maximum_block_duration_ms", _MS, _DURATION),
    ("block_count", None, None),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities = list(
        EpsnetTemperatureSensorEntity(runtime.coordinator, spec, entry_id=entry.entry_id)
        for spec in runtime.catalog.entities_for_platform("sensor")
    )
    entities.extend(
        EpsnetPollMetricSensorEntity(
            runtime.coordinator,
            entry_id=entry.entry_id,
            metric_key=metric_key,
            unit=unit,
            device_class=device_class,
        )
        for metric_key, unit, device_class in _POLL_METRIC_SENSORS
    )
    async_add_entities(entities)


class EpsnetPollMetricSensorEntity(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor exposing smoothed poll timing metrics."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _attr_has_entity_name = True
    _attr_state_class = "measurement"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator,
        *,
        entry_id: str,
        metric_key: str,
        unit: str | None = "ms",
        device_class: str | None = "duration",
    ) -> None:
        super().__init__(coordinator)
        self._metric_key = metric_key
        self._last_seen_available: bool | None = None
        self._last_seen_metrics_version = -1
        self._attr_translation_key = metric_key
        self._attr_unique_id = f"{entry_id}_{metric_key}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        # Counts are whole numbers; durations keep one decimal.
        self._attr_suggested_display_precision = 0 if device_class is None else 1

    @property
    def native_value(self) -> float | None:
        metrics: EpsnetPollMetricsSnapshot = self.coordinator.poll_metrics
        value = getattr(metrics, self._metric_key)
        if value is None:
            return None
        return round(value, 0 if self._attr_device_class is None else 1)

    def _handle_coordinator_update(self) -> None:
        metrics_version = self.coordinator.poll_metrics_version
        available = self.coordinator.last_update_success
        if (
            metrics_version == self._last_seen_metrics_version
            and available == self._last_seen_available
        ):
            return
        self._last_seen_available = available
        self._last_seen_metrics_version = metrics_version
        super()._handle_coordinator_update()


# DS18B20 (the 1-Wire probe behind SDS temperature channels) reports exactly
# 85.0 °C as its power-on/reset value; the PLC passes it through on a bus glitch.
DS18B20_GLITCH_C = 85.0


def filter_ds18b20_glitch(value, last_good):
    """Return `value`, or `last_good` when `value` is the DS18B20 85.0 sentinel."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == DS18B20_GLITCH_C:
        return last_good
    return value


class EpsnetTemperatureSensorEntity(EpsnetEntity, SensorEntity):
    """Temperature sensor derived from `.pub` variables."""

    _attr_state_class = "measurement"

    def __init__(self, coordinator, spec, *, entry_id: str) -> None:
        super().__init__(coordinator, spec, entry_id=entry_id)
        self._attr_device_class = spec.device_class
        self._attr_native_unit_of_measurement = spec.native_unit_of_measurement
        if spec.device_class == "temperature":
            # REALs from the PLC carry float noise (22.2999992…); one decimal is the
            # sensors' real resolution. Users can still change it per entity.
            self._attr_suggested_display_precision = 1
        self._last_good: float | int | str | None = None

    @property
    def native_value(self) -> float | int | str | None:
        # Hold the last good reading across a glitch so history has no spikes
        # (a sensor that has only ever glitched stays unknown).
        value = filter_ds18b20_glitch(self._state_value, self._last_good)
        self._last_good = value
        return value
