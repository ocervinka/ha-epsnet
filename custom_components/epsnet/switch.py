"""Switch platform for EPSNET."""

from __future__ import annotations

import asyncio

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .coordinator import output_is_on
from .entity import EpsnetEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EpsnetSwitchEntity(runtime.coordinator, spec, entry_id=entry.entry_id)
        for spec in runtime.catalog.entities_for_platform("switch")
    )


class EpsnetSwitchEntity(EpsnetEntity, SwitchEntity):
    """A switch: an output with its command variables, or a plain variable.

    An output is switched by pressing its ``_ON`` / ``_OFF`` command variables.
    A plain variable of the program (``spec.variable``) is the switch itself:
    turning it on or off writes 1 or 0 into it.

    An output without command variables (``spec.press_name``) is switched by a
    press of its front button on the module, which flips it. The output is
    read before (already as wanted: no press) and after: when it did not
    change, the PLC program does not act on that button, and the call fails
    so the switch shows the output's real state again.
    """

    def __init__(self, coordinator, spec, *, entry_id: str) -> None:
        super().__init__(coordinator, spec, entry_id=entry_id)
        self._attr_device_class = spec.device_class
        self._press_lock = asyncio.Lock()

    @property
    def is_on(self) -> bool | None:
        value = self._state_value
        return None if value is None else output_is_on(value)

    async def _async_switch_by_press(self, on: bool) -> None:
        async with self._press_lock:
            value = await self.coordinator.async_switch_by_press(
                self.spec.state_name, self.spec.press_name, on
            )
        if value is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="output_did_not_change",
                translation_placeholders={"name": self._full_name()},
            )
        self._set_optimistic_state(value)

    def _full_name(self) -> str:
        """The name with the module's, as Home Assistant shows the entity."""
        device = self.coordinator.catalog.devices_by_key.get(self.spec.device_key)
        return f"{device.name} {self.name}" if device is not None else str(self.name)

    async def async_turn_on(self, **kwargs) -> None:
        if self.spec.press_name is not None:
            await self._async_switch_by_press(True)
            return
        if self.spec.variable:
            await self._async_write_variable(True)
            return
        if self.spec.on_command_name is None:
            raise HomeAssistantError(f"{self.name} does not support turning on")
        await self.coordinator.async_press_command(self.spec.on_command_name)
        self._set_optimistic_state(True)

    async def async_turn_off(self, **kwargs) -> None:
        if self.spec.press_name is not None:
            await self._async_switch_by_press(False)
            return
        if self.spec.variable:
            await self._async_write_variable(False)
            return
        if self.spec.off_command_name is None:
            raise HomeAssistantError(f"{self.name} does not support turning off")
        await self.coordinator.async_press_command(self.spec.off_command_name)
        self._set_optimistic_state(False)

    async def _async_write_variable(self, value: bool) -> None:
        if self.spec.set_value_name is None:
            raise HomeAssistantError(f"{self.name} cannot be written")
        await self.coordinator.async_set_scalar(self.spec.set_value_name, value)
        self._set_optimistic_state(value)
