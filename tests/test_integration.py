"""In-process Home Assistant integration tests (pytest-homeassistant-custom-component).

The EPSNET client is patched with a small synthetic dataset (no PLC, no sockets,
no home data — reuses the catalog test fixtures), so the *real* config flow, entry
setup, platforms, coordinator, and service calls run inside a real in-process
HomeAssistant. This is the HA-idiomatic "tier 3": device I/O is mocked, everything
above it is exercised for real.
"""
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.epsnet.const import DOMAIN
from custom_components.epsnet.pubfile import parse_pub_text

from test_catalog import EXACT_MODEL_FIXTURE, EXACT_RUNTIME_INVENTORY

SYNTH_ENTRIES = parse_pub_text(EXACT_MODEL_FIXTURE)
SYNTH_INVENTORY = EXACT_RUNTIME_INVENTORY
_CLIENT = "custom_components.epsnet.epsnet_udp.EpsnetUdpClient"


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _patch_client(set_value: AsyncMock):
    """Patch the client so setup runs against synthetic data, not a socket.

    The setup calls yield (``asyncio.sleep(0)``) so the config-flow progress task
    behaves like the real (slow) fetch instead of completing eagerly and skipping
    the progress step.
    """

    async def _inventory(*_a, **_k):
        await asyncio.sleep(0)
        return SYNTH_INVENTORY

    async def _fetch(*_a, **_k):
        await asyncio.sleep(0)
        return ("SIM PLC", SYNTH_ENTRIES)

    async def _get_values(entries, *_a, **_k):
        # Return a value for every polled entry so entities are *available*
        # (HA skips turn_on on unavailable entities).
        return {e.name: 0 for e in entries}

    return patch.multiple(
        _CLIENT,
        discover_runtime_inventory=AsyncMock(side_effect=_inventory),
        async_fetch_pub_from_plc=AsyncMock(side_effect=_fetch),
        get_values=AsyncMock(side_effect=_get_values),
        set_value=set_value,
        async_close=AsyncMock(),
    )


async def _run_discover_flow(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "EPSNET", "host": "127.0.0.1", "port": 61682,
         "local_port": 61681, "scan_interval": 500},
    )
    assert result["type"] == FlowResultType.MENU and result["step_id"] == "source"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "discover"}
    )
    # discover runs run_setup under a progress step; let the task finish then advance.
    assert result["type"] == FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "confirm"
    assert result["description_placeholders"]["source"] == "plc"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    return result


async def test_discover_flow_sets_up_entry_and_entities(hass: HomeAssistant) -> None:
    with _patch_client(AsyncMock()):
        result = await _run_discover_flow(hass)
        await hass.async_block_till_done()

        entry = result["result"]
        assert entry.state is ConfigEntryState.LOADED
        # frozen map persisted (no runtime re-fetch relies on this)
        assert entry.data["map"]["source"] == "plc"
        assert entry.data["map"]["entries"]

        lights = [s for s in hass.states.async_all() if s.entity_id.startswith("light.")]
        switches = [s for s in hass.states.async_all() if s.entity_id.startswith("switch.")]
        assert lights, "expected light entities from the synthetic catalog"
        assert switches, "expected switch entities from the synthetic catalog"


async def test_turn_on_reaches_the_client_write(hass: HomeAssistant) -> None:
    set_value = AsyncMock()
    with _patch_client(set_value):
        await _run_discover_flow(hass)
        await hass.async_block_till_done()

        # Only entities backed by an _ON command write; turn each on until one
        # routes through the client, proving the HA service -> client wiring.
        targets = [
            s.entity_id for s in hass.states.async_all()
            if s.entity_id.split(".")[0] in ("switch", "light")
        ]
        assert targets, "expected switch/light entities"
        for eid in targets:
            with contextlib.suppress(Exception):
                await hass.services.async_call(
                    eid.split(".")[0], "turn_on", {"entity_id": eid}, blocking=True
                )
            if set_value.await_count:
                break
        await hass.async_block_till_done()
        assert set_value.await_count >= 1, "turn_on should reach the client write path"


async def test_poll_metric_sensors_take_their_names_from_translations(
    hass: HomeAssistant,
) -> None:
    with _patch_client(AsyncMock()):
        await _run_discover_flow(hass)
        await hass.async_block_till_done()

        names = {
            s.attributes.get("friendly_name") for s in hass.states.async_all("sensor")
        }
        assert any(n and n.endswith("Average read duration") for n in names), names
        assert any(n and n.endswith("Read block count") for n in names), names


async def test_unreachable_plc_reshows_the_source_menu_with_a_warning(
    hass: HomeAssistant,
) -> None:
    from custom_components.epsnet.epsnet_udp import EpsnetError

    with _patch_client(AsyncMock()), patch(
        f"{_CLIENT}.discover_runtime_inventory", AsyncMock(side_effect=EpsnetError("x"))
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"name": "EPSNET", "host": "127.0.0.1", "port": 61682,
             "local_port": 61681, "scan_interval": 500},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "discover"}
        )
        await hass.async_block_till_done()
        # Re-fetching the flow (what the frontend does) re-runs the shown step.
        for _ in range(2):
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
            assert result["type"] == FlowResultType.MENU
            assert result["step_id"] == "source_cannot_connect"

        # Picking an option from the warning menu still works.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "upload"}
        )
        assert result["type"] == FlowResultType.FORM and result["step_id"] == "upload"


async def test_czech_install_gets_czech_sensor_names(hass: HomeAssistant) -> None:
    hass.config.language = "cs"
    with _patch_client(AsyncMock()):
        await _run_discover_flow(hass)
        await hass.async_block_till_done()

        names = {
            s.attributes.get("friendly_name") for s in hass.states.async_all("sensor")
        }
        assert any(n and n.endswith("Průměrná doba čtení") for n in names), names


async def test_unsupported_modules_are_reported_not_created(
    hass: HomeAssistant,
) -> None:
    """An untested PLC: unknown module codes -> notice in the flow + Repairs,
    no empty devices, and the diagnostics download carries the raw reads."""
    import dataclasses

    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import issue_registry as ir

    from custom_components.epsnet.diagnostics import async_get_config_entry_diagnostics
    from custom_components.epsnet.runtime_registry import lookup_runtime_model_info

    base = SYNTH_INVENTORY.devices[0]
    unknown = dataclasses.replace(
        base,
        key="unknown",
        runtime_index=30,
        slot_index=30,
        runtime_type_code=0x0D99,
        model_info=lookup_runtime_model_info(0x0D99),
    )
    inventory = dataclasses.replace(
        SYNTH_INVENTORY,
        devices=SYNTH_INVENTORY.devices + (unknown,),
        raw_reads=(("root", "00ff"),),
    )

    with _patch_client(AsyncMock()), patch(
        f"{_CLIENT}.discover_runtime_inventory", AsyncMock(return_value=inventory)
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"name": "EPSNET", "host": "127.0.0.1", "port": 61682,
             "local_port": 61681, "scan_interval": 500},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "discover"}
        )
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["step_id"] == "confirm_unsupported"
        assert result["description_placeholders"]["unsupported_codes"] == "0x0d99"
        assert result["description_placeholders"]["unsupported_count"] == "1"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        entry = result["result"]
        assert entry.state is ConfigEntryState.LOADED

        models = {
            d.model for d in dr.async_get(hass).devices.values()
            if entry.entry_id in d.config_entries
        }
        assert models and not any(m.startswith("Unknown") for m in models)

        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"unsupported_modules_{entry.entry_id}"
        )
        assert issue is not None
        assert issue.translation_placeholders["codes"] == "0x0d99"

        diag = await async_get_config_entry_diagnostics(hass, entry)
        catalog = diag["catalog"]
        assert catalog["raw_reads"] == {"root": "00ff"}
        assert [d["runtime_type_code"] for d in catalog["unsupported_runtime_devices"]] == ["0x0d99"]


async def test_migration_drops_slot_identifiers_from_map_devices(hass: HomeAssistant) -> None:
    """4.1 -> 4.2: map devices keep only their map key; slot-only devices are kept."""
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, version=4, minor_version=1, title="EPSNET",
        data={"name": "EPSNET", "host": "127.0.0.1", "port": 61682,
              "local_port": 61681, "scan_interval": 500},
    )
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    map_device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_runtime_40015382_04"),
                     (DOMAIN, f"{entry.entry_id}_sw_room_a")},
    )
    slot_device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_runtime_40015382_09")},
    )

    from custom_components.epsnet import async_migrate_entry

    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 2
    assert registry.async_get(map_device.id).identifiers == {
        (DOMAIN, f"{entry.entry_id}_sw_room_a")
    }
    assert registry.async_get(slot_device.id).identifiers == {
        (DOMAIN, f"{entry.entry_id}_runtime_40015382_09")
    }


async def test_discover_without_a_map_asks_for_the_pub_file(hass: HomeAssistant) -> None:
    """A PLC with no stored .pub must not create an entry."""

    async def _inventory(*_a, **_k):
        await asyncio.sleep(0)
        return SYNTH_INVENTORY

    async def _no_map(*_a, **_k):
        await asyncio.sleep(0)
        return ("SIM PLC", ())

    with patch.multiple(
        _CLIENT,
        discover_runtime_inventory=AsyncMock(side_effect=_inventory),
        async_fetch_pub_from_plc=AsyncMock(side_effect=_no_map),
        async_close=AsyncMock(),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"name": "EPSNET", "host": "127.0.0.1", "port": 61682,
             "local_port": 61681, "scan_interval": 500},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "discover"}
        )
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "source_no_map"
    assert result["menu_options"] == ["discover", "upload", "inventory_only"]
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_no_map_can_continue_with_the_modules_only(hass: HomeAssistant) -> None:
    """Without a .pub the user can still create an entry (modules, no entities)."""

    async def _inventory(*_a, **_k):
        await asyncio.sleep(0)
        return SYNTH_INVENTORY

    async def _no_map(*_a, **_k):
        await asyncio.sleep(0)
        return ("SIM PLC", ())

    with patch.multiple(
        _CLIENT,
        discover_runtime_inventory=AsyncMock(side_effect=_inventory),
        async_fetch_pub_from_plc=AsyncMock(side_effect=_no_map),
        get_values=AsyncMock(return_value={}),
        get_point_values=AsyncMock(return_value={}),
        async_close=AsyncMock(),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"name": "EPSNET", "host": "127.0.0.1", "port": 61682,
             "local_port": 61681, "scan_interval": 500},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "discover"}
        )
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "inventory_only"}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["description_placeholders"]["points"] == "0"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.data["map"]["entries"] == []
        assert entry.data["program_entities_first"] is True
        assert entry.state is ConfigEntryState.LOADED

        from custom_components.epsnet.diagnostics import async_get_config_entry_diagnostics

        diag = await async_get_config_entry_diagnostics(hass, entry)
        assert diag["map"]["entry_count"] == 0
        assert diag["catalog"]["groups"]


async def test_entry_without_a_map_lists_modules_without_entities(hass: HomeAssistant) -> None:
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import issue_registry as ir
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, version=4, minor_version=2, title="EPSNET",
        data={"name": "EPSNET", "host": "127.0.0.1", "port": 61682,
              "local_port": 61681, "scan_interval": 500},
    )
    entry.add_to_hass(hass)
    with _patch_client(AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        platforms = ("light.", "switch.", "binary_sensor.")
        assert not [s for s in hass.states.async_all() if s.entity_id.startswith(platforms)]
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_variable_map_{entry.entry_id}")


def _patch_upload(text: str, tmp_path):
    """Stand in for HA's file_upload: the form's file id resolves to ``text``."""
    path = tmp_path / "upload.pub"
    path.write_text(text, encoding="utf-8")

    @contextlib.contextmanager
    def _process(_hass, _file_id):
        yield path

    return patch(
        "homeassistant.components.file_upload.process_uploaded_file", _process
    )


async def _open_reimport_file_form(hass: HomeAssistant) -> dict:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    assert result["menu_options"] == ["reimport_plc", "reimport_file", "settings"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reimport_file"}
    )
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "reimport_file"
    return result


async def test_reimport_from_a_file_replaces_the_map_in_the_same_entry(
    hass: HomeAssistant, tmp_path
) -> None:
    with _patch_client(AsyncMock()):
        await _run_discover_flow(hass)
        await hass.async_block_till_done()
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        entity_ids = set(hass.states.async_entity_ids())
        assert entry.data["map"]["source"] == "plc"

        result = await _open_reimport_file_form(hass)
        with _patch_upload(EXACT_MODEL_FIXTURE, tmp_path):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"map_file": "0123456789abcdef0123456789abcdef"}
            )
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "run_file_setup"
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.data["map"]["source"] == "file"
    assert set(hass.states.async_entity_ids()) == entity_ids


async def test_reimport_from_a_file_of_another_project_keeps_the_map(
    hass: HomeAssistant, tmp_path
) -> None:
    with _patch_client(AsyncMock()):
        await _run_discover_flow(hass)
        await hass.async_block_till_done()
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        old_map = entry.data["map"]

        result = await _open_reimport_file_form(hass)
        with _patch_upload("unrelated_counter R W 100 INT PUB_INOUT\n", tmp_path):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"map_file": "0123456789abcdef0123456789abcdef"}
            )
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])

    assert result["type"] == FlowResultType.FORM and result["step_id"] == "reimport_file"
    assert result["errors"] == {"map_file": "pub_no_match"}
    assert entry.data["map"] == old_map
