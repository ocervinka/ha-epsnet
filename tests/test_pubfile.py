from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.epsnet.pubfile import (
    PubFileParseError,
    load_pub_entries,
    parse_pub_line,
    parse_pub_text,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sample.pub"


def test_parse_pub_line_parses_bool_with_bit() -> None:
    entry = parse_pub_line(
        "OUTPUT_R0b_light_2_ON R B 30000 .0 BOOL PUB_INOUT",
        line_number=1,
    )
    assert entry is not None
    assert entry.name == "OUTPUT_R0b_light_2_ON"
    assert entry.address == 30000
    assert entry.bit == 0
    assert entry.data_type == "BOOL"


def test_load_pub_entries_loads_fixture() -> None:
    entries = load_pub_entries(FIXTURE)
    assert len(entries) == 17
    assert entries[0].name == "INPUT_R2_PIR_1"
    assert entries[-1].name == "OUTPUT_R0b_valve_1"


def test_parse_pub_line_rejects_invalid_content() -> None:
    with pytest.raises(PubFileParseError):
        parse_pub_line("not a valid pub line", line_number=99)


def test_parse_pub_line_rejects_incomplete_bit_line() -> None:
    with pytest.raises(PubFileParseError):
        parse_pub_line(
            "SW_stairs_12fl_GREEN1_TRIG R B 19573 .2 B",
            line_number=308,
        )


def test_parse_pub_text_rejoins_wrapped_line() -> None:
    entries = parse_pub_text(
        "\n".join(
            [
                "SW_stairs_12fl_GREEN1_TRIG R B 19573 .2 B",
                "OOL PUB_INOUT",
            ]
        )
    )

    assert len(entries) == 1
    assert entries[0].name == "SW_stairs_12fl_GREEN1_TRIG"
    assert entries[0].bit == 2
    assert entries[0].data_type == "BOOL"
    assert entries[0].access == "PUB_INOUT"
