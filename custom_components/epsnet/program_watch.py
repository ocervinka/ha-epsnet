"""Notice a changed PLC program and re-read its I/O layout.

The program checksum (S 70) is read every few minutes. When it changes, someone
has just sent a program: their tool probably still holds the PLC's management
session and the CPU was halted moments ago. So: wait until the checksum has been
stable for a minute, then try the module scan (which reads the layout) at most
three times with growing gaps. Same layout -> nothing changes; different layout
-> the entry is reloaded (entities rebuilt); no success -> a Repairs notice, and
no further attempt until the checksum changes again. Meanwhile, entities placed
by the program layout are unavailable.

The variable names are a second matter. They are stored with the checksum of the
program they were loaded for and are never re-read on their own (reading them
takes the PLC's management session). When the PLC runs another program, a
fixable Repairs notice offers to load them again (``repairs.py``).
"""

from __future__ import annotations

import asyncio
import logging

from .const import CONF_MAP, DOMAIN
from .epsnet_udp import EpsnetError
from .models import EpsnetPoint
from .program_layout import layout_fingerprint

LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL = 120.0
STABLE_FOR = 60.0
STABLE_POLL = 15.0
ATTEMPT_DELAYS = (0.0, 120.0, 600.0)

PROGRAM_CHECKSUM_POINT = EpsnetPoint(
    key="s70", name="S70", register="S", storage="D", address=70,
    bit=None, data_type="UDINT",
)


NAMES_ISSUE_PREFIX = "names_outdated_"


def names_issue_id(entry_id: str, checksum: int) -> str:
    """One notice per program: ignoring it holds for that program only."""
    return f"{NAMES_ISSUE_PREFIX}{entry_id}_{checksum:08x}"


def clear_names_issues(hass, entry_id: str, *, keep: str | None = None) -> None:
    """Drop the entry's "names are from the previous program" notices but ``keep``."""
    from homeassistant.helpers import issue_registry as ir

    prefix = f"{NAMES_ISSUE_PREFIX}{entry_id}_"
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id != keep:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def read_program_checksum(client) -> int | None:
    """S 70 (DWORD) holds the program checksum on the PLCs tested so far."""
    try:
        values = await client.get_point_values((PROGRAM_CHECKSUM_POINT,))
    except Exception:  # noqa: BLE001 — a missed read is retried next time
        return None
    value = values.get(PROGRAM_CHECKSUM_POINT.key)
    return None if value is None else int(value)


class ProgramWatcher:
    def __init__(self, hass, entry, client, coordinator, catalog, *, sleep=asyncio.sleep) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.coordinator = coordinator
        self.catalog = catalog
        self._sleep = sleep
        self._fingerprint = layout_fingerprint(catalog.runtime_inventory.program_layout)
        self.baseline: int | None = None

    @property
    def issue_id(self) -> str:
        return f"program_changed_{self.entry.entry_id}"

    async def run(self) -> None:
        while True:
            value = await read_program_checksum(self.client)
            if value is not None:
                if self.baseline is None:
                    self.baseline = value
                    # Also catches a program sent while Home Assistant was off.
                    self.check_names(value)
                elif value != self.baseline:
                    await self.handle_change()
            await self._sleep(CHECK_INTERVAL)

    def check_names(self, checksum: int) -> None:
        """Raise the "names may be out of date" notice, or clear it.

        The names carry the checksum of the program they were loaded for. An
        entry that has none yet (stored before this existed, or the read failed
        then) takes the current one: nothing says its names are old.
        """
        from homeassistant.helpers import issue_registry as ir

        blob = self.entry.data.get(CONF_MAP) or {}
        if not blob.get("entries"):
            return  # no names to go out of date
        loaded_for = blob.get("program_checksum")
        if loaded_for is None:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_MAP: {**blob, "program_checksum": checksum}},
            )
            return
        if loaded_for == checksum:
            clear_names_issues(self.hass, self.entry.entry_id)
            return
        issue_id = names_issue_id(self.entry.entry_id, checksum)
        clear_names_issues(self.hass, self.entry.entry_id, keep=issue_id)
        LOGGER.info(
            "The PLC runs another program (checksum %#010x) than the variable names "
            "were loaded for (%#010x)", checksum, loaded_for,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="names_outdated",
            data={"entry_id": self.entry.entry_id, "program_checksum": checksum},
        )

    async def _wait_until_stable(self) -> int | None:
        last, stable = None, 0.0
        while stable < STABLE_FOR:
            value = await read_program_checksum(self.client)
            if value is not None and value == last:
                stable += STABLE_POLL
            else:
                last, stable = value, 0.0
            await self._sleep(STABLE_POLL)
        return last

    def _set_stale(self, stale: bool) -> None:
        if self.coordinator.program_layout_stale != stale:
            self.coordinator.program_layout_stale = stale
            self.coordinator.async_update_listeners()

    async def handle_change(self) -> None:
        """One program change: re-read the layout, then reload, settle or give up."""
        from homeassistant.helpers import issue_registry as ir

        LOGGER.info("PLC program changed (checksum %#010x); re-reading its I/O layout", self.baseline or 0)
        if any(e.from_program for e in self.catalog.entities):
            self._set_stale(True)
        value = await self._wait_until_stable()
        if value is not None:
            self.check_names(value)
        for delay in ATTEMPT_DELAYS:
            await self._sleep(delay)
            try:
                inventory = await self.client.discover_runtime_inventory()
            except EpsnetError as err:
                LOGGER.debug("Re-reading the program layout failed: %s", err)
                continue
            layout = inventory.program_layout
            if layout is None or layout.error:
                LOGGER.debug("Program layout not readable: %s", layout and layout.error)
                continue
            self.baseline = value
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            if layout_fingerprint(layout) == self._fingerprint:
                LOGGER.info("PLC program changed, its I/O layout did not")
                self._set_stale(False)
            else:
                LOGGER.info("PLC program I/O layout changed; reloading the entry")
                self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
            return
        self.baseline = value
        LOGGER.warning("Could not re-read the changed PLC program; reload the integration")
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="program_changed",
        )
