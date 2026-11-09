"""EPSNET Home Assistant integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path

from .catalog import EntityCatalog, build_catalog, unsupported_module_summary
from .client import EpsnetClient
from .const import (
    ISSUE_TRACKER_URL,
    CONF_HOST,
    CONF_LOCAL_PORT,
    CONF_MAP,
    CONF_MAP_PATH,
    CONF_NAME,
    CONF_PORT,
    CONF_FAST_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL,
    DEFAULT_LOCAL_PORT,
    DEFAULT_PORT,
    DIAGNOSTIC_DIRECTORY,
    DOMAIN,
    DEFAULT_FAST_SCAN_INTERVAL,
    LEGACY_DEFAULT_SCAN_INTERVAL_SECONDS,
    MERGED_CATALOG_BASENAME,
    SUPPORTED_PLATFORMS,
)
from .diagnostics import build_catalog_snapshot, write_catalog_snapshot
from .epsnet_udp import EpsnetError, EpsnetUdpClient
from .map_sources import deserialize_entries
from .models import DetectedDevice, PubEntry, RuntimeDiscoveredDevice
from .pubfile import PubFileParseError, load_pub_entries

LOGGER = logging.getLogger(__name__)


def _legacy_scan_interval_to_milliseconds(value: object | None) -> int:
    """Convert pre-v3 stored seconds to the new millisecond config value."""
    legacy_seconds = (
        LEGACY_DEFAULT_SCAN_INTERVAL_SECONDS if value is None else max(0, int(value))
    )
    if legacy_seconds == 0:
        return 0
    return legacy_seconds * 1000


@dataclass(slots=True)
class EpsnetRuntimeData:
    """Runtime objects shared between Home Assistant platforms."""

    catalog: EntityCatalog
    client: EpsnetClient
    coordinator: object
    artifact_path: str
    pub_enrichment_active: bool


def _matched_devices_by_runtime_slot(
    catalog: EntityCatalog,
) -> dict[tuple[int, int], DetectedDevice]:
    return {
        (device.runtime_group_token, device.runtime_index): device
        for device in catalog.devices_by_key.values()
        if device.runtime_group_token is not None and device.runtime_index is not None
    }


def _registry_identifiers(entry_id: str, *keys: str) -> set[tuple[str, str]]:
    return {(DOMAIN, f"{entry_id}_{key}") for key in keys}


def _runtime_device_name(
    runtime_device: RuntimeDiscoveredDevice, matched_device: DetectedDevice | None
) -> str:
    if matched_device is not None:
        return matched_device.name
    return runtime_device.suggested_name


async def _async_register_runtime_devices(hass, *, entry_id: str, catalog: EntityCatalog) -> None:
    """Register all discovered runtime devices in the Home Assistant device registry."""
    from homeassistant.helpers import device_registry as dr

    device_registry = dr.async_get(hass)
    matched_devices = _matched_devices_by_runtime_slot(catalog)
    for runtime_device in catalog.runtime_inventory.devices:
        matched_device = matched_devices.get((runtime_device.group_token, runtime_device.runtime_index))
        registry_keys = (
            matched_device.registry_keys
            if matched_device is not None
            else (runtime_device.registry_key,)
        )
        device_registry.async_get_or_create(
            config_entry_id=entry_id,
            identifiers=_registry_identifiers(entry_id, *registry_keys),
            manufacturer=runtime_device.manufacturer,
            model=runtime_device.model,
            name=_runtime_device_name(runtime_device, matched_device),
        )


def _async_update_unsupported_issue(hass, entry, catalog: EntityCatalog) -> None:
    """Raise (or clear) a Repairs notice listing modules with no definition yet.

    Unknown modules get no device and no entities, so without this notice a
    user on an untested PLC would just see an empty integration.
    """
    from homeassistant.helpers import issue_registry as ir

    issue_id = f"unsupported_modules_{entry.entry_id}"
    count, codes = unsupported_module_summary(catalog)
    if not count:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    LOGGER.warning(
        "%d module(s) on the PLC bus are not supported yet (%s); they get no "
        "entities. Please report them at %s with the diagnostics download",
        count,
        codes,
        ISSUE_TRACKER_URL,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        learn_more_url=ISSUE_TRACKER_URL,
        translation_key="unsupported_modules",
        translation_placeholders={
            "count": str(count),
            "codes": codes,
            "issue_url": ISSUE_TRACKER_URL,
        },
    )


def _resolve_entry_map(entry) -> tuple[tuple[PubEntry, ...], str, str | None]:
    """Return (entries, source_label, plc_ident) for a config entry.

    Prefers the frozen map persisted by the config flow (v4+); falls back to a
    legacy on-disk ``.pub`` path (pre-v4 entries) so existing installs keep
    working without a forced re-configure.
    """
    blob = entry.data.get(CONF_MAP)
    if isinstance(blob, dict) and blob.get("entries") is not None:
        entries = deserialize_entries(blob["entries"])
        return entries, str(blob.get("source", "plc")), blob.get("plc_ident")

    raw_map_path = str(entry.data.get(CONF_MAP_PATH, "")).strip()
    if raw_map_path:
        map_path = Path(raw_map_path).expanduser()
        try:
            return load_pub_entries(map_path), "file", None
        except FileNotFoundError:
            LOGGER.warning(
                "Configured `.pub` file %s is unavailable; runtime-only discovery",
                map_path,
            )
        except PubFileParseError as err:
            LOGGER.warning(
                "Configured `.pub` file %s is invalid (%s); runtime-only discovery",
                map_path,
                err,
            )
    return (), "none", None


async def async_migrate_entry(hass, entry) -> bool:
    """Migrate older config entries to the EPSNET-only schema."""
    if entry.version >= 4:
        return True
    updated_data = dict(entry.data)
    updated_version = entry.version

    if updated_version < 2:
        legacy_transport = updated_data.get("transport")
        if legacy_transport not in {None, "epsnet"}:
            LOGGER.error(
                "EPSNET config entry %s still uses the removed PLCComS transport; "
                "re-create it with direct EPSNET settings.",
                entry.entry_id,
            )
            return False
        updated_data.pop("transport", None)
        updated_data.setdefault(CONF_PORT, DEFAULT_PORT)
        updated_data.setdefault(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT)
        updated_data.setdefault(CONF_NAME, entry.title)
        updated_version = 2

    if updated_version < 3:
        updated_data[CONF_SCAN_INTERVAL] = _legacy_scan_interval_to_milliseconds(
            updated_data.get(CONF_SCAN_INTERVAL)
        )
        updated_version = 3

    if updated_version < 4:
        # v4 introduces the frozen map blob (CONF_MAP). Pre-v4 entries keep their
        # legacy CONF_MAP_PATH; _resolve_entry_map falls back to it. No network
        # here -- the map is (re-)imported via the config/options flow instead.
        updated_version = 4

    hass.config_entries.async_update_entry(
        entry, data=updated_data, version=updated_version
    )
    return True


async def async_setup_entry(hass, entry) -> bool:
    """Set up EPSNET from a config entry."""
    from homeassistant.exceptions import ConfigEntryNotReady

    from .coordinator import EpsnetDataCoordinator

    host = str(entry.data[CONF_HOST])
    port = int(entry.data[CONF_PORT])
    local_port = int(entry.data.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT))

    entries, map_source, _plc_ident = _resolve_entry_map(entry)
    resolved_map_path: str | None = map_source if entries else None

    client = EpsnetUdpClient(host, port, local_port=local_port)
    try:
        # IDENTITY is always scanned live (device registration + address match);
        # ADDRESSES come from the frozen map persisted at setup -- no PLC map
        # re-fetch at runtime, so startup never contends for the exclusive session.
        inventory = await client.discover_runtime_inventory()

        catalog = build_catalog(entries, inventory)
        _async_update_unsupported_issue(hass, entry, catalog)
        if entries and not catalog.devices_by_key:
            LOGGER.warning(
                "Configured `.pub` file %s did not match any live discovered devices; "
                "continuing with runtime-only inventory mode",
                resolved_map_path,
            )
        artifact_path = Path(
            hass.config.path(
                DIAGNOSTIC_DIRECTORY,
                f"{entry.entry_id}_{MERGED_CATALOG_BASENAME}.yaml",
            )
        )
        snapshot = build_catalog_snapshot(
            catalog,
            host=host,
            port=port,
            local_port=local_port,
            map_path=resolved_map_path,
        )
        await asyncio.to_thread(write_catalog_snapshot, artifact_path, snapshot)
    except (EpsnetError, OSError) as err:
        await client.async_close()
        raise ConfigEntryNotReady(str(err)) from err
    except ConfigEntryNotReady:
        await client.async_close()
        raise

    fast_ms = int(entry.options.get(CONF_FAST_SCAN_INTERVAL, entry.data.get(CONF_FAST_SCAN_INTERVAL, DEFAULT_FAST_SCAN_INTERVAL)) or 0)
    fast_names = {spec.state_name for spec in catalog.entities_for_platform("binary_sensor")}
    fast_entries = tuple(e for e in catalog.state_entries() if e.name in fast_names)
    coordinator = EpsnetDataCoordinator(
        hass,
        client=client,
        catalog=catalog,
        name=entry.title,
        scan_interval_milliseconds=int(entry.data[CONF_SCAN_INTERVAL]),
        config_entry=entry,
        fast_scan_interval_milliseconds=fast_ms,
        fast_entries=fast_entries,
    )
    await coordinator.async_config_entry_first_refresh()
    coordinator.start_fast_lane()
    await _async_register_runtime_devices(hass, entry_id=entry.entry_id, catalog=catalog)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = EpsnetRuntimeData(
        catalog=catalog,
        client=client,
        coordinator=coordinator,
        artifact_path=str(artifact_path),
        pub_enrichment_active=bool(entries),
    )
    await hass.config_entries.async_forward_entry_setups(entry, SUPPORTED_PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload an EPSNET config entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, SUPPORTED_PLATFORMS
    )
    if unload_ok:
        if runtime is not None:
            await runtime.coordinator.async_shutdown()
            await runtime.client.async_close()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
