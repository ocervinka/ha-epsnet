"""Pure-logic tests for map serialization / CRC (no PLC, no HA)."""
from __future__ import annotations

from dataclasses import replace

from custom_components.epsnet.map_sources import (
    deserialize_entries,
    entries_crc,
    serialize_entries,
)
from custom_components.epsnet.models import PubEntry


def _entries() -> tuple[PubEntry, ...]:
    return (
        PubEntry("light_3", "R", "B", 100, None, "BOOL", "PUB_OUT", 1),
        PubEntry("temp_hall", "X", "F", 71, None, "REAL", "PUB_INOUT", 2, is_virtual=True),
        PubEntry("pir_1", "X", "B", 5, 3, "BOOL", "PUB_IN", 3),
    )


def test_serialize_deserialize_roundtrip() -> None:
    entries = _entries()
    assert deserialize_entries(serialize_entries(entries)) == entries


def test_serialize_is_json_safe() -> None:
    import json

    blob = serialize_entries(_entries())
    # Must survive a JSON round-trip (config entries are JSON-persisted).
    assert deserialize_entries(json.loads(json.dumps(blob))) == _entries()


def test_entries_crc_is_order_independent() -> None:
    entries = _entries()
    assert entries_crc(entries) == entries_crc(tuple(reversed(entries)))


def test_entries_crc_changes_with_address() -> None:
    base = _entries()
    moved = (replace(base[0], address=999),) + base[1:]
    assert entries_crc(base) != entries_crc(moved)
