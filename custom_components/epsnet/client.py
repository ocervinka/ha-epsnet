"""Shared transport protocol for EPSNET backends."""

from __future__ import annotations

from typing import Protocol

from .models import EpsnetPoint, EpsnetReadMetrics, PubEntry, RuntimeInventory, ScalarValue


class EpsnetClient(Protocol):
    """Transport interface used by the coordinator."""

    @property
    def last_read_metrics(self) -> EpsnetReadMetrics | None:
        """Return metrics for the most recent successful read batch."""

    async def discover_runtime_inventory(self) -> RuntimeInventory:
        """Fetch the live physical-device inventory from the PLC."""

    async def get_point_values(
        self, points: tuple[EpsnetPoint, ...]
    ) -> dict[str, ScalarValue]:
        """Read many generic addressable points."""

    async def set_point_value(self, point: EpsnetPoint, value: ScalarValue) -> None:
        """Write one generic addressable point."""

    async def get_values(self, entries: tuple[PubEntry, ...]) -> dict[str, ScalarValue]:
        """Read many `.pub` entries."""

    async def set_value(self, entry: PubEntry, value: ScalarValue) -> None:
        """Write one `.pub` entry."""

    async def async_close(self) -> None:
        """Release transport resources."""
