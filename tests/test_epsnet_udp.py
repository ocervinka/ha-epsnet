from __future__ import annotations

import logging
import socket
import struct
import threading

import pytest

from custom_components.epsnet.epsnet_udp import (
    _build_write_inner,
    _ACK,
    _build_discovery_inner,
    _build_discovery_parameter_read,
    _build_fixed_frame,
    _build_raw_read_inner,
    _build_variable_frame,
    _is_positive_fixed_ack,
    _split_variable_frames,
    _wrap_frame,
    EpsnetUdpClient,
    plan_point_read_spans,
)
from custom_components.epsnet import epsnet_udp
from custom_components.epsnet.models import EpsnetPoint, PubEntry


@pytest.fixture(autouse=True)
def _allow_localhost_sockets(socket_enabled):
    """Client integration tests use real localhost UDP; pytest-socket (pulled in
    by pytest-homeassistant-custom-component) blocks sockets by default."""
    yield


class ScriptedUdpServer:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = responses
        self._ready = threading.Event()
        self.requests: list[bytes] = []
        self.failures: list[str] = []
        self.port: int | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(1)

    def _run(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.bind(("127.0.0.1", 0))
                self.port = sock.getsockname()[1]
                self._ready.set()
                idx = 0
                while idx < len(self._responses):
                    data, addr = sock.recvfrom(4096)
                    self.requests.append(data)
                    print(f"DEBUG MOCK RECV: {data.hex()}")
                    
                    # Handle HELLO (fixed frame with service 0x69) automatically
                    # Fixed frame: 10 control address service checksum 16
                    # Wrapper (6 bytes) + Fixed (6 bytes)
                    # [SEQ 2] [02 00] [LEN 2] [10 control address service checksum 16]
                    # indices: 0-1, 2-3, 4-5, 6, 7, 8, 9, 10, 11
                    if len(data) == 12 and data[9] == 0x69:
                         sock.sendto(_ACK, addr)
                         continue
                         
                    response = self._responses[idx]
                    
                    # Fix sequence for wrapped responses
                    if len(response) >= 7 and response[2:4] == b"\x02\x00":
                        seq = data[0:2]
                        response = seq + response[2:]
                    
                    sock.sendto(response, addr)
                    idx += 1
        except Exception as err:  # pragma: no cover - test helper
            self.failures.append(str(err))
            self._ready.set()

    def close(self) -> None:
        self._thread.join(timeout=1)


def _ack_response(sequence: int) -> bytes:
    return _wrap_frame(sequence, _ACK)


def _fixed_ack_response(sequence: int) -> bytes:
    return _wrap_frame(sequence, _build_fixed_frame(0x01, 0x00, 0x00))


def _read_response(sequence: int, body: bytes) -> bytes:
    return _wrap_frame(sequence, _build_variable_frame(0x01, 0x00, b"\x08" + body))


def _variable_response(sequence: int, payload: bytes) -> bytes:
    return _wrap_frame(sequence, _build_variable_frame(0x01, 0x00, payload))


def _child_record_with_descriptor(token: int, length: int) -> bytes:
    return (
        bytes(0x46)
        + token.to_bytes(4, byteorder="big")
        + length.to_bytes(2, byteorder="big")
        + bytes(0x66 - 0x46 - 6)
    )


def _exact_descriptor_body(
    *,
    family_word: int,
    runtime_unit_code: int,
    text: str,
    trailer_word: int,
    length: int = 0x2E,
) -> bytes:
    header = (
        family_word.to_bytes(2, byteorder="big")
        + runtime_unit_code.to_bytes(2, byteorder="little")
        + b"\x00\x00"
    )
    text_bytes = text.encode("ascii")
    padding = length - len(header) - len(text_bytes) - 2
    assert padding >= 0
    return header + text_bytes + bytes(padding) + trailer_word.to_bytes(2, byteorder="big")


@pytest.mark.asyncio
async def test_epsnet_udp_client_reads_bool_and_float_values() -> None:
    grouped_x_body = bytes((0x00,)) + bytes(65) + struct.pack("<f", 26.7)
    server = ScriptedUdpServer(
        responses=[
            _read_response(1, b"\x00"),
            _read_response(2, grouped_x_body),
        ]
    )
    server.start()
    assert server.port is not None

    client = EpsnetUdpClient("127.0.0.1", server.port, local_port=16181, timeout=1.0)
    entries = (
        PubEntry(
            name="SW_room_a_TERM",
            register="X",
            storage="F",
            address=71,
            bit=None,
            data_type="REAL",
            access="PUB_INOUT",
            line_number=1,
        ),
        PubEntry(
            name="INPUT_R2_PIR_1",
            register="X",
            storage="B",
            address=5,
            bit=3,
            data_type="BOOL",
            access="PUB_INOUT",
            line_number=2,
        ),
        PubEntry(
            name="OUTPUT_R0b_light_2",
            register="R",
            storage="B",
            address=21530,
            bit=0,
            data_type="BOOL",
            access="PUB_INOUT",
            line_number=3,
        ),
    )

    try:
        values = await client.get_values(entries)
    finally:
        await client.async_close()
        server.close()

    assert not server.failures
    assert values["SW_room_a_TERM"] == pytest.approx(26.7, abs=0.02)
    assert values["INPUT_R2_PIR_1"] is False
    assert values["OUTPUT_R0b_light_2"] is False
    assert server.requests == [
        _wrap_frame(1, _build_raw_read_inner("R", address=21530, length=1)),
        _wrap_frame(2, _build_raw_read_inner("X", address=5, length=70)),
    ]


@pytest.mark.asyncio
async def test_epsnet_udp_client_writes_bool_commands_and_float_values() -> None:
    server = ScriptedUdpServer(
        responses=[
            bytes.fromhex("010002000001e5"),
            bytes.fromhex("020002000001e5"),
        ]
    )
    server.start()
    assert server.port is not None

    client = EpsnetUdpClient("127.0.0.1", server.port, local_port=16182, timeout=1.0)
    on_entry = PubEntry(
        name="OUTPUT_R0b_light_2_ON",
        register="R",
        storage="B",
        address=21520,
        bit=0,
        data_type="BOOL",
        access="PUB_INOUT",
        line_number=1,
    )
    dimmer_entry = PubEntry(
        name="DIMMER_R1_light_1",
        register="R",
        storage="F",
        address=20609,
        bit=None,
        data_type="REAL",
        access="PUB_INOUT",
        line_number=2,
    )

    try:
        await client.set_value(on_entry, True)
        await client.set_value(dimmer_entry, 42.5)
    finally:
        await client.async_close()
        server.close()

    assert not server.failures
    assert server.requests == [
        bytes.fromhex("01000200000e6808086800016310031054805b16"),
        bytes.fromhex("020002000012680c0c680001630c0381500400002a42b416"),
    ]


@pytest.mark.asyncio
async def test_epsnet_udp_client_reads_generic_runtime_points() -> None:
    server = ScriptedUdpServer(
        responses=[
            bytes.fromhex("01000200000d6807076801000814aed541e116"),
            bytes.fromhex("02000200000a68040468010008020b16"),
        ]
    )
    server.start()
    assert server.port is not None

    client = EpsnetUdpClient("127.0.0.1", server.port, local_port=16185, timeout=1.0)
    points = (
        EpsnetPoint(
            key="room_controller_09_temperature",
            name="WSB2-40 slot 09 temperature",
            register="X",
            storage="F",
            address=71,
            bit=None,
            data_type="REAL",
        ),
        EpsnetPoint(
            key="input_module_03_channel_04",
            name="IM2-140M slot 17 input 04",
            register="X",
            storage="B",
            address=5,
            bit=3,
            data_type="BOOL",
        ),
    )

    try:
        values = await client.get_point_values_one_by_one(points)
    finally:
        await client.async_close()
        server.close()

    assert not server.failures
    assert values == {
        "room_controller_09_temperature": pytest.approx(26.7, abs=0.02),
        "input_module_03_channel_04": False,
    }
    assert client.last_read_metrics is not None
    assert client.last_read_metrics.mode == "one_by_one"
    assert client.last_read_metrics.point_count == 2
    assert client.last_read_metrics.packet_count == 4
    assert server.requests == [
        bytes.fromhex("01000200000e6808086800016c0b00470004c316"),
        bytes.fromhex("02000200000e6808086800016c0b000500017e16"),
    ]


@pytest.mark.asyncio
async def test_epsnet_udp_client_falls_back_to_one_by_one_when_grouped_read_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = ScriptedUdpServer(
        responses=[
            _ack_response(1),
            bytes.fromhex("02000200000d6807076801000814aed541e116"),
            bytes.fromhex("03000200000a68040468010008020b16"),
        ]
    )
    server.start()
    assert server.port is not None

    client = EpsnetUdpClient("127.0.0.1", server.port, local_port=16189, timeout=1.0)
    points = (
        EpsnetPoint(
            key="room_controller_09_temperature",
            name="WSB2-40 slot 09 temperature",
            register="X",
            storage="F",
            address=71,
            bit=None,
            data_type="REAL",
        ),
        EpsnetPoint(
            key="input_module_03_channel_04",
            name="IM2-140M slot 17 input 04",
            register="X",
            storage="B",
            address=5,
            bit=3,
            data_type="BOOL",
        ),
    )

    try:
        caplog.set_level(logging.WARNING)
        values = await client.get_point_values(points)
    finally:
        await client.async_close()
        server.close()

    assert not server.failures
    assert values == {
        "room_controller_09_temperature": pytest.approx(26.7, abs=0.02),
        "input_module_03_channel_04": False,
    }
    assert client.last_read_metrics is not None
    assert client.last_read_metrics.mode == "one_by_one"
    assert "falling back to one-by-one polling" in caplog.text
    assert server.requests == [
        _wrap_frame(1, _build_raw_read_inner("X", address=5, length=70)),
        bytes.fromhex("02000200000e6808086800016c0b00470004c316"),
        bytes.fromhex("03000200000e6808086800016c0b000500017e16"),
    ]


def test_plan_point_read_spans_groups_points_by_register_and_length() -> None:
    points = (
        EpsnetPoint(
            key="y0",
            name="Y channel",
            register="Y",
            storage="B",
            address=1,
            bit=0,
            data_type="BOOL",
        ),
        EpsnetPoint(
            key="bool_a",
            name="Bool A",
            register="X",
            storage="B",
            address=5,
            bit=1,
            data_type="BOOL",
        ),
        EpsnetPoint(
            key="real_b",
            name="Real B",
            register="X",
            storage="F",
            address=8,
            bit=None,
            data_type="REAL",
        ),
        EpsnetPoint(
            key="bool_c",
            name="Bool C",
            register="X",
            storage="B",
            address=6,
            bit=0,
            data_type="BOOL",
        ),
        EpsnetPoint(
            key="x_far",
            name="Far X",
            register="X",
            storage="B",
            address=30,
            bit=None,
            data_type="BOOL",
        ),
    )

    spans = plan_point_read_spans(points, max_span_length=7)

    assert [(span.register, span.start_address, span.length) for span in spans] == [
        ("X", 5, 7),
        ("X", 30, 1),
        ("Y", 1, 1),
    ]
    assert [[point.key for point in span.points] for span in spans] == [
        ["bool_a", "bool_c", "real_b"],
        ["x_far"],
        ["y0"],
    ]


@pytest.mark.asyncio
async def test_epsnet_udp_client_reads_points_with_socket_reuse() -> None:
    server = ScriptedUdpServer(
        responses=[
            bytes.fromhex("01000200000d6807076801000814aed541e116"),
            bytes.fromhex("02000200000a68040468010008020b16"),
        ]
    )
    server.start()
    assert server.port is not None

    client = EpsnetUdpClient("127.0.0.1", server.port, local_port=16187, timeout=1.0)
    points = (
        EpsnetPoint(
            key="room_controller_09_temperature",
            name="WSB2-40 slot 09 temperature",
            register="X",
            storage="F",
            address=71,
            bit=None,
            data_type="REAL",
        ),
        EpsnetPoint(
            key="input_module_03_channel_04",
            name="IM2-140M slot 17 input 04",
            register="X",
            storage="B",
            address=5,
            bit=3,
            data_type="BOOL",
        ),
    )

    try:
        values = await client.get_point_values_with_socket_reuse(points)
    finally:
        await client.async_close()
        server.close()

    assert not server.failures
    assert values == {
        "room_controller_09_temperature": pytest.approx(26.7, abs=0.02),
        "input_module_03_channel_04": False,
    }
    assert server.requests == [
        bytes.fromhex("01000200000e6808086800016c0b00470004c316"),
        bytes.fromhex("02000200000e6808086800016c0b000500017e16"),
    ]


@pytest.mark.asyncio
async def test_epsnet_udp_client_reads_points_grouped_by_span() -> None:
    grouped_body = struct.pack("<f", 26.7) + bytes((0x0A,))
    server = ScriptedUdpServer(
        responses=[
            _read_response(1, grouped_body),
        ]
    )
    server.start()
    assert server.port is not None

    client = EpsnetUdpClient("127.0.0.1", server.port, local_port=16188, timeout=1.0)
    points = (
        EpsnetPoint(
            key="room_controller_09_temperature",
            name="WSB2-40 slot 09 temperature",
            register="X",
            storage="F",
            address=71,
            bit=None,
            data_type="REAL",
        ),
        EpsnetPoint(
            key="input_module_03_channel_02",
            name="IM2-140M slot 17 input 02",
            register="X",
            storage="B",
            address=75,
            bit=1,
            data_type="BOOL",
        ),
        EpsnetPoint(
            key="input_module_03_channel_04",
            name="IM2-140M slot 17 input 04",
            register="X",
            storage="B",
            address=75,
            bit=3,
            data_type="BOOL",
        ),
    )

    try:
        values = await client.get_point_values_grouped(points, max_span_length=5)
    finally:
        await client.async_close()
        server.close()

    assert not server.failures
    assert values == {
        "room_controller_09_temperature": pytest.approx(26.7, abs=0.02),
        "input_module_03_channel_02": True,
        "input_module_03_channel_04": True,
    }
    assert client.last_read_metrics is not None
    assert client.last_read_metrics.mode == "grouped"
    assert client.last_read_metrics.point_count == 3
    assert client.last_read_metrics.packet_count == 2
    assert client.last_read_metrics.span_count == 1
    assert server.requests == [
        _wrap_frame(1, _build_raw_read_inner("X", address=71, length=5)),
    ]


@pytest.mark.asyncio
async def test_epsnet_udp_client_writes_generic_runtime_points() -> None:
    server = ScriptedUdpServer(
        responses=[
            bytes.fromhex("010002000001e5"),
            bytes.fromhex("020002000001e5"),
        ]
    )
    server.start()
    assert server.port is not None

    client = EpsnetUdpClient("127.0.0.1", server.port, local_port=16186, timeout=1.0)
    on_point = EpsnetPoint(
        key="relay_module_05_channel_01_on",
        name="SA2-012M slot 24 relay 01 ON",
        register="R",
        storage="B",
        address=21520,
        bit=0,
        data_type="BOOL",
        writable=True,
    )
    dimmer_point = EpsnetPoint(
        key="dimmer_module_01_channel_01",
        name="DA2-22M slot 21 dimmer 01",
        register="R",
        storage="F",
        address=20609,
        bit=None,
        data_type="REAL",
        writable=True,
    )

    try:
        await client.set_point_value(on_point, True)
        await client.set_point_value(dimmer_point, 42.5)
    finally:
        await client.async_close()
        server.close()

    assert not server.failures
    assert server.requests == [
        bytes.fromhex("01000200000e6808086800016310031054805b16"),
        bytes.fromhex("020002000012680c0c680001630c0381500400002a42b416"),
    ]


@pytest.mark.asyncio
async def test_epsnet_udp_client_discovers_runtime_inventory() -> None:
    root_body = bytes.fromhex(
        "443130302043503230314d4b2056372e3200400f00002fff400100ba003a"
        "4001093c00564001de36002a400100f40062400101560062400100440076"
        "07050000fffc46e0"
    )
    group_table_body = bytes.fromhex(
        "000140010992025e40015382049e40015820049e"
    ) + b"\x00CIB I/O BUS\x00"
    group_header_1 = bytes.fromhex("0d43494220492f4f204255530000000000000004")
    group_header_2 = bytes.fromhex("0843494220492f4f204255530000000000000004")

    slot_map = bytes([0, 1, 2, 3, 4] + [0x7F] * 27)
    type_codes = b"".join(
        code.to_bytes(2, byteorder="big")
        for code in [0x0C25, 0x0C36, 0x0C1D, 0x0C4F, 0x0CED] + [0] * 27
    )
    first_chunk_1 = (
        bytes.fromhex("0000110500000000")
        + slot_map
        + type_codes
        + bytes(0xDC - 8 - 32 - 64)
    )
    first_chunk_2 = bytes.fromhex("0000120000000000") + bytes([0x7F] * 32) + bytes(
        0xDC - 8 - 32
    )
    child_tokens = b"".join(
        token.to_bytes(4, byteorder="big")
        for token in [
            0x40017200,
            0x40017794,
            0x40017002,
            0x40017398,
            0x40016F36,
        ]
        + [0] * 27
    )
    second_chunk_1 = bytes.fromhex("1001100110011001") + child_tokens + bytes(
        0xAC - 8 - 128
    )
    second_chunk_2 = bytes(0xAC)
    exact_descriptor_tokens = [
        0x4001BBB6,
        0x4001BBE4,
        0x4001BC12,
        0x4001BC40,
        0x4001BC6E,
    ]
    child_records = [
        _child_record_with_descriptor(token, 0x2E) for token in exact_descriptor_tokens
    ]
    exact_descriptors = [
        _exact_descriptor_body(
            family_word=0x1001,
            runtime_unit_code=0x0C25,
            text="IM2-140M 11H0100 22277634 1129",
            trailer_word=0x1AA8,
        ),
        _exact_descriptor_body(
            family_word=0x1001,
            runtime_unit_code=0x0C36,
            text="WSB2-40 11H0100 22395679 1135",
            trailer_word=0x6FD3,
        ),
        _exact_descriptor_body(
            family_word=0x1001,
            runtime_unit_code=0x0C1D,
            text="DA2-22M 13H0100 14181247 0928",
            trailer_word=0x8A21,
        ),
        _exact_descriptor_body(
            family_word=0x1001,
            runtime_unit_code=0x0C4F,
            text="SA2-012M 11H0100 22154270 1126",
            trailer_word=0xE2E6,
        ),
        _exact_descriptor_body(
            family_word=0x1001,
            runtime_unit_code=0x0CED,
            text="C-IT-0200I 12H0200 00000030 1136",
            trailer_word=0x73AF,
        ),
    ]

    server = ScriptedUdpServer(
        responses=[
            _variable_response(1, b"\x00\x09\x01\x03\x03CP201MK  B2.77.2"), # IDENT
            _read_response(2, root_body), # ROOT
            _read_response(3, bytes(0x20) + b"\x13\x01\x00\x02\x10\x01\x0a\x00\x02\x00\xb0\x1d\x00\x00\xb0\x3d\x00\x00\x28\x00"), # TmodulE1
            _read_response(4, group_table_body), # GROUP TABLE
            _read_response(5, group_header_1),
            _read_response(6, first_chunk_1),
            _read_response(7, second_chunk_1),
            _read_response(8, child_records[0]),
            _read_response(9, exact_descriptors[0]),
            _read_response(10, child_records[1]),
            _read_response(11, exact_descriptors[1]),
            _read_response(12, child_records[2]),
            _read_response(13, exact_descriptors[2]),
            _read_response(14, child_records[3]),
            _read_response(15, exact_descriptors[3]),
            _read_response(16, child_records[4]),
            _read_response(17, exact_descriptors[4]),
            _read_response(18, group_header_2),
            _read_response(19, first_chunk_2),
            _read_response(20, second_chunk_2),
        ]
    )
    server.start()
    assert server.port is not None

    client = EpsnetUdpClient("127.0.0.1", server.port, local_port=16183, timeout=1.0)
    try:
        inventory = await client.discover_runtime_inventory()
    finally:
        await client.async_close()
        server.close()

    assert not server.failures
    assert inventory.controller_identity == "CP201MK B2.77.2"
    assert inventory.root_identity == "100 CP201MK V7.2"
    assert inventory.group_table_token == 0x4001093C
    assert [(record.token, record.meta) for record in inventory.root_records[:3]] == [
        (0x400F0000, 0x2FFF),
        (0x400100BA, 0x003A),
        (0x4001093C, 0x0056),
    ]
    assert [group.token for group in inventory.groups] == [0x40015382, 0x40015820]
    assert inventory.groups[0].occupied_slot_count == 5
    assert inventory.groups[1].occupied_slot_count == 0
    assert [device.model for device in inventory.devices] == [
        "IM2-140M",
        "WSB2-40",
        "DA2-22M",
        "SA2-012M",
        "C-IT-0200I",
    ]
    assert [device.slot_index for device in inventory.devices] == [0, 1, 2, 3, 4]
    assert [device.child_token for device in inventory.devices] == [
        0x40017200,
        0x40017794,
        0x40017002,
        0x40017398,
        0x40016F36,
    ]
    assert [device.exact_descriptor.text for device in inventory.devices] == [
        "IM2-140M 11H0100 22277634 1129",
        "WSB2-40 11H0100 22395679 1135",
        "DA2-22M 13H0100 14181247 0928",
        "SA2-012M 11H0100 22154270 1126",
        "C-IT-0200I 12H0200 00000030 1136",
    ]
    assert inventory.devices[0].exact_descriptor is not None
    assert inventory.devices[0].exact_descriptor.family_word == 0x1001
    assert inventory.devices[0].exact_descriptor.runtime_unit_code == 0x0C25
    assert inventory.devices[0].exact_descriptor.compact_model_code == "11H0100"
    assert inventory.devices[0].exact_descriptor.instance_code == "22277634"
    assert inventory.devices[0].exact_descriptor.suffix_code == "1129"
    assert inventory.devices[0].child_record_hex is not None
    # Only check critical non-HELLO requests to be resilient to sequence changes
    critical_requests = [
        r for r in server.requests 
        if not (len(r) == 12 and r[9] == 0x69) # Filter auto HELLOs
    ]
    assert len(critical_requests) >= 7
    # IDENT
    assert critical_requests[0].endswith(_build_fixed_frame(0x00, 0x01, 0x6E))
    # ROOT
    assert _build_discovery_inner(0x01, b"\x00") in critical_requests[1]
    # TmodulE1
    assert _build_discovery_parameter_read(240, 0x400F0200) in critical_requests[2]
    # GROUP TABLE
    assert _build_discovery_parameter_read(0x7A, 0x4001093C) in critical_requests[3]


@pytest.mark.asyncio
async def test_epsnet_udp_client_reads_mapping_table_region() -> None:
    token = 0x400F0000
    # Create a dummy block with a TmodulE1 record at offset 0x218
    # Based on CAPTURE-ANALYSIS: 0x400f021a is the second record
    # record is 0x14 bytes (20 bytes)
    # let's put it at 0x200
    payload = bytes(0x200) + b"\x13\x01\x00\x02\x10\x01\x0a\x00\x02\x00\xb0\x1d\x00\x00\xb0\x3d\x00\x00\x28\x00"
    payload += bytes(0x1000 - len(payload))
    
    server = ScriptedUdpServer(
        responses=[
            _fixed_ack_response(1),
            _read_response(2, payload[:0xF0]),
            _read_response(3, payload[0xF0:0x1E0]),
            _read_response(4, payload[0x1E0:0x2D0]),
            # Continue reading until 0x1000... 
            # Actually read_discovery_block reads in chunks.
        ]
    )
    # The client read_discovery_block implementation might need many responses.
    # Let's check how read_discovery_block is implemented.
    token = 0x400220AE
    payload = bytes(range(256)) + bytes(range(4))
    server = ScriptedUdpServer(
        responses=[
            _read_response(1, payload[:0xF0]),
            _read_response(2, payload[0xF0:]),
        ]
    )
    server.start()
    assert server.port is not None

    client = EpsnetUdpClient("127.0.0.1", server.port, local_port=16184, timeout=1.0)
    try:
        block = await client.read_discovery_block(token, len(payload))
    finally:
        await client.async_close()
        server.close()

    assert not server.failures
    assert block == payload
    assert server.requests == [
        _wrap_frame(1, _build_fixed_frame(0x00, 0x01, 0x69)), # HELLO (auto)
        _wrap_frame(2, _build_discovery_parameter_read(0xF0, token)),
        _wrap_frame(3, _build_discovery_parameter_read(0x14, token + 0xF0)),
    ]


def test_positive_fixed_ack_accepts_real_device_hello_reply() -> None:
    assert _is_positive_fixed_ack(bytes.fromhex("100100000116")) is True
    assert _is_positive_fixed_ack(_ACK) is True


# --- On-PLC .pub fetch (6c 23), offline against the scripted fake PLC ---------


def _read_response_body(sequence: int, body: bytes) -> bytes:
    """Wrap a read reply: variable frame with read code 0x08 + body."""
    return _wrap_frame(sequence, _build_variable_frame(0x01, 0x00, b"\x08" + body))


def _storage_batch_response(sequence: int, blocks: list[bytes]) -> bytes:
    """One datagram carrying N block read-reply frames (batched fetch reply)."""
    inner = b"".join(_build_variable_frame(0x01, 0x00, b"\x08" + b) for b in blocks)
    return _wrap_frame(sequence, inner)


def test_split_variable_frames_splits_concatenated_reply() -> None:
    f1 = _build_variable_frame(0x01, 0x00, b"\x08abc")
    f2 = _build_variable_frame(0x01, 0x00, b"\x08defgh")
    f3 = _build_variable_frame(0x01, 0x00, b"\x08")
    frames = _split_variable_frames(f1 + f2 + f3 + b"\x00\x00")  # trailing padding ok
    assert frames == [f1, f2, f3]


def _init_reply_frame(size: int) -> bytes:
    # The unwrapped inner frame `_parse_storage_size` sees: read code 0x08 then a
    # 44-byte header+name field, then the 4-byte LE size (data offset 45).
    return _build_variable_frame(
        0x01, 0x00, b"\x08" + b"\x00" * 44 + size.to_bytes(4, "little")
    )


def test_parse_storage_size_adds_the_image_header() -> None:
    client = EpsnetUdpClient("127.0.0.1", 1, local_port=0)
    # The reported size excludes the image's 0x40-byte header.
    assert client._parse_storage_size(_init_reply_frame(322615)) == 322615 + 0x40


def test_parse_storage_size_gives_none_on_bad_reply() -> None:
    client = EpsnetUdpClient("127.0.0.1", 1, local_port=0)
    # implausible size (0) -> no size, not a crash; the fetch then tries the header
    assert client._parse_storage_size(_init_reply_frame(0)) is None


@pytest.mark.asyncio
async def test_fetch_pub_from_plc_batched_offline() -> None:
    pub_line = b"light_3 R B 100 BOOL PUB_OUT"
    last_line = b"\r\nlight_4 R B 101 BOOL PUB_OUT\r\n"
    total = 400  # exactly two 200-byte blocks -> one batch
    # The last line sits in the image's final 64 bytes, past the reported size.
    storage = pub_line + b"\x00" * (total - len(pub_line) - len(last_line)) + last_line
    reported = total - 0x40
    blocks = [storage[i : i + 200] for i in range(0, total, 200)]
    ident = b"D100 CP201MK V7.2"

    server = ScriptedUdpServer(
        [
            _read_response_body(0, ident),  # 6c 01 establish -> identity
            _read_response_body(0, b"\x00" * 44 + reported.to_bytes(4, "little")),  # 6c 23 40 init -> size
            _storage_batch_response(0, blocks),  # 6c 23 c8 batched blocks
        ]
    )
    server.start()
    try:
        client = EpsnetUdpClient("127.0.0.1", server.port, local_port=0, timeout=1.0)
        got_ident, entries = await client.async_fetch_pub_from_plc()
    finally:
        server.close()

    assert got_ident == "D100 CP201MK V7.2"
    assert [e.name for e in entries] == ["light_3", "light_4"]
    assert client.fetch_progress == (total, total)


def test_open_socket_ephemeral_skips_the_configured_local_port() -> None:
    client = EpsnetUdpClient("127.0.0.1", 9, local_port=16191, timeout=1.0)
    with client._open_socket() as fixed:
        assert fixed.getsockname()[1] == 16191
    with client._open_socket(ephemeral=True) as eph:
        assert eph.getsockname()[1] != 16191


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("_discover_runtime_inventory_blocking", ()),
        ("_read_discovery_block_blocking", (0x400F0200, 0x10)),
        ("_fetch_pub_blocking", ()),
    ],
)
def test_session_opening_requests_never_use_the_poll_port(method, args) -> None:
    """The PLC keeps its single session for the sender's address and port while
    packets keep arriving from it; opening it from the poll's fixed port would
    hold it for as long as Home Assistant polls (locking out other clients)."""
    client = EpsnetUdpClient("127.0.0.1", 9, local_port=16192, timeout=1.0)
    calls: list[dict] = []

    def fake_open_socket(**kwargs):
        calls.append(kwargs)
        raise OSError("stop here")

    client._open_socket = fake_open_socket  # type: ignore[method-assign]
    with pytest.raises(OSError):
        getattr(client, method)(*args)
    assert calls == [{"ephemeral": True}]


def test_bool_writes_touch_only_their_own_bit() -> None:
    def point(bit: int | None) -> EpsnetPoint:
        return EpsnetPoint(
            key="k", name="flag", register="R", storage="B", address=0x1234,
            bit=bit, data_type="BOOL", writable=True,
        )

    # bit BOOLs use the one-bit write: set = bit | 0x80, clear = bit
    assert _build_write_inner(point(5), True)[6:-2] == bytes.fromhex("6310033412" "85")
    assert _build_write_inner(point(5), False)[6:-2] == bytes.fromhex("6310033412" "05")
    # a BOOL that owns its whole byte is still written as a byte
    assert _build_write_inner(point(None), True)[6:-2] == bytes.fromhex("630c03341201" "01")


def _storage_image(files: bytes, total: int, tail: bytes) -> bytes:
    """A storage image of ``total`` bytes: 0x40 header with magic and size,
    ``files`` after it, then ``tail`` (old data or erased flash) to the end."""
    header = bytearray(0x40)
    header[0:4] = b"\x00\x03\x40\x00"
    header[0x2C:0x30] = (total - 0x40).to_bytes(4, "little")
    return bytes(header) + files + b"\x00" * (total - 0x40 - len(files)) + tail


async def _fetch_with_responses(responses: list[bytes]):
    server = ScriptedUdpServer(responses)
    server.start()
    try:
        client = EpsnetUdpClient("127.0.0.1", server.port, local_port=0, timeout=1.0)
        _ident, entries = await client.async_fetch_pub_from_plc()
    finally:
        server.close()
    return client, entries, server.requests


@pytest.mark.asyncio
async def test_fetch_takes_the_size_from_the_image_header_when_the_reply_has_none() -> None:
    """Old data after a shorter image is not erased, so the image's own header
    sets the end, not the first erased area."""
    image = _storage_image(b"\r\nlight_1 R B 100 BOOL PUB_OUT\r\n", 200, b"")
    old = b"\r\nold_light R B 900 BOOL PUB_OUT\r\n".ljust(800, b"\x00")
    storage = image + old  # 1000 bytes = one batch
    blocks = [storage[i : i + 200] for i in range(0, 1000, 200)]
    client, entries, _requests = await _fetch_with_responses(
        [
            _read_response_body(0, b"D100 CP201MK V7.2"),
            _read_response_body(0, b"\x00" * 48),  # init reply without a size
            _storage_batch_response(0, blocks[:1]),  # header block
            _storage_batch_response(0, blocks[:1]),  # the walk: one block
        ]
    )
    assert [e.name for e in entries] == ["light_1"]
    assert client.fetch_progress == (200, 200)


@pytest.mark.asyncio
async def test_fetch_without_any_size_stops_at_erased_flash(monkeypatch) -> None:
    # A small cap keeps a regression (walking past the erased area) short.
    monkeypatch.setattr(epsnet_udp, "_STORAGE_MAX_BYTES", 3000)
    files = b"\r\nlight_1 R B 100 BOOL PUB_OUT\r\n"
    first = (b"\x07\x07" + files).ljust(1000, b"\x00")  # no header magic
    erased = b"\xff" * 1000
    def batch(data: bytes) -> bytes:
        return _storage_batch_response(0, [data[i : i + 200] for i in range(0, 1000, 200)])
    client, entries, requests = await _fetch_with_responses(
        [
            _read_response_body(0, b"D100 CP201MK V7.2"),
            _read_response_body(0, b"\x00" * 48),
            _storage_batch_response(0, [first[:200]]),  # header block: no magic
            batch(first),
            batch(erased),
        ]
    )
    # hello, establish, init, header block, two batches; nothing after the erased one
    assert len(requests) == 6
    assert [e.name for e in entries] == ["light_1"]
    assert client.fetch_progress == (1000, 1000)
