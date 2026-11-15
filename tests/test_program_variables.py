"""The program's variables that no module entity uses (synthetic programs only).

A program may publish more than the modules' inputs and outputs and the command
variables of lights and switches. Each such variable is offered as an entity of
its own value on the PLC's device, created disabled. Addresses of the test
house: see test_address_pairing.
"""

from __future__ import annotations

from custom_components.epsnet.catalog import PROGRAM_VARIABLES_DEVICE_KEY, build_catalog
from custom_components.epsnet.pubfile import parse_pub_text
from tests.test_program_entities import DIMMER, HOUSE, RELAY, WSB, _inventory, _layout

HALL_LAMP = """
state_OUTPUT_hall_lamp Y B 1 .0 BOOL PUB_OUT
OUTPUT_hall_lamp R B 100 .0 BOOL PUB_INOUT
OUTPUT_hall_lamp_ON R B 100 .1 BOOL PUB_INOUT
OUTPUT_hall_lamp_OFF R B 100 .2 BOOL PUB_INOUT
OUTPUT_hall_lamp_TRIG R B 100 .3 BOOL PUB_INOUT
"""


def _catalog(pub: str, **kwargs):
    inventory = _inventory(_layout(HOUSE), [WSB, RELAY, DIMMER])
    return build_catalog(parse_pub_text(pub), inventory, **kwargs)


def _variables(catalog):
    return {entity.name: entity for entity in catalog.entities if entity.variable}


def test_variables_no_entity_uses_wait_as_disabled_entities_on_the_plc():
    catalog = _catalog(
        HALL_LAMP
        + """
PartyMode R B 300 .2 BOOL PUB_INOUT
HeatingSetpoint R F 200 REAL PUB_INOUT
PumpRunHours R W 310 UINT PUB_INOUT
GarageGateRequest R B 320 .0 BOOL PUB_IN
"""
    )
    variables = _variables(catalog)

    assert {name: spec.platform for name, spec in variables.items()} == {
        "PartyMode": "switch",
        "HeatingSetpoint": "number",
        "PumpRunHours": "number",
        # declared as an input of the program: written, and read all the same
        "GarageGateRequest": "switch",
    }
    for name, spec in variables.items():
        assert spec.key == f"var:{name}"
        assert (spec.state_name, spec.set_value_name) == (name, name)
        assert spec.enabled_by_default is False
        assert spec.device_key == PROGRAM_VARIABLES_DEVICE_KEY
    # the device is the PLC itself, called as it identifies itself
    plc = catalog.devices_by_key[PROGRAM_VARIABLES_DEVICE_KEY]
    assert plc.name == catalog.runtime_inventory.controller_identity.split(" ")[0]
    assert set(plc.entity_keys) == {spec.key for spec in variables.values()}
    assert catalog.variable_entity_count == 4
    # the lamp is switched as before
    lamp = next(e for e in catalog.entities if e.platform == "switch" and not e.variable)
    assert (lamp.state_name, lamp.on_command_name) == ("OUTPUT_hall_lamp", "OUTPUT_hall_lamp_ON")


def test_what_an_entity_already_stands_for_is_not_offered_again():
    catalog = _catalog(
        HALL_LAMP
        + """
KitchenDoorButton X B 4 .0 BOOL PUB_OUT
BalconyLamp Y B 1 .1 BOOL PUB_OUT
"""
    )
    # neither the lamp's state and commands (also the one it does not use),
    # nor a module's input or output
    assert _variables(catalog) == {}
    assert PROGRAM_VARIABLES_DEVICE_KEY not in catalog.devices_by_key


def test_a_variable_outside_the_programs_memory_is_only_shown():
    catalog = _catalog(
        """
BoilerRuns R B 320 .0 BOOL PUB_OUT
OutdoorTemperature R F 330 REAL PUB_OUT
SystemTick S B 20 .0 BOOL PUB_INOUT
SpareInput X B 900 .0 BOOL PUB_INOUT
SpareAnalog X F 904 REAL PUB_INOUT
"""
    )
    variables = _variables(catalog)
    assert {name: (spec.platform, spec.set_value_name) for name, spec in variables.items()} == {
        # the program publishes them for reading only
        "BoilerRuns": ("binary_sensor", None),
        "OutdoorTemperature": ("sensor", None),
        # system registers and inputs that are no module's are not the program's to set
        "SystemTick": ("binary_sensor", None),
        "SpareInput": ("binary_sensor", None),
        "SpareAnalog": ("sensor", None),
    }
    assert all(not spec.enabled_by_default for spec in variables.values())


def test_types_that_are_not_one_plain_value_and_far_addresses_are_left_out():
    catalog = _catalog(
        """
StairsDelay R L 400 TIME PUB_INOUT
LastAlarmText R B 500 STRING PUB_OUT
FarFlag R B 70000 .0 BOOL PUB_INOUT
LastWord R W 65535 UINT PUB_INOUT
LastByte R B 65535 USINT PUB_INOUT
"""
    )
    assert set(_variables(catalog)) == {"LastByte"}


def test_variables_keep_their_entities_when_the_modules_are_the_programs():
    """An entry set up without a map and given one later: the same variables."""
    pub = HALL_LAMP + "HeatingSetpoint R F 200 REAL PUB_INOUT\n"
    assert _variables(_catalog(pub, program_first=True)).keys() == _variables(_catalog(pub)).keys()
