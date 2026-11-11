"""Entity classification and model-first device recognition derived from .pub entries."""

from __future__ import annotations

from collections import defaultdict, Counter
from dataclasses import dataclass, replace
from functools import cmp_to_key, lru_cache
import re

from .runtime_registry import RUNTIME_MODEL_INFOS_BY_CODE
from .models import (
    AddressRange,
    DetectedDevice,
    DetectedDeviceEntryGroup,
    DeviceTypeDefinition,
    EntityCatalog,
    EntitySpec,
    MathFormula,
    PubEntry,
    RuntimeDiscoveredDevice,
    RuntimeInventory,
)

_STATE_PREFIX = "state_"
_COMMAND_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_TRIG", "r_cmd_trig"),
    ("_OFF", "r_cmd_off"),
    ("_ON", "r_cmd_on"),
)

GENERIC_MODEL_DEFINITIONS = (
    DeviceTypeDefinition(
        key="generic_relay_module",
        name="Generic relay module",
        subsystem="relay_outputs",
        entry_definitions=(),
    ),
    DeviceTypeDefinition(
        key="generic_input_module",
        name="Generic input module",
        subsystem="digital_inputs",
        entry_definitions=(),
    ),
    DeviceTypeDefinition(
        key="generic_room_controller",
        name="Generic room controller",
        subsystem="room_controls",
        entry_definitions=(),
    ),
    DeviceTypeDefinition(
        key="generic_dimmer_module",
        name="Generic dimmer module",
        subsystem="dimmers",
        entry_definitions=(),
    ),
    DeviceTypeDefinition(
        key="generic_analog_sensor_module",
        name="Generic analog sensor module",
        subsystem="analog_sensors",
        entry_definitions=(),
    ),
)
DEVICE_TYPES_BY_KEY = {
    definition.key: definition for definition in GENERIC_MODEL_DEFINITIONS
}

OUTPUT_COMMANDS = {"ON", "OFF", "TRIG"}
DIMMER_COMMANDS = {"ON", "OFF"}
SW_COMMANDS = {"ON", "OFF", "TRIG"}


@dataclass(frozen=True, slots=True)
class _DeviceCandidate:
    """One possible device-alias split for a family of raw stems."""

    prefix: str
    raw_stems: tuple[str, ...]
    type_key: str
    model: str
    score: tuple[int, int, int, int, int, int, int]


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _sorted_entries_by_address(entries: list[PubEntry] | tuple[PubEntry, ...]) -> list[PubEntry]:
    return sorted(entries, key=lambda entry: (entry.address, entry.bit or -1, entry.name))


def _sorted_entries_by_name(entries: list[PubEntry] | tuple[PubEntry, ...]) -> list[PubEntry]:
    return sorted(entries, key=lambda entry: entry.name.lower())


def _matches_signature(
    entry: PubEntry, *, register: str, storage: str, data_type: str
) -> bool:
    return (
        entry.register == register
        and entry.storage == storage
        and entry.data_type.upper() == data_type
    )


def _merge_address_ranges(entries: list[PubEntry]) -> tuple[AddressRange, ...]:
    if not entries:
        return ()

    ordered = sorted((entry.address, entry.address + entry.byte_size - 1) for entry in entries)
    merged: list[tuple[int, int]] = []
    for start_address, end_address in ordered:
        if not merged or start_address > merged[-1][1] + 1:
            merged.append((start_address, end_address))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end_address))

    first = entries[0]
    return tuple(
        AddressRange(
            register=first.register,
            storage=first.storage,
            data_type=first.data_type.upper(),
            start_address=start_address,
            end_address=end_address,
        )
        for start_address, end_address in merged
    )


def _build_entry_group(role: str, entries: list[PubEntry]) -> DetectedDeviceEntryGroup:
    ordered_entries = _sorted_entries_by_address(entries)
    first = ordered_entries[0]
    return DetectedDeviceEntryGroup(
        role=role,
        register=first.register,
        storage=first.storage,
        data_type=first.data_type.upper(),
        access_modes=tuple(sorted({entry.access for entry in ordered_entries})),
        address_ranges=_merge_address_ranges(ordered_entries),
        entry_names=tuple(entry.name for entry in ordered_entries),
    )


def _build_detected_device(
    *,
    key: str,
    name: str,
    type_key: str,
    model: str,
    role_entries: dict[str, list[PubEntry]],
) -> DetectedDevice:
    ordered_roles = tuple(
        _build_entry_group(role, entries)
        for role, entries in sorted(role_entries.items())
        if entries
    )
    all_entries = _sorted_entries_by_address(
        [entry for entries in role_entries.values() for entry in entries]
    )
    return DetectedDevice(
        key=key,
        name=name,
        type_key=type_key,
        model=model,
        subsystem=DEVICE_TYPES_BY_KEY[type_key].subsystem,
        entry_names=tuple(dict.fromkeys(entry.name for entry in all_entries)),
        entry_groups=ordered_roles,
    )


def _is_generic_model(model: str) -> bool:
    return model.startswith("generic_")


def _underscore_prefixes(raw_name: str) -> tuple[str, ...]:
    prefixes: list[str] = []
    for index, character in enumerate(raw_name):
        if character == "_" and index + 1 < len(raw_name):
            prefixes.append(raw_name[: index + 1])
    return tuple(prefixes)


def _longest_common_underscore_prefix(raw_stems: tuple[str, ...]) -> str | None:
    if not raw_stems:
        return None

    for prefix in reversed(_underscore_prefixes(raw_stems[0])):
        if all(stem.startswith(prefix) for stem in raw_stems):
            return prefix
    return None


def _aggregate_roles(
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]], stems: tuple[str, ...]
) -> dict[str, list[PubEntry]]:
    role_entries: dict[str, list[PubEntry]] = defaultdict(list)
    for stem in stems:
        for role, entries in raw_roles_by_stem[stem].items():
            role_entries[role].extend(entries)
    return dict(role_entries)


def _address_span(
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]], stems: tuple[str, ...]
) -> int:
    addresses = [
        entry.address
        for stem in stems
        for entries in raw_roles_by_stem[stem].values()
        for entry in entries
    ]
    if not addresses:
        return 0
    return max(addresses) - min(addresses)


def _candidate_score(
    *,
    exact: bool,
    raw_stem_count: int,
    role_kind_count: int,
    candidate_count: int,
    suffix_with_underscore_count: int,
    prefix_token_count: int,
    address_span: int,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        raw_stem_count if exact else 0,
        raw_stem_count,
        -candidate_count,
        role_kind_count,
        suffix_with_underscore_count,
        prefix_token_count,
        -address_span,
    )


def _add_score(
    left: tuple[int, int, int, int, int, int, int],
    right: tuple[int, int, int, int, int, int, int],
) -> tuple[int, int, int, int, int, int, int]:
    return tuple(a + b for a, b in zip(left, right, strict=True))


def _build_candidates(
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]],
    evaluator,
) -> list[_DeviceCandidate]:
    candidates: dict[tuple[str, tuple[str, ...]], _DeviceCandidate] = {}
    raw_stems = tuple(sorted(raw_roles_by_stem))
    for raw_stem in raw_stems:
        for prefix in _underscore_prefixes(raw_stem):
            grouped_stems = tuple(
                stem for stem in raw_stems if stem.startswith(prefix) and stem != prefix[:-1]
            )
            if not grouped_stems:
                continue

            evaluation = evaluator(raw_roles_by_stem, grouped_stems)
            if evaluation is None:
                continue
            type_key, model, exact = evaluation
            role_kind_count = len(
                {
                    role
                    for stem in grouped_stems
                    for role in raw_roles_by_stem[stem]
                }
            )
            suffix_with_underscore_count = sum(
                "_" in stem[len(prefix) :] for stem in grouped_stems
            )
            prefix_token_count = prefix.rstrip("_").count("_") + 1
            score = _candidate_score(
                exact=exact,
                raw_stem_count=len(grouped_stems),
                role_kind_count=role_kind_count,
                candidate_count=1,
                suffix_with_underscore_count=suffix_with_underscore_count,
                prefix_token_count=prefix_token_count,
                address_span=_address_span(raw_roles_by_stem, grouped_stems),
            )
            key = (prefix, grouped_stems)
            candidates[key] = _DeviceCandidate(
                prefix=prefix,
                raw_stems=grouped_stems,
                type_key=type_key,
                model=model,
                score=score,
            )
    return list(candidates.values())


def _select_candidates(
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]],
    evaluator,
) -> tuple[_DeviceCandidate, ...]:
    if not raw_roles_by_stem:
        return ()

    candidates = _build_candidates(raw_roles_by_stem, evaluator)
    if not candidates:
        return ()

    candidate_indexes_by_stem: dict[str, list[int]] = defaultdict(list)
    for index, candidate in enumerate(candidates):
        for raw_stem in candidate.raw_stems:
            candidate_indexes_by_stem[raw_stem].append(index)

    zero_score = (0, 0, 0, 0, 0, 0, 0)

    @lru_cache(maxsize=None)
    def _solve(remaining_stems: tuple[str, ...]) -> tuple[
        tuple[int, int, int, int, int, int, int], tuple[int, ...]
    ]:
        if not remaining_stems:
            return zero_score, ()

        remaining = set(remaining_stems)
        seed = remaining_stems[0]
        best_score: tuple[int, int, int, int, int, int, int] | None = None
        best_indexes: tuple[int, ...] = ()

        for candidate_index in candidate_indexes_by_stem[seed]:
            candidate = candidates[candidate_index]
            if not set(candidate.raw_stems).issubset(remaining):
                continue

            next_remaining = tuple(sorted(remaining - set(candidate.raw_stems)))
            score, chosen = _solve(next_remaining)
            total_score = _add_score(candidate.score, score)
            if best_score is None or total_score > best_score:
                best_score = total_score
                best_indexes = (candidate_index, *chosen)

        if best_score is None:
            return zero_score, ()
        return best_score, best_indexes

    _, selected_indexes = _solve(tuple(sorted(raw_roles_by_stem)))
    return tuple(candidates[index] for index in selected_indexes)


def _build_devices_from_raw_roles(
    *,
    family_key: str,
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]],
    evaluator,
) -> tuple[dict[str, DetectedDevice], dict[str, str], dict[str, str]]:
    devices: dict[str, DetectedDevice] = {}
    entry_to_device_key: dict[str, str] = {}
    entry_to_channel_name: dict[str, str] = {}

    for candidate in _select_candidates(raw_roles_by_stem, evaluator):
        resolved_prefix = (
            _longest_common_underscore_prefix(candidate.raw_stems) or candidate.prefix
        )
        device_alias = resolved_prefix[:-1]
        device_name = f"{family_key}_{device_alias}" if family_key else device_alias
        role_entries = _aggregate_roles(raw_roles_by_stem, candidate.raw_stems)
        device_key = _slugify(device_name)
        detected = _build_detected_device(
            key=device_key,
            name=device_name,
            type_key=candidate.type_key,
            model=candidate.model,
            role_entries=role_entries,
        )
        devices[device_key] = detected

        for raw_stem in candidate.raw_stems:
            channel_name = raw_stem[len(resolved_prefix) :] or raw_stem
            for role_entries_for_stem in raw_roles_by_stem[raw_stem].values():
                for entry in role_entries_for_stem:
                    entry_to_device_key[entry.name] = device_key
                    entry_to_channel_name[entry.name] = channel_name

    return devices, entry_to_device_key, entry_to_channel_name


def _role_entry_count(
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]],
    stems: tuple[str, ...],
    role: str,
) -> int:
    return sum(role in raw_roles_by_stem[stem] for stem in stems)


def _max_address_span_for_role(
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]],
    stems: tuple[str, ...],
    role: str,
) -> int:
    entries = [
        entry
        for stem in stems
        for entry in raw_roles_by_stem[stem].get(role, ())
    ]
    if not entries:
        return 0
    addresses = [entry.address for entry in entries]
    return max(addresses) - min(addresses)


def _collect_generic_raw_roles(
    entries_by_name: dict[str, PubEntry],
) -> dict[str, dict[str, list[PubEntry]]]:
    """Bucket every entry into (stem, role) using only the PLC toolchain's naming conventions.

    Y-register entries with the generated `state_` prefix have that
    prefix stripped to form the stem. R-register BOOL entries with
    `_ON`/`_OFF`/`_TRIG` suffixes (Foxtrot command triplet) have the suffix
    stripped. No user-chosen name prefixes (OUTPUT_/INPUT_/SW_/DIMMER_/...) are
    consulted — stems are the full entry names minus those two universal
    affixes.
    """
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for entry in entries_by_name.values():
        name = entry.name
        if entry.register == "Y" and name.startswith(_STATE_PREFIX):
            stem = name[len(_STATE_PREFIX):]
            if _matches_signature(entry, register="Y", storage="B", data_type="BOOL"):
                raw_roles_by_stem[stem]["y_bool_state"].append(entry)
            elif _matches_signature(entry, register="Y", storage="F", data_type="REAL"):
                raw_roles_by_stem[stem]["y_real_state"].append(entry)
            continue

        if _matches_signature(entry, register="R", storage="B", data_type="BOOL"):
            matched_suffix = False
            for suffix, role in _COMMAND_SUFFIXES:
                if name.endswith(suffix):
                    stem = name[: -len(suffix)]
                    raw_roles_by_stem[stem][role].append(entry)
                    matched_suffix = True
                    break
            if matched_suffix:
                continue
            raw_roles_by_stem[name]["r_bool"].append(entry)
            continue

        if _matches_signature(entry, register="R", storage="F", data_type="REAL"):
            raw_roles_by_stem[name]["r_real"].append(entry)
            continue

        if _matches_signature(entry, register="X", storage="B", data_type="BOOL"):
            raw_roles_by_stem[name]["x_bool"].append(entry)
            continue

        if _matches_signature(entry, register="X", storage="F", data_type="REAL"):
            raw_roles_by_stem[name]["x_real"].append(entry)
            continue

    return {stem: dict(roles) for stem, roles in raw_roles_by_stem.items()}


def _evaluate_generic_candidate(
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]],
    stems: tuple[str, ...],
) -> tuple[str, str, bool] | None:
    """Pick device type/model from the role shape of a clustered stem group.

    Relies on stem counts by role, not on any naming convention. Returns
    `None` when the cluster has no recognizable shape (e.g. stray `system_*`
    or unrelated R-register bookkeeping entries).
    """
    y_bool = _role_entry_count(raw_roles_by_stem, stems, "y_bool_state")
    y_real = _role_entry_count(raw_roles_by_stem, stems, "y_real_state")
    x_bool = _role_entry_count(raw_roles_by_stem, stems, "x_bool")
    x_real = _role_entry_count(raw_roles_by_stem, stems, "x_real")
    r_real = _role_entry_count(raw_roles_by_stem, stems, "r_real")
    r_bool = _role_entry_count(raw_roles_by_stem, stems, "r_bool")
    r_cmd = (
        _role_entry_count(raw_roles_by_stem, stems, "r_cmd_on")
        + _role_entry_count(raw_roles_by_stem, stems, "r_cmd_off")
        + _role_entry_count(raw_roles_by_stem, stems, "r_cmd_trig")
    )

    if y_bool == 0 and y_real == 0 and x_bool == 0 and x_real == 0 and r_real == 0 and r_bool == 0:
        return None

    y_bool_span = _max_address_span_for_role(raw_roles_by_stem, stems, "y_bool_state")
    y_real_span = _max_address_span_for_role(raw_roles_by_stem, stems, "y_real_state")
    x_bool_span = _max_address_span_for_role(raw_roles_by_stem, stems, "x_bool")
    x_real_span = _max_address_span_for_role(raw_roles_by_stem, stems, "x_real")

    if y_real >= 1 or (r_real >= 1 and r_cmd >= 1):
        exact = (
            y_real == 2
            and r_real == 2
            and y_real_span <= 7
            and x_bool >= 0
        )
        if exact:
            return "generic_dimmer_module", "DA2-22M", True
        return "generic_dimmer_module", "generic_dimmer_module", False

    if y_bool >= 1 and x_real >= 1:
        exact_wsb = (
            x_real == 1
            and y_bool in {2, 4}
            and x_bool == y_bool
            and y_bool_span == 0
            and x_bool_span == 0
        )
        if exact_wsb and y_bool == 2:
            return "generic_room_controller", "WSB2-20", True
        if exact_wsb and y_bool == 4:
            return "generic_room_controller", "WSB2-40", True
        return "generic_room_controller", "generic_room_controller", False

    if y_bool >= 1 or (r_bool >= 1 and r_cmd >= 1):
        exact_relay = (
            y_bool in {2, 4, 12}
            and y_bool_span <= max(1, (y_bool - 1) // 8)
            and x_bool in {0, y_bool}
        )
        if exact_relay:
            if y_bool == 2:
                return "generic_relay_module", "SA2-02M", True
            if y_bool == 4:
                return "generic_relay_module", "SA2-04M/Sn", True
            if y_bool == 12:
                return "generic_relay_module", "SA2-012M", True
        return "generic_relay_module", "generic_relay_module", False

    if x_bool >= 1 and x_real == 0:
        if x_bool == 4 and x_bool_span == 0:
            return "generic_input_module", "IM2-40B", True
        if x_bool == 14 and x_bool_span <= 1:
            return "generic_input_module", "IM2-140M", True
        return "generic_input_module", "generic_input_module", False

    if x_real >= 1 and x_bool >= 1:
        exact_analog = x_bool == 6 and x_real in {2, 3} and x_real_span <= 8
        if exact_analog:
            return "generic_analog_sensor_module", "C-IT-0200I", True
        return "generic_analog_sensor_module", "generic_analog_sensor_module", False

    if x_real >= 1:
        return "generic_analog_sensor_module", "generic_analog_sensor_module", False

    return None


def _detect_devices(
    entries_by_name: dict[str, PubEntry],
) -> tuple[dict[str, DetectedDevice], dict[str, str], dict[str, str]]:
    return _build_devices_from_raw_roles(
        family_key="",
        raw_roles_by_stem=_collect_generic_raw_roles(entries_by_name),
        evaluator=_evaluate_generic_candidate,
    )


_GENERIC_CROSS_TYPE_COMPATIBILITY: frozenset[tuple[str, str]] = frozenset(
    {
        ("generic_analog_sensor_module", "generic_room_controller"),
        ("generic_room_controller", "generic_analog_sensor_module"),
    }
)


def _is_runtime_candidate_compatible(
    candidate: DetectedDevice, runtime_device: RuntimeDiscoveredDevice
) -> bool:
    if candidate.model == runtime_device.model:
        return True
    if candidate.type_key == runtime_device.type_key and _is_generic_model(
        candidate.model
    ):
        return True
    if (
        _is_generic_model(candidate.model)
        and (candidate.type_key, runtime_device.type_key)
        in _GENERIC_CROSS_TYPE_COMPATIBILITY
    ):
        return True
    return False


def _merge_runtime_metadata(
    candidate: DetectedDevice, runtime_device: RuntimeDiscoveredDevice
) -> DetectedDevice:
    return replace(
        candidate,
        type_key=runtime_device.type_key,
        model=runtime_device.model,
        subsystem=runtime_device.subsystem,
        manufacturer=runtime_device.manufacturer,
        unit_code=runtime_device.unit_code,
        runtime_type_code=runtime_device.runtime_type_code,
        runtime_group_token=runtime_device.group_token,
        runtime_child_token=runtime_device.child_token,
        runtime_slot_index=runtime_device.slot_index,
        runtime_index=runtime_device.runtime_index,
        discovery_source="epsnet_runtime",
    )


def _image_starts(candidate: DetectedDevice) -> tuple[int | None, int | None]:
    """Return the candidate's first X and first Y address (``None`` if absent)."""
    starts: dict[str, list[int]] = {"X": [], "Y": []}
    for entry_group in candidate.entry_groups:
        for address_range in entry_group.address_ranges:
            if address_range.register in starts:
                starts[address_range.register].append(address_range.start_address)
    return (
        min(starts["X"]) if starts["X"] else None,
        min(starts["Y"]) if starts["Y"] else None,
    )


def _compare_image_order(a: DetectedDevice, b: DetectedDevice) -> int:
    """Order two map devices by where they sit in the X/Y images.

    Compare on X when both have inputs, else on Y when both have outputs, else
    on whatever address each has; ties fall back to the key for determinism.
    """
    (ax, ay), (bx, by) = _image_starts(a), _image_starts(b)
    if ax is not None and bx is not None and ax != bx:
        return -1 if ax < bx else 1
    if ay is not None and by is not None and ay != by:
        return -1 if ay < by else 1
    a_any = min(v for v in (ax, ay, 10**9) if v is not None)
    b_any = min(v for v in (bx, by, 10**9) if v is not None)
    if a_any != b_any:
        return -1 if a_any < b_any else 1
    return (a.key > b.key) - (a.key < b.key)


def _program_order_key(device: RuntimeDiscoveredDevice) -> tuple[int, ...]:
    position = device.program_position
    if position is not None:
        return (0, position)
    return (1, device.group_token, device.runtime_index, device.slot_index)


def _match_runtime_inventory(
    devices_by_key: dict[str, DetectedDevice],
    runtime_inventory: RuntimeInventory,
) -> tuple[
    dict[str, DetectedDevice],
    tuple[RuntimeDiscoveredDevice, ...],
    tuple[DetectedDevice, ...],
]:
    """Pair map devices with the physical modules found on the bus.

    The PLC lays its modules' data out in the X/Y images in the order of the
    program's hardware list, and discovery reports each module's position in
    that list. So walking the modules in program order and giving each one the
    earliest (by image address) still-unpaired compatible map device pairs
    same-model modules exactly, without knowing where the CIB data starts or
    how large each module is.
    """
    matched: dict[str, DetectedDevice] = {}
    available = dict(sorted(devices_by_key.items()))
    unmatched_runtime: list[RuntimeDiscoveredDevice] = []

    for runtime_device in sorted(runtime_inventory.devices, key=_program_order_key):
        candidates = [
            candidate
            for candidate in available.values()
            if _is_runtime_candidate_compatible(candidate, runtime_device)
        ]
        if not candidates:
            unmatched_runtime.append(runtime_device)
            continue
        exact = [c for c in candidates if c.model == runtime_device.model]
        best = min(exact or candidates, key=cmp_to_key(_compare_image_order))
        matched[best.key] = _merge_runtime_metadata(
            available.pop(best.key), runtime_device
        )

    unmatched_pub = tuple(sorted(available.values(), key=lambda d: d.key))
    return matched, tuple(unmatched_runtime), unmatched_pub




def _binary_sensor_device_class(channel_name: str) -> str | None:
    normalized = channel_name.lower()
    if "pir" in normalized:
        return "motion"
    if "magnet" in normalized:
        return "opening"
    return None


def _has_command_suffix(name: str) -> bool:
    return any(name.endswith(suffix) for suffix, _ in _COMMAND_SUFFIXES)


def _build_pub_entity_specs(
    entries_by_name: dict[str, PubEntry],
    devices_by_key: dict[str, DetectedDevice],
    entry_to_device_key: dict[str, str],
    entry_to_channel_name: dict[str, str],
) -> list[EntitySpec]:
    """Emit HA entities from pub entries using only device type + signature.

    No user-chosen name prefixes (OUTPUT_/INPUT_/SW_/DIMMER_/ANALOG_) are
    consulted. Each entry's register + storage + the owning device's
    `type_key` fully determine the emitted platform and role.
    """
    specs: list[EntitySpec] = []
    for entry in _sorted_entries_by_name(list(entries_by_name.values())):
        device_key = entry_to_device_key.get(entry.name)
        if device_key is None:
            continue
        device = devices_by_key.get(device_key)
        if device is None or not entry.readable:
            continue
        channel_name = entry_to_channel_name.get(entry.name, entry.name)
        type_key = device.type_key

        if _matches_signature(entry, register="R", storage="B", data_type="BOOL"):
            if _has_command_suffix(entry.name):
                continue
            if type_key not in {
                "generic_relay_module",
                "generic_room_controller",
            }:
                continue
            on_name = (
                entry.name + "_ON" if entry.name + "_ON" in entries_by_name else None
            )
            off_name = (
                entry.name + "_OFF" if entry.name + "_OFF" in entries_by_name else None
            )
            if on_name is None and off_name is None:
                continue
            normalized = channel_name.lower()
            if type_key == "generic_room_controller":
                platform = "light"
                device_class: str | None = None
            else:
                platform = "light" if "light" in normalized else "switch"
                device_class = "outlet" if "plug" in normalized else None
            specs.append(
                EntitySpec(
                    key=_slugify(entry.name),
                    platform=platform,
                    name=channel_name,
                    state_name=entry.name,
                    value_kind=entry.value_kind,
                    on_command_name=on_name,
                    off_command_name=off_name,
                    device_class=device_class,
                    device_key=device_key,
                )
            )
            continue

        if _matches_signature(entry, register="R", storage="F", data_type="REAL"):
            if type_key != "generic_dimmer_module":
                continue
            on_name = (
                entry.name + "_ON" if entry.name + "_ON" in entries_by_name else None
            )
            off_name = (
                entry.name + "_OFF" if entry.name + "_OFF" in entries_by_name else None
            )
            specs.append(
                EntitySpec(
                    key=_slugify(entry.name),
                    platform="light",
                    name=channel_name,
                    state_name=entry.name,
                    value_kind=entry.value_kind,
                    on_command_name=on_name,
                    off_command_name=off_name,
                    set_value_name=entry.name if entry.writable else None,
                    supports_brightness=True,
                    device_key=device_key,
                )
            )
            continue

        if _matches_signature(entry, register="X", storage="B", data_type="BOOL"):
            if type_key not in {
                "generic_input_module",
                "generic_room_controller",
                "generic_analog_sensor_module",
            }:
                continue
            specs.append(
                EntitySpec(
                    key=_slugify(entry.name),
                    platform="binary_sensor",
                    name=channel_name,
                    state_name=entry.name,
                    value_kind=entry.value_kind,
                    device_class=_binary_sensor_device_class(channel_name),
                    device_key=device_key,
                )
            )
            continue

        if _matches_signature(entry, register="X", storage="F", data_type="REAL"):
            if type_key not in {
                "generic_room_controller",
                "generic_analog_sensor_module",
            }:
                continue
            specs.append(
                EntitySpec(
                    key=_slugify(entry.name),
                    platform="sensor",
                    name=channel_name,
                    state_name=entry.name,
                    value_kind=entry.value_kind,
                    device_class="temperature",
                    native_unit_of_measurement="°C",
                    device_key=device_key,
                )
            )
            continue

        if entry.register == "Y" and entry.name.startswith(_STATE_PREFIX):
            if type_key not in {
                "generic_relay_module",
                "generic_dimmer_module",
                "generic_room_controller",
            }:
                continue
            stem = entry.name[len(_STATE_PREFIX):]
            if (
                stem in entries_by_name
                or stem + "_ON" in entries_by_name
                or stem + "_OFF" in entries_by_name
                or stem + "_TRIG" in entries_by_name
            ):
                continue
            if _matches_signature(entry, register="Y", storage="B", data_type="BOOL"):
                specs.append(
                    EntitySpec(
                        key=_slugify(stem),
                        platform="binary_sensor",
                        name=channel_name,
                        state_name=entry.name,
                        value_kind=entry.value_kind,
                        device_class=_binary_sensor_device_class(channel_name),
                        device_key=device_key,
                    )
                )
                continue
            if _matches_signature(entry, register="Y", storage="F", data_type="REAL"):
                specs.append(
                    EntitySpec(
                        key=_slugify(stem),
                        platform="sensor",
                        name=channel_name,
                        state_name=entry.name,
                        value_kind=entry.value_kind,
                        device_key=device_key,
                    )
                )
                continue

    return specs


def build_catalog(
    entries: tuple[PubEntry, ...], runtime_inventory: RuntimeInventory
) -> EntityCatalog:
    """Build the supported Home Assistant entity and device catalog.

    Runtime inventory is the identity source (what hardware exists); the `.pub`
    export is the address source (where each channel sits in X/Y/R RAM). The
    two are joined by name-stem clustering: entries that share a common
    underscore prefix after stripping the generated `state_` prefix and
    `_ON/_OFF/_TRIG` command suffixes are attributed to one logical device,
    then paired with the modules in program order (`_match_runtime_inventory`).
    """
    entries_by_name = {entry.name: entry for entry in entries}
    # Modules with an unknown type code have no channel layout, so they can
    # neither be matched nor produce entities. Keep them out of the inventory
    # the rest of the catalog sees (no empty "Unknown" devices in HA) and
    # report them separately. The groups keep the full discovery for diagnostics.
    unsupported_runtime_devices = tuple(
        device
        for device in runtime_inventory.devices
        if device.runtime_type_code not in RUNTIME_MODEL_INFOS_BY_CODE
    )
    if unsupported_runtime_devices:
        runtime_inventory = replace(
            runtime_inventory,
            devices=tuple(
                device
                for device in runtime_inventory.devices
                if device.runtime_type_code in RUNTIME_MODEL_INFOS_BY_CODE
            ),
        )
    devices_by_key, entry_to_device_key, entry_to_channel_name = _detect_devices(
        entries_by_name
    )
    devices_by_key, unmatched_runtime_devices, unmatched_pub_devices = (
        _match_runtime_inventory(devices_by_key, runtime_inventory)
    )
    entry_to_device_key = {
        entry_name: device_key
        for entry_name, device_key in entry_to_device_key.items()
        if device_key in devices_by_key
    }
    entry_to_channel_name = {
        entry_name: channel_name
        for entry_name, channel_name in entry_to_channel_name.items()
        if entry_name in entry_to_device_key
    }
    pub_entities = _build_pub_entity_specs(
        entries_by_name,
        devices_by_key,
        entry_to_device_key,
        entry_to_channel_name,
    )

    # Modules the map does not cover are listed as devices but get no entities:
    # without the map, where their data starts in the X/Y images is unknown
    # (the order and sizes are known, the start is not), and guessed addresses
    # would show plausible-looking wrong values.
    entities = tuple(pub_entities)
    state_names = tuple(dict.fromkeys(entity.state_name for entity in entities))

    for runtime_device in unmatched_runtime_devices:
        key = runtime_device.registry_key
        if key in devices_by_key:
            continue
        devices_by_key[key] = DetectedDevice(
            key=key,
            name=f"{runtime_device.model} slot {runtime_device.runtime_index}",
            type_key=runtime_device.type_key,
            model=runtime_device.model,
            subsystem=runtime_device.subsystem,
            manufacturer=runtime_device.manufacturer,
            unit_code=runtime_device.unit_code,
            runtime_type_code=runtime_device.runtime_type_code,
            runtime_group_token=runtime_device.group_token,
            runtime_child_token=runtime_device.child_token,
            runtime_slot_index=runtime_device.slot_index,
            runtime_index=runtime_device.runtime_index,
            discovery_source="epsnet_runtime_only",
        )

    entity_keys_by_device: dict[str, list[str]] = defaultdict(list)
    for entity in entities:
        if entity.device_key is not None:
            entity_keys_by_device[entity.device_key].append(entity.key)

    devices_with_entities = {
        key: replace(
            device,
            entity_keys=tuple(sorted(entity_keys_by_device.get(key, ()))),
        )
        for key, device in devices_by_key.items()
    }

    return EntityCatalog(
        entries_by_name=entries_by_name,
        device_types_by_key=DEVICE_TYPES_BY_KEY,
        devices_by_key=devices_with_entities,
        entities=entities,
        state_names=state_names,
        runtime_inventory=runtime_inventory,
        unmatched_runtime_devices=unmatched_runtime_devices,
        unmatched_pub_devices=unmatched_pub_devices,
        unsupported_runtime_devices=unsupported_runtime_devices,
    )


def unsupported_module_summary(catalog: EntityCatalog) -> tuple[int, str]:
    """Return (count, "0x0d31 ×11, 0x0c38") for modules with no definition yet."""
    counts = Counter(
        device.runtime_type_code for device in catalog.unsupported_runtime_devices
    )
    codes = ", ".join(
        f"0x{code:04x}" + (f" ×{count}" if count > 1 else "")
        for code, count in sorted(counts.items())
    )
    return sum(counts.values()), codes
