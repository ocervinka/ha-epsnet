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
CPU_RECORD = 0x0101  # a program's first bus record is the CPU's own
WALL_SWITCH, RELAY, DIMMER, SENSOR_IF = 3126, 3151, 3101, 3309


def _master(rack=0, slot=2, iin=0, iout=0x01000000, lines=(), conv_in=b"", conv_out=b""):
    return dict(rack=rack, slot=slot, iin=iin, iout=iout, lines=lines,
                conv_in=conv_in, conv_out=conv_out)


def build_program(masters, *, records_at=0x206, spare_records=0, others=(), corrupt_table=None,
                  other_table=None):
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
        table = add(other_table) if other_table is not None else 0
        records += struct.pack("<BBBBHHHIIH", 0x13, 1, 0, 0, logical, 10, 2, 7500, 0x2000 + 7500, table)

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
        conv_in=bytes([0x01, 0x93, 0x01, 0x95, 0x95, 0xEE, 0x01, 0x93]),
        conv_out=bytes([0x01, 0x01]),
    )
    first, sensor, last = _decode(build_program([program])).masters[0].devices
    assert not first.unsupported
    # the sensor's raw bytes are covered before the unknown code is reached
    assert not sensor.unsupported
    assert _addresses(sensor)[1:] == [("in", "X6", "REAL", 0.1), ("in", "X10", "REAL", 0.1)]
    assert last.unsupported == ("in: unknown conversion code 0xee",)
    # outputs do not depend on the input table
    assert _addresses(last) == [("out", "Y1", "BYTE", None)]


def test_a_copy_code_is_the_number_of_bytes_it_copies():
    # a programming tool that joins a module's plain items: 5 bytes in, 24 out
    program = _master(
        lines=((DEVICE, 3377, 1, 11, 24), (DEVICE, 3377, 2, 11, 24)),
        conv_in=bytes([0x01, 0x95, 0x01, 0x95, 0x05] * 2),
        conv_out=bytes([0x18, 0x18]),
    )
    first, second = _decode(build_program([program])).masters[0].devices
    assert not first.unsupported and not second.unsupported
    assert _addresses(first)[4:] == [("in", "X10", "BYTES5", None), ("out", "Y0", "BYTES24", None)]
    assert _addresses(second)[0] == ("in", "X15", "BYTE", None)
    assert _addresses(second)[-1] == ("out", "Y24", "BYTES24", None)


def test_a_switched_off_value_keeps_its_place_without_raw_bytes():
    program = _master(
        lines=((DEVICE, 3360, 1, 3, 1), (DEVICE, WALL_SWITCH, 2, 3, 1)),
        conv_in=bytes([0x03, 0x44, 0x44, 0x44, 0x01, 0x93]),
        conv_out=bytes([0x01, 0x01]),
    )
    layout = _decode(build_program([program]))
    module, switch = layout.masters[0].devices
    assert not module.unsupported and not switch.unsupported
    assert [(i.address, i.size, i.raw_size) for i in module.items if i.direction == "in"] == [
        (0, 3, 3), (3, 4, 0), (7, 4, 0), (11, 4, 0)]
    # the next module starts after the three places
    assert _addresses(switch)[0] == ("in", "X15", "BYTE", None)
    items = layout_snapshot(layout)["masters"][0]["devices"][0]["items"]
    assert items[1] == "in X3 REAL code 44, switched off"


def test_an_analog_output_is_a_real_made_of_one_raw_byte():
    program = _master(
        lines=((DEVICE, 3359, 1, 2, 2), (DEVICE, WALL_SWITCH, 2, 3, 1)),
        conv_in=bytes([0x02, 0x01, 0x93]),
        conv_out=bytes([0x01, 0xC2, 0x01]),
    )
    module, switch = _decode(build_program([program])).masters[0].devices
    assert not module.unsupported
    assert _addresses(module)[1:] == [("out", "Y0", "BYTE", None), ("out", "Y1", "REAL", None)]
    assert _addresses(switch)[-1] == ("out", "Y5", "BYTE", None)


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


EXPANSION_MODULE = 0x0000  # a record of a module on the PLC's other bus


@pytest.mark.parametrize("header", [True, False])
def test_other_modules_settings_tables_are_kept_for_the_diagnostics(header):
    settings = bytes(range(1, 31))
    program = build_program([HOUSE], others=(CPU_RECORD, EXPANSION_MODULE), other_table=settings)
    layout = decode_program_layout(BytesProgramSource(*program), header=header)
    assert layout.error is None and len(layout.items) == 10
    assert [(r.init_table > 0, r.table, r.table_note) for r in layout.other_records] == [
        (True, settings, None)] * 2
    assert layout_snapshot(layout)["other_records"][1]["table"] == settings.hex()


@pytest.mark.parametrize("header", [True, False])
def test_a_long_table_of_another_module_is_left_out(header):
    program = build_program([HOUSE], others=(CPU_RECORD, EXPANSION_MODULE), other_table=bytes(0x500))
    layout = decode_program_layout(BytesProgramSource(*program), header=header)
    assert len(layout.items) == 10
    record = layout.other_records[1]
    assert record.table == b"" and "longer than 1024 bytes" in record.table_note


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


def test_an_unknown_program_image_leaves_what_the_memory_areas_start_with():
    """For the diagnostics: enough to see what another firmware keeps there."""
    image, _tables = build_program([HOUSE])
    other_kind = b"\x00\x32" + image[2:]
    client = _client_on_memory({IMAGE_AT: other_kind})
    layout = client._read_program_layout_on_socket(None, (IMAGE_AT, OTHER[0], TABLES_AT))
    assert layout.error == "program image not found in the memory map"
    assert layout.notes == (
        f"0x400f0000: starts with {other_kind[:0x30].hex()}",
        "0x40010000: read refused: unmapped",
        "0x07050000: read refused: unmapped",
    )
    assert layout_snapshot(layout)["notes"] == list(layout.notes)


@pytest.mark.parametrize("records_at", [0x206, 0x20E])
def test_a_program_is_read_the_same_without_its_header(records_at):
    program = build_program([HOUSE, HOUSE], records_at=records_at, others=(CPU_RECORD,))
    with_header = _decode(program)
    without = decode_program_layout(BytesProgramSource(*program), header=False)
    assert with_header.error is None and len(with_header.items) == 20
    assert (without.masters, without.other_records, without.error) == (
        with_header.masters, with_header.other_records, None)


def test_without_the_header_a_broken_table_area_is_reported_not_guessed():
    program = build_program([HOUSE], others=(CPU_RECORD,), corrupt_table=2)
    layout = decode_program_layout(BytesProgramSource(*program), header=False)
    (master,) = layout.masters
    assert master.devices == ()
    assert "fails its checksum" in master.error and "2 tables before it" in master.error


def test_without_the_header_something_else_is_not_taken_for_a_program():
    layout = decode_program_layout(BytesProgramSource(bytes(0x400), bytes(0x100)), header=False)
    assert layout.error == "no bus-master records where a program keeps them"


def _client_refusing_the_program_header(
    image: bytes, tables: bytes, hidden: int = 0x200
) -> EpsnetUdpClient:
    """A PLC that hands out its program except the first ``hidden`` bytes, and
    refuses a read that runs past the end of the table area."""
    client = EpsnetUdpClient("127.0.0.1", 1, local_port=0)
    refused: list[tuple[int, int]] = []

    def read(_sock, *, token, length):
        if IMAGE_AT + hidden <= token and token + length <= IMAGE_AT + len(image):
            return image[token - IMAGE_AT : token - IMAGE_AT + length]
        if TABLES_AT <= token and token + length <= TABLES_AT + len(tables):
            return tables[token - TABLES_AT : token - TABLES_AT + length]
        refused.append((token, length))
        raise EpsnetProtocolError("refused")

    client._read_discovery_block_on_socket = read  # type: ignore[method-assign]
    client.refused = refused  # type: ignore[attr-defined]
    return client


def test_session_reader_reads_a_program_whose_header_the_plc_refuses():
    image, tables = build_program([HOUSE], records_at=0x20E, others=(CPU_RECORD,))
    image += bytes(0x200)  # the image goes on after the parts read here
    client = _client_refusing_the_program_header(image, tables)
    layout = client._read_program_layout_on_socket(None, (IMAGE_AT, *OTHER, TABLES_AT))

    assert layout.error is None
    assert (layout.image_address, layout.table_address) == (IMAGE_AT, TABLES_AT)
    assert len(layout.items) == 10
    assert layout.items == _decode((image, tables)).items
    assert layout.notes[0].startswith("0x400f0000: read refused")
    assert any("header is not readable" in note for note in layout.notes)
    # everything read is kept for the diagnostics
    assert layout.reads["tables"][0] == tables
    assert layout.reads["image"][0x200] == image[0x200:0x2F0]
    # asked in vain: the header, five short reads that look for where the
    # program becomes readable, and one read past the end of the table area
    in_program = [t for t, _ in client.refused if IMAGE_AT <= t < IMAGE_AT + len(image)]
    in_tables = [t for t, _ in client.refused if TABLES_AT <= t <= TABLES_AT + len(tables)]
    assert in_program[0] == IMAGE_AT and len(in_program) == 6 and len(in_tables) <= 1
    assert "0x400f0000: readable from +0x200" in layout.notes


@pytest.mark.parametrize("hidden", [0x10, 0x30, 0x40, 0x130, 0x1F0])
def test_session_reader_notes_where_a_hidden_program_start_ends(hidden):
    image, tables = build_program([HOUSE], records_at=0x20E, others=(CPU_RECORD,))
    image = bytes(range(256)) * 2 + image[0x200:] + bytes(0x200)
    client = _client_refusing_the_program_header(image, tables, hidden)
    layout = client._read_program_layout_on_socket(None, (IMAGE_AT, TABLES_AT))
    assert layout.error is None and len(layout.items) == 10
    (note,) = [n for n in layout.notes if "readable from" in n]
    assert note == (
        f"0x400f0000: readable from +{hidden:#x}, "
        f"which starts with {image[hidden:hidden + 0x10].hex()}"
    )


def test_session_reader_says_what_it_found_when_the_tables_cannot_be_followed():
    image, tables = build_program([HOUSE], others=(CPU_RECORD,), corrupt_table=1)
    client = _client_refusing_the_program_header(image + bytes(0x200), tables)
    layout = client._read_program_layout_on_socket(None, (IMAGE_AT, TABLES_AT))
    (master,) = layout.masters
    assert layout.table_address == TABLES_AT and master.devices == ()
    assert "fails its checksum" in master.error
    assert layout_snapshot(layout)["masters"][0]["error"] == master.error


def test_session_reader_without_header_or_records_finds_no_program():
    client = _client_refusing_the_program_header(bytes(0x400), bytes(0x40))
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
