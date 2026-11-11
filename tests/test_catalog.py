from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from custom_components.epsnet.catalog import _match_runtime_inventory, build_catalog
from custom_components.epsnet.models import (
    AddressRange,
    DetectedDevice,
    DetectedDeviceEntryGroup,
    RuntimeCibGroup,
    RuntimeDiscoveredDevice,
    RuntimeInventory,
)
from custom_components.epsnet.pubfile import load_pub_entries, parse_pub_text
from custom_components.epsnet.runtime_registry import lookup_runtime_model_info


FIXTURE = Path(__file__).parent / "fixtures" / "sample.pub"

EXACT_MODEL_FIXTURE = """
INPUT_stairs_inputs_panel_PIR_stairs_01fl X B 5 .0 BOOL PUB_INOUT
INPUT_stairs_inputs_panel_PIR_stairs_02fl X B 5 .1 BOOL PUB_INOUT
INPUT_stairs_inputs_panel_door_front X B 5 .2 BOOL PUB_INOUT
INPUT_stairs_inputs_panel_window_back X B 5 .3 BOOL PUB_INOUT
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
DIMMER_living_area_dimmer_button_cove X B 62 .0 BOOL PUB_INOUT
DIMMER_living_area_dimmer_button_table X B 62 .1 BOOL PUB_INOUT
state_DIMMER_living_area_dimmer_cove_light Y F 14 REAL PUB_OUT
DIMMER_living_area_dimmer_cove_light_ON R B 20570 .1 BOOL PUB_INOUT
DIMMER_living_area_dimmer_cove_light_OFF R B 20570 .2 BOOL PUB_INOUT
DIMMER_living_area_dimmer_cove_light R F 20609 REAL PUB_INOUT
state_DIMMER_living_area_dimmer_table_light Y F 18 REAL PUB_OUT
DIMMER_living_area_dimmer_table_light_ON R B 20574 .1 BOOL PUB_INOUT
DIMMER_living_area_dimmer_table_light_OFF R B 20574 .2 BOOL PUB_INOUT
DIMMER_living_area_dimmer_table_light R F 20613 REAL PUB_INOUT
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

PARTIAL_RELAY_FIXTURE = """
OUTPUT_panel_c_SW1 X B 80 .0 BOOL PUB_INOUT
OUTPUT_panel_c_SW2 X B 80 .1 BOOL PUB_INOUT
OUTPUT_panel_c_SW3 X B 80 .2 BOOL PUB_INOUT
OUTPUT_panel_c_SW4 X B 80 .3 BOOL PUB_INOUT
state_OUTPUT_panel_c_light_4 Y B 28 .0 BOOL PUB_OUT
OUTPUT_panel_c_light_4_ON R B 30000 .0 BOOL PUB_INOUT
OUTPUT_panel_c_light_4_OFF R B 30000 .1 BOOL PUB_INOUT
OUTPUT_panel_c_light_4_TRIG R B 30000 .2 BOOL PUB_INOUT
OUTPUT_panel_c_light_4 R B 30010 .0 BOOL PUB_INOUT
state_OUTPUT_panel_c_light_5 Y B 28 .1 BOOL PUB_OUT
OUTPUT_panel_c_light_5_ON R B 30028 .0 BOOL PUB_INOUT
OUTPUT_panel_c_light_5_OFF R B 30028 .1 BOOL PUB_INOUT
OUTPUT_panel_c_light_5_TRIG R B 30028 .2 BOOL PUB_INOUT
OUTPUT_panel_c_light_5 R B 30038 .0 BOOL PUB_INOUT
state_OUTPUT_panel_c_RE3_NC Y B 28 .2 BOOL PUB_OUT
state_OUTPUT_panel_c_RE4_NC Y B 28 .3 BOOL PUB_OUT
"""

ANALOG_MODEL_FIXTURE = """
C_IT_0200I_IN_1 X B 101 BOOL PUB_INOUT
C_IT_0200I_IN_2 X B 101 BOOL PUB_INOUT
C_IT_0200I_IN_3 X B 101 BOOL PUB_INOUT
C_IT_0200I_IN_4 X B 101 BOOL PUB_INOUT
C_IT_0200I_IN_5 X B 101 BOOL PUB_INOUT
C_IT_0200I_IN_6 X B 101 BOOL PUB_INOUT
C_IT_0200I_FLUE X F 102 REAL PUB_INOUT
C_IT_0200I_STOVE X F 106 REAL PUB_INOUT
C_IT_0200I_TERM X F 110 REAL PUB_INOUT
"""


def _make_runtime_inventory(type_codes: list[int]) -> RuntimeInventory:
    devices = tuple(
        RuntimeDiscoveredDevice(
            key=f"runtime_{index}",
            group_token=0x40015382,
            group_label="CIB I/O BUS",
            group_marker=0x0D,
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
        cpu_off_in=0x1D00,
        cpu_off_out=0x3D00,
        groups=(
            RuntimeCibGroup(
                token=0x40015382,
                meta=0x049E,
                label="CIB I/O BUS",
                marker=0x0D,
                kind=0x0004,
                header_hex=f"000011{len(devices):02x}00000000",
                slot_map=slot_map,
                devices=devices,
                off_in=0x1D11, # X = 0x1D11 - 0x1D00 = 17
                off_out=0x3EF4, # Y = 0x3EF4 - 0x3D00 = 500
                in_len=10,
                out_len=2,
            ),
        ),
        devices=devices,
    )


SAMPLE_RUNTIME_INVENTORY = _make_runtime_inventory(
    [0x0C25, 0x0C36, 0x0C1D, 0x0C4F, 0x0CED]
)
EXACT_RUNTIME_INVENTORY = _make_runtime_inventory([0x0C4E, 0x0C36, 0x0C1D, 0x0C32])
PARTIAL_RELAY_RUNTIME_INVENTORY = _make_runtime_inventory([0x0C32])
ANALOG_RUNTIME_INVENTORY = _make_runtime_inventory([0x0CED])


def _make_detected_device(
    key: str,
    *,
    type_key: str,
    model: str,
    subsystem: str,
    start_address: int,
    end_address: int,
) -> DetectedDevice:
    entry_name = f"{key}_entry"
    return DetectedDevice(
        key=key,
        name=key,
        type_key=type_key,
        model=model,
        subsystem=subsystem,
        entry_names=(entry_name,),
        entry_groups=(
            DetectedDeviceEntryGroup(
                role="test",
                register="X",
                storage="B",
                data_type="BOOL",
                access_modes=("PUB_INOUT",),
                address_ranges=(
                    AddressRange(
                        register="X",
                        storage="B",
                        data_type="BOOL",
                        start_address=start_address,
                        end_address=end_address,
                    ),
                ),
                entry_names=(entry_name,),
            ),
        ),
    )


def test_build_catalog_preserves_exported_channel_names() -> None:
    catalog = build_catalog(load_pub_entries(FIXTURE), SAMPLE_RUNTIME_INVENTORY)

    lights = catalog.entities_for_platform("light")
    switches = catalog.entities_for_platform("switch")
    sensors = catalog.entities_for_platform("sensor")
    binary_sensors = catalog.entities_for_platform("binary_sensor")

    assert [entity.name for entity in lights] == [
        "light_1",
        "light_2",
    ]
    assert [entity.name for entity in switches] == ["valve_1"]
    assert [entity.name for entity in sensors] == [
        "FLUE",
        "TERM",
    ]
    assert [entity.name for entity in binary_sensors] == [
        "door_contact_1",
        "PIR_1",
    ]
    assert [entity.device_key for entity in lights] == [
        "dimmer_r1",
        "output_r0b",
    ]
    assert [entity.device_key for entity in switches] == ["output_r0b"]
    assert [entity.device_key for entity in sensors] == [
        "c_it_0200i",
        "sw_room_a",
    ]
    assert [entity.device_key for entity in binary_sensors] == [
        "input_r2",
        "input_r2",
    ]


def test_build_catalog_ignores_internal_local_controls() -> None:
    catalog = build_catalog(load_pub_entries(FIXTURE), SAMPLE_RUNTIME_INVENTORY)
    keys = {entity.key for entity in catalog.entities}

    assert "output_r0b_sw1" not in keys
    assert "dimmer_r1_sw1" not in keys


def test_build_catalog_uses_runtime_models_for_partial_exports() -> None:
    catalog = build_catalog(load_pub_entries(FIXTURE), SAMPLE_RUNTIME_INVENTORY)

    assert set(catalog.device_types_by_key) == {
        "generic_analog_sensor_module",
        "generic_dimmer_module",
        "generic_input_module",
        "generic_relay_module",
        "generic_room_controller",
    }
    assert set(catalog.devices_by_key) == {
        "c_it_0200i",
        "dimmer_r1",
        "input_r2",
        "output_r0b",
        "sw_room_a",
    }

    output_device = catalog.devices_by_key["output_r0b"]
    assert output_device.name == "OUTPUT_R0b"
    assert output_device.type_key == "generic_relay_module"
    assert output_device.model == "SA2-012M"
    assert output_device.manufacturer == "ELKO EP / Teco"
    assert output_device.entity_keys == (
        "output_r0b_light_2",
        "output_r0b_valve_1",
    )
    assert {
        group.role: group.address_ranges
        for group in output_device.entry_groups
    }["r_bool"][0].start_address == 30010

    input_device = catalog.devices_by_key["input_r2"]
    assert input_device.name == "INPUT_R2"
    assert input_device.model == "IM2-140M"
    assert input_device.entity_keys == (
        "input_r2_door_contact_1",
        "input_r2_pir_1",
    )

    dimmer_device = catalog.devices_by_key["dimmer_r1"]
    assert dimmer_device.name == "DIMMER_R1"
    assert dimmer_device.model == "DA2-22M"
    assert dimmer_device.entity_keys == ("dimmer_r1_light_1",)

    room_device = catalog.devices_by_key["sw_room_a"]
    assert room_device.name == "SW_room_a"
    assert room_device.model == "WSB2-40"
    assert room_device.entity_keys == ("sw_room_a_term",)

    analog_device = catalog.devices_by_key["c_it_0200i"]
    assert analog_device.name == "C_IT_0200I"
    assert analog_device.model == "C-IT-0200I"
    assert analog_device.entity_keys == ("c_it_0200i_flue",)


def test_build_catalog_prefers_exact_models_and_keeps_exported_names() -> None:
    catalog = build_catalog(parse_pub_text(EXACT_MODEL_FIXTURE), EXACT_RUNTIME_INVENTORY)

    assert (
        catalog.devices_by_key["input_stairs_inputs_panel"].name
        == "INPUT_stairs_inputs_panel"
    )
    assert catalog.devices_by_key["input_stairs_inputs_panel"].model == "IM2-40B"

    room_device = catalog.devices_by_key["sw_room_b_panel"]
    assert room_device.name == "SW_room_b_panel"
    assert room_device.model == "WSB2-40"

    dimmer_device = catalog.devices_by_key["dimmer_living_area_dimmer"]
    assert dimmer_device.name == "DIMMER_living_area_dimmer"
    assert dimmer_device.model == "DA2-22M"

    output_device = catalog.devices_by_key["output_main_board_relay_panel"]
    assert output_device.name == "OUTPUT_main_board_relay_panel"
    assert output_device.model == "SA2-04M/Sn"

    assert sorted(entity.name for entity in catalog.entities_for_platform("light")) == [
        "cove_light",
        "led_scene_a",
        "led_scene_b",
        "led_scene_c",
        "led_scene_d",
        "light_balcony",
        "table_light",
    ]
    assert [entity.name for entity in catalog.entities_for_platform("switch")] == [
        "fan_bathroom",
        "pump_garden",
        "RE4_NC",
    ]
    assert set(entity.name for entity in catalog.entities_for_platform("binary_sensor")) == {
        "button_scene_a",
        "button_scene_b",
        "button_scene_c",
        "button_scene_d",
        "door_front",
        "PIR_stairs_01fl",
        "PIR_stairs_02fl",
        "window_back",
    }
    assert [entity.name for entity in catalog.entities_for_platform("sensor")] == ["room_temp"]
    assert room_device.entity_keys == (
        "sw_room_b_panel_button_scene_a",
        "sw_room_b_panel_button_scene_b",
        "sw_room_b_panel_button_scene_c",
        "sw_room_b_panel_button_scene_d",
        "sw_room_b_panel_led_scene_a",
        "sw_room_b_panel_led_scene_b",
        "sw_room_b_panel_led_scene_c",
        "sw_room_b_panel_led_scene_d",
        "sw_room_b_panel_room_temp",
    )


def test_build_catalog_recognizes_partial_relay_bank_model() -> None:
    catalog = build_catalog(
        parse_pub_text(PARTIAL_RELAY_FIXTURE), PARTIAL_RELAY_RUNTIME_INVENTORY
    )

    device = catalog.devices_by_key["output_panel_c"]
    assert device.name == "OUTPUT_panel_c"
    assert device.model == "SA2-04M/Sn"
    assert [entity.name for entity in catalog.entities_for_platform("light")] == [
        "light_4",
        "light_5",
    ]


def test_build_catalog_maps_observed_tecomat_family_to_exact_model() -> None:
    catalog = build_catalog(parse_pub_text(ANALOG_MODEL_FIXTURE), ANALOG_RUNTIME_INVENTORY)

    device = catalog.devices_by_key["c_it_0200i"]
    assert device.name == "C_IT_0200I"
    assert device.model == "C-IT-0200I"
    assert [entity.name for entity in catalog.entities_for_platform("sensor")] == [
        "FLUE",
        "STOVE",
        "TERM",
    ]


def test_build_catalog_ignores_undiscovered_pub_clusters() -> None:
    catalog = build_catalog(load_pub_entries(FIXTURE), _make_runtime_inventory([0x0C1D]))

    assert set(catalog.devices_by_key) == {"dimmer_r1"}
    assert [entity.name for entity in catalog.entities_for_platform("light")] == [
        "light_1"
    ]
    assert not catalog.entities_for_platform("switch")
    assert tuple(device.key for device in catalog.unmatched_pub_devices) == (
        "c_it_0200i",
        "input_r2",
        "output_r0b",
        "sw_room_a",
    )


def _with_program_positions(
    inventory: RuntimeInventory, positions: list[int]
) -> RuntimeInventory:
    """Give each runtime device a child record carrying its program position."""

    def record(position: int) -> str:
        return (bytes(0x3E) + position.to_bytes(2, "little") + bytes(0x66 - 0x40)).hex()

    devices = tuple(
        replace(device, child_record_hex=record(position))
        for device, position in zip(inventory.devices, positions, strict=True)
    )
    return replace(
        inventory,
        devices=devices,
        groups=tuple(replace(group, devices=devices) for group in inventory.groups),
    )


def _wsb(key: str, start: int) -> DetectedDevice:
    return _make_detected_device(
        key,
        type_key="generic_room_controller",
        model="WSB2-40",
        subsystem="room_controls",
        start_address=start,
        end_address=start + 4,
    )


def test_match_pairs_same_model_modules_in_program_order() -> None:
    # Three WSB2-40 in CIB slots 0, 1, 2; the program lists them as 3rd, 1st, 2nd.
    # The image is laid out in program order, so the map device with the lowest
    # address belongs to the module the program lists first (slot 1).
    inventory = _with_program_positions(
        _make_runtime_inventory([0x0C36, 0x0C36, 0x0C36]), [12, 10, 11]
    )
    matched, unmatched_runtime, unmatched_pub = _match_runtime_inventory(
        {"sw_hall": _wsb("sw_hall", 4), "sw_bed": _wsb("sw_bed", 9), "sw_bath": _wsb("sw_bath", 14)},
        inventory,
    )

    assert matched["sw_hall"].runtime_slot_index == 1
    assert matched["sw_bed"].runtime_slot_index == 2
    assert matched["sw_bath"].runtime_slot_index == 0
    assert unmatched_runtime == ()
    assert unmatched_pub == ()


def test_match_ignores_incompatible_models_and_reports_leftovers() -> None:
    inventory = _with_program_positions(
        _make_runtime_inventory([0x0C36, 0x0C36]), [7, 6]
    )
    relay = _make_detected_device(
        "relay_a",
        type_key="generic_relay_module",
        model="SA2-012M",
        subsystem="relay_outputs",
        start_address=0,
        end_address=1,
    )
    matched, unmatched_runtime, unmatched_pub = _match_runtime_inventory(
        {"relay_a": relay, "sw_only": _wsb("sw_only", 20)}, inventory
    )

    assert set(matched) == {"sw_only"}
    assert matched["sw_only"].runtime_slot_index == 1  # program position 6 comes first
    assert [device.slot_index for device in unmatched_runtime] == [0]
    assert tuple(device.key for device in unmatched_pub) == ("relay_a",)


def test_match_without_program_positions_falls_back_to_runtime_order() -> None:
    matched, _, _ = _match_runtime_inventory(
        {"sw_b": _wsb("sw_b", 30), "sw_a": _wsb("sw_a", 4)},
        _make_runtime_inventory([0x0C36, 0x0C36]),
    )

    assert matched["sw_a"].runtime_slot_index == 0
    assert matched["sw_b"].runtime_slot_index == 1


def test_build_catalog_supports_runtime_only_mode_without_pub_entries() -> None:
    # 0x0C36: WSB2-40 (5 channels: 1 temp + 4 buttons)
    # 0x0C1D: DA2-22M (4 channels: 2 state + 2 value)
    # 0x0C25: IM2-140M (14 channels)
    runtime_inventory = _make_runtime_inventory([0x0C36, 0x0C1D, 0x0C25])

    catalog = build_catalog((), runtime_inventory)

    assert len(catalog.devices_by_key) == 3
    assert "runtime_40015382_00" in catalog.devices_by_key # WSB2-40
    assert "runtime_40015382_01" in catalog.devices_by_key # DA2-22M
    assert "runtime_40015382_02" in catalog.devices_by_key # IM2-140M

    # WSB2-40: 1 temp sensor + 4 binary sensors (buttons) + 4 lights (LEDs)
    # DA2-22M: 2 lights
    # IM2-140M: 14 binary sensors
    # Total entities: 9 + 2 + 14 = 25
    assert len(catalog.entities) == 25

    # Check WSB2-40 entities
    wsb_entities = [e for e in catalog.entities if e.device_key == "runtime_40015382_00"]
    assert len(wsb_entities) == 9
    assert len([e for e in wsb_entities if e.platform == "sensor"]) == 1
    assert len([e for e in wsb_entities if e.platform == "binary_sensor"]) == 4
    assert len([e for e in wsb_entities if e.platform == "light"]) == 4

    # Check DA2-22M entities
    da_entities = [e for e in catalog.entities if e.device_key == "runtime_40015382_01"]
    assert len(da_entities) == 2
    assert all(e.platform == "light" for e in da_entities)
    assert all(e.supports_brightness for e in da_entities)

    # Check IM2-140M entities
    im_entities = [e for e in catalog.entities if e.device_key == "runtime_40015382_02"]
    assert len(im_entities) == 14
    assert all(e.platform == "binary_sensor" for e in im_entities)

    # Unmatched runtime devices are preserved for diagnostics
    assert len(catalog.unmatched_runtime_devices) == 3
    assert [d.model for d in catalog.unmatched_runtime_devices] == ["WSB2-40", "DA2-22M", "IM2-140M"]


def test_unknown_module_codes_get_no_device_and_are_reported() -> None:
    from custom_components.epsnet.catalog import unsupported_module_summary

    # A known WSB2-40 between unknown codes (as seen on an untested PLC model).
    runtime_inventory = _make_runtime_inventory([0x0D31, 0x0C36, 0x0D31, 0x0C38])

    catalog = build_catalog((), runtime_inventory)

    assert list(catalog.devices_by_key) == ["runtime_40015382_01"]
    assert all(e.device_key == "runtime_40015382_01" for e in catalog.entities)
    assert [d.runtime_type_code for d in catalog.runtime_inventory.devices] == [0x0C36]
    assert [d.slot_index for d in catalog.unsupported_runtime_devices] == [0, 2, 3]
    assert unsupported_module_summary(catalog) == (3, "0x0c38, 0x0d31 ×2")
    # The groups keep the full discovery for the diagnostics download.
    assert len(catalog.runtime_inventory.groups[0].devices) == 4


def test_known_modules_report_nothing_unsupported() -> None:
    from custom_components.epsnet.catalog import unsupported_module_summary

    catalog = build_catalog((), _make_runtime_inventory([0x0C36, 0x0C1D]))
    assert unsupported_module_summary(catalog) == (0, "")
