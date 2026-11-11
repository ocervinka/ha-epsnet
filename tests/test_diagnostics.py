from __future__ import annotations

from custom_components.epsnet.catalog import build_catalog
from custom_components.epsnet.diagnostics import build_catalog_snapshot
from custom_components.epsnet.models import RuntimeCibGroup, RuntimeDiscoveredDevice, RuntimeInventory
from custom_components.epsnet.pubfile import parse_pub_text
from custom_components.epsnet.runtime_registry import lookup_runtime_model_info

SNAPSHOT_FIXTURE = """
SW_room_b_panel_button_scene_a X B 41 .0 BOOL PUB_INOUT
SW_room_b_panel_button_scene_b X B 41 .1 BOOL PUB_INOUT
SW_room_b_panel_button_scene_c X B 41 .2 BOOL PUB_INOUT
SW_room_b_panel_button_scene_d X B 41 .3 BOOL PUB_INOUT
state_SW_room_b_panel_led_scene_a Y B 10 .0 BOOL PUB_OUT
SW_room_b_panel_led_scene_a_ON R B 19836 .0 BOOL PUB_INOUT
SW_room_b_panel_led_scene_a_OFF R B 19836 .1 BOOL PUB_INOUT
SW_room_b_panel_led_scene_a_TRIG R B 19836 .2 BOOL PUB_INOUT
SW_room_b_panel_led_scene_a R B 19846 .0 BOOL PUB_INOUT
state_SW_room_b_panel_led_scene_b Y B 10 .1 BOOL PUB_OUT
SW_room_b_panel_led_scene_b_ON R B 19864 .0 BOOL PUB_INOUT
SW_room_b_panel_led_scene_b_OFF R B 19864 .1 BOOL PUB_INOUT
SW_room_b_panel_led_scene_b_TRIG R B 19864 .2 BOOL PUB_INOUT
SW_room_b_panel_led_scene_b R B 19874 .0 BOOL PUB_INOUT
state_SW_room_b_panel_led_scene_c Y B 10 .2 BOOL PUB_OUT
SW_room_b_panel_led_scene_c_ON R B 19892 .0 BOOL PUB_INOUT
SW_room_b_panel_led_scene_c_OFF R B 19892 .1 BOOL PUB_INOUT
SW_room_b_panel_led_scene_c_TRIG R B 19892 .2 BOOL PUB_INOUT
SW_room_b_panel_led_scene_c R B 19902 .0 BOOL PUB_INOUT
state_SW_room_b_panel_led_scene_d Y B 10 .3 BOOL PUB_OUT
SW_room_b_panel_led_scene_d_ON R B 19920 .0 BOOL PUB_INOUT
SW_room_b_panel_led_scene_d_OFF R B 19920 .1 BOOL PUB_INOUT
SW_room_b_panel_led_scene_d_TRIG R B 19920 .2 BOOL PUB_INOUT
SW_room_b_panel_led_scene_d R B 19930 .0 BOOL PUB_INOUT
SW_room_b_panel_room_temp X F 71 REAL PUB_INOUT
OUTPUT_main_board_relay_panel_button_1 X B 80 .0 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_button_2 X B 80 .1 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_button_3 X B 80 .2 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_button_4 X B 80 .3 BOOL PUB_INOUT
state_OUTPUT_main_board_relay_panel_light_balcony Y B 28 .0 BOOL PUB_OUT
OUTPUT_main_board_relay_panel_light_balcony_ON R B 30000 .0 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_light_balcony_OFF R B 30000 .1 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_light_balcony_TRIG R B 30000 .2 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_light_balcony R B 30010 .0 BOOL PUB_INOUT
state_OUTPUT_main_board_relay_panel_fan_bathroom Y B 28 .1 BOOL PUB_OUT
OUTPUT_main_board_relay_panel_fan_bathroom_ON R B 30028 .0 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_fan_bathroom_OFF R B 30028 .1 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_fan_bathroom_TRIG R B 30028 .2 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_fan_bathroom R B 30038 .0 BOOL PUB_INOUT
state_OUTPUT_main_board_relay_panel_pump_garden Y B 28 .2 BOOL PUB_OUT
OUTPUT_main_board_relay_panel_pump_garden_ON R B 30056 .0 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_pump_garden_OFF R B 30056 .1 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_pump_garden_TRIG R B 30056 .2 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_pump_garden R B 30066 .0 BOOL PUB_INOUT
state_OUTPUT_main_board_relay_panel_RE4_NC Y B 28 .3 BOOL PUB_OUT
OUTPUT_main_board_relay_panel_RE4_NC_ON R B 30084 .0 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_RE4_NC_OFF R B 30084 .1 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_RE4_NC_TRIG R B 30084 .2 BOOL PUB_INOUT
OUTPUT_main_board_relay_panel_RE4_NC R B 30094 .0 BOOL PUB_INOUT
"""


def _make_runtime_inventory(type_codes: list[int]) -> RuntimeInventory:
    devices = tuple(
        RuntimeDiscoveredDevice(
            key=f"runtime_{index}",
            group_token=0x40015382,
            group_label="CIB I/O BUS",
            group_marker=0x08,
            group_kind=0x0004,
            runtime_index=index,
            slot_index=index,
            child_token=0x40017000 + (index * 0x66),
            runtime_type_code=type_code,
            model_info=lookup_runtime_model_info(type_code),
        )
        for index, type_code in enumerate(type_codes)
    )
    slot_map = tuple(range(len(devices))) + (0x7F,) * (32 - len(devices))
    return RuntimeInventory(
        controller_identity="CP201MK B2.77.2",
        root_identity="100 CP201MK V7.2",
        root_tokens=(0x400F0000, 0x400100BA, 0x4001093C),
        group_table_token=0x4001093C,
        groups=(
            RuntimeCibGroup(
                token=0x40015382,
                meta=0x049E,
                label="CIB I/O BUS",
                marker=0x08,
                kind=0x0004,
                header_hex=f"000011{len(devices):02x}00000000",
                slot_map=slot_map,
                devices=devices,
            ),
        ),
        devices=devices,
    )


def test_build_catalog_snapshot_includes_pub_role_counts_and_ranges() -> None:
    entries = parse_pub_text(SNAPSHOT_FIXTURE)
    runtime_inventory = _make_runtime_inventory([0x0C36, 0x0C32])
    catalog = build_catalog(entries, runtime_inventory)

    snapshot = build_catalog_snapshot(
        catalog,
        host="192.0.2.10",
        port=61682,
        local_port=61681,
        map_path="/tmp/test.pub",
    )

    runtime_devices = snapshot["groups"][0]["devices"]
    room_snapshot = runtime_devices[0]["pub_match"]
    output_snapshot = runtime_devices[1]["pub_match"]

    assert snapshot["source"] == {
        "discovery": "epsnet_runtime",
        "host": "192.0.2.10",
        "port": 61682,
        "local_port": 61681,
        "map_path": "/tmp/test.pub",
        "pub_enrichment_active": True,
        "pub_entry_count": len(entries),
    }
    assert snapshot["unmatched_runtime_devices"] == []
    assert snapshot["unmatched_pub_devices"] == []

    assert room_snapshot["device_key"] == "sw_room_b_panel"
    assert room_snapshot["registry_keys"] == ["sw_room_b_panel"]
    assert room_snapshot["role_entry_counts"] == {
        "r_bool": 4,
        "r_cmd_off": 4,
        "r_cmd_on": 4,
        "r_cmd_trig": 4,
        "x_bool": 4,
        "x_real": 1,
        "y_bool_state": 4,
    }
    room_groups = {
        group["role"]: group for group in room_snapshot["entry_groups"]
    }
    assert room_groups["x_real"]["address_ranges"] == [
        {
            "register": "X",
            "storage": "F",
            "data_type": "REAL",
            "start_address": 71,
            "end_address": 74,
        }
    ]

    assert output_snapshot["device_key"] == "output_main_board_relay_panel"
    assert output_snapshot["role_entry_counts"] == {
        "r_bool": 4,
        "r_cmd_off": 4,
        "r_cmd_on": 4,
        "r_cmd_trig": 4,
        "x_bool": 4,
        "y_bool_state": 4,
    }
    output_groups = {
        group["role"]: group for group in output_snapshot["entry_groups"]
    }
    assert output_groups["y_bool_state"]["address_ranges"] == [
        {
            "register": "Y",
            "storage": "B",
            "data_type": "BOOL",
            "start_address": 28,
            "end_address": 28,
        }
    ]
    assert output_groups["r_bool"]["address_ranges"] == [
        {
            "register": "R",
            "storage": "B",
            "data_type": "BOOL",
            "start_address": 30010,
            "end_address": 30010,
        },
        {
            "register": "R",
            "storage": "B",
            "data_type": "BOOL",
            "start_address": 30038,
            "end_address": 30038,
        },
        {
            "register": "R",
            "storage": "B",
            "data_type": "BOOL",
            "start_address": 30066,
            "end_address": 30066,
        },
        {
            "register": "R",
            "storage": "B",
            "data_type": "BOOL",
            "start_address": 30094,
            "end_address": 30094,
        },
    ]


def test_build_catalog_snapshot_marks_runtime_only_mode_without_pub() -> None:
    runtime_inventory = _make_runtime_inventory([0x0C36])
    catalog = build_catalog((), runtime_inventory)

    snapshot = build_catalog_snapshot(
        catalog,
        host="192.0.2.10",
        port=61682,
        local_port=61681,
        map_path=None,
    )

    assert snapshot["source"] == {
        "discovery": "epsnet_runtime",
        "host": "192.0.2.10",
        "port": 61682,
        "local_port": 61681,
        "map_path": None,
        "pub_enrichment_active": False,
        "pub_entry_count": 0,
    }
    assert snapshot["groups"][0]["devices"][0]["pub_match"] is None
    assert snapshot["unmatched_pub_devices"] == []
    assert snapshot["unmatched_runtime_devices"][0]["model"] == "WSB2-40"


def test_map_snapshot_lists_every_variable_like_a_pub_line() -> None:
    from custom_components.epsnet.diagnostics import map_snapshot
    from custom_components.epsnet.map_sources import entries_crc, serialize_entries

    entries = parse_pub_text(
        "lamp_a_ON R B 100 .0 BOOL PUB_INOUT\nroom_temp X F 71 REAL PUB_INOUT\n"
    )
    blob = {"source": "file", "plc_ident": None, "crc": entries_crc(entries),
            "entries": serialize_entries(entries)}
    snap = map_snapshot(blob)
    assert snap["source"] == "file"
    assert snap["entry_count"] == 2
    assert snap["entries"] == ["lamp_a_ON R B 100 .0 BOOL PUB_INOUT", "room_temp X F 71 REAL PUB_INOUT"]
    assert map_snapshot(None) == {"source": "none", "entry_count": 0, "entries": []}
