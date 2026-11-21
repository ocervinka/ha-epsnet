"""Control without command variables: wall buttons are pressed, outputs are
switched through their front buttons or by a value written into them.

Synthetic programs only. Addresses in the test house: wall switch buttons
X4.0-3, LEDs Y0.0-3; relay module front buttons X9.0-X10.3, relays Y1.0-Y2.3;
dimmer front buttons X12.0-1, outputs Y3 and Y7.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.epsnet.catalog import build_catalog
from custom_components.epsnet.const import DOMAIN
from custom_components.epsnet.pubfile import parse_pub_text
from tests.test_integration import (
    _CLIENT,
    _finish_progress,
    _patch_plc_with_a_program_layout,
    _pick_found_plc,
)
from tests.test_program_entities import DIMMER, HOUSE, RELAY, WSB, _inventory, _layout


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _no_plc_on_the_network():
    with patch("custom_components.epsnet.config_flow.async_search_plcs", AsyncMock(return_value=[])):
        yield


@pytest.fixture(autouse=True)
def _no_program_checksum_read():
    with patch(
        "custom_components.epsnet.program_watch.read_program_checksum",
        AsyncMock(return_value=None),
    ):
        yield


def _catalog(pub: str = ""):
    return build_catalog(
        parse_pub_text(pub) if pub else (), _inventory(_layout(HOUSE), [WSB, RELAY, DIMMER])
    )


def _place(catalog, name):
    entry = catalog.entries_by_name[name]
    return entry.register, entry.address, entry.bit


def test_every_wall_button_can_be_pressed():
    catalog = _catalog()
    presses = {e.key: e for e in catalog.entities if e.platform == "button"}
    assert set(presses) == {
        f"io_0_2_3126_1a2b_button_{name}_press" for name in ("up1", "down1", "up2", "down2")
    }
    press = presses["io_0_2_3126_1a2b_button_down1_press"]
    # the button's own input is what gets written
    assert _place(catalog, press.press_name) == ("X", 4, 1)
    assert press.name == "DOWN1" and press.enabled_by_default
    sensor = next(e for e in catalog.entities if e.key == "io_0_2_3126_1a2b_button_down1")
    assert sensor.platform == "binary_sensor" and sensor.device_key == press.device_key
    # inputs that are no buttons (a relay module's front buttons, a dimmer's
    # inputs) get no press entity of their own
    assert all("_button_" in key for key in presses)


def test_without_command_variables_an_output_is_switched_by_its_front_button():
    catalog = _catalog()
    entities = {e.key: e for e in catalog.entities}

    relay5 = entities["io_0_2_3151_2b3c_relay_re5"]
    assert (relay5.platform, relay5.enabled_by_default) == ("switch", True)
    assert _place(catalog, relay5.state_name) == ("Y", 1, 4)
    assert _place(catalog, relay5.press_name) == ("X", 9, 4)  # front button 5
    relay12 = entities["io_0_2_3151_2b3c_relay_re12"]
    assert _place(catalog, relay12.press_name) == ("X", 10, 3)
    # the front button itself is still shown
    assert entities["io_0_2_3151_2b3c_local_sw5"].platform == "binary_sensor"

    assert relay5.direct_output

    # a dimmer output is a light: on and off by its front button, dimmed by a
    # level written into the output
    dimmer = entities["io_0_2_3101_3c4d_dimmer_out2"]
    assert (dimmer.platform, dimmer.supports_brightness, dimmer.brightness_scale) == (
        "light", True, 100.0)
    assert _place(catalog, dimmer.state_name) == ("Y", 7, None)
    assert _place(catalog, dimmer.press_name) == ("X", 12, 1)
    assert dimmer.direct_output and "io_0_2_3101_3c4d_dimmer_out2_switch" not in entities

    # an indicator LED has no button of its own: a switch that writes the output
    led = entities["io_0_2_3126_1a2b_led_red1"]
    assert (led.platform, led.press_name, led.direct_output, led.enabled_by_default) == (
        "switch", None, True, True)
    assert _place(catalog, led.state_name)[0] == "Y"


def test_an_output_with_command_variables_is_not_switched_by_a_press():
    catalog = _catalog(
        """
state_OUTPUT_R1_lamp Y B 1 .0 BOOL PUB_OUT
OUTPUT_R1_lamp R B 300 .0 BOOL PUB_INOUT
OUTPUT_R1_lamp_ON R B 301 .0 BOOL PUB_INOUT
OUTPUT_R1_lamp_OFF R B 301 .1 BOOL PUB_INOUT
state_OUTPUT_R1_heater Y B 1 .1 BOOL PUB_OUT
"""
    )
    entities = {e.key: e for e in catalog.entities}
    lamp = entities["output_r1_lamp"]
    assert (lamp.platform, lamp.on_command_name, lamp.press_name) == (
        "switch", "OUTPUT_R1_lamp_ON", None)
    # its output is not offered a second time as a press switch
    assert entities["io_0_2_3151_2b3c_relay_re1"].platform == "binary_sensor"

    # the map shows only the state of the other output: its front button switches it
    heater = entities["output_r1_heater"]
    assert (heater.platform, heater.name, heater.enabled_by_default) == ("switch", "heater", True)
    assert heater.state_name == "state_OUTPUT_R1_heater"
    assert _place(catalog, heater.press_name) == ("X", 9, 1)


class _Plc:
    """Inputs and outputs as a PLC keeps them.

    ``front_buttons``: the program flips relay n on a press of front button n
    (a dimmer output goes to 100 or 0). ``assigns_outputs``: the program
    assigns its outputs every cycle, so a value written into one does not stay.
    """

    def __init__(self, *, front_buttons=True, assigns_outputs=False):
        self.values: dict[tuple[str, int, int | None], object] = {}
        self.front_buttons = front_buttons
        self.assigns_outputs = assigns_outputs
        self.written: list[tuple[tuple[str, int, int | None], object]] = []

    async def get_values(self, entries, *_a, **_k):
        return {e.name: self.values.get((e.register, e.address, e.bit), 0) for e in entries}

    async def set_value(self, entry, value):
        place = (entry.register, entry.address, entry.bit)
        self.written.append((place, value))
        if entry.register == "Y":
            if not self.assigns_outputs:
                self.values[place] = value
        elif self.front_buttons and entry.address in (9, 10):
            # relay module: front button n (X9.0..) flips relay n (Y1.0..)
            output = ("Y", entry.address - 8, entry.bit)
            self.values[output] = 0 if self.values.get(output) else 1
        elif self.front_buttons and entry.address == 12:
            # dimmer: front button 1 / 2 (X12.0 / .1) takes output Y3 / Y7 to 100 or 0
            output = ("Y", 3 + 4 * entry.bit, None)
            self.values[output] = 0.0 if self.values.get(output) else 100.0


async def _set_up_without_a_map(hass: HomeAssistant, plc: _Plc):
    result = await _finish_progress(hass, await _pick_found_plc(hass))
    assert result["step_id"] == "no_map"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "inventory_only"}
    )
    await hass.async_block_till_done()
    return result["result"]


def _patch_plc(plc: _Plc):
    return patch.multiple(
        _CLIENT,
        get_values=AsyncMock(side_effect=plc.get_values),
        set_value=AsyncMock(side_effect=plc.set_value),
    )


@pytest.fixture
def _fast_confirm():
    with patch("custom_components.epsnet.coordinator.PRESS_CONFIRM_SECONDS", 0.05), patch(
        "custom_components.epsnet.coordinator.PRESS_POLL_SECONDS", 0.001
    ), patch("custom_components.epsnet.coordinator.OUTPUT_CONFIRM_SECONDS", 0.001):
        yield


async def test_switch_presses_only_when_needed_and_reads_the_output_back(
    hass: HomeAssistant, _fast_confirm
) -> None:
    plc = _Plc()
    with _patch_plc_with_a_program_layout(), _patch_plc(plc):
        entry = await _set_up_without_a_map(hass, plc)
        relay = "switch.sa2_012m_2b3c_re5"
        assert hass.states.get(relay).state == "off"

        await hass.services.async_call("switch", "turn_on", {"entity_id": relay}, blocking=True)
        assert plc.written == [(("X", 9, 4), True)]
        assert hass.states.get(relay).state == "on"

        # already on: no press (a press would switch it off)
        await hass.services.async_call("switch", "turn_on", {"entity_id": relay}, blocking=True)
        assert len(plc.written) == 1

        await hass.services.async_call("switch", "turn_off", {"entity_id": relay}, blocking=True)
        assert plc.written[1:] == [(("X", 9, 4), True)]
        assert hass.states.get(relay).state == "off"

        # a wall button is simply pressed
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.wsb2_40_1a2b_up2"}, blocking=True
        )
        assert plc.written[2:] == [(("X", 4, 2), True)]
        await hass.config_entries.async_unload(entry.entry_id)


async def test_switch_writes_the_output_when_the_front_button_changes_nothing(
    hass: HomeAssistant, _fast_confirm
) -> None:
    plc = _Plc(front_buttons=False)
    with _patch_plc_with_a_program_layout(), _patch_plc(plc):
        entry = await _set_up_without_a_map(hass, plc)
        relay = "switch.sa2_012m_2b3c_re5"
        await hass.services.async_call("switch", "turn_on", {"entity_id": relay}, blocking=True)
        # the press first, then the output itself
        assert plc.written == [(("X", 9, 4), True), (("Y", 1, 4), True)]
        assert hass.states.get(relay).state == "on"
        await hass.services.async_call("switch", "turn_off", {"entity_id": relay}, blocking=True)
        assert plc.written[2:] == [(("X", 9, 4), True), (("Y", 1, 4), False)]
        assert hass.states.get(relay).state == "off"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_switch_reports_a_program_that_controls_the_output_itself(
    hass: HomeAssistant, _fast_confirm, caplog
) -> None:
    plc = _Plc(front_buttons=False, assigns_outputs=True)
    with _patch_plc_with_a_program_layout(), _patch_plc(plc):
        entry = await _set_up_without_a_map(hass, plc)
        relay = "switch.sa2_012m_2b3c_re5"
        with pytest.raises(HomeAssistantError) as raised:
            await hass.services.async_call("switch", "turn_on", {"entity_id": relay}, blocking=True)
        assert raised.value.translation_key == "output_did_not_change"
        assert raised.value.translation_placeholders == {"name": "SA2-012M 2B3C RE5"}
        assert plc.written == [(("X", 9, 4), True), (("Y", 1, 4), True)]
        assert hass.states.get(relay).state == "off"
        # the failure is also in the log, where one made from the UI can be found later
        assert (
            "SA2-012M 2B3C RE5 is not as wanted after a press of its front button and a "
            "value written into it: the PLC program controls this output itself"
        ) in caplog.text
        await hass.config_entries.async_unload(entry.entry_id)


async def test_an_led_is_a_switch_that_writes_its_output(
    hass: HomeAssistant, _fast_confirm
) -> None:
    plc = _Plc()
    with _patch_plc_with_a_program_layout(), _patch_plc(plc):
        entry = await _set_up_without_a_map(hass, plc)
        led = "switch.wsb2_40_1a2b_red1"
        assert hass.states.get(led).state == "off"
        await hass.services.async_call("switch", "turn_on", {"entity_id": led}, blocking=True)
        (place, value), = plc.written
        assert place[0] == "Y" and value is True
        assert hass.states.get(led).state == "on"

        # a program that drives its LEDs takes the value back: the switch says so
        plc.assigns_outputs = True
        with pytest.raises(HomeAssistantError) as raised:
            await hass.services.async_call("switch", "turn_off", {"entity_id": led}, blocking=True)
        assert raised.value.translation_key == "output_write_did_not_stay"
        assert raised.value.translation_placeholders == {"name": "WSB2-40 1A2B RED1"}
        assert hass.states.get(led).state == "on"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_dimmer_output_is_a_light_pressed_on_and_dimmed_by_a_written_level(
    hass: HomeAssistant, _fast_confirm
) -> None:
    plc = _Plc()
    with _patch_plc_with_a_program_layout(), _patch_plc(plc):
        entry = await _set_up_without_a_map(hass, plc)
        light = "light.da2_22m_3c4d_out2"
        assert hass.states.get(light).state == "off"

        await hass.services.async_call("light", "turn_on", {"entity_id": light}, blocking=True)
        assert plc.written == [(("X", 12, 1), True)]
        assert hass.states.get(light).attributes["brightness"] == 255

        await hass.services.async_call(
            "light", "turn_on", {"entity_id": light, "brightness": 102}, blocking=True
        )
        assert plc.written[1:] == [(("Y", 7, None), 40.0)]
        assert hass.states.get(light).attributes["brightness"] == 102

        await hass.services.async_call("light", "turn_off", {"entity_id": light}, blocking=True)
        assert plc.written[2:] == [(("X", 12, 1), True)]
        assert hass.states.get(light).state == "off"

        # no rule for the front button in the program: on is written as 100 %
        plc.front_buttons = False
        await hass.services.async_call("light", "turn_on", {"entity_id": light}, blocking=True)
        assert plc.written[3:] == [(("X", 12, 1), True), (("Y", 7, None), 100.0)]
        assert hass.states.get(light).state == "on"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_the_sensor_an_output_had_before_gives_way_to_its_switch(hass: HomeAssistant) -> None:
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.epsnet import _async_remove_replaced_entities

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    catalog = _catalog()

    def add(domain, key):
        return registry.async_get_or_create(
            domain, DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry).entity_id

    old_sensor = add("binary_sensor", "io_0_2_3151_2b3c_relay_re5")
    front_button = add("binary_sensor", "io_0_2_3151_2b3c_local_sw5")
    level = add("sensor", "io_0_2_3101_3c4d_dimmer_out1")
    not_in_catalog = add("sensor", "average_read_duration")

    _async_remove_replaced_entities(hass, entry, catalog)
    assert registry.async_get(old_sensor) is None
    # the dimmer's level sensor gives way to its light
    assert registry.async_get(level) is None
    for kept in (front_button, not_in_catalog):
        assert registry.async_get(kept) is not None
