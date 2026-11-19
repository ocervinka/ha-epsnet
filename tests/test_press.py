"""Control by a written press: wall buttons, and outputs through their front buttons.

Synthetic programs only. Addresses in the test house: wall switch buttons
X4.0-3; relay module front buttons X9.0-X10.3, relays Y1.0-Y2.3; dimmer front
buttons X12.0-1, outputs Y3 and Y7.
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

    # a dimmer output keeps its level and gets an on/off switch beside it
    level = entities["io_0_2_3101_3c4d_dimmer_out2"]
    switch = entities["io_0_2_3101_3c4d_dimmer_out2_switch"]
    assert (level.platform, switch.platform) == ("sensor", "switch")
    assert switch.state_name == level.state_name
    assert _place(catalog, switch.press_name) == ("X", 12, 1)


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
    """Outputs that flip on a press of their front button, as a program does it."""

    def __init__(self, *, reacts=True):
        self.bits: dict[tuple[str, int, int | None], int] = {}
        self.reacts = reacts
        self.written: list[tuple[tuple[str, int, int | None], object]] = []

    async def get_values(self, entries, *_a, **_k):
        return {e.name: self.bits.get((e.register, e.address, e.bit), 0) for e in entries}

    async def set_value(self, entry, value):
        place = (entry.register, entry.address, entry.bit)
        self.written.append((place, value))
        if self.reacts and entry.register == "X" and entry.address in (9, 10):
            # relay module: front button n (X9.0..) flips relay n (Y1.0..)
            output = ("Y", entry.address - 8, entry.bit)
            self.bits[output] = 0 if self.bits.get(output) else 1


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
    ):
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


async def test_switch_reports_a_program_that_ignores_the_front_button(
    hass: HomeAssistant, _fast_confirm
) -> None:
    plc = _Plc(reacts=False)
    with _patch_plc_with_a_program_layout(), _patch_plc(plc):
        entry = await _set_up_without_a_map(hass, plc)
        relay = "switch.sa2_012m_2b3c_re5"
        with pytest.raises(HomeAssistantError) as raised:
            await hass.services.async_call("switch", "turn_on", {"entity_id": relay}, blocking=True)
        assert raised.value.translation_key == "output_did_not_change"
        assert raised.value.translation_placeholders == {"name": "SA2-012M 2B3C RE5"}
        assert plc.written == [(("X", 9, 4), True)]
        assert hass.states.get(relay).state == "off"
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
    for kept in (front_button, level, not_in_catalog):
        assert registry.async_get(kept) is not None
