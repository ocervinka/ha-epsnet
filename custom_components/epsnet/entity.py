"""Shared Home Assistant entity helpers."""

from __future__ import annotations

import asyncio

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .models import EntitySpec, ScalarValue


class EpsnetEntity(CoordinatorEntity):
    """Base class for EPSNET entities backed by the shared coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, spec: EntitySpec, *, entry_id: str) -> None:
        super().__init__(coordinator)
        self.spec = spec
        self._entry_id = entry_id
        self._optimistic_state: ScalarValue | None = None
        self._output_lock = asyncio.Lock()
        self._attr_name = spec.name
        self._attr_unique_id = f"{entry_id}_{spec.key}"
        self._attr_entity_registry_enabled_default = spec.enabled_by_default
        if spec.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.add_state_name(self.spec.state_name)

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.remove_state_name(self.spec.state_name)
        await super().async_will_remove_from_hass()

    @property
    def available(self) -> bool:
        # placed from the program, or pressing an input placed from it
        placed = self.spec.from_program or self.spec.press_name is not None
        if placed and self.coordinator.program_layout_stale:
            return False
        return super().available and self.spec.state_name in self.coordinator.data

    @property
    def _state_value(self) -> bool | float | int | str | None:
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self.coordinator.data.get(self.spec.state_name)

    def _set_optimistic_state(self, value: ScalarValue) -> None:
        self._optimistic_state = value
        if self.hass is not None:
            self.async_write_ha_state()

    def _full_name(self) -> str:
        """The name with the module's, as Home Assistant shows the entity."""
        device = self.coordinator.catalog.devices_by_key.get(self.spec.device_key)
        return f"{device.name} {self.name}" if device is not None else str(self.name)

    def _output_kept_its_value(self, value: ScalarValue | None, *, pressed: bool) -> None:
        """Show what the output holds now, or fail when it is not as wanted."""
        if value is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=(
                    "output_did_not_change" if pressed else "output_write_did_not_stay"
                ),
                translation_placeholders={"name": self._full_name()},
            )
        self._set_optimistic_state(value)

    async def _async_switch_output(self, on: bool, on_value: ScalarValue = True) -> None:
        """Switch an output without command variables (``spec.direct_output``).

        By a press of its front button when the module has one, else or after
        that by a value written into it; the call fails when the output is
        still not as wanted, so the entity shows its real state again.
        """
        async with self._output_lock:
            value = await self.coordinator.async_switch_output(
                self.spec.state_name, on, press_name=self.spec.press_name, on_value=on_value
            )
        self._output_kept_its_value(value, pressed=self.spec.press_name is not None)

    async def _async_write_output(self, value: ScalarValue) -> None:
        """Write a value into an output without command variables."""
        async with self._output_lock:
            now = await self.coordinator.async_write_output(self.spec.state_name, value)
        self._output_kept_its_value(now, pressed=False)

    def _clear_optimistic_state(self) -> None:
        self._optimistic_state = None

    def _handle_coordinator_update(self) -> None:
        self._clear_optimistic_state()
        super()._handle_coordinator_update()

    @property
    def device_info(self) -> dict[str, object] | None:
        if self.spec.device_key is None:
            return None

        device = self.coordinator.catalog.devices_by_key.get(self.spec.device_key)
        if device is None:
            return None

        return {
            "identifiers": {
                (DOMAIN, f"{self._entry_id}_{registry_key}")
                for registry_key in device.registry_keys
            },
            "manufacturer": device.manufacturer or "Tecomat/iNELS",
            "model": device.model,
            "name": device.name,
        }
