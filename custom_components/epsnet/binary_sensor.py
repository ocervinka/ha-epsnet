"""Binary sensor platform for EPSNET."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity

from .const import DOMAIN
from .entity import EpsnetEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EpsnetBinarySensorEntity(runtime.coordinator, spec, entry_id=entry.entry_id)
        for spec in runtime.catalog.entities_for_platform("binary_sensor")
    )


class EpsnetBinarySensorEntity(EpsnetEntity, BinarySensorEntity):
    """Binary sensor derived from an `INPUT_*` variable."""

    def __init__(self, coordinator, spec, *, entry_id: str) -> None:
        super().__init__(coordinator, spec, entry_id=entry_id)
        self._attr_device_class = spec.device_class

    @property
    def is_on(self) -> bool | None:
        value = self._state_value
        return None if value is None else bool(value)
