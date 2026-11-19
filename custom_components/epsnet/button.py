"""Button platform for EPSNET: a press of a wall button, done by Home Assistant."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity

from .const import DOMAIN
from .entity import EpsnetEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EpsnetPressButtonEntity(runtime.coordinator, spec, entry_id=entry.entry_id)
        for spec in runtime.catalog.entities_for_platform("button")
    )


class EpsnetPressButtonEntity(EpsnetEntity, ButtonEntity):
    """Presses a button of a wall-switch module.

    Writes 1 to the button's input. The PLC program sees one short press and
    does what it does when the real button is pressed; the PLC then reads the
    input again, so nothing has to be released. A long or a double press
    cannot be made this way.
    """

    async def async_press(self) -> None:
        await self.coordinator.async_press_command(self.spec.press_name)
