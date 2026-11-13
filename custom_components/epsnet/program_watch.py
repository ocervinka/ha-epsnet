"""Notice a changed PLC program and re-read its I/O layout.

The program checksum (S 70) is read every few minutes. When it changes, someone
has just sent a program: their tool probably still holds the PLC's management
session and the CPU was halted moments ago. So: wait until the checksum has been
stable for a minute, then try the module scan (which reads the layout) at most
three times with growing gaps. Same layout -> nothing changes; different layout
-> the entry is reloaded (entities rebuilt); no success -> a Repairs notice, and
no further attempt until the checksum changes again. Meanwhile, entities placed
by the program layout are unavailable.
"""

from __future__ import annotations

import asyncio
import logging

from .const import DOMAIN
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
                elif value != self.baseline:
                    await self.handle_change()
            await self._sleep(CHECK_INTERVAL)

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
