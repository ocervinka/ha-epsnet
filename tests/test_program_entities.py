"""Entities placed from the PLC program's I/O layout (synthetic programs only)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.epsnet.catalog import build_catalog
from custom_components.epsnet.models import (
    MathFormula,
    PubEntry,
    RuntimeCibGroup,
    RuntimeDiscoveredDevice,
    RuntimeInventory,
)
from custom_components.epsnet.program_layout import (
    BytesProgramSource,
    decode_program_layout,
    layout_fingerprint,
)
from custom_components.epsnet.runtime_registry import lookup_runtime_model_info
from tests.test_program_layout import DEVICE, MASTER, _master, build_program

TABLES_AT = 0x07050000
WSB, RELAY, DIMMER, CIT = 0x0C36, 0x0C4F, 0x0C1D, 0x0CED


def _layout(program):
    image, tables = build_program([program])
    layout = decode_program_layout(BytesProgramSource(image, tables))
    return replace(layout, table_address=TABLES_AT)


def _child_record(table_offset: int) -> str:
    record = bytearray(0x66)
    record[0x40:0x44] = (TABLES_AT + table_offset).to_bytes(4, "big")
    return record.hex()


def _inventory(layout, codes, *, pair=True):
    lines = [d for d in layout.masters[0].devices if d.kind == "device"]
    devices = tuple(
        RuntimeDiscoveredDevice(
            key=f"runtime_{index}",
            group_token=0x40015382,
            group_label="CIB I/O BUS",
            group_marker=0x0D,
            group_kind=0x0004,
            runtime_index=index,
            slot_index=index,
            child_token=0x40017000 + index * 0x66,
            runtime_type_code=code,
            model_info=lookup_runtime_model_info(code),
            child_record_hex=_child_record(line.init_offset) if pair else None,
        )
        for index, (code, line) in enumerate(zip(codes, lines))
    )
    group = RuntimeCibGroup(
        token=0x40015382, meta=0x049E, label="CIB I/O BUS", marker=0x0D, kind=4,
        header_hex="", slot_map=tuple(range(len(devices))), devices=devices,
    )
    return RuntimeInventory(
        controller_identity="CPU", root_identity="CPU", root_tokens=(0x400F0000,),
        group_table_token=0x4001093C, groups=(group,), devices=devices,
        program_layout=layout,
    )


HOUSE = _master(
    lines=(
        (MASTER, 3128, 0, 2, 0),
        (DEVICE, 3126, 0x1A2B, 3, 1),  # wall switch: buttons, temperature / LEDs
        (DEVICE, 3151, 0x2B3C, 2, 2),  # relays: local switches / relays
        (DEVICE, 3101, 0x3C4D, 2, 2),  # dimmer: inputs, switches / 2 outputs
    ),
    conv_in=bytes([0x94, 0x01, 0x93, 0x02, 0x01, 0x01]),
    conv_out=bytes([0x01, 0x02, 0xC1, 0xC1]),
)


def _by_key(catalog):
    return {e.key: e for e in catalog.entities if e.from_program}


def test_without_a_map_every_channel_gets_a_read_only_entity():
    layout = _layout(HOUSE)
    catalog = build_catalog((), _inventory(layout, [WSB, RELAY, DIMMER]))
    entities = _by_key(catalog)

    temp = entities["io_0_2_3126_1a2b_temperature"]
    assert (temp.platform, temp.device_class, temp.enabled_by_default) == ("sensor", "temperature", True)
    entry = catalog.entries_by_name[temp.state_name]
    assert (entry.register, entry.storage, entry.address, entry.data_type) == ("X", "F", 5, "REAL")
    assert temp.transform_read == MathFormula.NONE  # already scaled by the firmware

    up1 = catalog.entries_by_name["io_0_2_3126_1a2b_button_up1"]
    assert (up1.register, up1.address, up1.bit) == ("X", 4, 0)
    relay12 = catalog.entries_by_name["io_0_2_3151_2b3c_relay_re12"]
    assert (relay12.register, relay12.address, relay12.bit) == ("Y", 2, 3)
    assert entities["io_0_2_3151_2b3c_relay_re12"].platform == "binary_sensor"
    out2 = catalog.entries_by_name["io_0_2_3101_3c4d_dimmer_out2"]
    assert (out2.register, out2.address, out2.data_type) == ("Y", 7, "REAL")
    # no switches or lights: outputs are shown, not switched
    assert {e.platform for e in entities.values()} == {"sensor", "binary_sensor"}
    # 4 buttons + temp + 4 LEDs, 12 + 12, 2 + 2 + 2
    assert len(entities) == 9 + 24 + 6
    devices = {e.device_key for e in entities.values()}
    assert {catalog.devices_by_key[k].name for k in devices} == {
        "WSB2-40 1A2B", "SA2-012M 2B3C", "DA2-22M 3C4D"}


def test_raw_thermometer_is_scaled_in_home_assistant():
    program = _master(lines=((DEVICE, 3126, 1, 3, 1),), conv_in=bytes([0x01, 0x02]), conv_out=b"\x01")
    catalog = build_catalog((), _inventory(_layout(program), [WSB]))
    temp = _by_key(catalog)["io_0_2_3126_0001_temperature"]
    entry = catalog.entries_by_name[temp.state_name]
    assert (entry.storage, entry.data_type, temp.transform_read) == ("W", "INT", MathFormula.DIV_100)


def test_with_a_map_only_channels_no_map_entity_shows_are_added_disabled():
    from custom_components.epsnet.program_entities import build_program_entities

    layout = _layout(HOUSE)
    inventory = _inventory(layout, [WSB, RELAY, DIMMER])
    shown = PubEntry(name="hall_up1", register="X", storage="B", address=4, bit=0,
                     data_type="BOOL", access="PUB_OUT", line_number=1)
    named_only = PubEntry(name="hall_down1", register="X", storage="B", address=4, bit=1,
                          data_type="BOOL", access="PUB_OUT", line_number=2)
    result = build_program_entities(
        layout, inventory.devices, {}, (shown, named_only), shown=(shown,),
        shown_devices={d.registry_key for d in inventory.devices})
    keys = {spec.key for spec in result.specs}
    assert "io_0_2_3126_1a2b_button_up1" not in keys  # a map entity shows it already
    assert "io_0_2_3126_1a2b_button_down1" in keys  # named in the map, but nothing shows it
    assert not any(spec.enabled_by_default for spec in result.specs)


def test_modules_without_the_record_pointer_are_paired_by_order():
    layout = _layout(HOUSE)
    catalog = build_catalog((), _inventory(layout, [WSB, RELAY, DIMMER], pair=False))
    entities = _by_key(catalog)
    assert entities["io_0_2_3126_1a2b_temperature"].device_key == "runtime_40015382_00"
    assert entities["io_0_2_3151_2b3c_relay_re1"].device_key == "runtime_40015382_01"


def test_modules_the_scan_does_not_match_get_their_own_device():
    layout = _layout(HOUSE)
    inventory = _inventory(layout, [WSB, RELAY, DIMMER], pair=False)
    inventory = replace(inventory, devices=inventory.devices[:1])  # the scan saw only one
    catalog = build_catalog((), inventory)
    relay = _by_key(catalog)["io_0_2_3151_2b3c_relay_re1"]
    assert relay.device_key == "io_0_2_3151_2b3c"
    assert catalog.devices_by_key[relay.device_key].name == "SA2-012M 2B3C"


def test_layout_not_matching_the_module_type_is_reported():
    # a sensor interface whose program has an extra item the descriptor does not know
    program = _master(
        lines=((DEVICE, 3309, 7, 8, 0),),
        conv_in=bytes([0x01, 0x95, 0x95, 0x93, 0x01]),
    )
    catalog = build_catalog((), _inventory(_layout(program), [CIT]))
    (report,) = catalog.program_report
    assert report["problems"] == ["in: the program has 5 data items, the module type 4"]
    assert not _by_key(catalog)


def test_sensor_interface_layout():
    program = _master(lines=((DEVICE, 3309, 7, 7, 0),), conv_in=bytes([0x01, 0x95, 0x95, 0x93]))
    catalog = build_catalog((), _inventory(_layout(program), [CIT]))
    entities = _by_key(catalog)
    flag = entities["io_0_2_3309_0007_status_vld1"]
    assert (flag.diagnostic, flag.enabled_by_default) == (True, False)
    assert catalog.entries_by_name[flag.state_name].bit == 1
    ai2 = catalog.entries_by_name["io_0_2_3309_0007_analog_2"]
    assert (ai2.address, ai2.data_type) == (5, "REAL")
    own = catalog.entries_by_name["io_0_2_3309_0007_internal_temperature"]
    assert own.address == 9


def test_fingerprint_ignores_raw_bytes_but_not_the_layout():
    a = _layout(HOUSE)
    assert layout_fingerprint(a) == layout_fingerprint(replace(a, reads={"image": {0: b"x"}}))
    moved = _layout(replace_house_start(HOUSE, 100))
    assert layout_fingerprint(a) != layout_fingerprint(moved)
    assert layout_fingerprint(None) is None


def replace_house_start(program, iin):
    return {**program, "iin": iin}


@pytest.mark.asyncio
async def test_watcher_settles_reloads_or_reports(monkeypatch):
    from types import SimpleNamespace

    from homeassistant.helpers import issue_registry as ir

    from custom_components.epsnet import program_watch

    created, deleted, reloads = [], [], []
    monkeypatch.setattr(ir, "async_create_issue", lambda *a, **k: created.append(a[2]))
    monkeypatch.setattr(ir, "async_delete_issue", lambda *a, **k: deleted.append(a[2]))
    layout = _layout(HOUSE)
    catalog = build_catalog((), _inventory(layout, [WSB, RELAY, DIMMER]))

    class Client:
        def __init__(self, inventories):
            self.inventories = list(inventories)

        async def get_point_values(self, points):
            return {"s70": 0x1234}

        async def discover_runtime_inventory(self):
            item = self.inventories.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    class Coordinator:
        program_layout_stale = False

        def async_update_listeners(self):
            pass

    async def no_sleep(_):
        return None

    hass = SimpleNamespace(config_entries=SimpleNamespace(async_schedule_reload=reloads.append))
    entry = SimpleNamespace(entry_id="e1")

    def watcher(inventories):
        coordinator = Coordinator()
        w = program_watch.ProgramWatcher(hass, entry, Client(inventories), coordinator, catalog,
                                         sleep=no_sleep)
        w.baseline = 0x1111
        return w, coordinator

    same = _inventory(layout, [WSB, RELAY, DIMMER])
    w, coordinator = watcher([same])
    await w.handle_change()
    assert (coordinator.program_layout_stale, w.baseline, reloads) == (False, 0x1234, [])

    moved = _inventory(_layout(replace_house_start(HOUSE, 100)), [WSB, RELAY, DIMMER])
    w, coordinator = watcher([program_watch.EpsnetError("busy"), moved])
    await w.handle_change()
    assert reloads == ["e1"]

    broken = replace(same, program_layout=replace(layout, error="not readable"))
    w, coordinator = watcher([broken, broken, broken])
    await w.handle_change()
    assert coordinator.program_layout_stale is True
    assert created == ["program_changed_e1"]
    assert w.baseline == 0x1234  # no new attempt until the checksum changes again


MAP_ADDED_LATER = """SW_hall_UP1 X B 4 .0 BOOL PUB_INOUT
SW_hall_DOWN1 X B 4 .1 BOOL PUB_INOUT
SW_hall_UP2 X B 4 .2 BOOL PUB_INOUT
SW_hall_DOWN2 X B 4 .3 BOOL PUB_INOUT
SW_hall_TERM X F 5 REAL PUB_INOUT
SW_hall_GREEN1 R B 100 .0 BOOL PUB_INOUT
SW_hall_GREEN1_ON R B 101 .0 BOOL PUB_INOUT
SW_hall_GREEN1_OFF R B 101 .1 BOOL PUB_INOUT
"""


def test_a_map_added_later_keeps_the_program_entities():
    from custom_components.epsnet.pubfile import parse_pub_text

    inventory = _inventory(_layout(HOUSE), [WSB, RELAY, DIMMER])
    before = build_catalog((), inventory)
    after = build_catalog(parse_pub_text(MAP_ADDED_LATER), inventory, program_first=True)

    program_before = {e.key: e for e in before.entities if e.from_program}
    program_after = {e.key: e for e in after.entities if e.from_program}
    assert program_after.keys() == program_before.keys()
    assert all(program_after[k].enabled_by_default == program_before[k].enabled_by_default
               for k in program_after)
    assert set(after.devices_by_key) == set(before.devices_by_key)
    # the map adds what the program alone cannot do, and nothing that duplicates it
    added = [e for e in after.entities if not e.from_program]
    assert added and {e.platform for e in added} <= {"light", "switch"}


def test_entries_set_up_with_a_map_are_unchanged_by_the_flag():
    from custom_components.epsnet.pubfile import parse_pub_text

    inventory = _inventory(_layout(HOUSE), [WSB, RELAY, DIMMER])
    entries = parse_pub_text(MAP_ADDED_LATER)
    normal = build_catalog(entries, inventory)
    with_map_entities = {e.device_key for e in normal.entities if not e.from_program}
    assert with_map_entities
    for e in normal.entities:
        if e.from_program and not e.diagnostic:
            # disabled where the map already shows the module, enabled where it shows nothing
            assert e.enabled_by_default == (e.device_key not in with_map_entities)


CRC = 0x0D31


@pytest.mark.parametrize(
    ("conv_in", "raw_in", "expected"),
    [
        # buttons, thermometer + humidity, external thermometer, then internal data
        (bytes([0x01, 0x95, 0x01, 0x95, 1, 1, 1, 1, 1]), 11,
         {"temperature": ("X", 1, "REAL"), "humidity": ("X", 5, "USINT"),
          "external_temperature": ("X", 6, "REAL")}),
        # external thermometer switched off in the program
        (bytes([0x01, 0x95, 0x01, 1, 1, 1, 1, 1]), 9,
         {"temperature": ("X", 1, "REAL"), "humidity": ("X", 5, "USINT")}),
        # an unknown code in the internal data: what comes before it is still placed
        (bytes([0x01, 0x95, 0x01, 0x95, 0x44]), 11,
         {"temperature": ("X", 1, "REAL"), "humidity": ("X", 5, "USINT"),
          "external_temperature": ("X", 6, "REAL")}),
    ],
)
def test_room_controller_variants(conv_in, raw_in, expected):
    program = _master(
        lines=((DEVICE, 3377, 0x5A5A, raw_in, 24),),
        conv_in=conv_in,
        conv_out=bytes([0x02] * 4 + [0x01] * 16),  # display contents: not described
    )
    catalog = build_catalog((), _inventory(_layout(program), [CRC]))
    entities = _by_key(catalog)
    values = {k.rsplit("5a5a_", 1)[1]: catalog.entries_by_name[k] for k, e in entities.items()
              if e.platform == "sensor"}
    assert {k: (v.register, v.address, v.data_type) for k, v in values.items()} == expected
    buttons = [k for k in entities if "_button_" in k]
    assert len(buttons) == 5
    assert all(e.platform != "sensor" or e.device_class in ("temperature", "humidity")
               for e in entities.values())


def test_diagnostics_list_bits_two_map_variables_share():
    from custom_components.epsnet.diagnostics import map_shared_addresses

    blob = {"entries": [
        {"name": "relay_1", "register": "Y", "address": 0, "bit": 0},
        {"name": "relay_9", "register": "Y", "address": 0, "bit": 0},  # belongs on Y1.0
        {"name": "relay_2", "register": "Y", "address": 0, "bit": 1},
        {"name": "whole_byte", "register": "Y", "address": 0, "bit": None},
        {"name": "flag_a", "register": "R", "address": 5, "bit": 0},
        {"name": "flag_b", "register": "R", "address": 5, "bit": 0},
    ]}
    assert map_shared_addresses(blob) == [{"address": "Y0.0", "pub_entries": ["relay_1", "relay_9"]}]
    assert map_shared_addresses(None) == []


@pytest.mark.asyncio
async def test_program_entities_no_longer_placed_leave_the_registry(hass):
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.epsnet import _async_remove_stale_program_entities
    from custom_components.epsnet.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    catalog = build_catalog((), _inventory(_layout(HOUSE), [WSB, RELAY, DIMMER]))

    def add(key):
        return registry.async_get_or_create(
            "binary_sensor", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry).entity_id

    kept = add("io_0_2_3151_2b3c_relay_re1")
    gone = add("io_0_2_3151_2b3c_relay_re13")
    map_entity = add("hall_light")  # not from the program: never touched
    broken = replace(catalog, runtime_inventory=replace(
        catalog.runtime_inventory,
        program_layout=replace(catalog.runtime_inventory.program_layout, error="not readable")))

    _async_remove_stale_program_entities(hass, entry, broken)
    assert registry.async_get(gone) is not None  # a failed read removes nothing
    _async_remove_stale_program_entities(hass, entry, catalog)
    assert registry.async_get(gone) is None
    assert registry.async_get(kept) is not None and registry.async_get(map_entity) is not None


async def test_reimport_of_a_file_naming_no_module_is_refused_with_program_entities(
    hass, tmp_path, enable_custom_integrations
):
    """Program-placed entities exist with any file, so they must not make a
    .pub of another project look like a match."""
    import asyncio
    import contextlib
    from unittest.mock import AsyncMock, patch

    from homeassistant.data_entry_flow import FlowResultType
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.epsnet.const import DOMAIN

    inventory = _inventory(_layout(HOUSE), [WSB, RELAY, DIMMER])
    upload = tmp_path / "other.pub"
    upload.write_text("unrelated_counter R W 100 INT PUB_INOUT\n", encoding="utf-8")

    @contextlib.contextmanager
    def _process(_hass, _file_id):
        yield upload

    entry = MockConfigEntry(
        domain=DOMAIN, version=4, minor_version=2, title="EPSNET",
        data={"name": "EPSNET", "host": "127.0.0.1", "port": 61682,
              "local_port": 61681, "scan_interval": 500},
    )
    entry.add_to_hass(hass)

    async def _inventory_soon(*_a, **_k):
        await asyncio.sleep(0)  # let the flow show its progress step
        return inventory

    with patch.multiple(
        "custom_components.epsnet.epsnet_udp.EpsnetUdpClient",
        discover_runtime_inventory=AsyncMock(side_effect=_inventory_soon),
        async_fetch_pub_from_plc=AsyncMock(return_value=("SIM PLC", ())),
        get_values=AsyncMock(return_value={}),
        get_point_values=AsyncMock(return_value={}),
        async_close=AsyncMock(),
    ), patch("homeassistant.components.file_upload.process_uploaded_file", _process), \
            patch("custom_components.epsnet.program_watch.ProgramWatcher.run", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        old_data = dict(entry.data)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "reimport_file"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"map_file": "0123456789abcdef0123456789abcdef"})
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])

    assert result["type"] == FlowResultType.FORM and result["step_id"] == "reimport_file"
    assert result["errors"] == {"map_file": "pub_no_match"}
    assert dict(entry.data) == old_data
    await hass.config_entries.async_unload(entry.entry_id)
