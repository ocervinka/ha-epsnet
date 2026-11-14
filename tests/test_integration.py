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
from custom_components.epsnet.epsnet_udp import DiscoveredPlc
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


_SEARCH = "custom_components.epsnet.config_flow.async_search_plcs"
SIM_PLC = DiscoveredPlc("127.0.0.1", "CP9999 B1.0")


@pytest.fixture(autouse=True)
def _no_plc_on_the_network():
    """The network search finds nothing unless a test says otherwise."""
    with patch(_SEARCH, AsyncMock(return_value=[])) as search:
        yield search


async def _pick_found_plc(hass: HomeAssistant, found=(SIM_PLC,)) -> dict:
    """Start the flow with ``found`` on the network and pick the first PLC."""
    with patch(_SEARCH, AsyncMock(return_value=list(found))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"plc": found[0].host}
    )


async def _finish_progress(hass: HomeAssistant, result: dict) -> dict:
    """Let the setup task end, then advance as the frontend does."""
    assert result["type"] == FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def _run_discover_flow(hass: HomeAssistant) -> dict:
    """The usual path: one PLC found, picked, read -- and the entry exists."""
    result = await _finish_progress(hass, await _pick_found_plc(hass))
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
        # nothing was asked: the name, ports and intervals are defaults
        assert entry.title == result["title"] == "CP201MK (127.0.0.1)"
        assert (entry.data["port"], entry.data["local_port"]) == (61682, 61681)
        assert (entry.data["scan_interval"], entry.data["fast_scan_interval"]) == (500, 100)
        assert result["description"] == "with_names"
        assert result["description_placeholders"]["variables"] == str(len(SYNTH_ENTRIES))

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


async def test_no_plc_found_asks_for_the_address_with_ports_folded_away(
    hass: HomeAssistant,
) -> None:
    with _patch_client(AsyncMock()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.FORM and result["step_id"] == "manual"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": " 192.0.2.7 ", "advanced": {"port": 61700, "local_port": 0}},
        )
        result = await _finish_progress(hass, result)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    data = result["result"].data
    assert (data["host"], data["port"], data["local_port"]) == ("192.0.2.7", 61700, 0)


async def test_unreachable_plc_returns_to_the_address_form_with_an_error(
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
            result["flow_id"], {"host": "192.0.2.7", "advanced": {"port": 61682, "local_port": 61681}}
        )
        result = await _finish_progress(hass, result)

    assert result["type"] == FlowResultType.FORM and result["step_id"] == "manual"
    assert result["errors"] == {"host": "cannot_connect"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_plcs_already_set_up_are_not_offered_again(hass: HomeAssistant) -> None:
    other = DiscoveredPlc("192.0.2.9", "CP8888 B2.0")
    with _patch_client(AsyncMock()):
        await _run_discover_flow(hass)
        await hass.async_block_till_done()

        with patch(_SEARCH, AsyncMock(return_value=[SIM_PLC, other])):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
        options = result["data_schema"].schema["plc"].config["options"]
        assert [o["value"] for o in options] == ["192.0.2.9", "manual"]
        assert options[0]["label"] == "CP8888 B2.0 · 192.0.2.9"

        # "Enter an address myself" leads to the address form.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"plc": "manual"}
        )
        assert result["type"] == FlowResultType.FORM and result["step_id"] == "manual"
        # ... where an address that is already set up is refused.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "127.0.0.1", "advanced": {"port": 61682, "local_port": 61681}}
        )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"


async def test_plc_in_use_offers_to_try_again(hass: HomeAssistant) -> None:
    from custom_components.epsnet.epsnet_udp import PlcBusyError

    async def _busy(*_a, **_k):
        await asyncio.sleep(0)
        raise PlcBusyError("held")

    with _patch_client(AsyncMock()):
        with patch(f"{_CLIENT}.async_fetch_pub_from_plc", AsyncMock(side_effect=_busy)):
            result = await _finish_progress(hass, await _pick_found_plc(hass))
            assert result["type"] == FlowResultType.MENU and result["step_id"] == "busy"
            assert result["menu_options"] == ["retry", "upload"]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "retry"}
        )
        result = await _finish_progress(hass, result)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()


async def test_a_plc_noticed_on_the_network_is_offered_and_added(
    hass: HomeAssistant,
) -> None:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    info = DhcpServiceInfo(ip="127.0.0.1", hostname="", macaddress="000a14010203")
    with _patch_client(AsyncMock()), patch(_SEARCH, AsyncMock(return_value=[SIM_PLC])) as search:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "dhcp"}, data=info
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "discovery_confirm"
        assert result["description_placeholders"] == {"ident": "CP9999 B1.0", "host": "127.0.0.1"}
        search.assert_awaited_once_with(["127.0.0.1"])  # asked that address only

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _finish_progress(hass, result)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert result["result"].unique_id == "00:0a:14:01:02:03"

        # Seen again, also at a new address: no second card, the address follows.
        moved = DhcpServiceInfo(ip="192.0.2.44", hostname="", macaddress="000a14010203")
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "dhcp"}, data=moved
        )
        await hass.async_block_till_done()
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        assert hass.config_entries.async_entries(DOMAIN)[0].data["host"] == "192.0.2.44"


async def test_a_network_card_that_is_no_plc_is_not_offered(hass: HomeAssistant) -> None:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    info = DhcpServiceInfo(ip="192.0.2.50", hostname="", macaddress="000a14aabbcc")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "dhcp"}, data=info
    )
    assert result["type"] == FlowResultType.ABORT and result["reason"] == "no_plc"


async def test_a_plc_set_up_by_host_name_is_not_offered_again(hass: HomeAssistant) -> None:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(
        domain=DOMAIN, version=4, minor_version=2, title="PLC",
        data={"host": "plc.example", "port": 61682, "local_port": 61681, "scan_interval": 500},
    ).add_to_hass(hass)
    info = DhcpServiceInfo(ip="192.0.2.60", hostname="", macaddress="000a14010203")
    with patch("socket.gethostbyname", return_value="192.0.2.60"), patch(
        _SEARCH, AsyncMock(return_value=[DiscoveredPlc("192.0.2.60", "CP9999 B1.0")])
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "dhcp"}, data=info
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_adding_by_hand_settles_the_discovered_card(hass: HomeAssistant) -> None:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    info = DhcpServiceInfo(ip="127.0.0.1", hostname="", macaddress="000a14010203")
    with _patch_client(AsyncMock()):
        with patch(_SEARCH, AsyncMock(return_value=[SIM_PLC])):
            discovered = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "dhcp"}, data=info
            )
        assert discovered["step_id"] == "discovery_confirm"

        result = await _run_discover_flow(hass)
        await hass.async_block_till_done()

    assert result["result"].unique_id == "00:0a:14:01:02:03"
    assert not hass.config_entries.flow.async_progress()


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
    """An untested PLC: unknown module codes -> a line at the end of setup + Repairs,
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
        result = await _run_discover_flow(hass)
        # not a question during setup: a line on the success screen + Repairs
        assert result["description"] == "with_names_unsupported"
        assert result["description_placeholders"]["unsupported_codes"] == "0x0d99"
        assert result["description_placeholders"]["unsupported_count"] == "1"
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
        result = await _finish_progress(hass, await _pick_found_plc(hass))

    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "no_map"
    assert result["menu_options"] == ["upload", "inventory_only"]
    assert result["description_placeholders"] == {
        "modules": str(len(SYNTH_INVENTORY.devices))
    }
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
        result = await _finish_progress(hass, await _pick_found_plc(hass))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "inventory_only"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["description"] == "read_only"
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
    assert result["menu_options"] == ["reimport_plc", "reimport_file", "speed", "diagnostics"]
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
        assert result["progress_action"] == "match_file"
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


async def test_removing_the_entry_removes_its_repairs(hass: HomeAssistant) -> None:
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
    issues = ir.async_get(hass)
    ir.async_create_issue(
        hass, DOMAIN, f"map_changed_{entry.entry_id}", is_fixable=False,
        severity=ir.IssueSeverity.WARNING, translation_key="map_changed",
    )
    assert issues.async_get_issue(DOMAIN, f"no_variable_map_{entry.entry_id}")

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not [i for i in issues.issues.values() if i.domain == DOMAIN]


async def test_setup_file_naming_no_module_is_questioned_but_can_be_used(
    hass: HomeAssistant, tmp_path
) -> None:
    """At setup there is no working map to protect: say it, then let the user decide."""

    async def _no_map(*_a, **_k):
        await asyncio.sleep(0)
        return ("SIM PLC", ())

    with _patch_client(AsyncMock()), patch(
        f"{_CLIENT}.async_fetch_pub_from_plc", AsyncMock(side_effect=_no_map)
    ):
        result = await _finish_progress(hass, await _pick_found_plc(hass))
        assert result["step_id"] == "no_map"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "upload"}
        )
        assert result["type"] == FlowResultType.FORM and result["step_id"] == "upload"
        with _patch_upload("unrelated_counter R W 100 INT PUB_INOUT\n", tmp_path):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"map_file": "0123456789abcdef0123456789abcdef"}
            )
            assert result["progress_action"] == "match_file"
            result = await _finish_progress(hass, result)
        assert result["type"] == FlowResultType.MENU
        assert result["step_id"] == "file_no_match"
        assert result["menu_options"] == ["upload", "use_file"]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "use_file"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["description"] == "read_only"
        await hass.async_block_till_done()

    entry = result["result"]
    assert [e["name"] for e in entry.data["map"]["entries"]] == ["unrelated_counter"]


async def test_update_speed_shows_measurements_and_checks_the_range(
    hass: HomeAssistant,
) -> None:
    import voluptuous as vol

    with _patch_client(AsyncMock()):
        await _run_discover_flow(hass)
        await hass.async_block_till_done()
        entry = hass.config_entries.async_entries(DOMAIN)[0]

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "speed"}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] in ("speed", "speed_unmeasured")
        if result["step_id"] == "speed":
            assert set(result["description_placeholders"]) == {
                "values", "read_ms", "cycle_ms", "fast_inputs"
            }
        with pytest.raises(vol.Invalid):
            result["data_schema"]({"scan_interval": 300_001, "fast_scan_interval": 100})

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"scan_interval": 200, "fast_scan_interval": 0}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert (entry.data["scan_interval"], entry.data["fast_scan_interval"]) == (200, 0)


async def test_diagnostics_download_hides_names_until_told_otherwise(
    hass: HomeAssistant,
) -> None:
    from custom_components.epsnet.diagnostics import async_get_config_entry_diagnostics

    with _patch_client(AsyncMock()):
        await _run_discover_flow(hass)
        await hass.async_block_till_done()
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert (await async_get_config_entry_diagnostics(hass, entry))["names_anonymized"]

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "diagnostics"}
        )
        assert result["type"] == FlowResultType.FORM and result["step_id"] == "diagnostics"
        # ticked the first time
        assert result["data_schema"]({}) == {"anonymize_diagnostics": True}

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"anonymize_diagnostics": False}
        )
        assert result["step_id"] == "diagnostics_download"
        link = result["description_placeholders"]["link"]
        assert link.startswith(
            f'<a href="/api/diagnostics/config_entry/{entry.entry_id}?authSig='
        )
        assert 'target="_blank"' in link  # else the frontend navigates instead
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

        # the choice is kept, and the integration was not reloaded for it
        assert entry.data["anonymize_diagnostics"] is False
        assert entry.state is ConfigEntryState.LOADED
        assert not (await async_get_config_entry_diagnostics(hass, entry))["names_anonymized"]

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "diagnostics"}
        )
        assert result["data_schema"]({}) == {"anonymize_diagnostics": False}


async def test_entries_from_before_keep_the_fast_lane_off(hass: HomeAssistant) -> None:
    """An entry that never stored the setting is not switched to the new default."""
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
        assert not hass.data[DOMAIN][entry.entry_id].coordinator.fast_lane_active
