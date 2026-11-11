"""Config flow for the EPSNET integration.

Wizard: connect (host/ports) -> choose source (discover from PLC / upload file)
-> run setup under a progress spinner -> confirm summary -> create entry. The
resolved published-variable map is frozen into the entry so runtime never
re-fetches it (see ``map_sources.freeze_setup`` / ``__init__.async_setup_entry``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult, FlowResultType
import voluptuous as vol

from .const import (
    ISSUE_TRACKER_URL,
    CONF_HOST,
    CONF_LOCAL_PORT,
    CONF_MAP,
    CONF_MAP_FILE,
    CONF_NAME,
    CONF_PORT,
    CONF_FAST_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL,
    DEFAULT_LOCAL_PORT,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_FAST_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SOURCE_DISCOVER,
    SOURCE_UPLOAD,
)
from .epsnet_udp import EpsnetError, EpsnetUdpClient, PlcBusyError
from .map_sources import SetupResult, freeze_setup, run_setup
from .models import PubEntry
from .pubfile import PubFileParseError, load_pub_entries

LOGGER = logging.getLogger(__name__)

_PUB_UPLOAD_ACCEPT = ".pub,.piz,.zip,text/plain"
_PROGRESS_POLL_SECONDS = 1.0


def _fetch_progress_placeholders(client) -> dict[str, str]:
    """"{read/total kB (n %)}" label for the progress step, or blank pre-fetch."""
    read, total = client.fetch_progress
    if total > 0:
        pct = int(min(read / total, 1.0) * 100)
        return {"progress": f"— {read // 1024} / {total // 1024} kB ({pct} %)"}
    return {"progress": ""}


async def _pump_map_fetch_progress(flow, manager, client, task: asyncio.Task) -> None:
    """Drive a determinate progress bar + a live "read/total kB (n %)" label.

    Runs concurrently with the setup/re-import task: reports the fraction via
    ``async_update_progress`` (the frontend's determinate bar) and re-invokes the
    progress step (``manager.async_configure``) so it re-renders with fresh
    ``description_placeholders`` -- the only way the label text actually reaches
    the frontend, which serialises a snapshot rather than live ``cur_step``.
    Best-effort: never lets a UI hiccup break the actual fetch.
    """
    while not task.done():
        try:
            read, total = client.fetch_progress
            if total > 0:
                flow.async_update_progress(min(read / total, 1.0))
                await manager.async_configure(flow.flow_id)
        except Exception as err:  # noqa: BLE001 - UI refresh must never break setup
            LOGGER.debug("map-fetch progress refresh skipped: %r", err)
        await asyncio.sleep(_PROGRESS_POLL_SECONDS)


class EpsnetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EPSNET."""

    VERSION = 4
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
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
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Required(
                    CONF_LOCAL_PORT,
                    default=entry.data.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=65535)),
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema)

    # -- Step 1: connection details -----------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {
                CONF_NAME: str(user_input.get(CONF_NAME) or DEFAULT_NAME),
                CONF_HOST: str(user_input[CONF_HOST]).strip(),
                CONF_PORT: int(user_input[CONF_PORT]),
                CONF_LOCAL_PORT: int(user_input[CONF_LOCAL_PORT]),
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
            }
            if not self._data[CONF_HOST]:
                errors[CONF_HOST] = "invalid_host"
            else:
                return await self.async_step_source()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required(CONF_LOCAL_PORT, default=DEFAULT_LOCAL_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=65535)
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=300_000)),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    # -- Step 2: choose the map source --------------------------------------

    async def async_step_source(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        # A failed attempt re-shows the menu under a variant step id
        # ("source_plc_busy" / "source_cannot_connect") so the warning comes
        # from the translations, not from English text built here.
        step_id = f"source_{self._error}" if self._error else "source"
        self._error = None
        return self.async_show_menu(
            step_id=step_id,
            menu_options=[SOURCE_DISCOVER, SOURCE_UPLOAD],
        )

    # Re-entry points for the warning variants: Home Assistant re-calls the
    # handler named by the shown step id (e.g. when the frontend reloads the
    # flow), so each variant must exist and re-show itself.
    async def async_step_source_plc_busy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._error = "plc_busy"
        return await self.async_step_source()

    async def async_step_source_cannot_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._error = "cannot_connect"
        return await self.async_step_source()

    async def async_step_source_no_map(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._error = "no_map"
        return await self.async_step_source()

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu -> discover from PLC: run setup under a progress spinner."""
        self._preset_entries = None
        return await self.async_step_run()

    async def async_step_upload(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu -> upload a .pub file, then scan + build under a spinner."""
        from homeassistant.components.file_upload import process_uploaded_file
        from homeassistant.helpers.selector import (
            FileSelector,
            FileSelectorConfig,
        )

        errors: dict[str, str] = {}
        if user_input is not None:
            file_id = user_input[CONF_MAP_FILE]
            try:
                entries = await self._parse_uploaded_pub(
                    process_uploaded_file, file_id
                )
            except FileNotFoundError:
                errors[CONF_MAP_FILE] = "path_not_found"
            except PubFileParseError:
                errors[CONF_MAP_FILE] = "invalid_pub"
            else:
                self._preset_entries = entries
                return await self.async_step_run()

        schema = vol.Schema(
            {
                vol.Required(CONF_MAP_FILE): FileSelector(
                    FileSelectorConfig(accept=_PUB_UPLOAD_ACCEPT)
                )
            }
        )
        return self.async_show_form(
            step_id="upload", data_schema=schema, errors=errors
        )

    async def _parse_uploaded_pub(
        self, process_uploaded_file, file_id: str
    ) -> tuple[PubEntry, ...]:
        """Read + parse the uploaded file inside its temp-file context."""

        def _read() -> tuple[PubEntry, ...]:
            with process_uploaded_file(self.hass, file_id) as path:
                return load_pub_entries(path)

        return await self.hass.async_add_executor_job(_read)

    # -- Shared progress step ------------------------------------------------

    async def async_step_run(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Run setup (scan + resolve map) as a background task under a spinner.

        Reached from both source choices; ``_preset_entries`` distinguishes the
        upload path from live PLC discovery. A dedicated step id is required so
        progress refreshes re-enter here, not the form/menu steps.
        """
        if self._setup_task is None:
            # Create the client up-front so the progress pump can read its
            # fetch_progress while run_setup executes.
            self._client = EpsnetUdpClient(
                self._data[CONF_HOST], self._data[CONF_PORT], local_port=0
            )
            self._setup_task = self.hass.async_create_task(self._async_do_setup())
            self.hass.async_create_task(
                _pump_map_fetch_progress(
                    self, self.hass.config_entries.flow, self._client, self._setup_task
                )
            )

        if not self._setup_task.done():
            return self.async_show_progress(
                step_id="run",
                progress_action="run_setup",
                description_placeholders=_fetch_progress_placeholders(self._client),
                progress_task=self._setup_task,
            )

        task, self._setup_task = self._setup_task, None
        try:
            self._result = task.result()
        except PlcBusyError:
            self._error = "plc_busy"
            await self._async_close_client()
            return self.async_show_progress_done(next_step_id="source")
        except (EpsnetError, OSError) as err:
            LOGGER.warning("EPSNET setup failed: %s", err)
            self._error = "cannot_connect"
            await self._async_close_client()
            return self.async_show_progress_done(next_step_id="source")

        if not self._result.address_count:
            # No map, no values: without it the CIB data's place in X/Y is
            # unknown and nothing can be switched. Ask for the .pub instead.
            self._error = "no_map"
            await self._async_close_client()
            return self.async_show_progress_done(next_step_id="source")

        return self.async_show_progress_done(next_step_id="confirm")

    async def _async_do_setup(self) -> SetupResult:
        # Client was created in async_step_run (ephemeral source port 0, never the
        # runtime coordinator's configured local_port) so the progress pump can
        # observe the fetch. The PLC does not require a fixed source port.
        assert self._client is not None
        if self._preset_entries is not None:
            return await run_setup(
                self._client,
                preset_entries=self._preset_entries,
                preset_source="file",
            )
        return await run_setup(self._client)

    # -- Step 3: confirm + create -------------------------------------------

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        assert self._result is not None
        result = self._result
        if user_input is not None:
            await self._async_close_client()
            data = {**self._data, CONF_MAP: freeze_setup(result)}
            return self.async_create_entry(title=self._data[CONF_NAME], data=data)

        from .catalog import unsupported_module_summary

        unsupported_count, unsupported_codes = unsupported_module_summary(
            result.catalog
        )
        placeholders = {
            "source": result.source,
            "ident": result.plc_ident or "—",
            "devices": str(len(result.catalog.devices_by_key)),
            "points": str(result.address_count),
        }
        if unsupported_count:
            placeholders |= {
                "unsupported_count": str(unsupported_count),
                "unsupported_codes": unsupported_codes,
                "issue_url": ISSUE_TRACKER_URL,
            }
        return self.async_show_form(
            # A variant step id carries the translated "unsupported modules"
            # notice; HA re-enters the handler named by the shown step id.
            step_id="confirm_unsupported" if unsupported_count else "confirm",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
        )

    async def async_step_confirm_unsupported(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm(user_input)

    async def _async_close_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.async_close()
            finally:
                self._client = None


class EpsnetOptionsFlow(config_entries.OptionsFlow):
    """Any-time settings: re-import the map (from the PLC or a file), change the
    polling intervals, see status.

    ``self.config_entry`` is provided by Home Assistant; do not store it.
    """

    def __init__(self) -> None:
        self._setup_task: Any | None = None
        self._result: SetupResult | None = None
        self._error: str | None = None
        self._reimport_client: EpsnetUdpClient | None = None
        self._reimport_owns_client = False
        self._reimport_entries: tuple[PubEntry, ...] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        blob = self.config_entry.data.get(CONF_MAP) or {}
        # Variant step ids carry the re-import warning (see async_step_source).
        step_id = f"init_{self._error}" if self._error else "init"
        self._error = None
        return self.async_show_menu(
            step_id=step_id,
            menu_options=["reimport_plc", "reimport_file", "settings"],
            description_placeholders={
                "source": str(blob.get("source", "—")),
                "ident": blob.get("plc_ident") or "—",
                "points": str(len(blob.get("entries", []))),
                "crc": hex(blob.get("crc", 0)),
            },
        )

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

    # -- Scan interval -------------------------------------------------------

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.config_entry
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_FAST_SCAN_INTERVAL: int(user_input.get(CONF_FAST_SCAN_INTERVAL, DEFAULT_FAST_SCAN_INTERVAL)),
                },
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(title="", data={})
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=300_000)),
                vol.Required(
                    CONF_FAST_SCAN_INTERVAL,
                    default=entry.data.get(CONF_FAST_SCAN_INTERVAL, DEFAULT_FAST_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=5_000)),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

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
        from homeassistant.helpers.selector import (
            FileSelector,
            FileSelectorConfig,
        )

        errors: dict[str, str] = {}
        if self._error == "pub_no_match":
            errors[CONF_MAP_FILE] = self._error
            self._error = None
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

        schema = vol.Schema(
            {
                vol.Required(CONF_MAP_FILE): FileSelector(
                    FileSelectorConfig(accept=_PUB_UPLOAD_ACCEPT)
                )
            }
        )
        return self.async_show_form(
            step_id="reimport_file", data_schema=schema, errors=errors
        )

    async def async_step_reimport_file_run(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self._async_reimport_progress("reimport_file_run")

    # -- Shared re-import progress -------------------------------------------

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
            self._setup_task = self.hass.async_create_task(self._async_reimport())
            self.hass.async_create_task(
                _pump_map_fetch_progress(
                    self,
                    self.hass.config_entries.options,
                    self._reimport_client,
                    self._setup_task,
                )
            )

        if not self._setup_task.done():
            return self.async_show_progress(
                step_id=step_id,
                progress_action="run_file_setup" if from_file else "run_setup",
                description_placeholders=_fetch_progress_placeholders(
                    self._reimport_client
                ),
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
        if from_file and not self._result.catalog.entities:
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
