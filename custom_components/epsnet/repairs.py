"""Repair flow of the "the PLC has a new program" notice.

The variable names are loaded once and kept; reading them again takes the PLC's
management session, so it is never done unasked. When the PLC runs another
program than the one the names were loaded for (``program_watch``), this flow
offers what fits the entry: read the names from the PLC again (when they came
from there), load the new ``.pub`` file (when they came from a file, or the PLC
turns out to store none), or keep the current names.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import voluptuous as vol

from .config_flow import NamesChange, ReimportSteps, apply_reimport
from .const import CONF_MAP


class NamesOutdatedRepairFlow(ReimportSteps, RepairsFlow):
    """Load the variable names again after the PLC got a new program."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._change: NamesChange | None = None

    def _reimport_config_entry(self):
        return self.hass.config_entries.async_get_entry(self._entry_id)

    def _reimport_flow_manager(self):
        return self.hass.data["repairs"]["flow_manager"]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self._reimport_config_entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        blob = entry.data.get(CONF_MAP) or {}
        error, self._error = self._error, None
        # Offer only what can work: names that came from the PLC are read
        # there again; otherwise (or once the PLC turned out to store none)
        # the new program's file is asked for.
        from_plc = blob.get("source") == "plc" and error != "no_map"
        return self.async_show_menu(
            # Variant step ids carry the warning of a failed attempt, so its
            # text comes from the translations.
            step_id=f"init_{error}" if error else "init",
            menu_options=["reimport_plc" if from_plc else "reimport_file", "keep"],
        )

    # Re-entry points for the warning variants (see the options flow).
    async def async_step_init_plc_busy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._error = "plc_busy"
        return await self.async_step_init()

    async def async_step_init_cannot_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._error = "cannot_connect"
        return await self.async_step_init()

    async def async_step_init_no_map(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._error = "no_map"
        return await self.async_step_init()

    async def async_step_keep(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """The new program changed nothing in the names: they now belong to it."""
        entry = self._reimport_config_entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        blob = entry.data.get(CONF_MAP) or {}
        self.hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_MAP: {
                    **blob,
                    "program_checksum": (self.data or {}).get("program_checksum"),
                },
            },
        )
        return self.async_create_entry(data={})

    async def async_step_reimport_done(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Apply the loaded names and show what changed."""
        if self._change is None:
            entry = self._reimport_config_entry()
            if entry is None or self._result is None:
                return self.async_abort(reason="entry_gone")
            self._change = apply_reimport(self.hass, entry, self._result, notify=False)
        if self._change.changed:
            return await self.async_step_done_changed()
        return await self.async_step_done_unchanged()

    async def async_step_done_changed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self._async_done("done_changed", user_input)

    async def async_step_done_unchanged(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self._async_done("done_unchanged", user_input)

    def _async_done(self, step_id: str, user_input: dict[str, Any] | None) -> FlowResult:
        if user_input is not None or self._change is None:
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({}),
            description_placeholders={
                "variables": str(self._change.variables),
                "added": str(self._change.added),
                "removed": str(self._change.removed),
                "moved": str(self._change.moved),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    return NamesOutdatedRepairFlow(str((data or {}).get("entry_id", "")))
