"""Switch platform for EPSNET."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .entity import EpsnetEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EpsnetSwitchEntity(runtime.coordinator, spec, entry_id=entry.entry_id)
        for spec in runtime.catalog.entities_for_platform("switch")
    )


class EpsnetSwitchEntity(EpsnetEntity, SwitchEntity):
    """Switch entity backed by `OUTPUT_*` variables."""

    def __init__(self, coordinator, spec, *, entry_id: str) -> None:
        super().__init__(coordinator, spec, entry_id=entry_id)
        self._attr_device_class = spec.device_class

    @property
    def is_on(self) -> bool | None:
        value = self._state_value
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs) -> None:
        if self.spec.on_command_name is None:
            raise HomeAssistantError(f"{self.name} does not support turning on")
        await self.coordinator.async_press_command(self.spec.on_command_name)
        self._set_optimistic_state(True)

    async def async_turn_off(self, **kwargs) -> None:
        if self.spec.off_command_name is None:
            raise HomeAssistantError(f"{self.name} does not support turning off")
        await self.coordinator.async_press_command(self.spec.off_command_name)
        self._set_optimistic_state(False)
