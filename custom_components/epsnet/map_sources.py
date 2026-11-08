"""Setup-time map-source orchestration (the "Director").

Gathers the published-variable map from the best available source and builds the
entity catalog. Sources are composable *by role*, not competing full-map producers:

* IDENTITY  -- the live CIB device inventory (what hardware exists). Required.
* ADDRESSES -- the ``.pub`` (name -> area/addr/bit/type). Provider priority:
               on-PLC fetch (``6c 23``) -> user-supplied file -> none.

The mapper (:func:`build_catalog`) merges IDENTITY + ADDRESSES. Providers are kept
as plain functions with priority inline; see ``docs/design/IMPORTER-ARCHITECTURE.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import zlib

from .catalog import EntityCatalog, build_catalog
from .epsnet_udp import EpsnetError, EpsnetUdpClient, PlcBusyError
from .models import PubEntry
from .pubfile import PubFileParseError, load_pub_entries

LOGGER = logging.getLogger(__name__)

# Version stamp for the persisted map blob, so a future format change can be
# detected (and the entry re-imported) rather than mis-parsed.
MAP_BLOB_VERSION = 1


@dataclass(frozen=True)
class SetupResult:
    """Outcome of setup-time map resolution."""

    catalog: EntityCatalog
    source: str  # "plc" | "file" | "none"
    plc_ident: str | None
    address_count: int
    entries: tuple[PubEntry, ...] = ()


def serialize_entries(entries: tuple[PubEntry, ...]) -> list[dict]:
    """Serialise pub entries to JSON-safe dicts for config-entry storage."""
    return [
        {
            "name": e.name,
            "register": e.register,
            "storage": e.storage,
            "address": e.address,
            "bit": e.bit,
            "data_type": e.data_type,
            "access": e.access,
            "line_number": e.line_number,
            "is_virtual": e.is_virtual,
        }
        for e in entries
    ]


def deserialize_entries(data: list[dict]) -> tuple[PubEntry, ...]:
    """Rebuild pub entries from the persisted config-entry representation."""
    return tuple(
        PubEntry(
            name=d["name"],
            register=d["register"],
            storage=d["storage"],
            address=int(d["address"]),
            bit=d["bit"],
            data_type=d["data_type"],
            access=d["access"],
            line_number=int(d.get("line_number", 0)),
            is_virtual=bool(d.get("is_virtual", False)),
        )
        for d in data
    )


def entries_crc(entries: tuple[PubEntry, ...]) -> int:
    """Order-independent CRC over the address-relevant fields of a map.

    Used to detect a changed PLC program (M5 Repairs) without persisting the
    whole program. Names + addresses + types define the map; line numbers do not.
    """
    payload = "\n".join(
        sorted(
            f"{e.name}|{e.register}|{e.storage}|{e.address}|{e.bit}|{e.data_type}|{e.access}"
            for e in entries
        )
    )
    return zlib.crc32(payload.encode("utf-8")) & 0xFFFFFFFF


def freeze_setup(result: SetupResult) -> dict:
    """Build the JSON-safe map blob persisted in the config entry."""
    return {
        "version": MAP_BLOB_VERSION,
        "source": result.source,
        "plc_ident": result.plc_ident,
        "crc": entries_crc(result.entries),
        "entries": serialize_entries(result.entries),
    }


async def _resolve_addresses(
    client: EpsnetUdpClient, *, user_pub_path: str | None
) -> tuple[tuple[PubEntry, ...], str, str | None]:
    """ADDRESSES role: on-PLC fetch (preferred) -> user file -> none.

    :class:`PlcBusyError` propagates (the caller decides whether to retry/abort);
    other PLC errors fall through to the next provider.
    """
    # 1. On-PLC .pub (credential-free, always current).
    try:
        ident, entries = await client.async_fetch_pub_from_plc()
    except PlcBusyError:
        raise
    except EpsnetError as err:
        LOGGER.info("On-PLC .pub fetch unavailable (%s); trying fallbacks", err)
    else:
        if entries:
            LOGGER.info("Using on-PLC .pub: %d points from %s", len(entries), ident)
            return entries, "plc", ident
        LOGGER.info("On-PLC .pub fetch returned no points; trying fallbacks")

    # 2. User-supplied file.
    if user_pub_path:
        try:
            entries = load_pub_entries(user_pub_path)
        except FileNotFoundError:
            LOGGER.warning("Configured .pub file %s not found", user_pub_path)
        except PubFileParseError as err:
            LOGGER.warning("Configured .pub file %s is invalid: %s", user_pub_path, err)
        else:
            LOGGER.info("Using user .pub file: %d points", len(entries))
            return entries, "file", None

    # 3. None -- runtime-inventory-only (generic; addresses limited to what the scan
    #    can derive). Phase 2 will add a generic-scan naming layer here.
    return (), "none", None


async def run_setup(
    client: EpsnetUdpClient,
    *,
    user_pub_path: str | None = None,
    preset_entries: tuple[PubEntry, ...] | None = None,
    preset_source: str | None = None,
) -> SetupResult:
    """Resolve IDENTITY (CIB scan) + ADDRESSES (.pub) and build the entity catalog.

    ``preset_entries`` short-circuits the ADDRESSES role with an already-parsed
    map (e.g. an uploaded file whose temp copy is gone by now); IDENTITY is still
    scanned live so the two stay consistent.
    """
    inventory = await client.discover_runtime_inventory()  # IDENTITY (required)
    if preset_entries is not None:
        entries, source, ident = preset_entries, (preset_source or "file"), None
    else:
        entries, source, ident = await _resolve_addresses(
            client, user_pub_path=user_pub_path
        )
    catalog = build_catalog(entries, inventory)
    if entries and not catalog.devices_by_key:
        LOGGER.warning(
            "Resolved .pub (source=%s) did not match any live device; "
            "continuing with runtime-only inventory",
            source,
        )
    return SetupResult(
        catalog=catalog,
        source=source,
        plc_ident=ident,
        address_count=len(entries),
        entries=tuple(entries),
    )
