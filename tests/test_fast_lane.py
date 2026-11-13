"""Fast lane: binary inputs polled on their own short interval, merged over the full cycle."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_harness_path = Path(__file__).resolve().parent / "test_scan_interval.py"
_spec = importlib.util.spec_from_file_location("test_scan_interval_harness", _harness_path)
_harness = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_harness)


class _Entry(SimpleNamespace):
    pass


class _Client:
    def __init__(self, values):
        self.values = values
        self.asked: list[tuple[str, ...]] = []
        self.last_read_metrics = None

    async def get_values(self, entries):
        self.asked.append(tuple(e.name for e in entries))
        return {e.name: self.values[e.name] for e in entries if e.name in self.values}


class _Catalog:
    def __init__(self, entries):
        self._entries = entries
        self.entities = ()

    def state_entries(self):
        return self._entries


def _coordinator(monkeypatch, *, fast_ms, client, entries):
    module = _harness._load_coordinator_module(monkeypatch)
    hass = _harness._FakeHass()
    fast = tuple(e for e in entries if e.name.startswith("in_"))
    coordinator = module.EpsnetDataCoordinator(
        hass,
        client=client,
        catalog=_Catalog(entries),
        name="EPSNET",
        scan_interval_milliseconds=500,
        fast_scan_interval_milliseconds=fast_ms,
        fast_entries=fast,
    )
    return coordinator, hass


def test_fast_lane_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = (_Entry(name="in_pir"), _Entry(name="temp"))
    coordinator, hass = _coordinator(monkeypatch, fast_ms=0, client=_Client({}), entries=entries)
    assert not coordinator.fast_lane_active
    coordinator.start_fast_lane()
    assert hass.background_tasks == []


def test_fast_lane_starts_one_background_task(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = (_Entry(name="in_pir"), _Entry(name="in_btn"), _Entry(name="temp"))
    coordinator, hass = _coordinator(monkeypatch, fast_ms=100, client=_Client({}), entries=entries)
    assert coordinator.fast_lane_active
    assert coordinator.fast_lane_stats["inputs"] == 2
    coordinator.start_fast_lane()
    coordinator.start_fast_lane()  # idempotent
    assert hass.background_tasks == [{"name": "EPSNET - fast lane", "eager_start": True}]


@pytest.mark.asyncio
async def test_full_cycle_skips_fast_inputs_and_keeps_fresh_fast_values(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = (_Entry(name="in_pir"), _Entry(name="temp"))
    client = _Client({"in_pir": False, "temp": 21.5})
    coordinator, _ = _coordinator(monkeypatch, fast_ms=100, client=client, entries=entries)
    # before the fast lane has produced anything: full read includes the inputs
    data = await coordinator._async_update_data()
    assert client.asked[-1] == ("in_pir", "temp")
    assert data == {"in_pir": False, "temp": 21.5}
    # fast lane running and holding a fresher value: full cycle must not re-read or overwrite it
    coordinator._fast_task = object()
    coordinator._fast_values = {"in_pir": True}
    data = await coordinator._async_update_data()
    assert client.asked[-1] == ("temp",)
    assert data == {"temp": 21.5, "in_pir": True}


@pytest.mark.asyncio
async def test_entities_starting_disabled_are_read_only_once_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _harness._load_coordinator_module(monkeypatch)
    catalog = _Catalog((_Entry(name="temp"), _Entry(name="tamper")))
    catalog.entities = (
        SimpleNamespace(state_name="temp", enabled_by_default=True, transform_read="none"),
        SimpleNamespace(state_name="tamper", enabled_by_default=False, transform_read="none"),
    )
    client = _Client({"temp": 21.5, "tamper": False})
    coordinator = module.EpsnetDataCoordinator(
        _harness._FakeHass(), client=client, catalog=catalog, name="EPSNET",
        scan_interval_milliseconds=500,
    )
    await coordinator._async_update_data()
    assert client.asked[-1] == ("temp",)
    coordinator.add_state_name("tamper")  # the user enabled it; the entity was added
    await coordinator._async_update_data()
    assert client.asked[-1] == ("temp", "tamper")
    coordinator.remove_state_name("tamper")
    await coordinator._async_update_data()
    assert client.asked[-1] == ("temp",)
