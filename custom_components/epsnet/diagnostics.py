"""Diagnostic artifact helpers for EPSNET runtime discovery."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import (
    AddressRange,
    DetectedDevice,
    DetectedDeviceEntryGroup,
    EntityCatalog,
    RuntimeDiscoveredDevice,
)
from .program_layout import cross_check, layout_snapshot


def _hex16(value: int) -> str:
    return f"0x{value:04x}"


def _hex32(value: int) -> str:
    return f"0x{value:08x}"


def _address_range_snapshot(address_range: AddressRange) -> dict[str, object]:
    return {
        "register": address_range.register,
        "storage": address_range.storage,
        "data_type": address_range.data_type,
        "start_address": address_range.start_address,
        "end_address": address_range.end_address,
    }


def _entry_group_snapshot(entry_group: DetectedDeviceEntryGroup) -> dict[str, object]:
    return {
        "role": entry_group.role,
        "register": entry_group.register,
        "storage": entry_group.storage,
        "data_type": entry_group.data_type,
        "access_modes": list(entry_group.access_modes),
        "entry_count": len(entry_group.entry_names),
        "address_ranges": [
            _address_range_snapshot(address_range)
            for address_range in entry_group.address_ranges
        ],
        "entry_names": list(entry_group.entry_names),
    }


def _pub_device_snapshot(device: DetectedDevice) -> dict[str, object]:
    return {
        "device_key": device.key,
        "registry_keys": list(device.registry_keys),
        "device_name": device.name,
        "type_key": device.type_key,
        "model": device.model,
        "manufacturer": device.manufacturer,
        "unit_code": device.unit_code,
        "entry_count": len(device.entry_names),
        "entity_count": len(device.entity_keys),
        "role_entry_counts": {
            entry_group.role: len(entry_group.entry_names)
            for entry_group in device.entry_groups
        },
        "entry_groups": [
            _entry_group_snapshot(entry_group) for entry_group in device.entry_groups
        ],
        "entry_names": list(device.entry_names),
        "entity_keys": list(device.entity_keys),
    }


def _runtime_device_snapshot(
    runtime_device: RuntimeDiscoveredDevice,
    matched_device: DetectedDevice | None,
) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "runtime_key": runtime_device.key,
        "runtime_index": runtime_device.runtime_index,
        "slot_index": runtime_device.slot_index,
        "group_token": _hex32(runtime_device.group_token),
        "group_label": runtime_device.group_label,
        "group_marker": runtime_device.group_marker,
        "group_kind": runtime_device.group_kind,
        "child_token": _hex32(runtime_device.child_token),
        "runtime_type_code": _hex16(runtime_device.runtime_type_code),
        "unit_code": runtime_device.unit_code,
        "manufacturer": runtime_device.manufacturer,
        "model": runtime_device.model,
        "description": runtime_device.description,
    }
    if runtime_device.child_record_hex is not None:
        snapshot["child_record_hex"] = runtime_device.child_record_hex
    if runtime_device.exact_descriptor is not None:
        snapshot["exact_descriptor"] = {
            "token": _hex32(runtime_device.exact_descriptor.token),
            "length": _hex16(runtime_device.exact_descriptor.length),
            "family_word": _hex16(runtime_device.exact_descriptor.family_word),
            "runtime_unit_code": _hex16(runtime_device.exact_descriptor.runtime_unit_code),
            "reserved_word": _hex16(runtime_device.exact_descriptor.reserved_word),
            "text": runtime_device.exact_descriptor.text,
            "display_name": runtime_device.exact_descriptor.display_name,
            "compact_model_code": runtime_device.exact_descriptor.compact_model_code,
            "instance_code": runtime_device.exact_descriptor.instance_code,
            "suffix_code": runtime_device.exact_descriptor.suffix_code,
            "trailer_word": _hex16(runtime_device.exact_descriptor.trailer_word),
            "raw_hex": runtime_device.exact_descriptor.raw_hex,
        }
    if matched_device is None or matched_device.discovery_source == "epsnet_runtime_only":
        snapshot["pub_match"] = None
    else:
        snapshot["pub_match"] = _pub_device_snapshot(matched_device)
    return snapshot


def build_catalog_snapshot(
    catalog: EntityCatalog,
    *,
    host: str,
    port: int,
    local_port: int,
    map_path: str | None,
) -> dict[str, object]:
    """Build a human-readable diagnostic snapshot for the merged catalog."""
    matched_runtime_devices = {
        (device.runtime_group_token, device.runtime_index): device
        for device in catalog.devices_by_key.values()
        if device.runtime_group_token is not None and device.runtime_index is not None
    }
    real_entries = [e for e in catalog.entries_by_name.values() if not e.is_virtual]
    return {
        "source": {
            "discovery": "epsnet_runtime",
            "host": host,
            "port": port,
            "local_port": local_port,
            "map_path": map_path,
            "pub_enrichment_active": bool(real_entries),
            "pub_entry_count": len(real_entries),
        },
        "controller_identity": catalog.runtime_inventory.controller_identity,
        "root_identity": catalog.runtime_inventory.root_identity,
        "group_table_token": _hex32(catalog.runtime_inventory.group_table_token),
        "root_tokens": [_hex32(token) for token in catalog.runtime_inventory.root_tokens],
        "root_records": [
            {
                "token": _hex32(record.token),
                "meta": _hex16(record.meta),
            }
            for record in catalog.runtime_inventory.root_records
        ],
        "groups": [
            {
                "token": _hex32(group.token),
                "meta": _hex16(group.meta),
                "label": group.label,
                "marker": group.marker,
                "kind": group.kind,
                "header": group.header_hex,
                "occupied_slot_count": group.occupied_slot_count,
                "empty_slot_count": group.empty_slot_count,
                "slot_map": list(group.slot_map),
                "devices": [
                    _runtime_device_snapshot(
                        runtime_device,
                        matched_runtime_devices.get(
                            (runtime_device.group_token, runtime_device.runtime_index)
                        ),
                    )
                    for runtime_device in group.devices
                ],
            }
            for group in catalog.runtime_inventory.groups
        ],
        "unmatched_runtime_devices": [
            _runtime_device_snapshot(runtime_device, None)
            for runtime_device in catalog.unmatched_runtime_devices
        ],
        "unmatched_pub_devices": [
            _pub_device_snapshot(device) for device in catalog.unmatched_pub_devices
        ],
        "unsupported_runtime_devices": [
            _runtime_device_snapshot(runtime_device, None)
            for runtime_device in catalog.unsupported_runtime_devices
        ],
        "raw_reads": {
            label: raw_hex for label, raw_hex in catalog.runtime_inventory.raw_reads
        },
        "program_devices": list(catalog.program_report),
        "program_layout": (
            None
            if catalog.runtime_inventory.program_layout is None
            else layout_snapshot(catalog.runtime_inventory.program_layout)
        ),
    }


def _yaml_scalar(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=True)


def _yaml_lines(value: object, *, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, dict):
                if item:
                    lines.append(f"{prefix}{key}:")
                    lines.extend(_yaml_lines(item, indent=indent + 2))
                else:
                    lines.append(f"{prefix}{key}: {{}}")
            elif isinstance(item, (list, tuple)):
                if item:
                    lines.append(f"{prefix}{key}:")
                    lines.extend(_yaml_lines(list(item), indent=indent + 2))
                else:
                    lines.append(f"{prefix}{key}: []")
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
        return lines

    if isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{prefix}- {{}}")
                    continue
                first_key, first_value = next(iter(item.items()))
                remaining_items = list(item.items())[1:]
                if isinstance(first_value, (dict, list, tuple)) and first_value:
                    lines.append(f"{prefix}- {first_key}:")
                    lines.extend(_yaml_lines(first_value, indent=indent + 4))
                elif isinstance(first_value, dict):
                    lines.append(f"{prefix}- {first_key}: {{}}")
                elif isinstance(first_value, (list, tuple)):
                    lines.append(f"{prefix}- {first_key}: []")
                else:
                    lines.append(f"{prefix}- {first_key}: {_yaml_scalar(first_value)}")
                for key, nested_value in remaining_items:
                    nested_prefix = " " * (indent + 2)
                    if isinstance(nested_value, dict):
                        if nested_value:
                            lines.append(f"{nested_prefix}{key}:")
                            lines.extend(_yaml_lines(nested_value, indent=indent + 4))
                        else:
                            lines.append(f"{nested_prefix}{key}: {{}}")
                    elif isinstance(nested_value, (list, tuple)):
                        if nested_value:
                            lines.append(f"{nested_prefix}{key}:")
                            lines.extend(_yaml_lines(list(nested_value), indent=indent + 4))
                        else:
                            lines.append(f"{nested_prefix}{key}: []")
                    else:
                        lines.append(f"{nested_prefix}{key}: {_yaml_scalar(nested_value)}")
            elif isinstance(item, (list, tuple)):
                if item:
                    lines.append(f"{prefix}-")
                    lines.extend(_yaml_lines(list(item), indent=indent + 2))
                else:
                    lines.append(f"{prefix}- []")
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return lines

    return [f"{prefix}{_yaml_scalar(value)}"]


def write_catalog_snapshot(path: Path, snapshot: dict[str, object]) -> None:
    """Write the merged-catalog diagnostic artifact as YAML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_yaml_lines(snapshot)) + "\n", encoding="utf-8")


def map_snapshot(blob: object) -> dict[str, object]:
    """The stored variable map, one .pub-style line per variable."""
    if not isinstance(blob, dict) or blob.get("entries") is None:
        return {"source": "none", "entry_count": 0, "entries": []}
    lines = []
    for e in blob["entries"]:
        bit = f" .{e['bit']}" if e.get("bit") is not None else ""
        lines.append(
            f"{e['name']} {e['register']} {e['storage']} {e['address']}{bit} "
            f"{e['data_type']} {e.get('access') or ''}".rstrip()
        )
    return {
        "source": blob.get("source"),
        "plc_ident": blob.get("plc_ident"),
        "crc": _hex32(int(blob.get("crc") or 0)),
        "entry_count": len(lines),
        "entries": lines,
    }


# Words the entity rules look for (motion / door sensors, lights, outlets) and
# the generic parts of a name; everything else in a variable name is the
# program author's own wording (rooms, people) and is replaced.
_KEEP_SUBSTRINGS = ("pir", "magnet", "light", "plug")
_KEEP_WORDS = frozenset({"state", "on", "off", "trig", "output", "input", "sw", "dimmer", "analog"})
_SHORT_CODE = re.compile(r"[A-Za-z]{1,3}\d+[A-Za-z]?")
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_NAME_KEYS = frozenset({
    "entries", "entry_names", "entity_keys", "device_key", "device_name", "registry_keys",
    "pub_entry", "pub_entries",
})


def _keep_word(word: str) -> bool:
    lower = word.lower()
    return (
        word.isdigit()
        or lower in _KEEP_WORDS
        or any(k in lower for k in _KEEP_SUBSTRINGS)
        or _SHORT_CODE.fullmatch(word) is not None
    )


def anonymize_names(data: dict[str, object], names: list[str]) -> dict[str, object]:
    """Replace the program's own words in variable names, consistently everywhere.

    Each word maps to the same placeholder (``w1``, ``w2``…) wherever it occurs,
    so shared prefixes, ``_ON``/``_OFF`` pairs and device grouping stay visible.
    Only name-carrying fields and the map lines are rewritten; module data,
    addresses and raw reads are untouched.
    """
    mapping: dict[str, str] = {}
    for name in names:
        for word in re.split(r"[^A-Za-z0-9]+", name):
            if word and not _keep_word(word) and word.lower() not in mapping:
                mapping[word.lower()] = f"w{len(mapping) + 1}"

    def sub(text: str) -> str:
        def one(m: re.Match) -> str:
            word = m.group(0)
            new = mapping.get(word.lower())
            if new is None:
                return word
            return new.upper() if word.isupper() else new
        return _TOKEN.sub(one, text)

    def sub_map_line(line: str) -> str:
        # only the name; zone, storage, type and access stay readable
        name, sep, rest = line.partition(" ")
        return sub(name) + sep + rest

    def walk(value: object, key: str | None) -> object:
        if isinstance(value, dict):
            return {k: walk(v, k if k in _NAME_KEYS else key) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        if key is not None and isinstance(value, str):
            return sub_map_line(value) if key == "entries" else sub(value)
        return value

    return walk(data, None)  # type: ignore[return-value]


def program_layout_vs_map(layout, blob: object) -> dict[str, object] | None:
    """Every X/Y variable of the stored map checked against the program's layout."""
    if layout is None or layout.error or not isinstance(blob, dict) or not blob.get("entries"):
        return None
    return cross_check(layout, list(blob["entries"]))


def map_shared_addresses(blob: object) -> list[dict[str, object]]:
    """X/Y bits that more than one variable of the stored map points at.

    Two names on one input or output bit are usually an export mistake (for
    example the upper relays of a module given the lower relays' byte), so the
    entity of one of them shows the other channel's state.
    """
    if not isinstance(blob, dict):
        return []
    by_bit: dict[tuple[str, int, int], list[str]] = {}
    for entry in blob.get("entries") or ():
        if not isinstance(entry, dict) or entry.get("register") not in ("X", "Y"):
            continue
        if entry.get("bit") is None:
            continue
        key = (str(entry["register"]), int(entry["address"]), int(entry["bit"]))
        by_bit.setdefault(key, []).append(str(entry.get("name")))
    return [
        {"address": f"{zone}{address}.{bit}", "pub_entries": names}
        for (zone, address, bit), names in sorted(by_bit.items())
        if len(names) > 1
    ]


async def _read_program_checksum(client) -> str | None:
    from .program_watch import read_program_checksum

    value = await read_program_checksum(client)
    return None if value is None else _hex32(value)


def names_hidden(entry) -> bool:
    """Whether the download replaces the program's own words in variable names.

    Hidden unless the user chose otherwise: the file is made to be sent to
    someone else.
    """
    from .const import CONF_ANONYMIZE_DIAGNOSTICS

    return bool(entry.data.get(CONF_ANONYMIZE_DIAGNOSTICS, True))


async def async_get_config_entry_diagnostics(hass, entry) -> dict[str, object]:
    """Home Assistant "Download diagnostics": the merged-catalog snapshot.

    Includes the raw discovery reads and the full stored variable map, so a
    report from an untested PLC model is enough to see how its module table
    and its program's variables are laid out. Variable names from the PLC
    program have their own words replaced by placeholders unless the user
    turned that off (see ``names_hidden``).
    """
    from homeassistant.loader import async_get_integration

    from .const import (
        CONF_HOST,
        CONF_LOCAL_PORT,
        CONF_MAP,
        CONF_PORT,
        DEFAULT_LOCAL_PORT,
        DOMAIN,
    )

    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return {"error": "integration is not loaded (check the log for the setup error)"}
    data: dict[str, object] = {
        "integration_version": str((await async_get_integration(hass, DOMAIN)).version),
        "names_anonymized": names_hidden(entry),
        "home_assistant": {"version": _ha_version()},
        "entity_count": len(runtime.catalog.entities),
        "program_checksum_s70": await _read_program_checksum(runtime.client),
        "map": map_snapshot(entry.data.get(CONF_MAP)),
        "program_layout_vs_map": program_layout_vs_map(
            runtime.catalog.runtime_inventory.program_layout, entry.data.get(CONF_MAP)
        ),
        "map_shared_addresses": map_shared_addresses(entry.data.get(CONF_MAP)),
        "catalog": build_catalog_snapshot(
            runtime.catalog,
            host=str(entry.data.get(CONF_HOST)),
            port=int(entry.data.get(CONF_PORT, 0)),
            local_port=int(entry.data.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT)),
            map_path=None,
        ),
    }
    if data["names_anonymized"]:
        names = [e.name for e in runtime.catalog.entries_by_name.values()]
        data = anonymize_names(data, names)
    return data


def _ha_version() -> str:
    from homeassistant.const import __version__

    return __version__
