"""Number platform for EPSNET: the program's numeric variables that can be set."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode

from .const import DOMAIN
from .entity import EpsnetEntity

# What each integer type can hold.
_INTEGER_RANGES = {
    "BYTE": (0, 255),
    "USINT": (0, 255),
    "SINT": (-128, 127),
    "INT": (-32768, 32767),
    "UINT": (0, 65535),
    "WORD": (0, 65535),
    "DINT": (-(2**31), 2**31 - 1),
    "UDINT": (0, 2**32 - 1),
    "DWORD": (0, 2**32 - 1),
}
# A real has no limits worth naming; these keep the input field usable.
_REAL_RANGE = (-1e9, 1e9)
_REAL_STEP = 0.01


def readable_real(value):
    """A REAL as the program wrote it: 21.3, not 21.299999237060547.

    The PLC's REAL has seven significant digits; the rest is noise of the
    conversion to Home Assistant's longer float.
    """
    if isinstance(value, float):
        return float(f"{value:.7g}")
    return value


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EpsnetNumberEntity(runtime.coordinator, spec, entry_id=entry.entry_id)
        for spec in runtime.catalog.entities_for_platform("number")
    )


class EpsnetNumberEntity(EpsnetEntity, NumberEntity):
    """A numeric variable of the PLC program: shows its value and sets it."""

    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, spec, *, entry_id: str) -> None:
        super().__init__(coordinator, spec, entry_id=entry_id)
        data_type = coordinator.catalog.entries_by_name[spec.state_name].data_type.upper()
        self._is_real = data_type == "REAL"
        low, high = _INTEGER_RANGES.get(data_type, _REAL_RANGE)
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = 1 if data_type in _INTEGER_RANGES else _REAL_STEP

    @property
    def native_value(self) -> float | int | None:
        value = self._state_value
        if value is None or isinstance(value, str):
            return None
        return readable_real(value) if self._is_real else value

    async def async_set_native_value(self, value: float) -> None:
        if self.spec.set_value_name is None:
            return
        new = int(value) if self._attr_native_step == 1 else float(value)
        await self.coordinator.async_set_scalar(self.spec.set_value_name, new)
        self._set_optimistic_state(new)
