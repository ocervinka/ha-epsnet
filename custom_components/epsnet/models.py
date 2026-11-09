"""Pure data models for the EPSNET integration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

ScalarValue = bool | float | int | str


@dataclass(frozen=True, slots=True)
class EpsnetReadMetrics:
    """One completed transport read batch."""

    duration_seconds: float
    point_count: int
    packet_count: int
    mode: str
    span_count: int | None = None
    # Per-block (per-span) request->reply timing within this batch.
    block_min_seconds: float | None = None
    block_avg_seconds: float | None = None
    block_max_seconds: float | None = None


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


class ValueKind(StrEnum):
    """Supported scalar value kinds."""

    BOOL = "bool"
    FLOAT = "float"
    INT = "int"
    STRING = "string"


def _value_kind_for_data_type(data_type: str) -> ValueKind:
    normalized = data_type.upper()
    if normalized == "BOOL":
        return ValueKind.BOOL
    if normalized in {"REAL", "LREAL"}:
        return ValueKind.FLOAT
    if normalized in {
        "BYTE",
        "USINT",
        "INT",
        "UINT",
        "DINT",
        "UDINT",
        "TIME",
        "TIME_OF_DAY",
        "DATE",
        "DATE_AND_TIME",
        "DWORD",
    }:
        return ValueKind.INT
    return ValueKind.STRING


def _byte_size_for_data_type(data_type: str, *, point_name: str) -> int:
    try:
        return _BYTE_SIZES[data_type.upper()]
    except KeyError as err:
        raise ValueError(
            f"Unsupported data type {data_type!r} for {point_name!r}"
        ) from err


@dataclass(frozen=True, slots=True)
class PubEntry:
    """One line from a .pub mapping file."""

    name: str
    register: str
    storage: str
    address: int
    bit: int | None
    data_type: str
    access: str
    line_number: int
    is_virtual: bool = False


    @property
    def readable(self) -> bool:
        """Return True when the exported variable can be read."""
        return self.access in {"PUB_OUT", "PUB_INOUT"}

    @property
    def writable(self) -> bool:
        """Return True when the exported variable can be written."""
        return self.access in {"PUB_IN", "PUB_INOUT"}

    @property
    def value_kind(self) -> ValueKind:
        """Return the high-level Python value kind."""
        return _value_kind_for_data_type(self.data_type)

    @property
    def byte_size(self) -> int:
        """Return the exported byte width for this value."""
        return _byte_size_for_data_type(self.data_type, point_name=self.name)

    def to_point(
        self,
        *,
        key: str | None = None,
        name: str | None = None,
        device_key: str | None = None,
        source: str = "pub",
    ) -> EpsnetPoint:
        """Convert this exported variable to a generic addressable point."""
        return EpsnetPoint(
            key=key or self.name,
            name=name or self.name,
            register=self.register,
            storage=self.storage,
            address=self.address,
            bit=self.bit,
            data_type=self.data_type,
            readable=self.readable,
            writable=self.writable,
            device_key=device_key,
            source=source,
        )


@dataclass(frozen=True, slots=True)
class EpsnetPoint:
    """One generic addressable point independent of `.pub` storage."""

    key: str
    name: str
    register: str
    storage: str
    address: int
    bit: int | None
    data_type: str
    readable: bool = True
    writable: bool = False
    device_key: str | None = None
    source: str = "runtime"

    @property
    def value_kind(self) -> ValueKind:
        """Return the high-level Python value kind."""
        return _value_kind_for_data_type(self.data_type)

    @property
    def byte_size(self) -> int:
        """Return the exported byte width for this value."""
        return _byte_size_for_data_type(self.data_type, point_name=self.name)


@dataclass(frozen=True, slots=True)
class AddressRange:
    """One concrete occupied address range in the PLC export."""

    register: str
    storage: str
    data_type: str
    start_address: int
    end_address: int


@dataclass(frozen=True, slots=True)
class DeviceEntryDefinition:
    """One abstract entry role that belongs to a detected device type."""

    role: str
    name_pattern: str
    register: str
    storage: str
    data_type: str


@dataclass(frozen=True, slots=True)
class DeviceTypeDefinition:
    """An abstract logical device type inferred from `.pub` exports."""

    key: str
    name: str
    subsystem: str
    entry_definitions: tuple[DeviceEntryDefinition, ...]


@dataclass(frozen=True, slots=True)
class DetectedDeviceEntryGroup:
    """One set of entries that share a common role in a detected device."""

    role: str
    register: str
    storage: str
    data_type: str
    access_modes: tuple[str, ...]
    address_ranges: tuple[AddressRange, ...]
    entry_names: tuple[str, ...]


class MathFormula(StrEnum):
    """Supported mathematical transformations for raw hardware values."""

    NONE = "none"
    DIV_10 = "div_10"
    DIV_100 = "div_100"
    MUL_10 = "mul_10"
    MUL_100 = "mul_100"


@dataclass(frozen=True, slots=True)
class RuntimeChannelDefinition:
    """One abstract channel exposed by a runtime model formula."""

    role: str
    name_template: str
    channel_index: int
    register: str
    storage: str
    data_type: str
    address_offset: int
    bit_offset: int | None = None
    writable: bool = False
    transform_read: MathFormula = MathFormula.NONE
    transform_write: MathFormula = MathFormula.NONE


@dataclass(frozen=True, slots=True)
class RuntimeModelInfo:
    """Manufacturer metadata resolved from a runtime unit code."""

    unit_code: int
    manufacturer: str
    model: str
    type_key: str
    subsystem: str
    description: str
    channels: tuple[RuntimeChannelDefinition, ...] = ()

    @property
    def input_size(self) -> int:
        """Return the total bytes of X (input) image occupied by this model."""
        return max(
            (
                c.address_offset + _byte_size_for_data_type(c.data_type, point_name=c.name_template)
                for c in self.channels
                if c.register == "X"
            ),
            default=0,
        )

    @property
    def output_size(self) -> int:
        """Return the total bytes of Y (output) image occupied by this model."""
        return max(
            (
                c.address_offset + _byte_size_for_data_type(c.data_type, point_name=c.name_template)
                for c in self.channels
                if c.register == "Y"
            ),
            default=0,
        )


@dataclass(frozen=True, slots=True)
class RuntimeDiscoveryRecord:
    """One generic token/meta pair from the public discovery map."""

    token: int
    meta: int


@dataclass(frozen=True, slots=True)
class RuntimeExactDescriptor:
    """One exact runtime descriptor block read from the public discovery map."""

    token: int
    length: int
    family_word: int
    runtime_unit_code: int
    reserved_word: int
    text: str
    display_name: str | None
    compact_model_code: str | None
    instance_code: str | None
    suffix_code: str | None
    trailer_word: int
    raw_hex: str


@dataclass(frozen=True, slots=True)
class RuntimeDiscoveredDevice:
    """One physical device discovered directly from the live PLC."""

    key: str
    group_token: int
    group_label: str
    group_marker: int
    group_kind: int
    runtime_index: int
    slot_index: int
    child_token: int
    runtime_type_code: int
    model_info: RuntimeModelInfo
    child_record_hex: str | None = None
    exact_descriptor: RuntimeExactDescriptor | None = None

    @property
    def manufacturer(self) -> str:
        """Return the resolved manufacturer label."""
        return self.model_info.manufacturer

    @property
    def model(self) -> str:
        """Return the exact resolved manufacturer model."""
        return self.model_info.model

    @property
    def type_key(self) -> str:
        """Return the generic device family used by the catalog matcher."""
        return self.model_info.type_key

    @property
    def subsystem(self) -> str:
        """Return the Home Assistant subsystem for this device family."""
        return self.model_info.subsystem

    @property
    def unit_code(self) -> int:
        """Return the decimal manufacturer unit code."""
        return self.model_info.unit_code

    @property
    def description(self) -> str:
        """Return a short device-family description."""
        return self.model_info.description

    @property
    def registry_key(self) -> str:
        """Return a stable runtime-derived registry key for this physical slot."""
        return f"runtime_{self.group_token:08x}_{self.slot_index:02d}"

    @property
    def suggested_name(self) -> str:
        """Return a generic runtime-only display name for this device."""
        if self.exact_descriptor is not None and self.exact_descriptor.display_name:
            base_name = self.exact_descriptor.display_name
        else:
            base_name = self.model
        return f"{base_name} slot {self.slot_index:02d}"


@dataclass(frozen=True, slots=True)
class RuntimeCibGroup:
    """One discovered CIB bus group with its parsed runtime inventory."""

    token: int
    meta: int
    label: str
    marker: int
    kind: int
    header_hex: str
    slot_map: tuple[int, ...]
    devices: tuple[RuntimeDiscoveredDevice, ...]
    off_in: int = 0
    off_out: int = 0
    in_len: int = 0
    out_len: int = 0

    @property
    def occupied_slot_count(self) -> int:
        """Return how many slots are populated in this group."""
        return sum(slot != 0x7F for slot in self.slot_map)

    @property
    def empty_slot_count(self) -> int:
        """Return how many slots are empty in this group."""
        return len(self.slot_map) - self.occupied_slot_count


@dataclass(frozen=True, slots=True)
class RuntimeInventory:
    """The complete live hardware topology snapshot from the PLC."""

    controller_identity: str
    root_identity: str
    root_tokens: tuple[int, ...]
    group_table_token: int
    groups: tuple[RuntimeCibGroup, ...]
    devices: tuple[RuntimeDiscoveredDevice, ...]
    root_records: tuple[RuntimeDiscoveryRecord, ...] = ()
    cpu_off_in: int = 0
    cpu_off_out: int = 0
    # (label, hex) of the raw discovery reads, kept for the diagnostics download
    # so layouts from other PLC models can be analysed from a user report.
    raw_reads: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class DetectedDevice:
    """A logical device recognized by matching .pub entries to model signatures."""

    key: str
    name: str
    type_key: str
    model: str
    subsystem: str
    manufacturer: str = "Teco"
    unit_code: int | None = None
    runtime_type_code: int | None = None
    runtime_group_token: int | None = None
    runtime_child_token: int | None = None
    runtime_slot_index: int | None = None
    runtime_index: int | None = None
    discovery_source: str = "pub_file"
    entry_names: tuple[str, ...] = ()
    entry_groups: tuple[DetectedDeviceEntryGroup, ...] = ()
    entity_keys: tuple[str, ...] = ()

    @property
    def registry_keys(self) -> tuple[str, ...]:
        """Return the stable identifiers for the Home Assistant device registry."""
        if self.discovery_source == "epsnet_runtime_only":
            return (self.key,)
        return (self.runtime_registry_key, self.key)

    @property
    def runtime_registry_key(self) -> str:
        """Return the key used to link this device to its runtime discovery record."""
        return f"runtime_{self.runtime_group_token:08x}_{self.runtime_slot_index:02d}"


@dataclass(frozen=True, slots=True)
class EntitySpec:
    """A Home Assistant entity derived from one or more `.pub` entries."""

    key: str
    platform: str
    name: str
    state_name: str
    value_kind: ValueKind
    on_command_name: str | None = None
    off_command_name: str | None = None
    set_value_name: str | None = None
    device_class: str | None = None
    native_unit_of_measurement: str | None = None
    enabled_by_default: bool = True
    supports_brightness: bool = False
    brightness_scale: float = 100.0
    device_key: str | None = None
    transform_read: MathFormula = MathFormula.NONE
    transform_write: MathFormula = MathFormula.NONE


@dataclass(frozen=True, slots=True)
class EntityCatalog:
    """All parsed entries and supported Home Assistant entity definitions."""

    entries_by_name: dict[str, PubEntry]
    device_types_by_key: dict[str, DeviceTypeDefinition]
    devices_by_key: dict[str, DetectedDevice]
    entities: tuple[EntitySpec, ...]
    state_names: tuple[str, ...]
    runtime_inventory: RuntimeInventory
    unmatched_runtime_devices: tuple[RuntimeDiscoveredDevice, ...] = ()
    unmatched_pub_devices: tuple[DetectedDevice, ...] = ()
    # Modules found on the bus whose type code has no definition yet. They get
    # no device and no entities; they are reported to the user instead.
    unsupported_runtime_devices: tuple[RuntimeDiscoveredDevice, ...] = ()

    def entities_for_platform(self, platform: str) -> tuple[EntitySpec, ...]:
        """Return all entity specs for one Home Assistant platform."""
        return tuple(entity for entity in self.entities if entity.platform == platform)

    def state_entries(self) -> tuple[PubEntry, ...]:
        """Return unique state entries in polling order."""
        return tuple(self.entries_by_name[name] for name in self.state_names)
