"""I/O layout of the PLC program: where every bus module's data sits in X/Y.

The compiled PLC program holds the complete I/O configuration: for each CIB bus
master the list of its devices in data order, each device's raw data length,
and per data item a conversion code saying how the firmware turns the device's
raw bus bytes into X/Y bytes. Walking these tables gives the X/Y address of
every channel without a variable map.

Pure decoding; the bytes come from a :class:`ProgramSource` (the PLC's memory
during the module scan, or files in tests). Anything unexpected yields an
explicit "unsupported" result with the reason, never a guess.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Protocol

# Program image header (big-endian 16-bit words at these offsets)
IMAGE_MAGIC = 0x002A
_HDR_LEN = 0x30
_HDR_WORD_TABLE = 0x1A
_HDR_WORD_TABLE_LEN = 0x1C
_HDR_RECORDS = 0x22
_HDR_RECORDS_LEN = 0x24
_HDR_TABLE_AREA_LEN = 0x26

# Bus-master records (20 B each, little-endian fields)
_RECORD_LEN = 20
_RECORD_MARKER = 0x13
_RECORD = struct.Struct("<BBBBHHHIIH")
LOGICAL_CIB_MASTER = 0x0110

# CIB master configuration table
_CIB_CODE = 0x00C9
_CIB_HEAD = struct.Struct("<HHBBBBBHBHHIIIIIIHHBB")
_CIB_LINE = struct.Struct("<BBHHH")
_LINE_MASTER = 0xC0
_LINE_DEVICE = 0x80
_INIT = struct.Struct("<HHHBB")

_ZONES = {0: "X", 1: "Y", 2: "S", 3: "R"}


@dataclass(frozen=True, slots=True)
class Conversion:
    """What one conversion code does: raw bus bytes -> X/Y bytes."""

    raw_size: int
    size: int
    value_type: str
    scale: float | None = None


# Verified against real programs from both programming tools.
CONVERSIONS: dict[int, Conversion] = {
    0x01: Conversion(1, 1, "BYTE"),
    0x02: Conversion(2, 2, "WORD"),
    0x03: Conversion(3, 3, "BYTES3"),
    0x93: Conversion(2, 4, "REAL", 0.01),
    0x95: Conversion(2, 4, "REAL", 0.1),
    0x94: Conversion(2, 4, "REAL"),
    0xC1: Conversion(1, 4, "REAL"),
}


class ProgramLayoutError(ValueError):
    """The program does not have the expected structure."""


class ProgramSource(Protocol):
    """Byte access to the program image and its table area."""

    def image(self, offset: int, length: int) -> bytes: ...

    def tables(self, offset: int, length: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ProgramItem:
    """One data item of a device in X/Y."""

    direction: str  # "in" (X) or "out" (Y)
    zone: str
    address: int
    size: int
    raw_size: int
    code: int
    value_type: str
    scale: float | None = None


@dataclass(frozen=True, slots=True)
class ProgramDevice:
    """One line of a bus master's configuration, in data order."""

    line: int
    kind: str  # "master" or "device"
    hw_code: int | None
    serial: int | None
    position: int | None
    raw_in: int
    raw_out: int
    items: tuple[ProgramItem, ...] = ()
    unsupported: tuple[str, ...] = ()  # reasons; empty = every item placed
    # where the device's settings table sits in the table area; the module scan
    # points at the same place, which pairs program lines with scanned modules
    init_offset: int | None = None


@dataclass(frozen=True, slots=True)
class ProgramMaster:
    """A CIB bus master and its devices."""

    rack: int
    slot: int
    init_table: int
    status_in: tuple[int, int]  # flat X offset, length (the master's own data)
    status_out: tuple[int, int]
    in_start: tuple[str, int] | None = None
    out_start: tuple[str, int] | None = None
    convert_in: bytes = b""
    convert_out: bytes = b""
    devices: tuple[ProgramDevice, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProgramRecord:
    """A bus-master record that is not a CIB master (kept for diagnostics)."""

    rack: int
    slot: int
    logical_address: int
    status_in: tuple[int, int]
    status_out: tuple[int, int]


@dataclass(frozen=True, slots=True)
class ProgramIoLayout:
    """Result of decoding a program's I/O configuration."""

    masters: tuple[ProgramMaster, ...] = ()
    other_records: tuple[ProgramRecord, ...] = ()
    error: str | None = None
    image_address: int | None = None
    table_address: int | None = None
    reads: dict[str, dict[int, bytes]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def items(self) -> tuple[ProgramItem, ...]:
        return tuple(
            item
            for master in self.masters
            for device in master.devices
            for item in device.items
        )


def _be16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _zoned(value: int) -> tuple[str, int]:
    zone = _ZONES.get(value >> 24)
    if zone is None:
        raise ProgramLayoutError(f"unknown zone in address {value:#010x}")
    return zone, value & 0xFFFFFF


def table_checksum(length: int, data: bytes) -> int:
    return ((length >> 8) + (length & 0xFF) + sum(data)) & 0xFFFF


class _Program:
    def __init__(self, source: ProgramSource) -> None:
        self.source = source
        header = source.image(0, _HDR_LEN)
        if len(header) < _HDR_LEN or _be16(header, 0) != IMAGE_MAGIC:
            raise ProgramLayoutError("not a program image (header marker)")
        self.table_area_len = _be16(header, _HDR_TABLE_AREA_LEN)
        records_at = _be16(header, _HDR_RECORDS)
        records_len = _be16(header, _HDR_RECORDS_LEN)
        if records_at < _HDR_LEN or records_len % _RECORD_LEN:
            raise ProgramLayoutError(
                f"bus-master records at {records_at:#x}, {records_len} B: unexpected"
            )
        self.records = source.image(records_at, records_len) if records_len else b""
        word_table_at = _be16(header, _HDR_WORD_TABLE)
        word_table_len = _be16(header, _HDR_WORD_TABLE_LEN)
        if word_table_at < _HDR_LEN or word_table_len % 2:
            raise ProgramLayoutError("table index: unexpected position or length")
        raw = source.image(word_table_at, word_table_len)
        offsets: list[int] = []
        for index in range(0, len(raw), 2):
            value = _be16(raw, index)
            if value == 0 and offsets:
                break
            offsets.append(value)
        self.table_offsets = tuple(offsets)
        self._tables: dict[int, bytes] = {}

    def table(self, number: int) -> bytes:
        if number in self._tables:
            return self._tables[number]
        if not 0 <= number < len(self.table_offsets):
            raise ProgramLayoutError(f"table {number} not in the table index")
        offset = self.table_offsets[number]
        if offset + 4 > self.table_area_len:
            raise ProgramLayoutError(f"table {number} outside the table area")
        length = _be16(self.source.tables(offset, 2), 0)
        if offset + 4 + length > self.table_area_len:
            raise ProgramLayoutError(f"table {number} longer than the table area")
        body = self.source.tables(offset + 2, length + 2)
        data, check = body[:length], _be16(body, length)
        if check != table_checksum(length, data):
            raise ProgramLayoutError(
                f"table {number} checksum mismatch (program being changed?)"
            )
        self._tables[number] = data
        return data


def _walk(
    codes: bytes,
    start: tuple[str, int],
    lines: list[ProgramDevice],
    direction: str,
) -> tuple[dict[int, list[ProgramItem]], dict[int, str]]:
    """Assign X/Y addresses to every data item; stop at the first unknown code."""
    zone, address = start
    items: dict[int, list[ProgramItem]] = {}
    problems: dict[int, str] = {}
    cursor = 0
    stopped: str | None = None
    for line in lines:
        raw_len = line.raw_in if direction == "in" else line.raw_out
        if stopped is not None:
            if raw_len:
                problems[line.line] = stopped
            continue
        consumed = 0
        placed: list[ProgramItem] = []
        while consumed < raw_len:
            if cursor >= len(codes):
                stopped = f"{direction}: conversion table ends early"
                break
            code = codes[cursor]
            conversion = CONVERSIONS.get(code)
            if conversion is None:
                stopped = f"{direction}: unknown conversion code {code:#04x}"
                break
            cursor += 1
            placed.append(
                ProgramItem(
                    direction=direction,
                    zone=zone,
                    address=address,
                    size=conversion.size,
                    raw_size=conversion.raw_size,
                    code=code,
                    value_type=conversion.value_type,
                    scale=conversion.scale,
                )
            )
            consumed += conversion.raw_size
            address += conversion.size
        if stopped is None and consumed != raw_len:
            stopped = f"{direction}: codes cover {consumed} raw bytes, line has {raw_len}"
            problems[line.line] = stopped
            continue
        if stopped is not None:
            # The items before the unknown code are placed correctly; everything
            # after it (the rest of this device and later devices) is not.
            problems[line.line] = stopped
            if placed:
                items[line.line] = placed
            continue
        items[line.line] = placed
    return items, problems


def _decode_master(program: _Program, record: tuple, init_table: int) -> ProgramMaster:
    _, _, rack, slot, _, len_in, len_out, off_in, off_out, _ = record
    master = ProgramMaster(
        rack=rack,
        slot=slot,
        init_table=init_table,
        status_in=(off_in, len_in),
        status_out=(off_out, len_out),
    )
    try:
        table = program.table(init_table)
        if len(table) < _CIB_HEAD.size or struct.unpack_from("<H", table)[0] != _CIB_CODE:
            raise ProgramLayoutError("bus master table is not a CIB configuration")
        head = _CIB_HEAD.unpack_from(table)
        iin, iout = head[13], head[14]
        conv_in_table, conv_out_table, line_count = head[17], head[18], head[20]
        if len(table) < _CIB_HEAD.size + line_count * _CIB_LINE.size:
            raise ProgramLayoutError(f"{line_count} device lines do not fit the table")
        in_start, out_start = _zoned(iin), _zoned(iout)
        convert_in = program.table(conv_in_table) if conv_in_table else b""
        convert_out = program.table(conv_out_table) if conv_out_table else b""

        lines: list[ProgramDevice] = []
        for index in range(line_count):
            kind_byte, _, init, raw_in, raw_out = _CIB_LINE.unpack_from(
                table, _CIB_HEAD.size + index * _CIB_LINE.size
            )
            if kind_byte == 0 and init == 0:
                continue  # unused line
            hw_code = serial = position = init_offset = None
            if init:
                init_offset = program.table_offsets[init] if init < len(program.table_offsets) else None
                init_data = program.table(init)
                if len(init_data) >= _INIT.size:
                    hw_code, _, serial, position, _ = _INIT.unpack_from(init_data)
            kind = {_LINE_MASTER: "master", _LINE_DEVICE: "device"}.get(
                kind_byte, f"kind {kind_byte:#04x}"
            )
            lines.append(
                ProgramDevice(
                    line=index,
                    kind=kind,
                    hw_code=hw_code,
                    serial=serial,
                    position=position,
                    raw_in=raw_in,
                    raw_out=raw_out,
                    init_offset=init_offset,
                )
            )

        in_items, in_problems = _walk(convert_in, in_start, lines, "in")
        out_items, out_problems = _walk(convert_out, out_start, lines, "out")
        devices = []
        for line in lines:
            problems = tuple(
                p for p in (in_problems.get(line.line), out_problems.get(line.line)) if p
            )
            devices.append(
                ProgramDevice(
                    line=line.line,
                    kind=line.kind,
                    hw_code=line.hw_code,
                    serial=line.serial,
                    position=line.position,
                    raw_in=line.raw_in,
                    raw_out=line.raw_out,
                    items=tuple(in_items.get(line.line, ()))
                    + tuple(out_items.get(line.line, ())),
                    unsupported=problems,
                    init_offset=line.init_offset,
                )
            )
        return ProgramMaster(
            rack=rack,
            slot=slot,
            init_table=init_table,
            status_in=(off_in, len_in),
            status_out=(off_out, len_out),
            in_start=in_start,
            out_start=out_start,
            convert_in=convert_in,
            convert_out=convert_out,
            devices=tuple(devices),
        )
    except ProgramLayoutError as err:
        return ProgramMaster(
            rack=master.rack,
            slot=master.slot,
            init_table=init_table,
            status_in=master.status_in,
            status_out=master.status_out,
            error=str(err),
        )


def decode_program_layout(source: ProgramSource) -> ProgramIoLayout:
    """Decode the I/O configuration; structural problems end up in ``error``."""
    try:
        program = _Program(source)
    except ProgramLayoutError as err:
        return ProgramIoLayout(error=str(err))
    masters: list[ProgramMaster] = []
    others: list[ProgramRecord] = []
    for offset in range(0, len(program.records), _RECORD_LEN):
        record = _RECORD.unpack_from(program.records, offset)
        marker, _, rack, slot, logical, len_in, len_out, off_in, off_out, init = record
        if marker != _RECORD_MARKER:
            return ProgramIoLayout(
                masters=tuple(masters),
                other_records=tuple(others),
                error=f"bus-master record {offset // _RECORD_LEN}: unexpected marker {marker:#04x}",
            )
        if logical == LOGICAL_CIB_MASTER:
            masters.append(_decode_master(program, record, init))
        else:
            others.append(
                ProgramRecord(rack, slot, logical, (off_in, len_in), (off_out, len_out))
            )
    return ProgramIoLayout(masters=tuple(masters), other_records=tuple(others))


class BytesProgramSource:
    """A program held in memory (the image and its table area as bytes)."""

    def __init__(self, image: bytes, tables: bytes) -> None:
        self._image = image
        self._tables = tables

    def image(self, offset: int, length: int) -> bytes:
        return self._image[offset : offset + length]

    def tables(self, offset: int, length: int) -> bytes:
        return self._tables[offset : offset + length]


# ---- comparison with a variable map ----

_Y_FLAT_BASE = 0x2000


def _status_ranges(layout: ProgramIoLayout) -> list[tuple[str, int, int]]:
    ranges = []
    records = [(m.status_in, m.status_out) for m in layout.masters] + [
        (r.status_in, r.status_out) for r in layout.other_records
    ]
    for (off_in, len_in), (off_out, len_out) in records:
        if len_in:
            ranges.append(("X", off_in, off_in + len_in))
        if len_out:
            ranges.append(("Y", off_out - _Y_FLAT_BASE, off_out - _Y_FLAT_BASE + len_out))
    return ranges


def cross_check(layout: ProgramIoLayout, entries: list[dict]) -> dict[str, object]:
    """Compare every X/Y variable of a map with the computed layout.

    ``entries`` are map entries (``name``, ``register``, ``address``, ``bit``,
    ``data_type``). A variable agrees when a computed item covers its address
    and the kinds fit (a REAL must start exactly on a REAL item).
    """
    items = layout.items
    status = _status_ranges(layout)
    agree = whole_byte = in_module_status = 0
    differences: list[dict[str, object]] = []
    for entry in entries:
        zone = entry.get("register")
        if zone not in ("X", "Y"):
            continue
        address = int(entry["address"])
        bit = entry.get("bit")
        data_type = str(entry.get("data_type") or "")
        hit = next(
            (
                i
                for i in items
                if i.zone == zone and i.address <= address < i.address + i.size
            ),
            None,
        )
        if hit is None:
            if any(z == zone and lo <= address < hi for z, lo, hi in status):
                in_module_status += 1
                continue
            reason = "no module data at this address"
        elif data_type == "REAL":
            if hit.value_type == "REAL" and hit.address == address:
                agree += 1
                continue
            reason = f"map says REAL, program has {hit.value_type} at {zone}{hit.address}"
        elif hit.value_type == "REAL":
            reason = f"map says {data_type}, program has REAL at {zone}{hit.address}"
        else:
            agree += 1
            if bit is None and data_type == "BOOL" and hit.size >= 1:
                whole_byte += 1
            continue
        differences.append(
            {
                "pub_entry": str(entry.get("name")),
                "address": f"{zone}{address}" + (f".{bit}" if bit is not None else ""),
                "data_type": data_type,
                "reason": reason,
            }
        )
    return {
        "agree": agree,
        "differ": len(differences),
        "in_module_status": in_module_status,
        "bool_without_bit": whole_byte,
        "differences": differences,
    }


def layout_snapshot(layout: ProgramIoLayout) -> dict[str, object]:
    """Diagnostics form of a decoded layout, including the raw bytes read."""

    def item(i: ProgramItem) -> str:
        scale = f" x{i.scale:g}" if i.scale else ""
        return f"{i.direction} {i.zone}{i.address} {i.value_type}{scale} code {i.code:02x}"

    return {
        "error": layout.error,
        "notes": list(layout.notes),
        "image_address": None if layout.image_address is None else f"0x{layout.image_address:08x}",
        "table_address": None if layout.table_address is None else f"0x{layout.table_address:08x}",
        "other_records": [
            {
                "rack": r.rack,
                "slot": r.slot,
                "logical_address": f"0x{r.logical_address:04x}",
                "status_in": list(r.status_in),
                "status_out": list(r.status_out),
            }
            for r in layout.other_records
        ],
        "masters": [
            {
                "rack": m.rack,
                "slot": m.slot,
                "error": m.error,
                "init_table": m.init_table,
                "status_in": list(m.status_in),
                "status_out": list(m.status_out),
                "in_start": None if m.in_start is None else f"{m.in_start[0]}{m.in_start[1]}",
                "out_start": None if m.out_start is None else f"{m.out_start[0]}{m.out_start[1]}",
                "convert_in": m.convert_in.hex(),
                "convert_out": m.convert_out.hex(),
                "devices": [
                    {
                        "line": d.line,
                        "kind": d.kind,
                        "hw_code": d.hw_code,
                        "serial": None if d.serial is None else f"{d.serial:04X}",
                        "raw_in": d.raw_in,
                        "raw_out": d.raw_out,
                        "unsupported": list(d.unsupported),
                        "items": [item(i) for i in d.items],
                    }
                    for d in m.devices
                ],
            }
            for m in layout.masters
        ],
        "reads": {
            area: {f"{offset:#06x}": data.hex() for offset, data in sorted(blocks.items())}
            for area, blocks in layout.reads.items()
        },
    }


def layout_fingerprint(layout: ProgramIoLayout | None) -> tuple | None:
    """What the entities depend on; equal fingerprints = same channels at the same places."""
    if layout is None or layout.error:
        return None
    return tuple(
        (
            m.rack,
            m.slot,
            m.in_start,
            m.out_start,
            m.convert_in,
            m.convert_out,
            m.error,
            tuple(
                (d.line, d.kind, d.hw_code, d.serial, d.raw_in, d.raw_out, d.unsupported)
                for d in m.devices
            ),
        )
        for m in layout.masters
    )
