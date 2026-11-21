"""The entity and device catalog: the scanned modules joined with the variable map.

A map variable belongs to the module whose inputs or outputs its address lies
in (the PLC program's I/O layout gives every module's place). Names only decide
what the address cannot: how a module is called, which variables in the
program's memory go with an output (its state and commands), and, where the
layout is not available, which module a group of variables is.
"""

from __future__ import annotations

from collections import defaultdict, Counter
from dataclasses import dataclass, replace
from functools import cmp_to_key, lru_cache
import re

from .program_entities import (
    ModulePlace,
    PlacedChannel,
    build_program_entities,
    module_places,
)
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
    ValueKind,
)

_STATE_PREFIX = "state_"
_COMMAND_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_TRIG", "r_cmd_trig"),
    ("_OFF", "r_cmd_off"),
    ("_ON", "r_cmd_on"),
)

# The device of the program's variables that no module entity uses. Its key
# cannot be a module's: those are made of letters, digits and underscores.
PROGRAM_VARIABLES_DEVICE_KEY = "program-variables"
PROGRAM_VARIABLE_KEY_PREFIX = "var:"
_VARIABLE_NUMBER_TYPES = frozenset(
    {"BYTE", "USINT", "SINT", "INT", "UINT", "WORD", "DINT", "UDINT", "DWORD", "REAL", "LREAL"}
)
# the client reads and writes with 16-bit addresses
_LAST_ADDRESS = 0xFFFF
# Key endings of what a press of an input gives: the input as it is written,
# and the on/off switch of an output that is shown by a value sensor.
PRESS_KEY_SUFFIX = "_press"

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

    # Only the variables some group can hold: one that fits none would end the
    # search at its place in the order and lose every variable after it.
    _, selected_indexes = _solve(tuple(sorted(candidate_indexes_by_stem)))
    return tuple(candidates[index] for index in selected_indexes)


def _attribute_stems(
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]],
    stems: tuple[str, ...],
    prefix: str,
    device_key: str,
    entry_to_device_key: dict[str, str],
    entry_to_channel_name: dict[str, str],
) -> None:
    """Give the stems' variables to a device; a channel is its stem without the device's prefix."""
    for raw_stem in stems:
        channel_name = (
            raw_stem[len(prefix) :] if prefix and raw_stem.startswith(prefix) else ""
        ) or raw_stem
        for role_entries_for_stem in raw_roles_by_stem[raw_stem].values():
            for entry in role_entries_for_stem:
                entry_to_device_key[entry.name] = device_key
                entry_to_channel_name[entry.name] = channel_name


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
    """Pair name-grouped map devices with modules, by type and order.

    Used where the program's layout does not say whose a variable's address is.
    The PLC lays its modules' data out in the X/Y images in the order of the
    program's hardware list, and discovery reports each module's position in
    that list. So walking the modules in program order and giving each one the
    earliest (by image address) still-unpaired compatible map device pairs
    same-model modules, without knowing where the CIB data starts or how large
    each module is.
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




# Roles of variables that sit in the X/Y images (a module's inputs and outputs).
_IMAGE_ROLES = ("x_bool", "x_real", "y_bool_state", "y_real_state")

# A module's inputs lie next to each other in X, and its outputs in Y: 13 bytes
# in the largest module seen. Variables that share a name prefix but lie
# further apart than this are not one module's.
_MODULE_IMAGE_SPAN_MAX = 64


def _spread_wider_than_a_module(
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]], stems: tuple[str, ...]
) -> bool:
    for roles in (("x_bool", "x_real"), ("y_bool_state", "y_real_state")):
        addresses = [
            entry.address
            for stem in stems
            for role in roles
            for entry in raw_roles_by_stem[stem].get(role, ())
        ]
        if addresses and max(addresses) - min(addresses) >= _MODULE_IMAGE_SPAN_MAX:
            return True
    return False


def _shared_prefix_length(stem: str, other: str) -> int:
    for prefix in reversed(_underscore_prefixes(stem)):
        if other.startswith(prefix):
            return len(prefix)
    return 0


def _place_stems(
    raw_roles_by_stem: dict[str, dict[str, list[PubEntry]]],
    candidates: tuple[_DeviceCandidate, ...],
    owner: dict[tuple[str, int], int],
) -> dict[str, int]:
    """Stem -> index of the module its variables belong to.

    ``owner`` says whose each byte of the X/Y images is. A variable lying in a
    module's inputs or outputs belongs to that module, whatever it is called.
    Variables in the program's own memory (an output's state and its commands)
    have no such address; they go with the variables the name clustering
    groups them with.
    """
    placed: dict[str, int] = {}
    for stem, roles in raw_roles_by_stem.items():
        hits = Counter(
            owner[(entry.register, entry.address)]
            for role in _IMAGE_ROLES
            for entry in roles.get(role, ())
            if (entry.register, entry.address) in owner
        )
        if hits:
            placed[stem] = min(hits, key=lambda index: (-hits[index], index))
    for candidate in candidates:
        if _spread_wider_than_a_module(raw_roles_by_stem, candidate.raw_stems):
            continue  # a shared prefix, not a module: each variable for itself
        anchors = sorted(stem for stem in candidate.raw_stems if stem in placed)
        if not anchors:
            continue
        for stem in candidate.raw_stems:
            if stem in anchors:
                continue
            nearest = min(
                anchors,
                key=lambda anchor: (-_shared_prefix_length(stem, anchor), placed[anchor], anchor),
            )
            placed[stem] = placed[nearest]
    return placed


def _could_lie_in(image_bytes, roles: dict[str, list[PubEntry]]) -> bool:
    """False for a variable in X or Y that lies far from the module's own bytes.

    Such a variable is not the module's, whatever it is called. (One close by
    can be: the layout does not place every byte of every module.)
    """
    for role in _IMAGE_ROLES:
        for entry in roles.get(role, ()):
            distances = [
                abs(address - entry.address)
                for register, address in image_bytes
                if register == entry.register
            ]
            if not distances or min(distances) >= _MODULE_IMAGE_SPAN_MAX:
                return False
    return True


def _device_prefix(stems: tuple[str, ...]) -> str | None:
    """The name prefix a module's variables share: its name in the program.

    All of them, or most: one variable named differently does not unname the
    module. ``None`` when they share none (names without a common structure);
    the module is then known by its hardware only.
    """
    counts = Counter(
        prefix for stem in stems for prefix in _underscore_prefixes(stem)
    )
    if not counts:
        return None
    prefix = max(counts, key=lambda candidate: (counts[candidate], len(candidate), candidate))
    if counts[prefix] < len(stems) and counts[prefix] * 2 <= len(stems):
        return None
    return prefix if _slugify(prefix) else None


def _free_key(key: str, taken: dict[str, DetectedDevice]) -> tuple[str, int]:
    """(``key`` or the first free ``key_2``, ``key_3``…, its number)."""
    number, free = 1, key
    while free in taken:
        number += 1
        free = f"{key}_{number}"
    return free, number


def _resolve_devices(
    entries_by_name: dict[str, PubEntry],
    runtime_inventory: RuntimeInventory,
) -> tuple[
    dict[str, DetectedDevice],
    dict[str, str],
    dict[str, str],
    tuple[RuntimeDiscoveredDevice, ...],
    tuple[DetectedDevice, ...],
]:
    """Find the device of every map variable.

    Returns the devices that have a module, the variables' device and channel
    names, the modules the map says nothing about, and the name groups no
    module was found for.

    By address first: the PLC program's I/O layout says which bytes of the X/Y
    images are whose, so a variable there has its module for certain. Such a
    module is called by the prefix its variables share (``SW_hall`` for
    ``SW_hall_UP1``, ``SW_hall_TERM``…); when they share none it stays known
    by its hardware, and its variables keep their full names.

    What the layout does not place (no layout read, a module it could not
    decode, groups of variables in the program's memory only) is grouped by
    name and paired with the remaining modules in order, as before the layout
    was known.
    """
    raw_roles_by_stem = _collect_generic_raw_roles(entries_by_name)
    candidates = _select_candidates(raw_roles_by_stem, _evaluate_generic_candidate)
    places = module_places(runtime_inventory.program_layout, runtime_inventory.devices)
    owner = {
        image_byte: index
        for index, place in enumerate(places)
        for image_byte in place.image_bytes
    }
    placed = _place_stems(raw_roles_by_stem, candidates, owner)

    devices: dict[str, DetectedDevice] = {}
    entry_to_device_key: dict[str, str] = {}
    entry_to_channel_name: dict[str, str] = {}

    stems_by_place: dict[int, list[str]] = defaultdict(list)
    for stem, index in placed.items():
        stems_by_place[index].append(stem)
    prefix_by_place = {
        index: _device_prefix(tuple(stems)) for index, stems in stems_by_place.items()
    }
    # A variable in the program's memory that starts with a module's name is
    # that module's too, also when the name clustering put it elsewhere.
    for stem in sorted(set(raw_roles_by_stem) - set(placed)):
        named_like = [
            (-len(prefix), index)
            for index, prefix in prefix_by_place.items()
            if prefix
            and stem.startswith(prefix)
            and _could_lie_in(places[index].image_bytes, raw_roles_by_stem[stem])
        ]
        if named_like:
            index = min(named_like)[1]
            placed[stem] = index
            stems_by_place[index].append(stem)
    for index in sorted(stems_by_place):
        runtime_device = places[index].runtime
        stems = tuple(sorted(stems_by_place[index]))
        prefix = prefix_by_place[index]
        if prefix is None:
            prefix = ""
            key = runtime_device.registry_key
            name = f"{runtime_device.model} slot {runtime_device.runtime_index}"
        else:
            key, number = _free_key(_slugify(prefix[:-1]), devices)
            name = prefix[:-1] if number == 1 else f"{prefix[:-1]} {number}"
        detected = _merge_runtime_metadata(
            _build_detected_device(
                key=key,
                name=name,
                type_key=runtime_device.type_key,
                model=runtime_device.model,
                role_entries=_aggregate_roles(raw_roles_by_stem, stems),
            ),
            runtime_device,
        )
        devices[key] = replace(
            detected,
            paired_by="address",
            # known by its hardware: not a device the map names
            discovery_source=detected.discovery_source if prefix else "epsnet_runtime_only",
        )
        _attribute_stems(
            raw_roles_by_stem, stems, prefix, key, entry_to_device_key, entry_to_channel_name
        )

    # The rest by name: groups none of whose variables the layout places, for
    # the modules none of whose inputs or outputs the map names.
    by_name: dict[str, DetectedDevice] = {}
    stems_by_name_key: dict[str, list[tuple[tuple[str, ...], str]]] = defaultdict(list)
    for candidate in candidates:
        if any(stem in placed for stem in candidate.raw_stems):
            continue
        if _spread_wider_than_a_module(raw_roles_by_stem, candidate.raw_stems):
            continue
        prefix = _longest_common_underscore_prefix(candidate.raw_stems) or candidate.prefix
        key = _slugify(prefix[:-1])
        if key in devices:
            key, _ = _free_key(key, {**devices, **by_name})
        by_name[key] = _build_detected_device(
            key=key,
            name=prefix[:-1],
            type_key=candidate.type_key,
            model=candidate.model,
            role_entries=_aggregate_roles(raw_roles_by_stem, candidate.raw_stems),
        )
        stems_by_name_key[key].append((candidate.raw_stems, prefix))
    named_modules = {places[index].runtime.registry_key for index in stems_by_place} | {
        # also by a variable that only gives its name to an input or output
        places[owner[(entry.register, entry.address)]].runtime.registry_key
        for entry in entries_by_name.values()
        if (entry.register, entry.address) in owner
    }
    matched, _, unmatched_pub_devices = _match_runtime_inventory(
        by_name,
        replace(
            runtime_inventory,
            devices=tuple(
                device
                for device in runtime_inventory.devices
                if device.registry_key not in named_modules
            ),
        ),
    )
    for key, device in matched.items():
        devices[key] = replace(device, paired_by="order")
        for stems, prefix in stems_by_name_key[key]:
            _attribute_stems(
                raw_roles_by_stem, stems, prefix, key, entry_to_device_key, entry_to_channel_name
            )

    with_device = {
        (device.runtime_group_token, device.runtime_slot_index) for device in devices.values()
    }
    unmatched_runtime_devices = tuple(
        device
        for device in sorted(runtime_inventory.devices, key=_program_order_key)
        if (device.group_token, device.slot_index) not in with_device
    )
    return (
        devices,
        entry_to_device_key,
        entry_to_channel_name,
        unmatched_runtime_devices,
        unmatched_pub_devices,
    )


def _map_names_by_address(
    entries: tuple[PubEntry, ...],
    specs: list[EntitySpec],
    entry_to_channel_name: dict[str, str],
) -> dict[tuple[str, int, int | None], tuple[str, str]]:
    """What the map calls the inputs and outputs: (name, data type) by address.

    An output a light or a switch already stands for is left out: its entity
    carries the name, and the raw output keeps its hardware label.
    """
    switched = {spec.state_name for spec in specs if spec.platform in ("light", "switch")}
    names: dict[tuple[str, int, int | None], tuple[str, str]] = {}
    for entry in _sorted_entries_by_name(entries):
        if entry.register not in ("X", "Y"):
            continue
        stem = entry.name
        if entry.register == "Y" and stem.startswith(_STATE_PREFIX):
            stem = stem[len(_STATE_PREFIX) :]
        if stem in switched:
            continue
        names.setdefault(
            (entry.register, entry.address, entry.bit),
            (entry_to_channel_name.get(entry.name) or stem, entry.data_type),
        )
    return names


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


def _build_variable_entity_specs(
    entries: tuple[PubEntry, ...],
    entities: tuple[EntitySpec, ...],
    entry_to_device_key: dict[str, str],
    module_bytes: set[tuple[str, int]],
) -> list[EntitySpec]:
    """The published variables no entity uses, each offered as it is.

    A program may publish more than the modules' inputs and outputs and the
    command variables of lights and switches: flags, setpoints, commands under
    names of its own. Each becomes an entity of the variable's value, created
    disabled: nothing tells a command from a status flag, so the user picks.
    A variable in the program's memory can be written (a switch, a number);
    one elsewhere is only shown.

    Left out: what an entity already stands for (its state and commands, an
    input or output of a module) and types that are not one plain value.
    """
    used: set[str] = set()
    for spec in entities:
        used.update(
            name
            for name in (
                spec.state_name,
                spec.on_command_name,
                spec.off_command_name,
                spec.set_value_name,
            )
            if name
        )
        if spec.platform in ("light", "switch"):
            # the whole family of the output, also the commands it does not use
            used.add(_STATE_PREFIX + spec.state_name)
            used.update(spec.state_name + suffix for suffix, _ in _COMMAND_SUFFIXES)

    specs: list[EntitySpec] = []
    for entry in _sorted_entries_by_name(list(entries)):
        if entry.is_virtual or entry.name in used:
            continue
        if entry.register in ("X", "Y") and (
            entry.name in entry_to_device_key
            or (entry.register, entry.address) in module_bytes
        ):
            continue  # a module's input or output: the module's entities show it
        data_type = entry.data_type.upper()
        is_bool = data_type == "BOOL"
        if not is_bool and data_type not in _VARIABLE_NUMBER_TYPES:
            continue
        if entry.address < 0 or entry.address + entry.byte_size - 1 > _LAST_ADDRESS:
            continue
        writable = entry.writable and entry.register == "R"
        if is_bool:
            platform = "switch" if writable else "binary_sensor"
        else:
            platform = "number" if writable else "sensor"
        specs.append(
            EntitySpec(
                key=PROGRAM_VARIABLE_KEY_PREFIX + entry.name,
                platform=platform,
                name=entry.name,
                state_name=entry.name,
                value_kind=entry.value_kind,
                set_value_name=entry.name if writable else None,
                enabled_by_default=False,
                device_key=PROGRAM_VARIABLES_DEVICE_KEY,
                variable=True,
            )
        )
    return specs


def _press_entry(button: PlacedChannel) -> PubEntry:
    """The input of a button, as the place a press is written to."""
    return PubEntry(
        name=button.key + PRESS_KEY_SUFFIX,
        register=button.register,
        storage="B",
        address=button.address,
        bit=button.bit,
        data_type="BOOL",
        access="PUB_INOUT",
        line_number=0,
        is_virtual=True,
    )


def _add_press_entities(
    entities: tuple[EntitySpec, ...],
    entries_by_name: dict[str, PubEntry],
    channels: list[PlacedChannel],
) -> tuple[tuple[EntitySpec, ...], dict[str, PubEntry]]:
    """Control that needs no command variables, with or without a variable map.

    Two things the PLC does make it possible. It takes a 1 written to an input
    as one short press: the program sees it for a cycle, then the PLC reads the
    input again. And an output keeps a value written into it, unless the
    program assigns that output itself. So:

    - every button of a wall-switch module gets a button entity that presses
      it. The program does whatever it does on a press of the real button.
    - an output without command variables is controlled itself. A relay or an
      indicator LED becomes a switch, a dimmer output a light with brightness.
      A relay or dimmer module has a front button per output, and programs
      commonly flip output n on front button n: such an output is switched by
      a press of that button first (only when it is not in the wanted state),
      and by a value written into it when the press changes nothing. An LED
      and a brightness are written directly. The output is read back each time.

    Needs the program's I/O layout (it says where the buttons are). Returns the
    entities and the entries of the inputs that are pressed.
    """
    specs = list(entities)
    shown: dict[tuple[str, int, int | None], int] = {}
    commanded: set[tuple[str, int, int | None]] = set()
    for index, spec in enumerate(specs):
        if spec.variable:
            continue
        entry = entries_by_name[spec.state_name]
        if entry.register in ("X", "Y"):
            shown.setdefault((entry.register, entry.address, entry.bit), index)
        if spec.platform in ("light", "switch"):
            # an output its command variables switch: the map says where it is
            output = entries_by_name.get(_STATE_PREFIX + spec.state_name)
            if output is not None and output.register == "Y":
                commanded.add((output.register, output.address, output.bit))

    pressed: dict[str, PubEntry] = {}
    added: list[EntitySpec] = []
    front_buttons: dict[str, list[PlacedChannel]] = defaultdict(list)
    switched: dict[str, list[PlacedChannel]] = defaultdict(list)
    outputs: list[PlacedChannel] = []
    for channel in channels:
        if channel.role == "local_switch":
            front_buttons[channel.module].append(channel)
        elif channel.role in ("relay", "dimmer"):
            switched[channel.module].append(channel)
            outputs.append(channel)
        elif channel.role == "led":
            outputs.append(channel)
        if channel.role != "button":
            continue
        index = shown.get((channel.register, channel.address, channel.bit))
        sensor = specs[index] if index is not None else None
        entry = _press_entry(channel)
        pressed[entry.name] = entry
        added.append(
            EntitySpec(
                key=entry.name,
                platform="button",
                name=sensor.name if sensor else channel.label,
                state_name=entry.name,
                value_kind=ValueKind.BOOL,
                enabled_by_default=sensor.enabled_by_default if sensor else True,
                device_key=sensor.device_key if sensor else channel.device_key,
                from_program=True,
                press_name=entry.name,
            )
        )

    front_button_of: dict[str, PlacedChannel] = {}
    for module, module_outputs in switched.items():
        buttons = front_buttons.get(module, [])
        if len(buttons) == len(module_outputs):
            # otherwise there is no front button per output: nothing says which is whose
            front_button_of.update(
                (output.key, button) for button, output in zip(buttons, module_outputs)
            )

    for output in outputs:
        place = (output.register, output.address, output.bit)
        index = shown.get(place)
        if place in commanded or index is None:
            continue
        spec = specs[index]
        button = front_button_of.get(output.key)
        entry = _press_entry(button) if button is not None else None
        press_name = entry.name if entry is not None else None
        if spec.platform == "binary_sensor":
            specs[index] = replace(
                spec,
                platform="switch",
                device_class="outlet" if "plug" in spec.name.lower() else None,
                press_name=press_name,
                direct_output=True,
            )
        elif spec.platform == "sensor" and output.role == "dimmer":
            specs[index] = replace(
                spec,
                platform="light",
                device_class=None,
                native_unit_of_measurement=None,
                supports_brightness=True,
                brightness_scale=100.0,
                press_name=press_name,
                direct_output=True,
            )
        else:
            continue
        if entry is not None:
            pressed[entry.name] = entry
    return tuple(specs + added), pressed


def _program_variables_device(runtime_inventory: RuntimeInventory) -> DetectedDevice:
    """The PLC itself, as the device of its program's variables."""
    model = (runtime_inventory.controller_identity or "").split(" ", 1)[0] or "PLC"
    return DetectedDevice(
        key=PROGRAM_VARIABLES_DEVICE_KEY,
        name=model,
        type_key="plc",
        model=model,
        subsystem="plc",
    )


def build_catalog(
    entries: tuple[PubEntry, ...],
    runtime_inventory: RuntimeInventory,
    *,
    program_first: bool = False,
) -> EntityCatalog:
    """Build the supported Home Assistant entity and device catalog.

    Runtime inventory is the identity source (what hardware exists); the `.pub`
    export names it and adds what only the program knows (an output's state and
    command variables in R). The two are joined by address: a variable in a
    module's inputs or outputs is that module's (`_resolve_devices`).
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
    (
        devices_by_key,
        entry_to_device_key,
        entry_to_channel_name,
        unmatched_runtime_devices,
        unmatched_pub_devices,
    ) = _resolve_devices(entries_by_name, runtime_inventory)
    pub_entities = _build_pub_entity_specs(
        entries_by_name,
        devices_by_key,
        entry_to_device_key,
        entry_to_channel_name,
    )
    map_names = _map_names_by_address(entries, pub_entities, entry_to_channel_name)

    # Modules the map says nothing about are listed as devices; their entities
    # come from the program's I/O layout below (none when it was not read).
    entities = tuple(pub_entities)

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

    # Channels placed by the PLC program's own I/O layout.
    layout = runtime_inventory.program_layout
    keep_all = bool(program_first and entries and layout is not None and not layout.error)
    if keep_all:
        # The entry was set up without a map, so its entities are the program's
        # (keyed by hardware). A map added later must not replace them: it names
        # them, and adds what the program alone cannot do (lights / switches).
        devices_by_key, entities = _keep_runtime_device_keys(devices_by_key, entities)

    def _known_by_hardware(device_key: str | None) -> bool:
        # every module of an entry set up without a map, and any module the
        # map gives no name of its own
        device = devices_by_key.get(device_key) if device_key else None
        return keep_all or (
            device is not None and device.discovery_source == "epsnet_runtime_only"
        )

    def _is_the_programs(spec: EntitySpec) -> bool:
        # An input or output of a module known by its hardware is shown by the
        # program's entity (named by the map), not by one keyed by a variable
        # name.
        return spec.platform in ("binary_sensor", "sensor") and _known_by_hardware(
            spec.device_key
        )

    shown_specs = tuple(spec for spec in entities if not _is_the_programs(spec))
    program = build_program_entities(
        layout,
        runtime_inventory.devices,
        devices_by_key,
        entries,
        keep_all=keep_all,
        names=map_names,
        shown=tuple(entries_by_name[spec.state_name] for spec in shown_specs),
        # Only a module the map names as a device has its other channels start
        # disabled; one known by its hardware keeps all of them enabled.
        shown_devices={
            spec.device_key
            for spec in shown_specs
            if spec.device_key and not _known_by_hardware(spec.device_key)
        },
    )
    # A map entity reading a byte the program places for such a module gives
    # way to the program's entity (also the map's whole-byte flags without a bit).
    placed_bytes = {(entry.register, entry.address) for entry in program.entries.values()}
    entities = tuple(
        spec
        for spec in entities
        if not (
            _is_the_programs(spec)
            and _state_address(entries_by_name, spec)[:2] in placed_bytes
        )
    )
    devices_by_key.update(program.devices)
    entries_by_name.update(program.entries)
    # An input the map names like a motion sensor or a door contact is one,
    # as for the map's own entities (hardware labels never read like that).
    entities = entities + tuple(
        replace(spec, device_class=_binary_sensor_device_class(spec.name))
        if spec.platform == "binary_sensor" and spec.device_class is None
        else spec
        for spec in program.specs
    )
    entities, pressed = _add_press_entities(entities, entries_by_name, program.channels)
    entries_by_name.update(pressed)
    variable_specs = _build_variable_entity_specs(
        entries,
        entities,
        entry_to_device_key,
        {
            image_byte
            for place in module_places(layout, runtime_inventory.devices)
            for image_byte in place.image_bytes
        }
        | placed_bytes,
    )
    if variable_specs:
        entities = entities + tuple(variable_specs)
        devices_by_key[PROGRAM_VARIABLES_DEVICE_KEY] = _program_variables_device(
            runtime_inventory
        )
    state_names = tuple(dict.fromkeys(entity.state_name for entity in entities))

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
        program_report=tuple(program.report),
        channels_named_by_map=program.named,
    )


def _state_address(entries_by_name, spec) -> tuple[str, int, int | None]:
    entry = entries_by_name[spec.state_name]
    return entry.register, entry.address, entry.bit


def _keep_runtime_device_keys(devices_by_key, entities):
    """Identify map devices by their module slot, as before the map existed."""
    renamed = {
        key: device.runtime_registry_key
        for key, device in devices_by_key.items()
        if device.runtime_group_token is not None and device.discovery_source != "epsnet_runtime_only"
    }
    devices = {renamed.get(key, key): replace(device, key=renamed.get(key, key))
               for key, device in devices_by_key.items()}
    specs = tuple(
        replace(spec, device_key=renamed.get(spec.device_key, spec.device_key))
        for spec in entities
    )
    return devices, specs


def unsupported_module_summary(catalog: EntityCatalog) -> tuple[int, str]:
    """Return (count, "0x0d99 ×11, 0x0c38") for modules with no definition yet."""
    counts = Counter(
        device.runtime_type_code for device in catalog.unsupported_runtime_devices
    )
    codes = ", ".join(
        f"0x{code:04x}" + (f" ×{count}" if count > 1 else "")
        for code, count in sorted(counts.items())
    )
    return sum(counts.values()), codes
