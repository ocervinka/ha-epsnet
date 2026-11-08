"""Shared Home Assistant entity helpers."""

from __future__ import annotations

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
        self._attr_name = spec.name
        self._attr_unique_id = f"{entry_id}_{spec.key}"
        self._attr_entity_registry_enabled_default = spec.enabled_by_default

    @property
    def available(self) -> bool:
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
