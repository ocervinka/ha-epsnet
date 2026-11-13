"""Entities from the PLC program's I/O layout (no variable map needed).

Every module channel gets an address from the program (see program_layout) and
a meaning from the module's descriptor (its data items in conversion-code
order). Entities are keyed by hardware identity, never by X/Y address:
``<master rack>_<master slot>_<hw code>_<serial>_<channel>``. Outputs are
read-only here: the PLC program rewrites Y every cycle, so they are shown, not
switched.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .models import (
    DetectedDevice,
    EntitySpec,
    MathFormula,
    ModuleItem,
    PubEntry,
    RuntimeDiscoveredDevice,
    RuntimeModelInfo,
    ValueKind,
)
from .program_layout import ProgramDevice, ProgramIoLayout, ProgramItem, ProgramMaster
from .runtime_registry import RUNTIME_MODEL_INFOS_BY_CODE

# Where a scanned module's record points at its settings table (BE32 address).
_CHILD_TABLE_POINTER_OFFSET = 0x40
PROGRAM_ENTITY_PREFIX = "io_"

# role -> (platform, device_class, unit, diagnostic)
_ROLES: dict[str, tuple[str, str | None, str | None, bool]] = {
    "button": ("binary_sensor", None, None, False),
    "input": ("binary_sensor", None, None, False),
    "local_switch": ("binary_sensor", None, None, False),
    "relay": ("binary_sensor", "power", None, False),
    "led": ("binary_sensor", "light", None, False),
    "tamper": ("binary_sensor", "tamper", None, True),
    "status": ("binary_sensor", None, None, True),
    "temperature": ("sensor", "temperature", "°C", False),
    "analog_temperature": ("sensor", "temperature", "°C", False),
    "dimmer": ("sensor", None, "%", False),
    "humidity": ("sensor", "humidity", "%", False),
}


@dataclass(slots=True)
class ProgramEntities:
    entries: dict[str, PubEntry] = field(default_factory=dict)
    specs: list[EntitySpec] = field(default_factory=list)
    devices: dict[str, DetectedDevice] = field(default_factory=dict)
    report: list[dict[str, object]] = field(default_factory=list)


def _child_table_offset(device: RuntimeDiscoveredDevice, table_address: int) -> int | None:
    if not device.child_record_hex:
        return None
    raw = bytes.fromhex(device.child_record_hex)
    if len(raw) < _CHILD_TABLE_POINTER_OFFSET + 4:
        return None
    address = int.from_bytes(
        raw[_CHILD_TABLE_POINTER_OFFSET : _CHILD_TABLE_POINTER_OFFSET + 4], "big"
    )
    offset = address - table_address
    return offset if offset >= 0 else None


def pair_modules(
    layout: ProgramIoLayout, runtime_devices: tuple[RuntimeDiscoveredDevice, ...]
) -> dict[tuple[int, int, int], RuntimeDiscoveredDevice]:
    """(master rack, master slot, line) -> the scanned module it configures.

    Exact where the scanned module's record points at its settings table in the
    program; otherwise modules of one type are paired in order, when the program
    and the scan have the same number of them.
    """
    if layout.table_address is None:
        return {}
    by_offset: dict[int, RuntimeDiscoveredDevice] = {}
    for device in runtime_devices:
        offset = _child_table_offset(device, layout.table_address)
        if offset is not None:
            by_offset[offset] = device
    pairs: dict[tuple[int, int, int], RuntimeDiscoveredDevice] = {}
    unpaired: dict[int, list[tuple[int, int, int]]] = {}
    for master in layout.masters:
        for line in master.devices:
            if line.kind != "device":
                continue
            where = (master.rack, master.slot, line.line)
            device = by_offset.get(line.init_offset) if line.init_offset is not None else None
            if device is not None and device.runtime_type_code == line.hw_code:
                pairs[where] = device
            elif line.hw_code is not None:
                unpaired.setdefault(line.hw_code, []).append(where)
    used = {id(device) for device in pairs.values()}
    for hw_code, lines in unpaired.items():
        left = sorted(
            (d for d in runtime_devices
             if d.runtime_type_code == hw_code and id(d) not in used),
            key=lambda d: (d.group_token, d.runtime_index, d.slot_index),
        )
        if len(left) == len(lines):
            pairs.update(zip(lines, left))
    return pairs


def _fits(definition: ModuleItem, item: ProgramItem) -> bool:
    bits = [c.bit for c in definition.channels if c.bit is not None]
    if bits:
        return item.value_type != "REAL" and max(bits) // 8 < item.size
    if definition.byte_value:
        return item.value_type == "BYTE"
    return item.value_type in ("REAL", "WORD")


def _match_items(
    items: tuple[ModuleItem, ...],
    placed: list[ProgramItem],
    direction: str,
    *,
    extra_ignored: bool,
) -> tuple[list[tuple[ModuleItem, ProgramItem]], str | None]:
    """Pair the descriptor's items with the program's, in order."""
    if not any(i.optional for i in items) and not extra_ignored:
        if len(items) != len(placed):
            return [], (
                f"{direction}: the program has {len(placed)} data items, "
                f"the module type {len(items)}"
            )
    pairs: list[tuple[ModuleItem, ProgramItem]] = []
    cursor = 0
    for definition in items:
        if cursor < len(placed) and _fits(definition, placed[cursor]):
            pairs.append((definition, placed[cursor]))
            cursor += 1
        elif not definition.optional:
            item = placed[cursor] if cursor < len(placed) else None
            where = f"{item.zone}{item.address}" if item else "the end"
            return [], f"{direction}: item at {where} does not fit the module type"
    if cursor < len(placed) and not extra_ignored:
        return [], f"{direction}: {len(placed) - cursor} data item(s) more than the module type"
    return pairs, None


def _entry_for(channel, item: ProgramItem) -> tuple[str, str, int, int | None, MathFormula]:
    """(storage, data_type, address, bit, transform) of one channel."""
    if channel.bit is not None:
        return "B", "BOOL", item.address + channel.bit // 8, channel.bit % 8, MathFormula.NONE
    if item.value_type == "REAL":
        return "F", "REAL", item.address, None, MathFormula.NONE
    if item.value_type == "WORD":
        return "W", "INT", item.address, None, channel.raw_scale
    return "B", "USINT", item.address, None, channel.raw_scale


def _covered(entries: tuple[PubEntry, ...]) -> set[tuple[str, int, int | None]]:
    return {(e.register, e.address, e.bit) for e in entries if e.register in ("X", "Y")}


def build_program_entities(
    layout: ProgramIoLayout | None,
    runtime_devices: tuple[RuntimeDiscoveredDevice, ...],
    devices_by_key: dict[str, DetectedDevice],
    map_entries: tuple[PubEntry, ...],
    *,
    keep_named: dict[tuple[str, int, int | None], str] | None = None,
    shown: tuple[PubEntry, ...] | None = None,
    shown_devices: set[str] | None = None,
) -> ProgramEntities:
    """Entities for the channels the program places.

    Default: only the channels no map entity shows (``shown``: the map entries
    the map's entities read; all map entries if not given); with a map they start
    disabled (nothing new appears), without one everything but diagnostic
    channels starts enabled.

    ``keep_named`` (an entry first set up without a map, map added later): every
    channel keeps its entity, enabled as before; the map only supplies display
    names, keyed by (register, address, bit).
    """
    result = ProgramEntities()
    if layout is None or layout.error:
        return result
    if keep_named is not None:
        covered: set[tuple[str, int, int | None]] = set()
        enabled = True
        names = keep_named
    else:
        # a channel counts as covered only when a map entity already shows it
        covered = _covered(map_entries if shown is None else shown)
        enabled = not map_entries
        names = {}
    device_key_by_slot = {
        (d.runtime_group_token, d.runtime_child_token): key
        for key, d in devices_by_key.items()
        if d.runtime_group_token is not None
    }
    pairs = pair_modules(layout, runtime_devices)

    for master in layout.masters:
        for line in master.devices:
            if line.kind != "device":
                continue
            _add_device(result, master, line, pairs, device_key_by_slot, covered, enabled,
                        devices_by_key, names, shown_devices or set())
    return result


def _add_device(result, master: ProgramMaster, line: ProgramDevice, pairs,
                device_key_by_slot, covered, enabled, devices_by_key, names,
                shown_devices) -> None:
    serial = f"{line.serial:04X}" if line.serial is not None else "?"
    info: RuntimeModelInfo | None = RUNTIME_MODEL_INFOS_BY_CODE.get(line.hw_code or -1)
    report: dict[str, object] = {
        "master": f"{master.rack}/{master.slot}",
        "line": line.line,
        "hw_code": line.hw_code,
        "serial": serial,
        "model": info.model if info else None,
        "problems": list(line.unsupported),
        "channels": 0,
    }
    result.report.append(report)
    if info is None:
        report["problems"].append("module type not supported yet")
        return
    stem = f"{master.rack}_{master.slot}_{info.unit_code}_{serial.lower()}"
    runtime = pairs.get((master.rack, master.slot, line.line))
    if runtime is None:
        # not matched with a scanned module: the program's own device for it
        report["problems"].append("not matched with a module found by the scan")
        device_key = f"{PROGRAM_ENTITY_PREFIX}{stem}"
        result.devices[device_key] = DetectedDevice(
            key=device_key,
            name=f"{info.model} {serial}",
            type_key=info.type_key,
            model=info.model,
            subsystem=info.subsystem,
            manufacturer=info.manufacturer,
            unit_code=info.unit_code,
            discovery_source="epsnet_program",
        )
    else:
        device_key = device_key_by_slot.get((runtime.group_token, runtime.child_token))
    existing = devices_by_key.get(device_key) if device_key else None
    if existing is not None and existing.discovery_source == "epsnet_runtime_only":
        # a module the map does not cover: name it by model and bus address
        result.devices[device_key] = replace(existing, name=f"{info.model} {serial}")
    if device_key is None:
        device_key = runtime.registry_key
        result.devices[device_key] = DetectedDevice(
            key=device_key,
            name=f"{info.model} {serial}",
            type_key=info.type_key,
            model=info.model,
            subsystem=info.subsystem,
            manufacturer=info.manufacturer,
            unit_code=info.unit_code,
            runtime_type_code=runtime.runtime_type_code,
            runtime_group_token=runtime.group_token,
            runtime_child_token=runtime.child_token,
            runtime_slot_index=runtime.slot_index,
            runtime_index=runtime.runtime_index,
            discovery_source="epsnet_program",
        )
    report["device_key"] = device_key
    # with a map, a module none of whose channels the map shows is new: enabled
    on = enabled or device_key not in shown_devices

    for direction, items in (("in", info.inputs), ("out", info.outputs)):
        placed = [i for i in line.items if i.direction == direction]
        if not items:
            continue  # this side of the module is not described
        if any(p.startswith(f"{direction}:") for p in line.unsupported):
            # items before an unknown code are placed right; usable only when
            # the module type allows extra items after the described ones
            if not (info.extra_items_ignored and placed):
                continue
        pairs_, problem = _match_items(
            items, placed, direction, extra_ignored=info.extra_items_ignored
        )
        if problem:
            report["problems"].append(problem)
            continue
        for definition, item in pairs_:
            for channel in definition.channels:
                storage, data_type, address, bit, transform = _entry_for(channel, item)
                if (item.zone, address, bit) in covered:
                    continue
                platform, device_class, unit, diagnostic = _ROLES[channel.role]
                key = f"{PROGRAM_ENTITY_PREFIX}{stem}_{channel.key}"
                result.entries[key] = PubEntry(
                    name=key,
                    register=item.zone,
                    storage=storage,
                    address=address,
                    bit=bit,
                    data_type=data_type,
                    access="PUB_OUT",
                    line_number=0,
                    is_virtual=True,
                )
                result.specs.append(
                    EntitySpec(
                        key=key,
                        platform=platform,
                        name=names.get((item.zone, address, bit), channel.label),
                        state_name=key,
                        value_kind=ValueKind.BOOL if data_type == "BOOL" else (
                            ValueKind.FLOAT if data_type == "REAL" else ValueKind.INT),
                        device_class=device_class,
                        native_unit_of_measurement=unit,
                        enabled_by_default=on and not diagnostic,
                        device_key=device_key,
                        transform_read=transform,
                        diagnostic=diagnostic,
                        from_program=True,
                    )
                )
                report["channels"] = int(report["channels"]) + 1
