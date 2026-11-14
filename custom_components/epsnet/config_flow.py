"""Config flow for the EPSNET integration.

Setup asks as little as it can. It searches the local network for PLCs and lets
the user pick one (an address is typed only when none answers), then scans the
modules and reads the variable names under a progress step and creates the
entry. Further questions appear only when the PLC cannot answer them itself: it
stores no .pub (upload one, or continue read-only), another program holds its
connection (retry, or upload), or an uploaded file names none of its modules.

The resolved variable map is frozen into the entry so runtime never re-fetches
it (see ``map_sources.freeze_setup`` / ``__init__.async_setup_entry``).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from ipaddress import ip_address
import logging
import socket
from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
import voluptuous as vol

from .const import (
    CONF_ANONYMIZE_DIAGNOSTICS,
    CONF_FAST_SCAN_INTERVAL,
    CONF_HOST,
    CONF_LOCAL_PORT,
    CONF_MAP,
    CONF_MAP_FILE,
    CONF_NAME,
    CONF_PORT,
    CONF_PROGRAM_FIRST,
    CONF_SCAN_INTERVAL,
    DEFAULT_FAST_SCAN_INTERVAL,
    DEFAULT_LOCAL_PORT,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_TRACKER_URL,
    NEW_ENTRY_FAST_SCAN_INTERVAL,
)
from .diagnostics import names_hidden
from .epsnet_udp import (
    DiscoveredPlc,
    EpsnetError,
    EpsnetUdpClient,
    PlcBusyError,
    async_search_plcs,
    plc_model,
)
from .map_sources import SetupResult, freeze_setup, run_setup
from .models import PubEntry
from .pubfile import PubFileParseError, load_pub_entries

LOGGER = logging.getLogger(__name__)

CONF_PLC = "plc"
CONF_ADVANCED = "advanced"
_MANUAL = "manual"

_PUB_UPLOAD_ACCEPT = ".pub,.piz,.zip,text/plain"
_PROGRESS_POLL_SECONDS = 0.5
_DOWNLOAD_LINK_LIFETIME = timedelta(minutes=10)

_PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
_LOCAL_PORT = vol.All(vol.Coerce(int), vol.Range(min=0, max=65535))
_SCAN_INTERVAL = vol.All(vol.Coerce(int), vol.Range(min=0, max=300_000))
_FAST_SCAN_INTERVAL = vol.All(vol.Coerce(int), vol.Range(min=0, max=5_000))


def _fetch_progress(client) -> tuple[str, float | None]:
    """("read / total kB", fraction) of the map read, or ("", None) before it starts."""
    if client is None:
        return "", None
    read, total = client.fetch_progress
    if total <= 0:
        return "", None
    return f"{read // 1024} / {total // 1024} kB", min(read / total, 1.0)


async def _pump_progress(flow, manager, task: asyncio.Task, snapshot) -> None:
    """Re-render the progress step whenever what it shows has changed.

    ``snapshot()`` returns ``(progress_action, placeholders, fraction)``. A new
    fraction moves the frontend's determinate bar (``async_update_progress``);
    a new action or placeholder re-invokes the step (``manager.async_configure``)
    -- the only way fresh text reaches the frontend, which serialises a snapshot
    rather than live ``cur_step``. Best-effort: a UI hiccup never breaks setup.
    """
    shown = snapshot()
    while not task.done():
        # Wakes at once when the task ends, so a finished flow leaves nothing behind.
        await asyncio.wait([task], timeout=_PROGRESS_POLL_SECONDS)
        if task.done():
            break
        try:
            state = snapshot()
            if state == shown:
                continue
            shown = state
            if state[2] is not None:
                flow.async_update_progress(state[2])
            await manager.async_configure(flow.flow_id)
        except Exception as err:  # noqa: BLE001 - UI refresh must never break setup
            LOGGER.debug("progress refresh skipped: %r", err)


def _file_names_a_module(result: SetupResult) -> bool:
    """True when the map itself gave at least one entity.

    Entities placed from the PLC program exist with any file, so they do not
    show that a file belongs to this PLC.
    """
    return any(not entity.from_program for entity in result.catalog.entities)


def _round_ms(milliseconds: float) -> str:
    """Whole tens of milliseconds: no decimal separator to localise."""
    return str(int(round(milliseconds / 10.0)) * 10)


def _module_count(result: SetupResult) -> int:
    return len(result.catalog.runtime_inventory.devices)


def _pub_file_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_MAP_FILE): FileSelector(
                FileSelectorConfig(accept=_PUB_UPLOAD_ACCEPT)
            )
        }
    )


class EpsnetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EPSNET."""

    VERSION = 4
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._found: list[DiscoveredPlc] | None = None
        self._ident: str | None = None
        self._modules: int | None = None
        self._client: EpsnetUdpClient | None = None
        self._setup_task: Any | None = None
        self._result: SetupResult | None = None
        self._preset_entries: tuple[PubEntry, ...] | None = None
        self._error: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return EpsnetOptionsFlow()

    # -- Reconfigure: change connection on an existing entry ----------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_HOST: str(user_input[CONF_HOST]).strip(),
                    CONF_PORT: int(user_input[CONF_PORT]),
                    CONF_LOCAL_PORT: int(user_input[CONF_LOCAL_PORT]),
                },
            )
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data.get(CONF_HOST)): str,
                vol.Required(
                    CONF_PORT, default=entry.data.get(CONF_PORT, DEFAULT_PORT)
                ): _PORT,
                vol.Required(
                    CONF_LOCAL_PORT,
                    default=entry.data.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT),
                ): _LOCAL_PORT,
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema)

    # -- Step 1: which PLC ---------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Search the network and offer what answered; no answer -> ask the address."""
        if user_input is not None:
            choice = user_input[CONF_PLC]
            plc = next((p for p in self._found or () if p.host == choice), None)
            if plc is None:
                return await self.async_step_manual()
            return await self._async_connect(plc.host, ident=plc.ident)

        if self._found is None:
            self._found = await self._async_search()
        if not self._found:
            return await self.async_step_manual()
        options = [
            SelectOptionDict(value=plc.host, label=f"{plc.ident} · {plc.host}")
            for plc in self._found
        ]
        # The label of this one comes from the translations (selector.plc).
        options.append(SelectOptionDict(value=_MANUAL, label=_MANUAL))
        schema = vol.Schema(
            {
                vol.Required(CONF_PLC, default=self._found[0].host): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_PLC,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def _async_search(self) -> list[DiscoveredPlc]:
        """PLCs answering on the local networks, without the ones already set up."""
        from homeassistant.components import network

        try:
            addresses = await network.async_get_ipv4_broadcast_addresses(self.hass)
        except Exception as err:  # noqa: BLE001 - the search is only a convenience
            LOGGER.debug("no broadcast addresses for the PLC search: %r", err)
            return []
        found = await async_search_plcs(addresses)
        configured = {
            entry.data.get(CONF_HOST) for entry in self._async_current_entries()
        }
        return [plc for plc in found if plc.host not in configured]

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Type the PLC address; the ports sit in a collapsed section."""
        errors: dict[str, str] = {}
        if self._error:
            # Back from a failed connection: show why. Any input passed along
            # is the one that just failed, not a new answer.
            errors[CONF_HOST] = self._error
            self._error = None
            user_input = None
        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            advanced = user_input.get(CONF_ADVANCED) or {}
            if not host:
                errors[CONF_HOST] = "invalid_host"
            else:
                return await self._async_connect(
                    host,
                    port=int(advanced.get(CONF_PORT, DEFAULT_PORT)),
                    local_port=int(advanced.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT)),
                )

        host_key = (
            vol.Required(CONF_HOST, default=self._data[CONF_HOST])
            if self._data.get(CONF_HOST)
            else vol.Required(CONF_HOST)
        )
        schema = vol.Schema(
            {
                host_key: str,
                vol.Required(CONF_ADVANCED): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_PORT,
                                default=self._data.get(CONF_PORT, DEFAULT_PORT),
                            ): _PORT,
                            vol.Required(
                                CONF_LOCAL_PORT,
                                default=self._data.get(
                                    CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT
                                ),
                            ): _LOCAL_PORT,
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )
        return self.async_show_form(
            step_id="manual", data_schema=schema, errors=errors
        )

    async def _async_connect(
        self,
        host: str,
        *,
        port: int = DEFAULT_PORT,
        local_port: int = DEFAULT_LOCAL_PORT,
        ident: str | None = None,
    ) -> FlowResult:
        """Remember the target and start reading it."""
        self._async_abort_entries_match({CONF_HOST: host})
        self._data = {CONF_HOST: host, CONF_PORT: port, CONF_LOCAL_PORT: local_port}
        self._ident = ident
        if self.unique_id is None and (unique_id := self._discovered_unique_id(host)):
            # Home Assistant noticed this PLC by itself as well: finishing here
            # also settles that "Discovered" card.
            await self.async_set_unique_id(unique_id, raise_on_progress=False)
        return await self.async_step_run()

    def _discovered_unique_id(self, host: str) -> str | None:
        for flow in self._async_in_progress(include_uninitialized=True):
            context = flow["context"]
            placeholders = context.get("title_placeholders") or {}
            if placeholders.get("host") == host and context.get("unique_id"):
                return context["unique_id"]
        return None

    # -- A PLC that Home Assistant noticed on the network --------------------

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> FlowResult:
        """A network card of the PLC maker was seen: offer it if it is a PLC."""
        host = discovery_info.ip
        await self.async_set_unique_id(format_mac(discovery_info.macaddress))
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        if await self._async_address_is_set_up(host):
            return self.async_abort(reason="already_configured")
        answered = await async_search_plcs([host])
        if not answered:
            return self.async_abort(reason="no_plc")
        self._data = {
            CONF_HOST: host,
            CONF_PORT: DEFAULT_PORT,
            CONF_LOCAL_PORT: DEFAULT_LOCAL_PORT,
        }
        self._ident = answered[0].ident
        self.context["title_placeholders"] = {
            "name": f"{answered[0].model} ({host})",
            "host": host,
        }
        return await self.async_step_discovery_confirm()

    async def _async_address_is_set_up(self, address: str) -> bool:
        """True when an entry already reaches this IP address, also by host name."""
        for entry in self._async_current_entries():
            host = str(entry.data.get(CONF_HOST, ""))
            if host == address:
                return True
            try:
                ip_address(host)
            except ValueError:
                try:
                    resolved = await self.hass.async_add_executor_job(
                        socket.gethostbyname, host
                    )
                except OSError:
                    continue
                if resolved == address:
                    return True
        return False

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._async_abort_entries_match({CONF_HOST: self._data[CONF_HOST]})
            return await self.async_step_run()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={
                "ident": self._ident or "PLC",
                "host": self._data[CONF_HOST],
            },
        )

    # -- Step 2: read the PLC under a progress step --------------------------

    def _progress_state(self) -> tuple[str, dict[str, str] | None, float | None]:
        if self._preset_entries is not None:
            return "match_file", None, None
        if self._modules is None:
            return "connect", {"host": self._data[CONF_HOST]}, None
        text, fraction = _fetch_progress(self._client)
        return (
            "read_names",
            {
                "ident": self._ident or self._data[CONF_HOST],
                "modules": str(self._modules),
                "progress": text,
            },
            fraction,
        )

    async def async_step_run(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Scan the modules and resolve the variable names as a background task.

        ``_preset_entries`` (an uploaded file) replaces the read from the PLC;
        the modules are scanned live either way. A dedicated step id is needed
        so progress refreshes re-enter here, not a form or menu step.
        """
        if self._setup_task is None:
            # Ephemeral source port: the PLC does not need a fixed one, and the
            # configured local port may be in use by a running entry.
            self._client = EpsnetUdpClient(
                self._data[CONF_HOST], self._data[CONF_PORT], local_port=0
            )
            self._modules = None
            # Not eager: the step always shows its progress first, also when
            # the read fails at once.
            self._setup_task = self.hass.async_create_task(
                self._async_do_setup(), eager_start=False
            )
            self.hass.async_create_task(
                _pump_progress(
                    self,
                    self.hass.config_entries.flow,
                    self._setup_task,
                    self._progress_state,
                )
            )

        if not self._setup_task.done():
            action, placeholders, _fraction = self._progress_state()
            return self.async_show_progress(
                step_id="run",
                progress_action=action,
                description_placeholders=placeholders,
                progress_task=self._setup_task,
            )

        task, self._setup_task = self._setup_task, None
        await self._async_close_client()
        try:
            result = task.result()
        except PlcBusyError:
            return self.async_show_progress_done(next_step_id="busy")
        except (EpsnetError, OSError) as err:
            LOGGER.warning("EPSNET setup failed: %s", err)
            self._error = "cannot_connect"
            return self.async_show_progress_done(next_step_id="manual")

        self._result = result
        self._modules = _module_count(result)
        if not result.address_count:
            # No names: the modules' inputs and outputs can still be shown
            # read-only from the PLC program. Ask for the .pub, or go on without.
            return self.async_show_progress_done(next_step_id="no_map")
        if self._preset_entries is not None and not _file_names_a_module(result):
            return self.async_show_progress_done(next_step_id="file_no_match")
        return self.async_show_progress_done(next_step_id="finish")

    async def _async_do_setup(self) -> SetupResult:
        assert self._client is not None

        def _inventory_read(inventory) -> None:
            self._ident = inventory.controller_identity or self._ident
            self._modules = len(inventory.devices)

        if self._preset_entries is not None:
            return await run_setup(
                self._client,
                preset_entries=self._preset_entries,
                preset_source="file",
            )
        return await run_setup(self._client, on_inventory=_inventory_read)

    async def _async_close_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.async_close()
            finally:
                self._client = None

    # -- Asked only when the PLC cannot answer it ----------------------------

    async def async_step_no_map(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """The PLC stores no .pub: upload one, or continue read-only."""
        if self._result is None:
            return await self.async_step_run()
        return self.async_show_menu(
            step_id="no_map",
            menu_options=["upload", "inventory_only"],
            description_placeholders={"modules": str(self._modules or 0)},
        )

    async def async_step_busy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Another program holds the PLC's connection."""
        return self.async_show_menu(step_id="busy", menu_options=["retry", "upload"])

    async def async_step_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_run()

    async def async_step_file_no_match(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """The uploaded file names none of the modules: most likely another project."""
        if self._result is None:
            return await self.async_step_upload()
        return self.async_show_menu(
            step_id="file_no_match",
            menu_options=["upload", "use_file"],
            description_placeholders={"modules": str(self._modules or 0)},
        )

    async def async_step_use_file(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_finish()

    async def async_step_inventory_only(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_finish()

    async def async_step_upload(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Upload a .pub file, then scan the modules and match it under a spinner."""
        from homeassistant.components.file_upload import process_uploaded_file

        errors: dict[str, str] = {}
        if user_input is not None:
            file_id = user_input[CONF_MAP_FILE]

            def _read() -> tuple[PubEntry, ...]:
                with process_uploaded_file(self.hass, file_id) as path:
                    return load_pub_entries(path)

            try:
                entries = await self.hass.async_add_executor_job(_read)
            except FileNotFoundError:
                errors[CONF_MAP_FILE] = "path_not_found"
            except PubFileParseError:
                errors[CONF_MAP_FILE] = "invalid_pub"
            else:
                if not entries:
                    errors[CONF_MAP_FILE] = "invalid_pub"
                else:
                    self._preset_entries = entries
                    return await self.async_step_run()

        return self.async_show_form(
            step_id="upload", data_schema=_pub_file_schema(), errors=errors
        )

    # -- Create the entry ----------------------------------------------------

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create the entry; the summary goes onto Home Assistant's success screen."""
        from .catalog import unsupported_module_summary

        if self._result is None:
            return await self.async_step_run()
        result = self._result
        host = self._data[CONF_HOST]
        self._async_abort_entries_match({CONF_HOST: host})
        ident = result.catalog.runtime_inventory.controller_identity or self._ident
        title = f"{plc_model(ident) or 'PLC'} ({host})"
        data = {
            CONF_NAME: title,
            **self._data,
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_FAST_SCAN_INTERVAL: NEW_ENTRY_FAST_SCAN_INTERVAL,
            CONF_MAP: freeze_setup(result),
        }
        if not result.entries:
            data[CONF_PROGRAM_FIRST] = True

        placeholders = {
            "modules": str(_module_count(result)),
            "variables": str(result.address_count),
        }
        description = "with_names" if _file_names_a_module(result) else "read_only"
        unsupported_count, unsupported_codes = unsupported_module_summary(
            result.catalog
        )
        if unsupported_count:
            description += "_unsupported"
            placeholders |= {
                "unsupported_count": str(unsupported_count),
                "unsupported_codes": unsupported_codes,
                "issue_url": ISSUE_TRACKER_URL,
            }
        return self.async_create_entry(
            title=title,
            data=data,
            description=description,
            description_placeholders=placeholders,
        )


class EpsnetOptionsFlow(config_entries.OptionsFlow):
    """Any-time settings: reload the variable names (from the PLC or a file),
    change the update speed, download diagnostics.

    ``self.config_entry`` is provided by Home Assistant; do not store it.
    """

    def __init__(self) -> None:
        self._setup_task: Any | None = None
        self._result: SetupResult | None = None
        self._error: str | None = None
        self._reimport_client: EpsnetUdpClient | None = None
        self._reimport_owns_client = False
        self._reimport_entries: tuple[PubEntry, ...] | None = None

    def _runtime(self):
        return (self.hass.data.get(DOMAIN) or {}).get(self.config_entry.entry_id)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        blob = self.config_entry.data.get(CONF_MAP) or {}
        runtime = self._runtime()
        # Variant step ids carry the warning of a failed reload, so its text
        # comes from the translations, not from English built here.
        step_id = f"init_{self._error}" if self._error else "init"
        self._error = None
        return self.async_show_menu(
            step_id=step_id,
            menu_options=["reimport_plc", "reimport_file", "speed", "diagnostics"],
            description_placeholders={
                "modules": (
                    str(len(runtime.catalog.runtime_inventory.devices))
                    if runtime is not None
                    else "?"
                ),
                "variables": str(len(blob.get("entries", []))),
            },
        )

    # Re-entry points for the warning variants: Home Assistant re-calls the
    # handler named by the shown step id (e.g. when the frontend reloads the
    # flow), so each variant must exist and re-show itself.
    async def async_step_init_plc_busy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._error = "plc_busy"
        return await self.async_step_init()

    async def async_step_init_cannot_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._error = "cannot_connect"
        return await self.async_step_init()

    async def async_step_init_no_map(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._error = "no_map"
        return await self.async_step_init()

    # -- Update speed --------------------------------------------------------

    def _measurements(self) -> dict[str, str] | None:
        """This installation's own read times, once the first cycles are in."""
        from . import fast_lane_entries

        runtime = self._runtime()
        if runtime is None:
            return None
        timing = runtime.coordinator.current_timing_ms
        if timing is None:
            return None
        return {
            "values": str(runtime.coordinator.full_read_count),
            "read_ms": _round_ms(timing[0]),
            "cycle_ms": _round_ms(timing[1]),
            "fast_inputs": str(len(fast_lane_entries(runtime.catalog))),
        }

    async def async_step_speed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.config_entry
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_FAST_SCAN_INTERVAL: int(user_input[CONF_FAST_SCAN_INTERVAL]),
                },
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(title="", data={})
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): _SCAN_INTERVAL,
                vol.Required(
                    CONF_FAST_SCAN_INTERVAL,
                    default=entry.data.get(
                        CONF_FAST_SCAN_INTERVAL, DEFAULT_FAST_SCAN_INTERVAL
                    ),
                ): _FAST_SCAN_INTERVAL,
            }
        )
        measured = self._measurements()
        return self.async_show_form(
            # Without measurements (the entry is not running) a variant step
            # carries the text without the figures.
            step_id="speed" if measured else "speed_unmeasured",
            data_schema=schema,
            description_placeholders=measured,
        )

    async def async_step_speed_unmeasured(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_speed(user_input)

    # -- Diagnostics download ------------------------------------------------

    async def async_step_diagnostics(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose whether the file hides the program's own names; the choice is kept."""
        entry = self.config_entry
        if user_input is not None:
            hide = bool(user_input[CONF_ANONYMIZE_DIAGNOSTICS])
            if entry.data.get(CONF_ANONYMIZE_DIAGNOSTICS) is not hide:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_ANONYMIZE_DIAGNOSTICS: hide}
                )
            return await self.async_step_diagnostics_download()
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ANONYMIZE_DIAGNOSTICS, default=names_hidden(entry)
                ): bool
            }
        )
        return self.async_show_form(step_id="diagnostics", data_schema=schema)

    async def async_step_diagnostics_download(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show a link to Home Assistant's own diagnostics download for this entry."""
        from homeassistant.components.http.auth import async_sign_path

        if user_input is not None:
            return self.async_create_entry(title="", data={})
        entry_id = self.config_entry.entry_id
        url = async_sign_path(
            self.hass,
            f"/api/diagnostics/config_entry/{entry_id}",
            _DOWNLOAD_LINK_LIFETIME,
        )
        # A plain link would be taken by the frontend's own page navigation;
        # one that opens a new tab reaches the server and downloads the file.
        # Its text is the name the browser saves the file under.
        link = f'<a href="{url}" target="_blank">config_entry-{DOMAIN}-{entry_id}.json</a>'
        return self.async_show_form(
            step_id="diagnostics_download",
            data_schema=vol.Schema({}),
            description_placeholders={"link": link},
        )

    # -- Re-import the map from the PLC -------------------------------------

    async def async_step_reimport_plc(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._reimport_entries = None
        return await self._async_reimport_progress("reimport_plc")

    # -- Re-import the map from an uploaded file -----------------------------

    async def async_step_reimport_file(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Upload a .pub for an existing entry (the PLC holds none, or an old one)."""
        from homeassistant.components.file_upload import process_uploaded_file

        errors: dict[str, str] = {}
        if self._error == "pub_no_match":
            # Back from a file that matched nothing: any input passed along is
            # that same upload, not a new one.
            errors[CONF_MAP_FILE] = self._error
            self._error = None
            user_input = None
        if user_input is not None:
            file_id = user_input[CONF_MAP_FILE]

            def _read() -> tuple[PubEntry, ...]:
                with process_uploaded_file(self.hass, file_id) as path:
                    return load_pub_entries(path)

            try:
                entries = await self.hass.async_add_executor_job(_read)
            except FileNotFoundError:
                errors[CONF_MAP_FILE] = "path_not_found"
            except PubFileParseError:
                errors[CONF_MAP_FILE] = "invalid_pub"
            else:
                if not entries:
                    errors[CONF_MAP_FILE] = "invalid_pub"
                else:
                    self._reimport_entries = entries
                    return await self.async_step_reimport_file_run()

        return self.async_show_form(
            step_id="reimport_file", data_schema=_pub_file_schema(), errors=errors
        )

    async def async_step_reimport_file_run(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self._async_reimport_progress("reimport_file_run")

    # -- Shared re-import progress -------------------------------------------

    def _progress_state(self) -> tuple[str, dict[str, str] | None, float | None]:
        if self._reimport_entries is not None:
            return "match_file", None, None
        text, fraction = _fetch_progress(self._reimport_client)
        return "read_names", {"progress": text}, fraction

    async def _async_reimport_progress(self, step_id: str) -> FlowResult:
        """Scan the PLC and build the catalog under a spinner, then apply it.

        With ``_reimport_entries`` set (uploaded file) only the module scan talks
        to the PLC; otherwise the map is fetched from the PLC too.
        """
        from_file = self._reimport_entries is not None
        if self._setup_task is None:
            self._reimport_client, self._reimport_owns_client = (
                self._resolve_reimport_client()
            )
            self._setup_task = self.hass.async_create_task(
                self._async_reimport(), eager_start=False
            )
            self.hass.async_create_task(
                _pump_progress(
                    self,
                    self.hass.config_entries.options,
                    self._setup_task,
                    self._progress_state,
                )
            )

        if not self._setup_task.done():
            action, placeholders, _fraction = self._progress_state()
            return self.async_show_progress(
                step_id=step_id,
                progress_action=action,
                description_placeholders=placeholders,
                progress_task=self._setup_task,
            )

        task, self._setup_task = self._setup_task, None
        try:
            self._result = task.result()
        except PlcBusyError:
            self._error = "plc_busy"
            return self.async_show_progress_done(next_step_id="init")
        except (EpsnetError, OSError) as err:
            LOGGER.warning("EPSNET re-import failed: %s", err)
            self._error = "cannot_connect"
            return self.async_show_progress_done(next_step_id="init")
        if not self._result.address_count:
            # Never replace a working map with an empty one.
            self._error = "no_map"
            return self.async_show_progress_done(next_step_id="init")
        if from_file and not _file_names_a_module(self._result):
            # Nothing in the file pairs with a module this PLC has: almost
            # certainly the wrong project. Keep the working map and ask again.
            self._error = "pub_no_match"
            return self.async_show_progress_done(next_step_id="reimport_file")
        return self.async_show_progress_done(next_step_id="reimport_done")

    def _resolve_reimport_client(self) -> tuple[EpsnetUdpClient, bool]:
        """Return (client, owns_it). Reuse the LIVE runtime client so its lock
        serialises the (exclusive) fetch against ongoing polling -- one PLC
        conversation, no contention. Fall back to a transient ephemeral client
        (which we then own and must close) only if the entry isn't set up.
        """
        entry = self.config_entry
        runtime = (self.hass.data.get(DOMAIN) or {}).get(entry.entry_id)
        client = getattr(runtime, "client", None)
        if client is not None:
            return client, False
        return (
            EpsnetUdpClient(
                entry.data[CONF_HOST], entry.data[CONF_PORT], local_port=0
            ),
            True,
        )

    async def _async_reimport(self) -> SetupResult:
        try:
            if self._reimport_entries is not None:
                return await run_setup(
                    self._reimport_client,
                    preset_entries=self._reimport_entries,
                    preset_source="file",
                )
            return await run_setup(self._reimport_client)
        finally:
            if self._reimport_owns_client and self._reimport_client is not None:
                await self._reimport_client.async_close()

    async def async_step_reimport_done(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        from homeassistant.helpers import issue_registry as ir

        assert self._result is not None
        entry = self.config_entry
        old_blob = entry.data.get(CONF_MAP) or {}
        old_crc = old_blob.get("crc")
        new_blob = freeze_setup(self._result)

        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_MAP: new_blob}
        )

        issue_id = f"map_changed_{entry.entry_id}"
        if old_crc is not None and old_crc != new_blob["crc"]:
            # Program on the PLC changed since the last import. Notify only --
            # the fresh map is already applied; the card is informational.
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="map_changed",
                translation_placeholders={
                    "old_crc": hex(old_crc),
                    "new_crc": hex(new_blob["crc"]),
                    "points": str(self._result.address_count),
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(title="", data={})
