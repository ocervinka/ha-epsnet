"""Light platform for EPSNET."""

from __future__ import annotations

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .entity import EpsnetEntity


def _to_brightness(value: float, scale: float) -> int:
    return max(0, min(255, round((value / scale) * 255)))


def _from_brightness(value: int, scale: float) -> float:
    return round((value / 255) * scale, 3)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EpsnetLightEntity(runtime.coordinator, spec, entry_id=entry.entry_id)
        for spec in runtime.catalog.entities_for_platform("light")
    )


class EpsnetLightEntity(EpsnetEntity, LightEntity):
    """Light entity derived from relay or dimmer `.pub` variables."""

    def __init__(self, coordinator, spec, *, entry_id: str) -> None:
        super().__init__(coordinator, spec, entry_id=entry_id)
        if spec.supports_brightness:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF

    def _optimistic_on_value(self, brightness: int | None) -> bool | float:
        if not self.spec.supports_brightness:
            return True
        if brightness is not None:
            return _from_brightness(int(brightness), self.spec.brightness_scale)
        value = self._state_value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0:
            return float(value)
        return self.spec.brightness_scale

    def _optimistic_off_value(self) -> bool | float:
        if self.spec.supports_brightness:
            return 0.0
        return False

    @property
    def is_on(self) -> bool | None:
        value = self._state_value
        if value is None:
            return None
        if self.spec.supports_brightness:
            return float(value) > 0
        return bool(value)

    @property
    def brightness(self) -> int | None:
        if not self.spec.supports_brightness:
            return None
        value = self._state_value
        if value is None:
            return None
        return _to_brightness(float(value), self.spec.brightness_scale)

    async def async_turn_on(self, **kwargs) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if self.spec.supports_brightness and brightness is not None:
            if self.spec.set_value_name is None:
                raise HomeAssistantError(f"{self.name} does not support dimming")
            await self.coordinator.async_set_scalar(
                self.spec.set_value_name,
                _from_brightness(int(brightness), self.spec.brightness_scale),
            )
        elif self.spec.on_command_name is not None:
            await self.coordinator.async_press_command(self.spec.on_command_name)
        elif self.spec.set_value_name is not None:
            await self.coordinator.async_set_scalar(
                self.spec.set_value_name, self.spec.brightness_scale
            )
        else:
            raise HomeAssistantError(f"{self.name} does not support turning on")
        self._set_optimistic_state(self._optimistic_on_value(brightness))

    async def async_turn_off(self, **kwargs) -> None:
        if self.spec.off_command_name is not None:
            await self.coordinator.async_press_command(self.spec.off_command_name)
        elif self.spec.set_value_name is not None:
            await self.coordinator.async_set_scalar(self.spec.set_value_name, 0)
        else:
            raise HomeAssistantError(f"{self.name} does not support turning off")
        self._set_optimistic_state(self._optimistic_off_value())
