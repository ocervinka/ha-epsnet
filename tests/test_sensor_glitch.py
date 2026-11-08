"""Tier 1: DS18B20 85 °C glitch filtering for temperature sensors."""

from custom_components.epsnet.sensor import filter_ds18b20_glitch


def test_normal_values_pass_through():
    assert filter_ds18b20_glitch(24.9, None) == 24.9
    assert filter_ds18b20_glitch(-3.5, 24.9) == -3.5
    assert filter_ds18b20_glitch(None, 24.9) is None


def test_sentinel_holds_last_good_value():
    assert filter_ds18b20_glitch(85.0, 24.9) == 24.9
    assert filter_ds18b20_glitch(85, 24.9) == 24.9


def test_sentinel_with_no_history_stays_unknown():
    assert filter_ds18b20_glitch(85.0, None) is None


def test_near_sentinel_is_a_real_reading():
    assert filter_ds18b20_glitch(84.9, 24.9) == 84.9
    assert filter_ds18b20_glitch(85.1, 24.9) == 85.1
