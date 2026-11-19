"""Map variables find their module by address (synthetic programs only).

The program's I/O layout says whose every byte of the X/Y images is, so a map
variable lying there belongs to that module whatever it is called. Addresses in
the test house: wall switch buttons X4.0-3, temperature X5, LEDs Y0.0-3; relay
module local switches X9.0-X10.3, relays Y1.0-Y2.3; dimmer inputs X11.0-1, local
switches X12.0-1, outputs Y3 and Y7.
"""

from __future__ import annotations

from dataclasses import replace

from custom_components.epsnet.catalog import PROGRAM_VARIABLES_DEVICE_KEY, build_catalog
from custom_components.epsnet.pubfile import parse_pub_text
from tests.test_program_entities import DIMMER, HOUSE, RELAY, WSB, _inventory, _layout
from tests.test_program_layout import DEVICE, MASTER, _master

TWO_WALL_SWITCHES = _master(
    lines=(
        (MASTER, 3128, 0, 2, 0),
        (DEVICE, 3126, 0x1111, 3, 1),  # buttons X4, temperature X5, LEDs Y0
        (DEVICE, 3126, 0x2222, 3, 1),  # buttons X9, temperature X10, LEDs Y1
    ),
    conv_in=bytes([0x94, 0x01, 0x93, 0x01, 0x93]),
    conv_out=bytes([0x01, 0x01]),
)


def _house():
    return _inventory(_layout(HOUSE), [WSB, RELAY, DIMMER])


def _catalog(pub: str, inventory=None, **kwargs):
    return build_catalog(parse_pub_text(pub), inventory or _house(), **kwargs)


def _entities(catalog):
    """The modules' entities (the program's other variables are tested apart)."""
    return {entity.key: entity for entity in catalog.entities if not entity.variable}


def _module_devices(catalog):
    return {key: device for key, device in catalog.devices_by_key.items()
            if key != PROGRAM_VARIABLES_DEVICE_KEY}


FREE_NAMES = """
KitchenDoorButton X B 4 .0 BOOL PUB_OUT
HallTemperature X F 5 REAL PUB_OUT
BalconyLamp Y B 1 .0 BOOL PUB_OUT
BalconyLampRequest R B 100 .0 BOOL PUB_INOUT
HeatingSetpoint R F 200 REAL PUB_INOUT
"""


def test_names_without_any_structure_name_the_inputs_and_outputs():
    without_map = build_catalog((), _house())
    catalog = _catalog(FREE_NAMES)
    entities = _entities(catalog)

    # the same entities as without a map (keyed by hardware), now with names
    assert entities.keys() == _entities(without_map).keys()
    assert entities["io_0_2_3126_1a2b_button_up1"].name == "KitchenDoorButton"
    assert entities["io_0_2_3126_1a2b_temperature"].name == "HallTemperature"
    assert entities["io_0_2_3151_2b3c_relay_re1"].name == "BalconyLamp"
    assert entities["io_0_2_3126_1a2b_button_down1"].name == "DOWN1"  # not in the map
    assert catalog.channels_named_by_map == 3
    # nothing is hidden because of the map; nothing is switched by command
    # variables, only by a press of a button
    assert all(e.enabled_by_default for e in entities.values() if not e.diagnostic)
    assert {e.platform for e in entities.values()} == {
        "binary_sensor", "sensor", "button", "switch"}
    assert all(e.press_name for e in entities.values() if e.platform in ("button", "switch"))
    # the modules stay known by their hardware
    assert {d.name for d in _module_devices(catalog).values()} == {
        "WSB2-40 1A2B", "SA2-012M 2B3C", "DA2-22M 3C4D"}
    assert set(_module_devices(catalog)) == set(without_map.devices_by_key)


def test_inputs_named_like_motion_or_door_sensors_are_shown_as_such():
    catalog = _catalog(
        """
PirStairs X B 4 .0 BOOL PUB_OUT
MagnetFrontDoor X B 4 .1 BOOL PUB_OUT
Doorbell X B 4 .2 BOOL PUB_OUT
"""
    )
    entities = _entities(catalog)
    assert entities["io_0_2_3126_1a2b_button_up1"].device_class == "motion"
    assert entities["io_0_2_3126_1a2b_button_down1"].device_class == "opening"
    assert entities["io_0_2_3126_1a2b_button_up2"].device_class is None


def test_a_map_naming_nothing_on_any_module_names_nothing():
    catalog = _catalog("HeatingSetpoint R F 200 REAL PUB_INOUT\nSpare X B 900 .0 BOOL PUB_OUT\n")
    assert catalog.channels_named_by_map == 0
    assert all(e.from_program for e in _entities(catalog).values())


def test_a_module_is_found_by_its_address_not_by_its_place_in_the_order():
    """Only the second of two alike wall switches is in the map: by order the
    first module would have taken its names."""
    inventory = _inventory(_layout(TWO_WALL_SWITCHES), [WSB, WSB])
    catalog = _catalog(
        """
SW_attic_UP1 X B 9 .0 BOOL PUB_OUT
SW_attic_DOWN1 X B 9 .1 BOOL PUB_OUT
SW_attic_TERM X F 10 REAL PUB_OUT
""",
        inventory,
    )
    attic = catalog.devices_by_key["sw_attic"]
    assert (attic.runtime_slot_index, attic.paired_by) == (1, "address")
    entities = _entities(catalog)
    assert entities["sw_attic_term"].device_key == "sw_attic"
    # the first module keeps its own, hardware-named entities
    assert entities["io_0_2_3126_1111_temperature"].device_key == "runtime_40015382_00"
    assert not catalog.unmatched_pub_devices
    assert [d.slot_index for d in catalog.unmatched_runtime_devices] == [0]


def test_modules_sharing_a_name_prefix_stay_separate_devices():
    """A prefix that is a room, not a module: each module is its own device."""
    inventory = _inventory(_layout(TWO_WALL_SWITCHES), [WSB, WSB])
    catalog = _catalog(
        """
Kitchen_Up X B 4 .0 BOOL PUB_OUT
Kitchen_Down X B 4 .1 BOOL PUB_OUT
Kitchen_Left X B 9 .0 BOOL PUB_OUT
Kitchen_Right X B 9 .1 BOOL PUB_OUT
""",
        inventory,
    )
    entities = _entities(catalog)
    assert entities["kitchen_up"].device_key == "kitchen"
    assert entities["kitchen_left"].device_key == "kitchen_2"
    devices = catalog.devices_by_key
    assert (devices["kitchen"].name, devices["kitchen"].runtime_slot_index) == ("Kitchen", 0)
    assert (devices["kitchen_2"].name, devices["kitchen_2"].runtime_slot_index) == ("Kitchen 2", 1)


def test_one_variable_named_differently_does_not_unname_the_module():
    catalog = _catalog(
        """
SW_hall_UP1 X B 4 .0 BOOL PUB_OUT
SW_hall_DOWN1 X B 4 .1 BOOL PUB_OUT
SW_hall_TERM X F 5 REAL PUB_OUT
Doorbell X B 4 .3 BOOL PUB_OUT
"""
    )
    entities = _entities(catalog)
    assert catalog.devices_by_key["sw_hall"].model == "WSB2-40"
    assert (entities["sw_hall_up1"].name, entities["sw_hall_up1"].device_key) == ("UP1", "sw_hall")
    # it is the module's all the same, under its full name
    assert (entities["doorbell"].name, entities["doorbell"].device_key) == ("Doorbell", "sw_hall")


def test_variables_of_one_module_the_names_split_are_one_device():
    """Names that group as two modules (four lights look like a four-relay
    module of their own) are one device: they lie in one module's outputs."""
    lines = []
    for bit, name in enumerate(("light_a", "light_b", "light_c", "light_d", "pump", "zone")):
        lines += [
            f"state_OUTPUT_R1_{name} Y B 1 .{bit} BOOL PUB_OUT",
            f"OUTPUT_R1_{name} R B {300 + bit * 10} .0 BOOL PUB_INOUT",
            f"OUTPUT_R1_{name}_ON R B {301 + bit * 10} .0 BOOL PUB_INOUT",
            f"OUTPUT_R1_{name}_OFF R B {301 + bit * 10} .1 BOOL PUB_INOUT",
        ]
    catalog = _catalog("\n".join(lines))
    switched = {e.key: e for e in catalog.entities
                if e.platform in ("light", "switch") and e.press_name is None}
    assert set(switched) == {f"output_r1_{name}" for name in (
        "light_a", "light_b", "light_c", "light_d", "pump", "zone")}
    assert {e.device_key for e in switched.values()} == {"output_r1"}
    assert catalog.devices_by_key["output_r1"].model == "SA2-012M"
    assert not catalog.unmatched_pub_devices


def test_command_variables_work_with_any_names():
    """An output's state and command variables make it switchable, also when
    the names have no module prefix; the other outputs are only shown."""
    catalog = _catalog(
        """
state_Lamp Y B 1 .0 BOOL PUB_OUT
Lamp R B 300 .0 BOOL PUB_INOUT
Lamp_ON R B 301 .0 BOOL PUB_INOUT
Lamp_OFF R B 301 .1 BOOL PUB_INOUT
Pump Y B 1 .1 BOOL PUB_OUT
"""
    )
    entities = _entities(catalog)
    lamp = entities["lamp"]
    assert (lamp.platform, lamp.name, lamp.device_key) == ("switch", "Lamp", "runtime_40015382_01")
    assert (lamp.on_command_name, lamp.off_command_name) == ("Lamp_ON", "Lamp_OFF")
    assert catalog.devices_by_key["runtime_40015382_01"].name == "SA2-012M 2B3C"
    # the switch carries the name; its relay keeps the hardware label
    assert entities["io_0_2_3151_2b3c_relay_re1"].name == "RE1"
    assert entities["io_0_2_3151_2b3c_relay_re2"].name == "Pump"
    # a module known by its hardware hides none of its channels
    assert entities["io_0_2_3151_2b3c_relay_re3"].enabled_by_default


def test_a_variable_of_another_kind_at_the_address_gives_no_name():
    catalog = _catalog(
        """
ButtonByte X B 4 USINT PUB_OUT
TempFirstByte X B 5 USINT PUB_OUT
RelayWord Y B 1 .0 REAL PUB_OUT
"""
    )
    entities = _entities(catalog)
    assert entities["io_0_2_3126_1a2b_button_up1"].name == "UP1"
    assert entities["io_0_2_3126_1a2b_temperature"].name == "Temperature"
    assert entities["io_0_2_3151_2b3c_relay_re1"].name == "RE1"
    assert catalog.channels_named_by_map == 0


def test_a_modules_other_channels_take_the_maps_names_and_stay_disabled():
    """With a map that names the module as a device, the channels its entities
    do not show are extra: named after their variables, off until enabled."""
    catalog = _catalog(
        """
state_OUTPUT_R1_light_hall Y B 1 .0 BOOL PUB_OUT
OUTPUT_R1_light_hall R B 300 .0 BOOL PUB_INOUT
OUTPUT_R1_light_hall_ON R B 301 .0 BOOL PUB_INOUT
OUTPUT_R1_light_hall_OFF R B 301 .1 BOOL PUB_INOUT
OUTPUT_R1_wall_button X B 9 .0 BOOL PUB_OUT
"""
    )
    entities = _entities(catalog)
    assert entities["output_r1_light_hall"].platform == "light"
    button = entities["io_0_2_3151_2b3c_local_sw1"]
    assert (button.name, button.enabled_by_default, button.device_key) == (
        "wall_button", False, "output_r1")
    assert entities["io_0_2_3151_2b3c_relay_re1"].name == "RE1"
    assert catalog.channels_named_by_map == 1


def test_variable_groups_in_memory_only_pair_with_modules_the_map_does_not_name():
    """A group with no input or output of its own is still paired by its kind
    and order, but never with a module whose variables are known."""
    virtual = """
VIRT_scene R B 400 .0 BOOL PUB_INOUT
VIRT_scene_ON R B 401 .0 BOOL PUB_INOUT
VIRT_scene_OFF R B 401 .1 BOOL PUB_INOUT
"""
    catalog = _catalog(virtual + "KitchenDoorButton X B 4 .0 BOOL PUB_OUT\n")
    scene = _entities(catalog)["virt_scene"]
    assert (scene.platform, scene.device_key) == ("switch", "virt")
    virt = catalog.devices_by_key["virt"]
    assert (virt.model, virt.paired_by) == ("SA2-012M", "order")

    # the relay module has names of its own: the group finds no module
    catalog = _catalog(virtual + "Pump Y B 1 .1 BOOL PUB_OUT\n")
    assert "virt_scene" not in _entities(catalog)
    assert [d.key for d in catalog.unmatched_pub_devices] == ["virt"]


def test_a_variable_no_group_can_hold_does_not_lose_the_ones_after_it():
    """Grouping by name used to stop at the first variable that fits no group
    (here ``Aaa``, first in the order)."""
    inventory = replace(_house(), program_layout=None)  # names and order only
    catalog = _catalog(
        """
Aaa R F 200 REAL PUB_INOUT
SW_hall_UP1 X B 4 .0 BOOL PUB_OUT
SW_hall_DOWN1 X B 4 .1 BOOL PUB_OUT
SW_hall_TERM X F 5 REAL PUB_OUT
""",
        inventory,
    )
    hall = catalog.devices_by_key["sw_hall"]
    assert (hall.model, hall.paired_by) == ("WSB2-40", "order")
    assert {"sw_hall_up1", "sw_hall_down1", "sw_hall_term"} <= set(_entities(catalog))


def test_free_names_added_to_an_entry_set_up_without_a_map():
    without_map = build_catalog((), _house())
    catalog = _catalog(FREE_NAMES, program_first=True)
    entities = _entities(catalog)
    assert entities.keys() == _entities(without_map).keys()
    assert entities["io_0_2_3151_2b3c_relay_re1"].name == "BalconyLamp"
    assert catalog.channels_named_by_map == 3
