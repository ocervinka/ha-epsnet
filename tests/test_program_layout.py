"""Decoding the I/O layout of a PLC program (synthetic programs only)."""

from __future__ import annotations

import struct

import pytest

from custom_components.epsnet.epsnet_udp import EpsnetProtocolError, EpsnetUdpClient
from custom_components.epsnet.program_layout import (
    BytesProgramSource,
    cross_check,
    decode_program_layout,
    layout_snapshot,
    table_checksum,
)

MASTER, DEVICE = 0xC0, 0x80
WALL_SWITCH, RELAY, DIMMER, SENSOR_IF = 3126, 3151, 3101, 3309


def _master(rack=0, slot=2, iin=0, iout=0x01000000, lines=(), conv_in=b"", conv_out=b""):
    return dict(rack=rack, slot=slot, iin=iin, iout=iout, lines=lines,
                conv_in=conv_in, conv_out=conv_out)


def build_program(masters, *, records_at=0x206, spare_records=0, others=(), corrupt_table=None):
    """A program image + table area in the documented formats."""
    tables: list[bytes] = [b"\x01\x02"]

    def add(data: bytes) -> int:
        tables.append(data)
        return len(tables) - 1

    records = b""
    for index, m in enumerate(masters):
        line_bytes = b""
        for kind, hw, serial, raw_in, raw_out in m["lines"]:
            init = add(struct.pack("<HHHBB", hw, 0, serial, 1, 0)) if hw else 0
            line_bytes += struct.pack("<BBHHH", kind, 0, init, raw_in, raw_out)
        conv_in, conv_out = add(m["conv_in"]), add(m["conv_out"])
        head = struct.pack(
            "<HHBBBBBHBHHIIIIIIHHBB", 0xC9, 0, 9, 63, 12, 0, 0, 0, 0, 0, 0, 0, 0,
            m["iin"], m["iout"], 0, 0, conv_in, conv_out, 0, len(m["lines"]),
        )
        table = add(head + line_bytes)
        records += struct.pack("<BBBBHHHIIH", 0x13, 1, m["rack"], m["slot"], 0x0110,
                               10, 2, 7600 + 16 * index, 0x2000 + 7600 + 16 * index, table)
    for logical in others:
        records += struct.pack("<BBBBHHHIIH", 0x13, 1, 0, 0, logical, 10, 2, 7500, 0x2000 + 7500, 0)

    area = bytearray(b"\x00\x00")
    offsets = []
    for number, data in enumerate(tables):
        if len(area) % 2:
            area += b"\x00"
        offsets.append(len(area))
        check = table_checksum(len(data), data)
        if number == corrupt_table:
            check ^= 1
        area += struct.pack(">H", len(data)) + data + struct.pack(">H", check)

    spare = b"\x13" + bytes(19)
    word_table_at = records_at + len(records) + spare_records * 20 + 2
    word_table = b"".join(struct.pack(">H", o) for o in offsets) + b"\x00\x00"
    image = bytearray(word_table_at + len(word_table) + 64)
    struct.pack_into(">H", image, 0x00, 0x002A)
    struct.pack_into(">H", image, 0x02, 0x01D4)
    struct.pack_into(">H", image, 0x12, records_at + len(records) + spare_records * 20)
    struct.pack_into(">HH", image, 0x1A, word_table_at, len(word_table))
    struct.pack_into(">HH", image, 0x22, records_at, len(records))
    struct.pack_into(">H", image, 0x26, len(area))
    image[records_at:records_at + len(records)] = records
    for i in range(spare_records):
        at = records_at + len(records) + 20 * i
        image[at:at + 20] = spare
    image[word_table_at:word_table_at + len(word_table)] = word_table
    return bytes(image), bytes(area)


def _decode(image_and_tables):
    return decode_program_layout(BytesProgramSource(*image_and_tables))


def _addresses(device):
    return [(i.direction, f"{i.zone}{i.address}", i.value_type, i.scale) for i in device.items]


HOUSE = _master(
    lines=(
        (MASTER, 3128, 0, 2, 0),
        (DEVICE, WALL_SWITCH, 0x1A2B, 3, 1),
        (0, 0, 0, 0, 0),  # unused line
        (DEVICE, RELAY, 0x2B3C, 2, 2),
        (DEVICE, DIMMER, 0x3C4D, 2, 2),
    ),
    conv_in=bytes([0x94, 0x01, 0x93, 0x02, 0x01, 0x01]),
    conv_out=bytes([0x01, 0x02, 0xC1, 0xC1]),
)


@pytest.mark.parametrize("records_at", [0x206, 0x20E])
def test_walk_places_every_item(records_at):
    layout = _decode(build_program([HOUSE], records_at=records_at, spare_records=3))

    assert layout.error is None
    (master,) = layout.masters
    assert (master.rack, master.slot, master.in_start, master.out_start) == (0, 2, ("X", 0), ("Y", 0))
    master_line, switch, relay, dimmer = master.devices
    assert [d.line for d in master.devices] == [0, 1, 3, 4]
    assert _addresses(master_line) == [("in", "X0", "REAL", None)]
    assert (switch.hw_code, switch.serial) == (WALL_SWITCH, 0x1A2B)
    assert _addresses(switch) == [
        ("in", "X4", "BYTE", None),
        ("in", "X5", "REAL", 0.01),
        ("out", "Y0", "BYTE", None),
    ]
    assert _addresses(relay) == [("in", "X9", "WORD", None), ("out", "Y1", "WORD", None)]
    assert _addresses(dimmer) == [
        ("in", "X11", "BYTE", None),
        ("in", "X12", "BYTE", None),
        ("out", "Y3", "REAL", None),
        ("out", "Y7", "REAL", None),
    ]
    assert not any(d.unsupported for d in master.devices)


def test_raw_temperature_stays_a_word():
    program = _master(lines=((DEVICE, WALL_SWITCH, 1, 3, 1),),
                      conv_in=bytes([0x01, 0x02]), conv_out=bytes([0x01]))
    (device,) = _decode(build_program([program])).masters[0].devices
    assert _addresses(device)[:2] == [("in", "X0", "BYTE", None), ("in", "X1", "WORD", None)]


def test_unknown_code_stops_only_from_that_device_on():
    program = _master(
        lines=(
            (DEVICE, WALL_SWITCH, 1, 3, 1),
            (DEVICE, SENSOR_IF, 2, 5, 0),
            (DEVICE, WALL_SWITCH, 3, 3, 1),
        ),
        conv_in=bytes([0x01, 0x93, 0x01, 0x95, 0x95, 0x44, 0x01, 0x93]),
        conv_out=bytes([0x01, 0x01]),
    )
    first, sensor, last = _decode(build_program([program])).masters[0].devices
    assert not first.unsupported
    # the sensor's raw bytes are covered before the unknown code is reached
    assert not sensor.unsupported
    assert _addresses(sensor)[1:] == [("in", "X6", "REAL", 0.1), ("in", "X10", "REAL", 0.1)]
    assert last.unsupported == ("in: unknown conversion code 0x44",)
    # outputs do not depend on the input table
    assert _addresses(last) == [("out", "Y1", "BYTE", None)]


def test_codes_not_matching_the_line_length_are_unsupported():
    program = _master(lines=((DEVICE, RELAY, 1, 2, 0),), conv_in=bytes([0x01, 0x02]))
    (device,) = _decode(build_program([program])).masters[0].devices
    assert device.unsupported == ("in: codes cover 3 raw bytes, line has 2",)
    assert device.items == ()


def test_two_masters_and_other_records():
    second = _master(rack=0, slot=3, iin=1024, iout=0x01000400,
                     lines=((DEVICE, RELAY, 7, 2, 2),), conv_in=b"\x02", conv_out=b"\x02")
    layout = _decode(build_program([HOUSE, second], others=(0x0101, 0x07E1)))
    assert [(m.slot, m.in_start) for m in layout.masters] == [(2, ("X", 0)), (3, ("X", 1024))]
    assert _addresses(layout.masters[1].devices[0]) == [
        ("in", "X1024", "WORD", None), ("out", "Y1024", "WORD", None)]
    assert [r.logical_address for r in layout.other_records] == [0x0101, 0x07E1]


def test_bad_table_checksum_is_reported_not_guessed():
    image, tables = build_program([HOUSE])
    master_table = _decode((image, tables)).masters[0].init_table
    layout = _decode(build_program([HOUSE], corrupt_table=master_table))
    assert layout.masters[0].error == f"table {master_table} checksum mismatch (program being changed?)"
    assert layout.masters[0].devices == ()


def test_not_a_program():
    assert _decode((bytes(0x100), bytes(16))).error == "not a program image (header marker)"


def test_cross_check_reports_disagreements():
    layout = _decode(build_program([HOUSE]))
    entries = [
        {"name": "hall_UP1", "register": "X", "address": 4, "bit": 0, "data_type": "BOOL"},
        {"name": "hall_TEMP", "register": "X", "address": 5, "bit": None, "data_type": "REAL"},
        {"name": "relays", "register": "Y", "address": 1, "bit": None, "data_type": "WORD"},
        {"name": "dimmer", "register": "Y", "address": 3, "bit": None, "data_type": "REAL"},
        {"name": "flags", "register": "X", "address": 11, "bit": None, "data_type": "BOOL"},
        {"name": "bus_status", "register": "X", "address": 7600, "bit": None, "data_type": "BYTE"},
        {"name": "wrong_temp", "register": "X", "address": 6, "bit": None, "data_type": "REAL"},
        {"name": "nowhere", "register": "X", "address": 300, "bit": 1, "data_type": "BOOL"},
        {"name": "setpoint", "register": "R", "address": 5, "bit": None, "data_type": "REAL"},
    ]
    result = cross_check(layout, entries)
    assert (result["agree"], result["differ"], result["in_module_status"], result["bool_without_bit"]) == (
        5, 2, 1, 1)
    assert [d["pub_entry"] for d in result["differences"]] == ["wrong_temp", "nowhere"]


def test_snapshot_carries_the_raw_reads():
    layout = _decode(build_program([HOUSE]))
    snapshot = layout_snapshot(layout)
    assert snapshot["masters"][0]["devices"][1]["items"][1] == "in X5 REAL x0.01 code 93"
    assert snapshot["masters"][0]["convert_in"] == "940193020101"


def _client_on_memory(memory: dict[int, bytes]) -> EpsnetUdpClient:
    client = EpsnetUdpClient("127.0.0.1", 1, local_port=0)
    reads: list[tuple[int, int]] = []

    def read(_sock, *, token, length):
        for base, data in memory.items():
            if base <= token < base + len(data):
                reads.append((token, length))
                return data[token - base : token - base + length]
        raise EpsnetProtocolError("unmapped")

    client._read_discovery_block_on_socket = read  # type: ignore[method-assign]
    client.reads = reads  # type: ignore[attr-defined]
    return client


IMAGE_AT, TABLES_AT = 0x400F0000, 0x07050000
OTHER = (0x40010000, 0x40020000, 0x40030000, 0x40040000, 0x40050000, 0x40060000)


def test_session_reader_finds_the_program_in_the_usual_entries():
    image, tables = build_program([HOUSE])
    client = _client_on_memory({IMAGE_AT: image, TABLES_AT: tables})
    layout = client._read_program_layout_on_socket(None, (IMAGE_AT, *OTHER, TABLES_AT))
    assert layout.error is None
    assert (layout.image_address, layout.table_address) == (IMAGE_AT, TABLES_AT)
    assert len(layout.items) == 10
    assert set(layout.reads) == {"image", "tables"}


def test_session_reader_finds_the_program_by_contents():
    image, tables = build_program([HOUSE])
    client = _client_on_memory({OTHER[1]: image, OTHER[3]: tables})
    layout = client._read_program_layout_on_socket(None, (IMAGE_AT, *OTHER, TABLES_AT))
    assert (layout.image_address, layout.table_address) == (OTHER[1], OTHER[3])
    assert len(layout.items) == 10


def test_session_reader_never_raises():
    client = _client_on_memory({})
    layout = client._read_program_layout_on_socket(None, (IMAGE_AT, TABLES_AT))
    assert layout.error == "program image not found in the memory map"


def test_diagnostics_compare_the_stored_map():
    from custom_components.epsnet.diagnostics import program_layout_vs_map

    layout = _decode(build_program([HOUSE]))
    blob = {"entries": [
        {"name": "hall_TEMP", "register": "X", "storage": "F", "address": 5, "bit": None, "data_type": "REAL"},
    ]}
    assert program_layout_vs_map(layout, blob)["agree"] == 1
    assert program_layout_vs_map(None, blob) is None
    assert program_layout_vs_map(layout, None) is None


def test_memory_map_keeps_areas_outside_the_module_space():
    from custom_components.epsnet.epsnet_udp import _parse_memory_map

    body = b"\x00ID\x00" + b"".join(
        struct.pack(">IH", a, 0x10) for a in (IMAGE_AT, *OTHER, TABLES_AT)) + b"\x46\xe0"
    assert _parse_memory_map(body) == (IMAGE_AT, *OTHER, TABLES_AT)
