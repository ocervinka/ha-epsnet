"""Direct EPSNET UDP client built from captured FT1.2 traffic."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
import re
import socket
import struct
from time import monotonic, sleep

from .models import (
    EpsnetPoint,
    EpsnetReadMetrics,
    PubEntry,
    RuntimeCibGroup,
    RuntimeDiscoveryRecord,
    RuntimeDiscoveredDevice,
    RuntimeExactDescriptor,
    RuntimeInventory,
    ScalarValue,
    ValueKind,
)
from .program_layout import (
    IMAGE_MAGIC,
    ProgramIoLayout,
    decode_program_layout,
    table_checksum,
)
from .pubfile import parse_pub_text
from .runtime_registry import lookup_runtime_model_info

LOGGER = logging.getLogger(__name__)

_WRAPPER_MARKER = b"\x02\x00"
_ACK = b"\xe5"
_READ_PREFIX = b"\x6c\x0b"
_WRITE_PREFIX = b"\x63\x0c"
# One-bit write: per bit <zone> <address LE> <bit number, | 0x80 to set it>; the other
# seven bits of the byte are left as they are.
_WRITE_BIT_PREFIX = b"\x63\x10"
_READ_RESPONSE_CODE = 0x08
_WRITE_RESPONSE_CODE = 0x09
_HELLO_SERVICE = 0x69
_IDENT_SERVICE = 0x6E
_DISCOVERY_ROOT_SERVICE = 0x01
_DISCOVERY_PARAMETER_SERVICE = 0x02
_LOGIN_SERVICE = 0x49
_FILE_SERVICE = 0x6C
_FILE_ZONE = 0x28
_DISCOVERY_GROUP_META = 0x049E
_DISCOVERY_GROUP_TABLE_INDEX = 2
_DISCOVERY_GROUP_HEADER_LENGTH = 0x14
_DISCOVERY_FIRST_CHUNK_LENGTH = 0xDC
_DISCOVERY_SECOND_CHUNK_LENGTH = 0xAC
_DISCOVERY_SLOT_COUNT = 32
_DISCOVERY_MAX_READ_LENGTH = 0xF0
_DISCOVERY_CHILD_RECORD_LENGTH = 0x66
_DISCOVERY_DESCRIPTOR_POINTER_OFFSET = 0x46

# ---- on-PLC .pub fetch (6c 23 project-storage block read) ----
# Sequence: HELLO(0x7d) -> 6c 01 (identify/establish) ->
# 6c 23 walk of the project storage. Runtime reads are non-exclusive; only this
# management session is exclusive (see PlcBusyError).
_MGMT_ADDRESS = 0x7D  # management/updater station (vs 0x01 for runtime reads)
_ESTABLISH_SERVICE = 0x01  # 6c 01 after HELLO -> establishes the session, returns ident
_STORAGE_SERVICE = 0x23  # 6c 23 project-storage read
_STORAGE_AREA = 0x10  # project-storage area/bank byte
_STORAGE_INIT_LEN = 0x40  # length byte on the seek/init frame
_STORAGE_BLOCK_LEN = 0xC8  # 200-byte data blocks
_STORAGE_MAX_BYTES = 0x80000  # 512 KiB safety cap on the walk
_STORAGE_END_RUNS = 4  # consecutive out-of-range replies => end of storage
_STORAGE_READ_RETRIES = 6  # per-datagram retries to ride out transient VPN packet loss
# Several 200-byte block reads are pipelined into one datagram (5 -> a reply of
# ~1059 B, safely under a 1500 B MTU). This cuts round-trips ~5x:
# ~323 datagrams instead of ~1614 for the ~322 KB store -> ~25 s vs ~200 s over VPN.
_STORAGE_BATCH = 5
# The init/seek reply (6c 23 40) reports the project-storage size as a
# 4-byte LE field; parsing it lets us read exactly that far (no over-read) and
# gives a real 100% for progress. Offset within the read-response body:
_STORAGE_SIZE_OFFSET = 45
# The storage image starts with a 0x40-byte header and the reported size counts
# from the end of that header, so the whole image is size + 0x40 bytes.
_STORAGE_HEADER_LEN = 0x40
# The header starts with this magic and holds the same size at 0x2C.
_STORAGE_HEADER_MAGIC = b"\x00\x03\x40\x00"
_STORAGE_HEADER_SIZE_OFFSET = 0x2C
# With no usable size at all, the walk stops at the first batch that reads as
# erased flash. Not the preferred end: storage writes don't erase, so old data
# can follow a shorter image.
_STORAGE_ERASED = 0xFF
_MGMT_BUSY_STATUS = 0x03  # fixed-frame status "communication not established" (session held)
# The management session is global and exclusive; a preceding op (e.g. the CIB scan)
# can leave it held until the PLC times it out. Retry long enough to ride that out.
_MGMT_ESTABLISH_ATTEMPTS = 20
_MGMT_RETRY_DELAY = 2.0

# One published-variable definition line inside the storage dump:
#   <name> <R|X|S|Y> <B|F|...> <addr> [.bit] <type> PUB_<dir>
_PUB_LINE_RE = re.compile(
    rb"[A-Za-z_][\w]{0,60} [RXSY] [BFWLD] \d+(?: \.\d+)? "
    rb"(?:BOOL|INT|UINT|SINT|USINT|DINT|UDINT|REAL|WORD|DWORD|BYTE) PUB_[A-Z]+"
)

_ZONE_CODES = {
    "X": 0x00,
    "Y": 0x01,
    "S": 0x02,
    "R": 0x03,
}

_BYTE_SIZES = {
    "BOOL": 1,
    "BYTE": 1,
    "USINT": 1,
    "INT": 2,
    "UINT": 2,
    "DINT": 4,
    "UDINT": 4,
    "DWORD": 4,
    "REAL": 4,
    "TIME": 4,
    "TIME_OF_DAY": 4,
    "DATE": 4,
    "LREAL": 8,
    "DATE_AND_TIME": 8,
}

_SIGNED_INT_TYPES = {"INT", "DINT", "TIME", "TIME_OF_DAY"}


class EpsnetError(RuntimeError):
    """Base direct EPSNET communication error."""


class EpsnetProtocolError(EpsnetError):
    """Raised when a UDP packet does not match the expected EPSNET framing."""


class PlcBusyError(EpsnetError):
    """The PLC's single management session is held by another client.

    The exclusive management session is only needed for the ``6c 23`` project-storage
    read used to fetch the on-PLC ``.pub`` at setup. Runtime variable polling is not
    affected (it is non-exclusive).
    """


PointLike = PubEntry | EpsnetPoint


@dataclass(frozen=True, slots=True)
class EpsnetReadSpan:
    """One contiguous raw read span planned for grouped polling."""

    register: str
    start_address: int
    length: int
    points: tuple[EpsnetPoint, ...]

    @property
    def end_address(self) -> int:
        """Return the first address after the span."""
        return self.start_address + self.length


def _describe_point(point: PointLike) -> str:
    bit_suffix = f".{point.bit}" if point.bit is not None else ""
    return f"{point.register}{point.storage} {point.address}{bit_suffix} {point.data_type}"


def _checksum(payload: bytes) -> int:
    return sum(payload) & 0xFF


def _build_variable_frame(control: int, address: int, data: bytes) -> bytes:
    payload = bytes((control, address)) + data
    length = len(payload)
    return (
        b"\x68"
        + bytes((length, length))
        + b"\x68"
        + payload
        + bytes((_checksum(payload), 0x16))
    )


def _build_fixed_frame(control: int, address: int, service: int) -> bytes:
    payload = bytes((control, address, service))
    return b"\x10" + payload + bytes((_checksum(payload), 0x16))


def _wrap_frame(sequence: int, inner: bytes) -> bytes:
    return (
        sequence.to_bytes(2, byteorder="little")
        + _WRAPPER_MARKER
        + len(inner).to_bytes(2, byteorder="big")
        + inner
    )


def _parse_wrapper(datagram: bytes) -> tuple[int, bytes]:
    if len(datagram) < 7:
        raise EpsnetProtocolError(f"Datagram too short: {datagram.hex()}")
    if datagram[2:4] != _WRAPPER_MARKER:
        raise EpsnetProtocolError(f"Unexpected wrapper marker: {datagram.hex()}")
    sequence = int.from_bytes(datagram[0:2], byteorder="little")
    length = int.from_bytes(datagram[4:6], byteorder="big")
    inner = datagram[6:]
    if len(inner) != length:
        raise EpsnetProtocolError(
            f"Wrapper length mismatch: expected {length}, received {len(inner)}"
        )
    return sequence, inner


def _parse_variable_frame(frame: bytes) -> tuple[int, int, bytes]:
    if len(frame) < 8 or frame[0] != 0x68:
        raise EpsnetProtocolError(f"Not a variable frame: {frame.hex()}")
    if frame[1] != frame[2] or frame[3] != 0x68:
        raise EpsnetProtocolError(f"Malformed variable frame header: {frame.hex()}")
    if frame[-1] != 0x16:
        raise EpsnetProtocolError(f"Malformed variable frame terminator: {frame.hex()}")
    length = frame[1]
    payload = frame[4:-2]
    if len(payload) != length:
        raise EpsnetProtocolError(
            f"Variable frame payload mismatch: expected {length}, received {len(payload)}"
        )
    if frame[-2] != _checksum(payload):
        raise EpsnetProtocolError(f"Checksum mismatch: {frame.hex()}")
    control, address = payload[0], payload[1]
    return control, address, payload[2:]


def _split_variable_frames(data: bytes) -> list[bytes]:
    """Split concatenated FT1.2 variable frames (a batched reply) into frames.

    Each variable frame is ``68 L L 68 <L payload> <cksum> 16`` -> 4 + L + 2 bytes.
    Stops at the first byte that isn't a frame start (tolerates trailing padding).
    """
    frames: list[bytes] = []
    pos = 0
    end = len(data)
    while pos + 6 <= end and data[pos] == 0x68:
        length = data[pos + 1]
        frame_end = pos + 4 + length + 2
        if frame_end > end:
            break
        frames.append(data[pos:frame_end])
        pos = frame_end
    return frames


def _parse_fixed_frame(frame: bytes) -> tuple[int, int, int]:
    if len(frame) != 6 or frame[0] != 0x10 or frame[-1] != 0x16:
        raise EpsnetProtocolError(f"Not a fixed frame: {frame.hex()}")
    payload = frame[1:-2]
    if frame[-2] != _checksum(payload):
        raise EpsnetProtocolError(f"Checksum mismatch: {frame.hex()}")
    return payload[0], payload[1], payload[2]


def _is_positive_fixed_ack(frame: bytes) -> bool:
    if frame == _ACK:
        return True
    try:
        return _parse_fixed_frame(frame) == (0x01, 0x00, 0x00)
    except EpsnetProtocolError:
        return False


def _zone_code_for_register(register: str, *, context: str) -> int:
    try:
        return _ZONE_CODES[register.upper()]
    except KeyError as err:
        raise EpsnetProtocolError(
            f"Unsupported register zone {register!r} for {context!r}"
        ) from err


def _zone_code(point: PointLike) -> int:
    return _zone_code_for_register(point.register, context=point.name)


def _byte_size(point: PointLike) -> int:
    if point.value_kind is ValueKind.BOOL:
        return 1
    normalized = point.data_type.upper()
    try:
        return _BYTE_SIZES[normalized]
    except KeyError as err:
        raise EpsnetProtocolError(
            f"Unsupported data type {point.data_type!r} for {point.name!r}"
        ) from err


def _encode_value(point: PointLike, value: ScalarValue) -> bytes:
    if point.value_kind is ValueKind.BOOL:
        flag = bool(value)
        if point.bit is None:
            return bytes((1 if flag else 0,))
        return bytes((((1 << point.bit) if flag else 0),))

    normalized = point.data_type.upper()
    if point.value_kind is ValueKind.FLOAT:
        if normalized == "LREAL":
            return struct.pack("<d", float(value))
        return struct.pack("<f", float(value))

    if point.value_kind is ValueKind.INT:
        integer = int(value)
        size = _byte_size(point)
        return integer.to_bytes(
            size,
            byteorder="little",
            signed=normalized in _SIGNED_INT_TYPES,
        )

    raise EpsnetProtocolError(f"Unsupported writable value kind {point.value_kind!r}")


def _decode_value(point: PointLike, payload: bytes) -> ScalarValue:
    if len(payload) != _byte_size(point):
        raise EpsnetProtocolError(
            f"Unexpected payload size for {point.name!r}: {len(payload)}"
        )

    if point.value_kind is ValueKind.BOOL:
        byte_value = payload[0]
        if point.bit is None:
            return bool(byte_value)
        return bool(byte_value & (1 << point.bit))

    normalized = point.data_type.upper()
    if point.value_kind is ValueKind.FLOAT:
        if normalized == "LREAL":
            return float(struct.unpack("<d", payload)[0])
        return float(struct.unpack("<f", payload)[0])

    if point.value_kind is ValueKind.INT:
        return int.from_bytes(
            payload,
            byteorder="little",
            signed=normalized in _SIGNED_INT_TYPES,
        )

    return payload.rstrip(b"\x00").decode("utf-8", errors="replace")


def _point_end_address(point: PointLike) -> int:
    return point.address + _byte_size(point)


def _build_read_inner(point: PointLike) -> bytes:
    if point.address < 0 or point.address > 0xFFFF:
        raise EpsnetProtocolError(
            f"Direct short EPSNET read only supports 16-bit addresses: {point.name!r}"
        )
    data = (
        _READ_PREFIX
        + bytes((_zone_code(point),))
        + point.address.to_bytes(2, byteorder="little")
        + bytes((_byte_size(point),))
    )
    return _build_variable_frame(control=0x00, address=0x01, data=data)


def _build_raw_read_inner(register: str, *, address: int, length: int) -> bytes:
    if address < 0 or address > 0xFFFF:
        raise EpsnetProtocolError(
            f"Direct short EPSNET read only supports 16-bit addresses: {register!r} {address}"
        )
    if length <= 0 or length > 0xFF:
        raise EpsnetProtocolError(
            f"Direct short EPSNET raw read length must be between 1 and 255 bytes: {length}"
        )
    data = (
        _READ_PREFIX
        + bytes((_zone_code_for_register(register, context=f"{register} span"),))
        + address.to_bytes(2, byteorder="little")
        + bytes((length,))
    )
    return _build_variable_frame(control=0x00, address=0x01, data=data)


def _build_write_inner(point: PointLike, value: ScalarValue) -> bytes:
    if point.address < 0 or point.address > 0xFFFF:
        raise EpsnetProtocolError(
            f"Direct short EPSNET write only supports 16-bit addresses: {point.name!r}"
        )
    if point.value_kind is ValueKind.BOOL and point.bit is not None:
        if not 0 <= point.bit <= 7:
            raise EpsnetProtocolError(f"Bit number out of range for {point.name!r}: {point.bit}")
        data = (
            _WRITE_BIT_PREFIX
            + bytes((_zone_code(point),))
            + point.address.to_bytes(2, byteorder="little")
            + bytes((point.bit | (0x80 if value else 0),))
        )
        return _build_variable_frame(control=0x00, address=0x01, data=data)
    payload = _encode_value(point, value)
    data = (
        _WRITE_PREFIX
        + bytes((_zone_code(point),))
        + point.address.to_bytes(2, byteorder="little")
        + bytes((len(payload),))
        + payload
    )
    return _build_variable_frame(control=0x00, address=0x01, data=data)


def _build_discovery_inner(service: int, payload: bytes = b"") -> bytes:
    return _build_variable_frame(
        control=0x00,
        address=0x01,
        data=b"\x6c" + bytes((service,)) + payload,
    )


def _build_discovery_parameter_read(length_byte: int, token: int) -> bytes:
    return _build_discovery_inner(
        _DISCOVERY_PARAMETER_SERVICE,
        bytes((length_byte,)) + token.to_bytes(4, byteorder="big"),
    )


def _parse_read_like_body(response: bytes, *, context: str) -> bytes:
    if response[:1] == b"\x10":  # fixed error frame instead of a read response
        _, _, status = _parse_fixed_frame(response)
        if status == _MGMT_BUSY_STATUS:
            raise PlcBusyError(
                f"PLC busy during {context}; the exclusive session is held "
                "elsewhere (e.g. a just-finished map fetch not yet released)"
            )
        raise EpsnetProtocolError(f"{context} returned status {status:#04x}")
    control, address, payload = _parse_variable_frame(response)
    if (control, address) != (0x01, 0x00):
        raise EpsnetProtocolError(
            f"Unexpected {context} response header: {control:#04x}, {address:#04x}"
        )
    if not payload or payload[0] != _READ_RESPONSE_CODE:
        raise EpsnetProtocolError(
            f"Unexpected {context} response code: {response.hex()}"
        )
    return payload[1:]


def _parse_variable_payload(response: bytes, *, context: str) -> bytes:
    control, address, payload = _parse_variable_frame(response)
    if (control, address) != (0x01, 0x00):
        raise EpsnetProtocolError(
            f"Unexpected {context} response header: {control:#04x}, {address:#04x}"
        )
    return payload


def _decode_identity_text(body: bytes) -> str:
    return body.rstrip(b"\x00").decode("ascii", errors="replace")


def _decode_ident_payload(payload: bytes) -> str:
    try:
        start_index = next(
            index for index, byte in enumerate(payload) if 0x20 <= byte <= 0x7E
        )
    except StopIteration as err:
        raise EpsnetProtocolError(f"IDENT payload contained no printable text: {payload.hex()}") from err
    return " ".join(
        payload[start_index:].rstrip(b"\x00\x7f").decode("ascii", errors="replace").split()
    )


def plan_point_read_spans(
    points: tuple[EpsnetPoint, ...], *, max_span_length: int = 0xF0
) -> tuple[EpsnetReadSpan, ...]:
    """Group points into conservative contiguous raw spans by register bank.

    The live wire format addresses only the register bank plus byte offset, so the
    planner merges points by register and contiguous byte coverage, regardless of
    `.pub` storage class (`B`, `F`, ...). Each span is capped to keep the first
    experiment below the suspected grouped-read wire limit.
    """

    if not points:
        return ()
    if max_span_length <= 0 or max_span_length > 0xFF:
        raise ValueError("max_span_length must be between 1 and 255 bytes")

    ordered = sorted(
        points,
        key=lambda point: (
            point.register.upper(),
            point.address,
            _point_end_address(point),
            point.bit if point.bit is not None else -1,
            point.name,
        ),
    )

    spans: list[EpsnetReadSpan] = []
    current_register = ""
    current_start = 0
    current_end = 0
    current_points: list[EpsnetPoint] = []

    def flush_current() -> None:
        if not current_points:
            return
        spans.append(
            EpsnetReadSpan(
                register=current_register,
                start_address=current_start,
                length=current_end - current_start,
                points=tuple(current_points),
            )
        )

    for point in ordered:
        register = point.register.upper()
        point_start = point.address
        point_end = _point_end_address(point)

        if not current_points:
            current_register = register
            current_start = point_start
            current_end = point_end
            current_points = [point]
            continue

        proposed_end = max(current_end, point_end)
        if (
            register != current_register
            or proposed_end - current_start > max_span_length
        ):
            flush_current()
            current_register = register
            current_start = point_start
            current_end = point_end
            current_points = [point]
            continue

        current_end = proposed_end
        current_points.append(point)

    flush_current()
    return tuple(spans)


def _parse_discovery_records(
    body: bytes, *, start_offset: int = 0
) -> tuple[RuntimeDiscoveryRecord, ...]:
    records: list[RuntimeDiscoveryRecord] = []
    cursor = start_offset
    while cursor + 6 <= len(body) and body[cursor] == 0x40:
        records.append(
            RuntimeDiscoveryRecord(
                token=int.from_bytes(body[cursor : cursor + 4], byteorder="big"),
                meta=int.from_bytes(body[cursor + 4 : cursor + 6], byteorder="big"),
            )
        )
        cursor += 6
    return tuple(records)


def _parse_memory_map(body: bytes) -> tuple[int, ...]:
    """All memory areas of the root reply: after the identity text, up to 8 × (BE32 address,
    BE16 length). Unlike the module-scan records, areas outside 0x40xxxxxx are kept."""
    try:
        cursor = body.index(0x00, 1) + 1
    except ValueError:
        return ()
    addresses: list[int] = []
    while cursor + 6 <= len(body) and len(addresses) < _MEMORY_MAP_ENTRIES:
        addresses.append(int.from_bytes(body[cursor : cursor + 4], byteorder="big"))
        cursor += 6
    return tuple(addresses)


def _parse_root_body(body: bytes) -> tuple[str, tuple[RuntimeDiscoveryRecord, ...]]:
    if not body:
        raise EpsnetProtocolError("Root discovery reply was empty")
    declared_length = body[0]
    if declared_length != len(body):
        raise EpsnetProtocolError(
            f"Root discovery length mismatch: declared {declared_length}, got {len(body)}"
        )
    try:
        identity_end = body.index(0x00, 1)
    except ValueError as err:
        raise EpsnetProtocolError("Root discovery identity text was not terminated") from err
    identity = _decode_identity_text(body[1:identity_end])
    records = _parse_discovery_records(body, start_offset=identity_end + 1)
    if len(records) <= _DISCOVERY_GROUP_TABLE_INDEX:
        raise EpsnetProtocolError(
            f"Root discovery did not expose the group table token: {body.hex()}"
        )
    return identity, records


def _parse_group_table(body: bytes) -> tuple[RuntimeDiscoveryRecord, ...]:
    if len(body) < 2:
        raise EpsnetProtocolError("Group table discovery reply was too short")
    records = _parse_discovery_records(body, start_offset=2)
    if not records:
        raise EpsnetProtocolError("Group table discovery reply contained no tokens")
    return records


def _parse_group_header(body: bytes) -> tuple[int, str, int]:
    if len(body) < 3:
        raise EpsnetProtocolError("Group header reply was too short")
    marker = body[0]
    label = _decode_identity_text(body[1:-2].split(b"\x00", 1)[0])
    kind = int.from_bytes(body[-2:], byteorder="big")
    return marker, label, kind


def _extract_child_tokens(chunk: bytes, occupied_count: int) -> tuple[int, ...]:
    if occupied_count == 0:
        return (0,) * _DISCOVERY_SLOT_COUNT
    required_bytes = _DISCOVERY_SLOT_COUNT * 4
    for start in range(0, len(chunk) - required_bytes + 1):
        values = tuple(
            int.from_bytes(chunk[start + offset : start + offset + 4], byteorder="big")
            for offset in range(0, required_bytes, 4)
        )
        non_zero_values = sum(value != 0 for value in values)
        if non_zero_values < occupied_count:
            continue
        if all(value == 0 or value >= 0x40000000 for value in values):
            return values
    raise EpsnetProtocolError("Could not locate the child-token block in the group tail")


def _parse_child_descriptor_pointer(child_record: bytes) -> tuple[int, int] | None:
    if len(child_record) < _DISCOVERY_DESCRIPTOR_POINTER_OFFSET + 6:
        raise EpsnetProtocolError("Child record was too short to contain a descriptor pointer")
    token = int.from_bytes(
        child_record[
            _DISCOVERY_DESCRIPTOR_POINTER_OFFSET : _DISCOVERY_DESCRIPTOR_POINTER_OFFSET + 4
        ],
        byteorder="big",
    )
    length = int.from_bytes(
        child_record[
            _DISCOVERY_DESCRIPTOR_POINTER_OFFSET + 4 : _DISCOVERY_DESCRIPTOR_POINTER_OFFSET + 6
        ],
        byteorder="big",
    )
    if token == 0 and length == 0:
        return None
    return token, length


def _split_exact_descriptor_text(
    text: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    parts = text.split()
    if len(parts) < 4:
        return None, None, None, None
    display_name = " ".join(parts[:-3]) or None
    compact_model_code, instance_code, suffix_code = parts[-3:]
    return display_name, compact_model_code, instance_code, suffix_code


def _parse_exact_descriptor(
    *, token: int, length: int, body: bytes
) -> RuntimeExactDescriptor:
    if len(body) != length:
        raise EpsnetProtocolError(
            f"Descriptor length mismatch for {token:#010x}: declared {length}, got {len(body)}"
        )
    if len(body) < 8:
        raise EpsnetProtocolError(f"Descriptor body was too short for {token:#010x}")
    text = " ".join(body[6:-2].rstrip(b"\x00").decode("ascii", errors="replace").split())
    display_name, compact_model_code, instance_code, suffix_code = _split_exact_descriptor_text(
        text
    )
    return RuntimeExactDescriptor(
        token=token,
        length=length,
        family_word=int.from_bytes(body[0:2], byteorder="big"),
        runtime_unit_code=int.from_bytes(body[2:4], byteorder="little"),
        reserved_word=int.from_bytes(body[4:6], byteorder="big"),
        text=text,
        display_name=display_name,
        compact_model_code=compact_model_code,
        instance_code=instance_code,
        suffix_code=suffix_code,
        trailer_word=int.from_bytes(body[-2:], byteorder="big"),
        raw_hex=body.hex(),
    )


def _parse_group_devices(
    *,
    group_token: int,
    label: str,
    marker: int,
    kind: int,
    chunk1: bytes,
    chunk2: bytes,
) -> tuple[str, tuple[int, ...], tuple[RuntimeDiscoveredDevice, ...]]:
    minimum_chunk1 = 8 + _DISCOVERY_SLOT_COUNT + (_DISCOVERY_SLOT_COUNT * 2)
    if len(chunk1) < minimum_chunk1:
        raise EpsnetProtocolError("First group-tail chunk was too short")
    header = chunk1[:8].hex()
    slot_map = tuple(chunk1[8 : 8 + _DISCOVERY_SLOT_COUNT])
    type_codes_offset = 8 + _DISCOVERY_SLOT_COUNT
    type_codes = tuple(
        int.from_bytes(
            chunk1[type_codes_offset + index : type_codes_offset + index + 2],
            byteorder="big",
        )
        for index in range(0, _DISCOVERY_SLOT_COUNT * 2, 2)
    )
    occupied_count = sum(slot != 0x7F for slot in slot_map)
    child_tokens = _extract_child_tokens(chunk2, occupied_count)
    slot_by_runtime_index = {
        runtime_index: slot_index
        for slot_index, runtime_index in enumerate(slot_map)
        if runtime_index != 0x7F
    }
    devices = tuple(
        RuntimeDiscoveredDevice(
            key=f"group_{group_token:08x}_slot_{slot_by_runtime_index[runtime_index]:02d}",
            group_token=group_token,
            group_label=label,
            group_marker=marker,
            group_kind=kind,
            runtime_index=runtime_index,
            slot_index=slot_by_runtime_index[runtime_index],
            child_token=child_tokens[runtime_index],
            runtime_type_code=type_codes[runtime_index],
            model_info=lookup_runtime_model_info(type_codes[runtime_index]),
        )
        for runtime_index in sorted(slot_by_runtime_index)
    )
    return header, slot_map, devices


_TMODULE1_HEADER_LENGTH = 6
_TMODULE1_RECORD_LENGTH = 20
_TMODULE1_MARKER = 0x13
_TMODULE1_LOG_CPU = 0x0101
_TMODULE1_LOG_CIB = 0x0110


def _parse_tmodule1(body: bytes) -> tuple[tuple[int, int, int, int, int, int, int], ...]:
    """Parse the TmodulE1 table into one record per bus entry.

    Each record is ``(rack, addr, log_type, in_len, out_len, off_in, off_out)``.
    The offsets are byte addresses in the PLC's full data image.
    """
    records: list[tuple[int, int, int, int, int, int, int]] = []
    cursor = _TMODULE1_HEADER_LENGTH
    while cursor + _TMODULE1_RECORD_LENGTH <= len(body):
        chunk = body[cursor : cursor + _TMODULE1_RECORD_LENGTH]
        cursor += _TMODULE1_RECORD_LENGTH
        if chunk[0] != _TMODULE1_MARKER:
            break
        log_type = int.from_bytes(chunk[4:6], byteorder="little")
        rack = chunk[2]
        addr = chunk[3]
        in_len = int.from_bytes(chunk[6:8], byteorder="little")
        out_len = int.from_bytes(chunk[8:10], byteorder="little")
        off_in = int.from_bytes(chunk[10:14], byteorder="little")
        off_out = int.from_bytes(chunk[14:18], byteorder="little")
        records.append((rack, addr, log_type, in_len, out_len, off_in, off_out))
    return tuple(records)


_PROGRAM_IMAGE_ENTRY = 0  # memory-map entries seen on the PLCs tested so far
_PROGRAM_TABLES_ENTRY = 7
_MEMORY_MAP_ENTRIES = 8
_TABLE_AREA_PROBES = 3  # tables checked before a memory area counts as the table area


class _SessionProgramSource:
    """Program image and table area read from PLC memory, in blocks, with a log.

    ``reads`` keeps every block read (area -> offset -> bytes) for diagnostics.
    """

    def __init__(self, read, image_address: int, table_address: int, table_limit: int) -> None:
        self._read = read
        self.image_address = image_address
        self.table_address = table_address
        self._table_limit = table_limit
        self.reads: dict[str, dict[int, bytes]] = {"image": {}, "tables": {}}

    def image(self, offset: int, length: int) -> bytes:
        data = self._read(self.image_address + offset, length)
        self.reads["image"][offset] = data
        return data

    def tables(self, offset: int, length: int) -> bytes:
        block = _DISCOVERY_MAX_READ_LENGTH
        blocks = self.reads["tables"]
        out = bytearray()
        position = offset
        while position < offset + length:
            start = position - position % block
            if start not in blocks:
                size = min(block, self._table_limit - start)
                if size <= 0:
                    break
                blocks[start] = self._read(self.table_address + start, size)
            chunk = blocks[start]
            out += chunk[position - start : offset + length - start]
            if len(chunk) < block:
                break
            position = start + block
        return bytes(out)


def _plausible_storage_total(size: int) -> int | None:
    """Whole image length for a header size field, or ``None`` if implausible."""
    if not 0 < size <= _STORAGE_MAX_BYTES - _STORAGE_HEADER_LEN:
        return None
    return size + _STORAGE_HEADER_LEN


def _is_erased(data: bytes | bytearray) -> bool:
    """True for a non-empty run of erased flash (all ``0xff``)."""
    return bool(data) and data.count(_STORAGE_ERASED) == len(data)


# Station address the vendor's own network search puts in its identity request.
_SEARCH_ADDRESS = 0x7E
_SEARCH_TIMEOUT = 1.0


@dataclass(frozen=True, slots=True)
class DiscoveredPlc:
    """One PLC that answered the network search."""

    host: str
    ident: str  # the PLC's own identity text: type and version

    @property
    def model(self) -> str:
        """The PLC type (first word of the identity text)."""
        return self.ident.split(" ", 1)[0]


def plc_model(ident: str | None) -> str:
    """PLC type from an identity text, or an empty string."""
    return (ident or "").split(" ", 1)[0]


def _parse_search_reply(datagram: bytes) -> str | None:
    """Identity text of a search reply, or ``None`` for anything else."""
    try:
        _sequence, inner = _parse_wrapper(datagram)
        _control, _address, payload = _parse_variable_frame(inner)
        return _decode_ident_payload(payload) or None
    except EpsnetProtocolError:
        return None


def _host_sort_key(host: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in host.split("."))
    except ValueError:
        return (0,)


def _search_blocking(
    addresses: tuple[str, ...], port: int, timeout: float
) -> list[DiscoveredPlc]:
    request = _wrap_frame(
        1, _build_fixed_frame(0x00, _SEARCH_ADDRESS, _IDENT_SERVICE)
    )
    found: dict[str, str] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        sent = 0
        for address in addresses:
            try:
                sock.sendto(request, (address, port))
                sent += 1
            except OSError as err:
                LOGGER.debug("PLC search could not send to %s: %s", address, err)
        if not sent:
            return []
        deadline = monotonic() + timeout
        while (remaining := deadline - monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                datagram, sender = sock.recvfrom(1024)
            except OSError:  # includes the timeout
                break
            ident = _parse_search_reply(datagram)
            if ident:
                found.setdefault(sender[0], ident)
    return [
        DiscoveredPlc(host, found[host]) for host in sorted(found, key=_host_sort_key)
    ]


async def async_search_plcs(
    addresses,
    *,
    port: int = 61682,
    timeout: float = _SEARCH_TIMEOUT,
) -> list[DiscoveredPlc]:
    """Find PLCs on the local network.

    Broadcasts one identity request to each of ``addresses`` (broadcast
    addresses of the local networks) and collects the PLCs that answer within
    ``timeout`` seconds. Read-only; takes no session on the PLC. Only PLCs on
    the same network segment can answer.
    """
    try:
        return await asyncio.to_thread(
            _search_blocking, tuple(str(a) for a in addresses), port, timeout
        )
    except OSError as err:
        LOGGER.debug("PLC search failed: %s", err)
        return []


class EpsnetUdpClient:
    """Minimal direct EPSNET UDP client for the current supported entity set."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        local_port: int,
        timeout: float = 2.0,
    ) -> None:
        self._host = host
        self._port = port
        self._local_port = local_port
        self._timeout = timeout
        self._sequence = 1
        self._lock = asyncio.Lock()
        self._last_read_metrics: EpsnetReadMetrics | None = None

    @property
    def last_read_metrics(self) -> EpsnetReadMetrics | None:
        """Return metrics for the most recent successful read batch."""
        return self._last_read_metrics

    def _record_read_metrics(
        self,
        *,
        duration_seconds: float,
        point_count: int,
        packet_count: int,
        mode: str,
        span_count: int | None = None,
        block_min_seconds: float | None = None,
        block_avg_seconds: float | None = None,
        block_max_seconds: float | None = None,
    ) -> EpsnetReadMetrics:
        metrics = EpsnetReadMetrics(
            duration_seconds=duration_seconds,
            point_count=point_count,
            packet_count=packet_count,
            mode=mode,
            span_count=span_count,
            block_min_seconds=block_min_seconds,
            block_avg_seconds=block_avg_seconds,
            block_max_seconds=block_max_seconds,
        )
        self._last_read_metrics = metrics
        return metrics

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence = (self._sequence + 1) & 0xFFFF
        return sequence

    def _open_socket(self, *, ephemeral: bool = False) -> socket.socket:
        """Create and configure a blocking UDP socket for one exchange session.

        ``ephemeral=True`` skips the configured local port. Use it for anything
        that opens the PLC's session (discovery ``6c 01``, parameter/file/login
        services, the map fetch): the PLC holds that single session for the
        sender's address and port for as long as packets keep arriving from it,
        so opening it from the fixed port the poll uses would lock every other
        client out for as long as Home Assistant runs.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(self._timeout)
        if self._local_port != 0 and not ephemeral:
            try:
                sock.bind(("", self._local_port))
            except OSError as err:
                sock.close()
                raise EpsnetError(f"Failed to bind to local port {self._local_port}: {err}") from err
        sock.connect((self._host, self._port))
        return sock

    def _exchange_blocking_on_socket(self, sock: socket.socket, inner: bytes) -> bytes:
        sequence = self._next_sequence()
        datagram = _wrap_frame(sequence, inner)
        deadline = monotonic() + self._timeout
        sock.send(datagram)

        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise EpsnetError("Timed out while waiting for EPSNET reply")
            sock.settimeout(remaining)
            try:
                response = sock.recv(4096)
            except (TimeoutError, socket.timeout) as err:
                raise EpsnetError("Timed out while waiting for EPSNET reply") from err
            
            if response == _ACK:
                return response

            received_sequence, inner_response = _parse_wrapper(response)
            if received_sequence == sequence:
                return inner_response

    def _hello_discovery_on_socket(self, sock: socket.socket) -> None:
        hello_response = self._exchange_blocking_on_socket(
            sock, _build_fixed_frame(0x00, 0x01, _HELLO_SERVICE)
        )
        if not _is_positive_fixed_ack(hello_response):
            raise EpsnetProtocolError(
                f"HELLO unexpectedly returned {hello_response.hex()}"
            )

    def _read_discovery_block_on_socket(
        self, sock: socket.socket, *, token: int, length: int
    ) -> bytes:
        if length < 0:
            raise EpsnetProtocolError(f"Discovery read length must be non-negative: {length}")
        payload = bytearray()
        offset = 0
        while offset < length:
            chunk_length = min(_DISCOVERY_MAX_READ_LENGTH, length - offset)
            response = self._exchange_blocking_on_socket(
                sock,
                _build_discovery_parameter_read(chunk_length, token + offset),
            )
            payload.extend(
                _parse_read_like_body(
                    response, context=f"discovery block {token + offset:#010x}"
                )
            )
            offset += chunk_length
        return bytes(payload)

    def _enrich_runtime_device_on_socket(
        self, sock: socket.socket, device: RuntimeDiscoveredDevice
    ) -> RuntimeDiscoveredDevice:
        child_record = self._read_discovery_block_on_socket(
            sock,
            token=device.child_token,
            length=_DISCOVERY_CHILD_RECORD_LENGTH,
        )
        descriptor_pointer = _parse_child_descriptor_pointer(child_record)
        exact_descriptor = None
        if descriptor_pointer is not None:
            descriptor_token, descriptor_length = descriptor_pointer
            descriptor_body = self._read_discovery_block_on_socket(
                sock,
                token=descriptor_token,
                length=descriptor_length,
            )
            exact_descriptor = _parse_exact_descriptor(
                token=descriptor_token,
                length=descriptor_length,
                body=descriptor_body,
            )
        return replace(
            device,
            child_record_hex=child_record.hex(),
            exact_descriptor=exact_descriptor,
        )

    async def _exchange(self, inner: bytes) -> bytes:
        async with self._lock:
            try:
                return await asyncio.to_thread(self._exchange_blocking, inner)
            except OSError as err:
                raise EpsnetError(str(err)) from err

    def _exchange_blocking(self, inner: bytes) -> bytes:
        with self._open_socket() as sock:
            return self._exchange_blocking_on_socket(sock, inner)

    async def identify(self) -> bytes:
        """Return the raw IDENT response payload."""
        response = await self._exchange(_build_fixed_frame(0x00, 0x01, _IDENT_SERVICE))
        if response == _ACK:
            raise EpsnetProtocolError("IDENT unexpectedly returned ACK")
        return _parse_variable_payload(response, context="IDENT")

    async def read_discovery_block(self, token: int, length: int) -> bytes:
        """Read one arbitrary public-discovery block by token and declared length."""
        async with self._lock:
            try:
                return await asyncio.to_thread(
                    self._read_discovery_block_blocking, token, length
                )
            except OSError as err:
                raise EpsnetError(str(err)) from err

    def _read_discovery_block_blocking(self, token: int, length: int) -> bytes:
        with self._open_socket(ephemeral=True) as sock:
            self._hello_discovery_on_socket(sock)
            return self._read_discovery_block_on_socket(sock, token=token, length=length)

    async def async_login(self, username: str = "admin", password: str = "") -> bool:
        """Login to the PLC for file access."""
        async with self._lock:
            return await asyncio.to_thread(self._login_blocking, username, password)

    def _login_blocking(self, username: str, password: str) -> bool:
        # Service 0x49: username\x00password\x00
        payload = username.encode("ascii") + b"\x00" + password.encode("ascii") + b"\x00"
        request = _build_variable_frame(0x00, 0x01, bytes((_LOGIN_SERVICE,)) + payload)
        
        with self._open_socket(ephemeral=True) as sock:
            self._hello_discovery_on_socket(sock)
            try:
                response = self._exchange_blocking_on_socket(sock, request)
                # Any response is good? Usually a fixed ACK or variable frame 0x09
                return True
            except Exception:
                return False

    def _get_file_blocking(self, filename: str) -> bytes:
        # 1. Open File via Service 0x6C, Zone 0x28, Mode 0x02
        # Command: 0x6c 0x28 0x02 0x04 0x00 00 00 00 filename\x00
        open_header = bytes([_FILE_SERVICE, _FILE_ZONE, 0x02, 0x04, 0x00, 0x00, 0x00, 0x00])
        payload = open_header + filename.encode("ascii") + b"\x00"
        request = _build_variable_frame(0x00, 0x01, payload)
        
        with self._open_socket(ephemeral=True) as sock:
            self._hello_discovery_on_socket(sock)
            try:
                response = self._exchange_blocking_on_socket(sock, request)
                _, _, body = _parse_variable_frame(response)
                # body[0] is 0x08 (READ), body[1] is 0x28 (ZONE)
                # body[2] is handle
                if len(body) < 3:
                     raise EpsnetProtocolError(f"Failed to get file handle: {response.hex()}")
                handle = body[2]
            except Exception as err:
                 LOGGER.debug("Could not open file %s: %s", filename, err)
                 return b""
            
            # 2. Read File
            file_data = bytearray()
            chunk_index = 0
            while True:
                # Command: 0x6c 0x28 handle chunk_index chunk_size
                read_payload = bytes([_FILE_SERVICE, _FILE_ZONE, handle, chunk_index, 200])
                read_request = _build_variable_frame(0x00, 0x01, read_payload)
                try:
                    read_response = self._exchange_blocking_on_socket(sock, read_request)
                    _, _, read_body = _parse_variable_frame(read_response)
                    
                    # read_body[0] is 0x08, read_body[1] is 0x28, read_body[2] is handle
                    chunk = read_body[3:]
                    if not chunk:
                        break
                    file_data.extend(chunk)
                    chunk_index += 1
                    if len(chunk) < 200: # Last chunk
                        break
                except Exception:
                    break
            
            # 3. Close File (Best effort)
            close_payload = bytes([_FILE_SERVICE, _FILE_ZONE, handle, 0x00, 0x00]) # Example close
            try:
                self._exchange_blocking_on_socket(sock, _build_variable_frame(0x00, 0x01, close_payload))
            except Exception:
                pass
                
            return bytes(file_data)

    async def async_get_file(self, filename: str) -> bytes:
        """Download a file from the PLC using the 0x49/0x4A/0x4B services."""
        async with self._lock:
            return await asyncio.to_thread(self._get_file_blocking, filename)

    async def async_fetch_pub_from_plc(self) -> tuple[str, tuple[PubEntry, ...]]:
        """Fetch the on-PLC ``.pub`` map directly from the CPU.

        Returns ``(plc_ident, entries)``. Uses the exclusive management session
        (``HELLO`` -> ``6c 01`` -> ``6c 23`` storage walk); raises
        :class:`PlcBusyError` if another client holds that session.
        Runtime polling is unaffected. Do this once at setup and cache the result.
        """
        async with self._lock:
            return await asyncio.to_thread(self._fetch_pub_blocking)

    def _fetch_pub_blocking(self) -> tuple[str, tuple[PubEntry, ...]]:
        with self._open_socket(ephemeral=True) as sock:
            ident = self._establish_mgmt_with_retry(sock)
            # Seek/init: reply reports the project-storage size.
            init_reply = self._exchange_blocking_on_socket(
                sock,
                _build_variable_frame(
                    0x00,
                    _MGMT_ADDRESS,
                    bytes(
                        (_FILE_SERVICE, _STORAGE_SERVICE, _STORAGE_INIT_LEN, _STORAGE_AREA, 0, 0, 0)
                    ),
                ),
            )
            total_size = self._parse_storage_size(init_reply)
            if total_size is None:
                total_size = self._storage_size_from_image_header(sock)
            stop_at_erased = total_size is None
            if total_size is None:
                LOGGER.warning(
                    "Could not read the project-storage size; reading up to the "
                    "first erased area (at most %d bytes)",
                    _STORAGE_MAX_BYTES,
                )
                total_size = _STORAGE_MAX_BYTES
            self._fetch_total_bytes = total_size
            self._fetch_read_bytes = 0

            # Read exactly `total_size` bytes, pipelining _STORAGE_BATCH blocks per
            # datagram so the fetch is ~5x fewer round-trips.
            storage = bytearray()
            offset = 0
            while offset < total_size:
                batch_start = len(storage)
                offsets = [
                    offset + i * _STORAGE_BLOCK_LEN
                    for i in range(_STORAGE_BATCH)
                    if offset + i * _STORAGE_BLOCK_LEN < total_size
                ]
                for block in self._read_pub_storage_batch(sock, offsets):
                    # Keep byte alignment even if a block was dropped: a zero-fill
                    # only costs the (rare) PUB_ line straddling that gap.
                    storage.extend(
                        block if block is not None else b"\x00" * _STORAGE_BLOCK_LEN
                    )
                offset += len(offsets) * _STORAGE_BLOCK_LEN
                if stop_at_erased and _is_erased(storage[batch_start:]):
                    total_size = batch_start
                    self._fetch_total_bytes = total_size
                self._fetch_read_bytes = min(offset, total_size)

            storage = storage[:total_size]
            entries = self._extract_pub_entries(bytes(storage))
            LOGGER.debug(
                "Fetched .pub from PLC %s: %d bytes storage (%d datagrams), %d points",
                ident,
                len(storage),
                -(-total_size // (_STORAGE_BATCH * _STORAGE_BLOCK_LEN)),
                len(entries),
            )
            return ident, entries

    def _parse_storage_size(self, init_reply: bytes) -> int | None:
        """Total project-storage size (bytes) from the ``6c 23 40`` init reply.

        The read-response body carries a 4-byte little-endian size at a fixed
        offset. It excludes the image's 0x40-byte header, which is added back so
        the last 64 bytes are read too. ``None`` if it can't be parsed or looks
        implausible.
        """
        try:
            _control, _address, body = _parse_variable_frame(init_reply)
            size = int.from_bytes(
                body[_STORAGE_SIZE_OFFSET : _STORAGE_SIZE_OFFSET + 4], "little"
            )
        except (EpsnetProtocolError, IndexError):
            size = 0
        return _plausible_storage_total(size)

    def _storage_size_from_image_header(self, sock: socket.socket) -> int | None:
        """Total size from the image's own header (the first block), or ``None``."""
        block = self._read_pub_storage_batch(sock, [0])[0]
        if block is None or not block.startswith(_STORAGE_HEADER_MAGIC):
            return None
        size = int.from_bytes(
            block[_STORAGE_HEADER_SIZE_OFFSET : _STORAGE_HEADER_SIZE_OFFSET + 4], "little"
        )
        return _plausible_storage_total(size)

    @property
    def fetch_progress(self) -> tuple[int, int]:
        """(bytes_read, total_bytes) of the in-flight/last map fetch (0,0 if none)."""
        return (
            getattr(self, "_fetch_read_bytes", 0),
            getattr(self, "_fetch_total_bytes", 0),
        )

    def _read_pub_storage_batch(
        self, sock: socket.socket, offsets: list[int]
    ) -> list[bytes | None]:
        """Read a pipelined batch of 200-byte blocks; result aligns to ``offsets``.

        One datagram carries all the block requests; the reply carries all the
        block frames in order. ``None`` marks a block whose frame was missing or
        an end/error reply. The whole datagram is retried on transient loss.
        """
        inner = b"".join(
            _build_variable_frame(
                0x00,
                _MGMT_ADDRESS,
                bytes((_FILE_SERVICE, _STORAGE_SERVICE, _STORAGE_BLOCK_LEN, _STORAGE_AREA))
                + off.to_bytes(3, "big"),
            )
            for off in offsets
        )
        for _ in range(_STORAGE_READ_RETRIES):
            try:
                resp = self._exchange_blocking_on_socket(sock, inner)
            except EpsnetError:
                continue  # transient VPN loss -> retry the whole datagram
            results: list[bytes | None] = []
            for frame in _split_variable_frames(resp):
                if not frame or frame[0] == 0x10:  # fixed frame -> out-of-range/error
                    results.append(None)
                    continue
                try:
                    _control, _address, body = _parse_variable_frame(frame)
                except EpsnetProtocolError:
                    results.append(None)
                    continue
                if not body or body[0] != _READ_RESPONSE_CODE:
                    results.append(None)
                else:
                    results.append(body[1:])
            # Align to the request count (short reply -> pad with None).
            results += [None] * (len(offsets) - len(results))
            return results[: len(offsets)]
        return [None] * len(offsets)

    def _establish_mgmt_with_retry(self, sock: socket.socket) -> str:
        # Establishing the exclusive management session is inherently flaky right
        # after another op (e.g. the CIB scan): the PLC may still hold the session
        # (0x03 busy, released within ~7s) or drop a UDP reply (HELLO/establish
        # timeout). Retry on either -- the socket discards any stale late replies.
        last_error: EpsnetError | None = None
        for attempt in range(_MGMT_ESTABLISH_ATTEMPTS):
            try:
                return self._establish_mgmt_on_socket(sock)
            except EpsnetError as err:
                last_error = err
                LOGGER.debug(
                    "PLC management establish failed (attempt %d/%d): %s",
                    attempt + 1,
                    _MGMT_ESTABLISH_ATTEMPTS,
                    err,
                )
                sleep(_MGMT_RETRY_DELAY)
        assert last_error is not None
        raise last_error

    def _establish_mgmt_on_socket(self, sock: socket.socket) -> str:
        """HELLO + ``6c 01`` on the management station; returns the PLC identity.

        Raises :class:`PlcBusyError` if the session is held by another client.
        """
        self._exchange_blocking_on_socket(
            sock, _build_fixed_frame(0x00, _MGMT_ADDRESS, _HELLO_SERVICE)
        )
        resp = self._exchange_blocking_on_socket(
            sock,
            _build_variable_frame(
                0x00, _MGMT_ADDRESS, bytes((_FILE_SERVICE, _ESTABLISH_SERVICE))
            ),
        )
        if resp[:1] == b"\x10":  # fixed frame -> status/error, not identity data
            _, _, status = _parse_fixed_frame(resp)
            if status == _MGMT_BUSY_STATUS:
                raise PlcBusyError(
                    "PLC management session is busy (held by another client)"
                )
            raise EpsnetProtocolError(f"Establish returned status {status:#04x}")
        _control, _address, body = _parse_variable_frame(resp)
        # body = <read code 0x08> <NUL-terminated ASCII identity> <binary>
        ident = body[1:].split(b"\x00", 1)[0]
        return ident.decode("ascii", "ignore").strip()

    @staticmethod
    def _extract_pub_entries(storage: bytes) -> tuple[PubEntry, ...]:
        """Extract published-variable definitions from the reassembled storage bytes.

        The storage interleaves the `.pub` text with web/project files; we pull the
        `PUB_*` lines (reassembled contiguously so names are not split at block
        seams) and hand them to the existing `.pub` parser.
        """
        seen: set[bytes] = set()
        lines: list[str] = []
        for match in _PUB_LINE_RE.findall(storage):
            if match in seen:
                continue
            seen.add(match)
            lines.append(" " + match.decode("latin1"))
        return parse_pub_text("\n".join(lines))

    async def debug_read_mapping_table(self) -> None:
        """Read and log the potential mapping table region for development."""
        token = 0x400F0000
        size = 0x1000 # 4KB
        try:
            block = await self.read_discovery_block(token, size)
            LOGGER.info("Mapping Table Region (0x400F0000) Head: %s", block[:256].hex())
            # Look for TmodulE1 markers (usually starts with rack/addr/log)
            # Rack 0, Addr 2, Log 0x0110 is common for CIB
            search_pattern = b"\x00\x02\x10\x01" # Little endian log 0x0110
            pos = block.find(search_pattern)
            if pos != -1:
                 LOGGER.info("Found potential TmodulE1 at +0x%04x: %s", pos, block[pos:pos+32].hex())
        except Exception as err:
            LOGGER.error("Failed to read debug mapping table: %s", err)

    async def discover_runtime_inventory(self) -> RuntimeInventory:
        """Fetch the live CIB device inventory from the PLC.

        The CIB discovery service shares the PLC's exclusive session gate, so a
        scan started right after a just-finished map fetch can transiently see
        ``0x03`` "not established" until the prior session is released (~7s).
        Retry on that transient busy before giving up.
        """
        async with self._lock:
            last_busy: PlcBusyError | None = None
            for _ in range(_MGMT_ESTABLISH_ATTEMPTS):
                try:
                    return await asyncio.to_thread(
                        self._discover_runtime_inventory_blocking
                    )
                except PlcBusyError as err:
                    last_busy = err
                    await asyncio.sleep(_MGMT_RETRY_DELAY)
                except ValueError as err:
                    raise EpsnetProtocolError(str(err)) from err
                except OSError as err:
                    raise EpsnetError(str(err)) from err
            assert last_busy is not None
            raise last_busy

    def _discover_runtime_inventory_blocking(self) -> RuntimeInventory:
        with self._open_socket(ephemeral=True) as sock:
            self._hello_discovery_on_socket(sock)

            identify_response = self._exchange_blocking_on_socket(
                sock, _build_fixed_frame(0x00, 0x01, _IDENT_SERVICE)
            )
            controller_identity = _decode_ident_payload(
                _parse_variable_payload(identify_response, context="IDENT")
            )

            root_response = self._exchange_blocking_on_socket(
                sock,
                _build_discovery_inner(_DISCOVERY_ROOT_SERVICE, b"\x00"),
            )
            root_body = _parse_read_like_body(root_response, context="root discovery")
            raw_reads: list[tuple[str, str]] = [("root", root_body.hex())]
            root_identity, root_records = _parse_root_body(root_body)
            root_tokens = tuple(record.token for record in root_records)
            
            tmodule1_bank = root_tokens[0]
            tmodule1_data = self._read_discovery_block_on_socket(
                sock, token=tmodule1_bank + 0x200, length=240
            )
            LOGGER.debug("TmodulE1 raw (%d bytes): %s", len(tmodule1_data), tmodule1_data.hex())
            raw_reads.append(("tmodule1", tmodule1_data.hex()))
            tmodule1_all_records = _parse_tmodule1(tmodule1_data)
            LOGGER.debug("TmodulE1 all records: %s", tmodule1_all_records)

            cpu_off_in, cpu_off_out = 0, 0
            tmodule1_cib_records = []
            for rack, addr, log_type, in_len, out_len, off_in, off_out in tmodule1_all_records:
                if rack == 0 and addr == 0:
                    cpu_off_in, cpu_off_out = off_in, off_out
                if log_type == _TMODULE1_LOG_CIB:
                    tmodule1_cib_records.append((rack, addr, in_len, out_len, off_in, off_out))

            group_table_token = root_tokens[_DISCOVERY_GROUP_TABLE_INDEX]

            group_table_response = self._exchange_blocking_on_socket(
                sock,
                _build_discovery_parameter_read(0x7A, group_table_token),
            )
            group_table_body = _parse_read_like_body(
                group_table_response, context="group table discovery"
            )
            raw_reads.append(("group_table", group_table_body.hex()))
            group_records = _parse_group_table(group_table_body)
            LOGGER.debug(
                "Group table: %d entries: %s",
                len(group_records),
                [(f"0x{r.token:08x}", f"meta=0x{r.meta:04x}") for r in group_records],
            )

            groups: list[RuntimeCibGroup] = []
            devices: list[RuntimeDiscoveredDevice] = []
            cib_bus_idx = 0
            for group_record in group_records:
                group_token = group_record.token
                meta = group_record.meta
                if meta != _DISCOVERY_GROUP_META:
                    continue

                if cib_bus_idx < len(tmodule1_cib_records):
                    _rack, _addr, in_len, out_len, off_in, off_out = tmodule1_cib_records[cib_bus_idx]
                else:
                    in_len, out_len, off_in, off_out = 0, 0, 0, 0
                cib_bus_idx += 1

                group_header_response = self._exchange_blocking_on_socket(
                    sock,
                    _build_discovery_parameter_read(
                        _DISCOVERY_GROUP_HEADER_LENGTH, group_token
                    ),
                )
                marker, label, kind = _parse_group_header(
                    _parse_read_like_body(
                        group_header_response,
                        context=f"group header {group_token:#010x}",
                    )
                )

                tail_base_token = group_token + _DISCOVERY_GROUP_HEADER_LENGTH
                first_chunk_response = self._exchange_blocking_on_socket(
                    sock,
                    _build_discovery_parameter_read(
                        _DISCOVERY_FIRST_CHUNK_LENGTH,
                        tail_base_token,
                    ),
                )
                second_chunk_response = self._exchange_blocking_on_socket(
                    sock,
                    _build_discovery_parameter_read(
                        _DISCOVERY_SECOND_CHUNK_LENGTH,
                        tail_base_token + _DISCOVERY_FIRST_CHUNK_LENGTH,
                    ),
                )

                chunk1 = _parse_read_like_body(
                    first_chunk_response,
                    context=f"group tail {group_token:#010x} first chunk",
                )
                chunk2 = _parse_read_like_body(
                    second_chunk_response,
                    context=f"group tail {group_token:#010x} second chunk",
                )
                raw_reads.append((f"group_{group_token:08x}_chunk1", chunk1.hex()))
                raw_reads.append((f"group_{group_token:08x}_chunk2", chunk2.hex()))
                header_hex, slot_map, group_devices = _parse_group_devices(
                    group_token=group_token,
                    label=label,
                    marker=marker,
                    kind=kind,
                    chunk1=chunk1,
                    chunk2=chunk2,
                )
                enriched_devices = tuple(
                    self._enrich_runtime_device_on_socket(sock, device)
                    for device in group_devices
                )
                
                groups.append(
                    RuntimeCibGroup(
                        token=group_token,
                        meta=meta,
                        label=label,
                        marker=marker,
                        kind=kind,
                        header_hex=header_hex,
                        slot_map=slot_map,
                        devices=enriched_devices,
                        off_in=off_in,
                        off_out=off_out,
                        in_len=in_len,
                        out_len=out_len,
                    )
                )
                devices.extend(enriched_devices)

            program_layout = self._read_program_layout_on_socket(
                sock, _parse_memory_map(root_body)
            )

        return RuntimeInventory(
            controller_identity=controller_identity,
            root_identity=root_identity,
            root_tokens=root_tokens,
            group_table_token=group_table_token,
            groups=tuple(groups),
            devices=tuple(devices),
            root_records=root_records,
            cpu_off_in=cpu_off_in,
            cpu_off_out=cpu_off_out,
            raw_reads=tuple(raw_reads),
            program_layout=program_layout,
        )

    @staticmethod
    def _check_table_area(read, token: int, offsets: list[int], limit: int) -> str | None:
        """None when the first tables at ``token`` are intact, else why not."""
        if not offsets:
            return "empty table index"
        for offset in offsets:
            try:
                length = struct.unpack_from(">H", read(token + offset, 2))[0]
                if not 0 < length <= 4096 or offset + length + 4 > limit:
                    return f"table at +{offset:#x}: implausible length {length}"
                body = read(token + offset + 2, length + 2)
            except EpsnetProtocolError as err:
                return f"read at +{offset:#x} refused: {err}"
            if len(body) != length + 2 or struct.unpack_from(
                ">H", body, length
            )[0] != table_checksum(length, body[:length]):
                return f"table at +{offset:#x}: checksum mismatch"
        return None

    def _read_program_layout_on_socket(
        self, sock: socket.socket, memory_map: tuple[int, ...]
    ) -> ProgramIoLayout:
        """Read the program's I/O configuration in the open session.

        Never fails the scan: any problem becomes the layout's ``error``.
        """

        def read(address: int, length: int) -> bytes:
            return self._read_discovery_block_on_socket(sock, token=address, length=length)

        def ordered(first: int, exclude: int | None = None) -> list[int]:
            tokens = [t for t in memory_map if t and t != exclude]
            if first < len(memory_map) and memory_map[first] in tokens:
                tokens.remove(memory_map[first])
                tokens.insert(0, memory_map[first])
            return tokens

        try:
            image_address = header = None
            for token in ordered(_PROGRAM_IMAGE_ENTRY):
                try:
                    candidate = read(token, 0x30)
                except EpsnetProtocolError:
                    continue
                if len(candidate) == 0x30 and struct.unpack_from(">H", candidate)[0] == IMAGE_MAGIC:
                    image_address, header = token, candidate
                    break
            if image_address is None or header is None:
                return ProgramIoLayout(error="program image not found in the memory map")
            table_limit = struct.unpack_from(">H", header, 0x26)[0]
            word_table = struct.unpack_from(">H", header, 0x1A)[0]
            index = read(image_address + word_table, 2 * _TABLE_AREA_PROBES)
            probe_offsets = [
                o for o in struct.unpack_from(f">{len(index) // 2}H", index) if o
            ][:_TABLE_AREA_PROBES]

            notes: list[str] = []
            table_address = None
            for token in ordered(_PROGRAM_TABLES_ENTRY, exclude=image_address):
                problem = self._check_table_area(read, token, probe_offsets, table_limit)
                if problem is None:
                    table_address = token
                    break
                notes.append(f"{token:#010x}: {problem}")
            if table_address is None:
                return ProgramIoLayout(
                    image_address=image_address,
                    error="program table area not found in the memory map",
                    notes=tuple(notes),
                )

            source = _SessionProgramSource(read, image_address, table_address, table_limit)
            layout = decode_program_layout(source)
            return replace(
                layout,
                image_address=image_address,
                table_address=table_address,
                reads=source.reads,
                notes=tuple(notes) + layout.notes,
            )
        except (EpsnetError, OSError, ValueError, struct.error) as err:
            LOGGER.debug("Program layout read failed: %s", err)
            return ProgramIoLayout(error=f"program read failed: {err}")

    def _read_point_values_blocking_on_socket(
        self,
        sock: socket.socket,
        points: tuple[EpsnetPoint, ...],
        *,
        batch_started: float,
    ) -> dict[str, ScalarValue]:
        values: dict[str, ScalarValue] = {}
        total_points = len(points)
        for index, point in enumerate(points, start=1):
            try:
                response = self._exchange_blocking_on_socket(sock, _build_read_inner(point))
                if response == _ACK:
                    raise EpsnetProtocolError(f"Read for {point.name!r} returned bare ACK")
                control, address, payload = _parse_variable_frame(response)
                if (control, address) != (0x01, 0x00):
                    raise EpsnetProtocolError(
                        f"Unexpected read response header for {point.name!r}: "
                        f"{control:#04x}, {address:#04x}"
                    )
                if not payload or payload[0] != _READ_RESPONSE_CODE:
                    raise EpsnetProtocolError(
                        f"Unexpected read response code for {point.name!r}: {response.hex()}"
                    )
                values[point.key] = _decode_value(point, payload[1:])
            except Exception as err:
                LOGGER.warning(
                    "EPSNET read failed at point %d/%d for %s (%s) after %.3fs: %s",
                    index,
                    total_points,
                    point.name,
                    _describe_point(point),
                    monotonic() - batch_started,
                    err,
                )
                raise
        return values

    def _read_raw_span_blocking_on_socket(
        self,
        sock: socket.socket,
        *,
        register: str,
        start_address: int,
        length: int,
    ) -> bytes:
        response = self._exchange_blocking_on_socket(
            sock,
            _build_raw_read_inner(register, address=start_address, length=length),
        )
        if response == _ACK:
            raise EpsnetProtocolError(
                f"Raw read for {register} {start_address}+{length} returned bare ACK"
            )
        payload = _parse_read_like_body(
            response,
            context=f"raw read {register} {start_address}+{length}",
        )
        if len(payload) != length:
            raise EpsnetProtocolError(
                f"Raw read {register} {start_address}+{length} returned {len(payload)} bytes"
            )
        return payload

    async def get_point_values(
        self, points: tuple[EpsnetPoint, ...]
    ) -> dict[str, ScalarValue]:
        """Read many points using grouped polling with legacy fallback."""
        if not points:
            return {}

        try:
            return await self.get_point_values_grouped(points)
        except Exception as err:
            LOGGER.warning(
                "EPSNET grouped read failed for %d points; falling back to one-by-one polling: %s",
                len(points),
                err,
            )
            return await self.get_point_values_one_by_one(points)

    async def get_point_values_one_by_one(
        self, points: tuple[EpsnetPoint, ...]
    ) -> dict[str, ScalarValue]:
        """Read many points using the original one-point-per-request path."""
        if not points:
            return {}

        batch_started = monotonic()
        values: dict[str, ScalarValue] = {}
        total_points = len(points)
        block_durations: list[float] = []
        for index, point in enumerate(points, start=1):
            try:
                point_started = monotonic()
                response = await self._exchange(_build_read_inner(point))
                block_durations.append(monotonic() - point_started)
                if response == _ACK:
                    raise EpsnetProtocolError(f"Read for {point.name!r} returned bare ACK")
                control, address, payload = _parse_variable_frame(response)
                if (control, address) != (0x01, 0x00):
                    raise EpsnetProtocolError(
                        f"Unexpected read response header for {point.name!r}: "
                        f"{control:#04x}, {address:#04x}"
                    )
                if not payload or payload[0] != _READ_RESPONSE_CODE:
                    raise EpsnetProtocolError(
                        f"Unexpected read response code for {point.name!r}: {response.hex()}"
                    )
                values[point.key] = _decode_value(point, payload[1:])
            except Exception as err:
                LOGGER.warning(
                    "EPSNET read failed at point %d/%d for %s (%s) after %.3fs: %s",
                    index,
                    total_points,
                    point.name,
                    _describe_point(point),
                    monotonic() - batch_started,
                    err,
                )
                raise

        batch_duration = monotonic() - batch_started
        # In one-by-one mode each point IS its own "block" (one request/reply), so
        # the block metrics stay meaningful: block_count == total points, and the
        # per-block timing is per single-point read. A block_count jump (e.g. 22 ->
        # 248) is itself the signal that this cycle fell back off the grouped path.
        metrics = self._record_read_metrics(
            duration_seconds=batch_duration,
            point_count=total_points,
            packet_count=total_points * 2,
            mode="one_by_one",
            span_count=total_points,
            block_min_seconds=min(block_durations) if block_durations else None,
            block_avg_seconds=(
                sum(block_durations) / len(block_durations) if block_durations else None
            ),
            block_max_seconds=max(block_durations) if block_durations else None,
        )
        if LOGGER.isEnabledFor(logging.DEBUG):
            point_rate = metrics.point_count / batch_duration if batch_duration > 0 else 0.0
            packet_rate = metrics.packet_count / batch_duration if batch_duration > 0 else 0.0
            LOGGER.debug(
                "EPSNET read batch ok: points=%d duration=%.3fs point_rate=%.1f/s packet_rate=%.1f/s mode=one_by_one",
                metrics.point_count,
                metrics.duration_seconds,
                point_rate,
                packet_rate,
            )
        return values

    async def get_point_values_with_socket_reuse(
        self, points: tuple[EpsnetPoint, ...]
    ) -> dict[str, ScalarValue]:
        """Read many points while reusing a single UDP socket for the whole batch."""
        if not points:
            return {}

        async with self._lock:
            try:
                return await asyncio.to_thread(
                    self._get_point_values_with_socket_reuse_blocking,
                    points,
                )
            except OSError as err:
                raise EpsnetError(str(err)) from err

    def _get_point_values_with_socket_reuse_blocking(
        self, points: tuple[EpsnetPoint, ...]
    ) -> dict[str, ScalarValue]:
        batch_started = monotonic()
        with self._open_socket() as sock:
            values = self._read_point_values_blocking_on_socket(
                sock,
                points,
                batch_started=batch_started,
            )

        batch_duration = monotonic() - batch_started
        metrics = self._record_read_metrics(
            duration_seconds=batch_duration,
            point_count=len(points),
            packet_count=len(points) * 2,
            mode="socket_reuse",
        )
        if LOGGER.isEnabledFor(logging.DEBUG):
            point_rate = metrics.point_count / batch_duration if batch_duration > 0 else 0.0
            packet_rate = metrics.packet_count / batch_duration if batch_duration > 0 else 0.0
            LOGGER.debug(
                "EPSNET read batch ok: points=%d duration=%.3fs point_rate=%.1f/s packet_rate=%.1f/s mode=socket_reuse",
                metrics.point_count,
                metrics.duration_seconds,
                point_rate,
                packet_rate,
            )
        return values

    async def get_point_values_grouped(
        self,
        points: tuple[EpsnetPoint, ...],
        *,
        max_span_length: int = 0xF0,
    ) -> dict[str, ScalarValue]:
        """Read many points by coalescing them into grouped raw spans.

        The transport lock is held per span (one request/reply), not for the whole
        batch, so a concurrent small read (the coordinator's fast lane for binary
        inputs) or a write can interleave between spans instead of waiting for a
        full ~0.6 s cycle. Each span uses a short-lived socket, so only one socket
        is ever bound to the local port at a time.
        """
        if not points:
            return {}
        batch_started = monotonic()
        spans = plan_point_read_spans(points, max_span_length=max_span_length)
        values: dict[str, ScalarValue] = {}
        block_durations: list[float] = []
        for index, span in enumerate(spans, start=1):
            async with self._lock:
                try:
                    raw, block_duration = await asyncio.to_thread(self._read_span_blocking, span)
                except OSError as err:
                    raise EpsnetError(str(err)) from err
                except Exception as err:
                    LOGGER.warning(
                        "EPSNET grouped read failed at span %d/%d for %s %d+%d (%d points) after %.3fs: %s",
                        index,
                        len(spans),
                        span.register,
                        span.start_address,
                        span.length,
                        len(span.points),
                        monotonic() - batch_started,
                        err,
                    )
                    raise
            block_durations.append(block_duration)
            for point in span.points:
                offset = point.address - span.start_address
                values[point.key] = _decode_value(
                    point,
                    raw[offset : offset + point.byte_size],
                )
        batch_duration = monotonic() - batch_started
        metrics = self._record_read_metrics(
            duration_seconds=batch_duration,
            point_count=len(points),
            packet_count=len(spans) * 2,
            mode="grouped",
            span_count=len(spans),
            block_min_seconds=min(block_durations) if block_durations else None,
            block_avg_seconds=(
                sum(block_durations) / len(block_durations) if block_durations else None
            ),
            block_max_seconds=max(block_durations) if block_durations else None,
        )
        if LOGGER.isEnabledFor(logging.DEBUG):
            point_rate = metrics.point_count / batch_duration if batch_duration > 0 else 0.0
            packet_rate = metrics.packet_count / batch_duration if batch_duration > 0 else 0.0
            LOGGER.debug(
                "EPSNET read batch ok: points=%d duration=%.3fs point_rate=%.1f/s packet_rate=%.1f/s mode=grouped spans=%d",
                metrics.point_count,
                metrics.duration_seconds,
                point_rate,
                packet_rate,
                metrics.span_count,
            )
        return values

    def _read_span_blocking(self, span: EpsnetReadSpan) -> tuple[bytes, float]:
        """Read one raw span on a fresh socket; returns (payload, seconds)."""
        started = monotonic()
        with self._open_socket() as sock:
            raw = self._read_raw_span_blocking_on_socket(
                sock,
                register=span.register,
                start_address=span.start_address,
                length=span.length,
            )
        return raw, monotonic() - started

    async def get_values(self, entries: tuple[PubEntry, ...]) -> dict[str, ScalarValue]:
        return await self.get_point_values(tuple(entry.to_point() for entry in entries))

    async def get_values_one_by_one(
        self, entries: tuple[PubEntry, ...]
    ) -> dict[str, ScalarValue]:
        return await self.get_point_values_one_by_one(tuple(entry.to_point() for entry in entries))

    async def get_values_with_socket_reuse(
        self, entries: tuple[PubEntry, ...]
    ) -> dict[str, ScalarValue]:
        return await self.get_point_values_with_socket_reuse(
            tuple(entry.to_point() for entry in entries)
        )

    async def get_values_grouped(
        self,
        entries: tuple[PubEntry, ...],
        *,
        max_span_length: int = 0xF0,
    ) -> dict[str, ScalarValue]:
        return await self.get_point_values_grouped(
            tuple(entry.to_point() for entry in entries),
            max_span_length=max_span_length,
        )

    async def set_point_value(self, point: EpsnetPoint, value: ScalarValue) -> None:
        write_started = monotonic()
        try:
            response = await self._exchange(_build_write_inner(point, value))
            if response == _ACK:
                LOGGER.debug(
                    "EPSNET write ok: %s (%s) value=%r duration=%.3fs ack=bare",
                    point.name,
                    _describe_point(point),
                    value,
                    monotonic() - write_started,
                )
                return

            control, address, payload = _parse_variable_frame(response)
            if (control, address) != (0x01, 0x00) or payload not in {b"", b"\x08"}:
                raise EpsnetProtocolError(
                    f"Unexpected write response for {point.name!r}: {response.hex()}"
                )
            LOGGER.debug(
                "EPSNET write ok: %s (%s) value=%r duration=%.3fs ack=variable",
                point.name,
                _describe_point(point),
                value,
                monotonic() - write_started,
            )
        except Exception as err:
            LOGGER.warning(
                "EPSNET write failed for %s (%s) value=%r after %.3fs: %s",
                point.name,
                _describe_point(point),
                value,
                monotonic() - write_started,
                err,
            )
            raise

    async def set_value(self, entry: PubEntry, value: ScalarValue) -> None:
        await self.set_point_value(entry.to_point(), value)

    async def async_close(self) -> None:
        """The client uses short-lived sockets, so nothing is held open."""
