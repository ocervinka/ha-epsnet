from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
from typing import Generic, TypeVar

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EPSNET_DIR = REPO_ROOT / "custom_components" / "epsnet"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _register_package(monkeypatch: pytest.MonkeyPatch, package_name: str) -> None:
    package = types.ModuleType(package_name)
    package.__path__ = [str(EPSNET_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)


def _install_voluptuous_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    voluptuous = types.ModuleType("voluptuous")

    class Invalid(Exception):
        pass

    missing = object()

    class _Marker:
        def __init__(self, key: str, *, default=missing, required: bool) -> None:
            self.key = key
            self.default = default
            self.required = required

        def __hash__(self) -> int:
            return hash((self.key, self.required))

    def Required(key: str, default=missing) -> _Marker:
        return _Marker(key, default=default, required=True)

    def Optional(key: str, default=missing) -> _Marker:
        return _Marker(key, default=default, required=False)

    def Coerce(type_):
        def validator(value):
            try:
                return type_(value)
            except Exception as err:  # pragma: no cover - simple stub
                raise Invalid(str(err)) from err

        return validator

    def Range(*, min=None, max=None):
        def validator(value):
            if min is not None and value < min:
                raise Invalid(f"Value {value} is smaller than {min}")
            if max is not None and value > max:
                raise Invalid(f"Value {value} is larger than {max}")
            return value

        return validator

    def All(*validators):
        def validator(value):
            current = value
            for item in validators:
                current = item(current)
            return current

        return validator

    class Schema:
        def __init__(self, schema_dict):
            self.schema_dict = schema_dict

        def __call__(self, data):
            validated = {}
            for marker, validator in self.schema_dict.items():
                key = marker.key if isinstance(marker, _Marker) else marker
                default = marker.default if isinstance(marker, _Marker) else missing
                required = marker.required if isinstance(marker, _Marker) else True

                if key in data:
                    value = data[key]
                elif default is not missing:
                    value = default
                elif required:
                    raise Invalid(f"Missing required key: {key}")
                else:
                    continue

                if validator is str:
                    if not isinstance(value, str):
                        raise Invalid(f"Expected string for {key}")
                    validated[key] = value
                    continue

                validated[key] = validator(value)

            return validated

    voluptuous.All = All
    voluptuous.Coerce = Coerce
    voluptuous.Invalid = Invalid
    voluptuous.Optional = Optional
    voluptuous.Range = Range
    voluptuous.Required = Required
    voluptuous.Schema = Schema
    monkeypatch.setitem(sys.modules, "voluptuous", voluptuous)


def _install_homeassistant_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)

    core = types.ModuleType("homeassistant.core")
    core.callback = lambda func: func
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)


def _load_config_flow_module(monkeypatch: pytest.MonkeyPatch):
    package_name = "epsnet_testpkg_config"
    _register_package(monkeypatch, package_name)
    _install_homeassistant_stubs(monkeypatch)
    _install_voluptuous_stub(monkeypatch)

    config_entries = types.ModuleType("homeassistant.config_entries")

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs) -> None:
            return None

        def async_show_form(self, *, step_id, data_schema, errors):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors,
            }

        def async_create_entry(self, *, title, data):
            return {"type": "create_entry", "title": title, "data": data}

    class OptionsFlow:
        def __init_subclass__(cls, **kwargs) -> None:
            return None

    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow
    config_entries.ConfigEntry = object
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries)

    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict

    class FlowResultType:
        SHOW_PROGRESS = "show_progress"

    data_entry_flow.FlowResultType = FlowResultType
    monkeypatch.setitem(sys.modules, "homeassistant.data_entry_flow", data_entry_flow)

    epsnet_udp = types.ModuleType(f"{package_name}.epsnet_udp")

    class EpsnetError(Exception):
        pass

    class PlcBusyError(EpsnetError):
        pass

    epsnet_udp.EpsnetError = EpsnetError
    epsnet_udp.PlcBusyError = PlcBusyError
    epsnet_udp.EpsnetUdpClient = object
    monkeypatch.setitem(sys.modules, f"{package_name}.epsnet_udp", epsnet_udp)

    map_sources = types.ModuleType(f"{package_name}.map_sources")
    map_sources.SetupResult = object
    map_sources.freeze_setup = lambda result: {}
    map_sources.run_setup = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, f"{package_name}.map_sources", map_sources)

    models = types.ModuleType(f"{package_name}.models")
    models.PubEntry = object
    monkeypatch.setitem(sys.modules, f"{package_name}.models", models)

    pubfile = types.ModuleType(f"{package_name}.pubfile")

    class PubFileParseError(Exception):
        pass

    pubfile.PubFileParseError = PubFileParseError
    pubfile.load_pub_entries = lambda path: ()
    monkeypatch.setitem(sys.modules, f"{package_name}.pubfile", pubfile)

    _load_module(f"{package_name}.const", EPSNET_DIR / "const.py")
    return _load_module(f"{package_name}.config_flow", EPSNET_DIR / "config_flow.py")


def _load_coordinator_module(monkeypatch: pytest.MonkeyPatch):
    package_name = "epsnet_testpkg_coordinator"
    _register_package(monkeypatch, package_name)
    _install_homeassistant_stubs(monkeypatch)

    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
    type_var = TypeVar("type_var")

    class UpdateFailed(Exception):
        def __init__(self, message: str, retry_after: float | None = None) -> None:
            super().__init__(message)
            self.retry_after = retry_after

    class DataUpdateCoordinator(Generic[type_var]):
        def __init__(
            self,
            hass,
            logger,
            *,
            config_entry=None,
            name: str,
            update_interval=None,
        ) -> None:
            self.hass = hass
            self.logger = logger
            self.config_entry = config_entry
            self.name = name
            self._listeners = {}
            self._retry_after = None
            self._unsub_refresh = None
            self._update_interval = None
            self._update_interval_seconds = None
            self.update_interval = update_interval
            self.super_schedule_calls = 0

        @property
        def update_interval(self):
            return self._update_interval

        @update_interval.setter
        def update_interval(self, value) -> None:
            self._update_interval = value
            self._update_interval_seconds = value.total_seconds() if value else None

        def _schedule_refresh(self) -> None:
            self.super_schedule_calls += 1

        def _async_unsub_refresh(self) -> None:
            if self._unsub_refresh is not None:
                self._unsub_refresh()
                self._unsub_refresh = None

        async def _handle_refresh_interval(self) -> None:
            return None

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.update_coordinator", update_coordinator
    )

    client = types.ModuleType(f"{package_name}.client")
    client.EpsnetClient = object
    monkeypatch.setitem(sys.modules, f"{package_name}.client", client)

    epsnet_udp = types.ModuleType(f"{package_name}.epsnet_udp")

    class EpsnetError(Exception):
        pass

    epsnet_udp.EpsnetError = EpsnetError
    monkeypatch.setitem(sys.modules, f"{package_name}.epsnet_udp", epsnet_udp)

    models = types.ModuleType(f"{package_name}.models")

    class MathFormula:
        NONE = "none"
        DIV_10 = "div_10"
        DIV_100 = "div_100"
        MUL_10 = "mul_10"
        MUL_100 = "mul_100"

    class EntityCatalog:
        entities = ()

        def state_entries(self):
            return ()

    models.MathFormula = MathFormula
    models.EntityCatalog = EntityCatalog
    models.ScalarValue = bool | float | int | str
    monkeypatch.setitem(sys.modules, f"{package_name}.models", models)

    return _load_module(f"{package_name}.coordinator", EPSNET_DIR / "coordinator.py")


def _load_init_module(monkeypatch: pytest.MonkeyPatch):
    package_name = "epsnet_testpkg_init"
    _register_package(monkeypatch, package_name)

    catalog = types.ModuleType(f"{package_name}.catalog")
    catalog.EntityCatalog = object
    catalog.build_catalog = lambda *args, **kwargs: None
    catalog.unsupported_module_summary = lambda *args, **kwargs: (0, "")
    monkeypatch.setitem(sys.modules, f"{package_name}.catalog", catalog)

    client = types.ModuleType(f"{package_name}.client")
    client.EpsnetClient = object
    monkeypatch.setitem(sys.modules, f"{package_name}.client", client)

    diagnostics = types.ModuleType(f"{package_name}.diagnostics")
    diagnostics.build_catalog_snapshot = lambda *args, **kwargs: {}
    diagnostics.write_catalog_snapshot = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, f"{package_name}.diagnostics", diagnostics)

    epsnet_udp = types.ModuleType(f"{package_name}.epsnet_udp")

    class EpsnetError(Exception):
        pass

    epsnet_udp.EpsnetError = EpsnetError
    epsnet_udp.EpsnetUdpClient = object
    monkeypatch.setitem(sys.modules, f"{package_name}.epsnet_udp", epsnet_udp)

    models = types.ModuleType(f"{package_name}.models")
    models.DetectedDevice = object
    models.RuntimeDiscoveredDevice = object
    models.PubEntry = object
    monkeypatch.setitem(sys.modules, f"{package_name}.models", models)

    map_sources = types.ModuleType(f"{package_name}.map_sources")
    map_sources.deserialize_entries = lambda data: ()
    monkeypatch.setitem(sys.modules, f"{package_name}.map_sources", map_sources)

    pubfile = types.ModuleType(f"{package_name}.pubfile")

    class PubFileParseError(Exception):
        pass

    pubfile.PubFileParseError = PubFileParseError
    pubfile.load_pub_entries = lambda path: ()
    monkeypatch.setitem(sys.modules, f"{package_name}.pubfile", pubfile)

    _load_module(f"{package_name}.const", EPSNET_DIR / "const.py")
    spec = importlib.util.spec_from_file_location(
        package_name,
        EPSNET_DIR / "__init__.py",
        submodule_search_locations=[str(EPSNET_DIR)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_config_flow_accepts_millisecond_scan_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_flow = _load_config_flow_module(monkeypatch)
    assert config_flow.DEFAULT_SCAN_INTERVAL == 500
    flow = config_flow.EpsnetConfigFlow()
    result = await flow.async_step_user()
    schema = result["data_schema"]

    validated = schema(
        {
            config_flow.CONF_NAME: "EPSNET",
            config_flow.CONF_HOST: "192.0.2.10",
            config_flow.CONF_PORT: 61682,
            config_flow.CONF_LOCAL_PORT: 61681,
            config_flow.CONF_SCAN_INTERVAL: 200,
        }
    )

    assert validated[config_flow.CONF_SCAN_INTERVAL] == 200

    with pytest.raises(config_flow.vol.Invalid):
        schema(
            {
                config_flow.CONF_NAME: "EPSNET",
                config_flow.CONF_HOST: "192.0.2.10",
                config_flow.CONF_PORT: 61682,
                config_flow.CONF_LOCAL_PORT: 61681,
                    config_flow.CONF_SCAN_INTERVAL: 300_001,
            }
        )


class _FakeHandle:
    def __init__(self, callback, *, delay: float | None = None) -> None:
        self.callback = callback
        self.delay = delay
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeLoop:
    def __init__(self) -> None:
        self.soon_handles: list[_FakeHandle] = []
        self.later_handles: list[_FakeHandle] = []

    def call_soon(self, callback) -> _FakeHandle:
        handle = _FakeHandle(callback)
        self.soon_handles.append(handle)
        return handle

    def call_later(self, delay: float, callback) -> _FakeHandle:
        handle = _FakeHandle(callback, delay=delay)
        self.later_handles.append(handle)
        return handle


class _FakeHass:
    def __init__(self) -> None:
        self.loop = _FakeLoop()
        self.background_tasks: list[dict[str, object]] = []

    def async_create_background_task(self, coro, *, name: str, eager_start: bool):
        self.background_tasks.append({"name": name, "eager_start": eager_start})
        coro.close()
        return None


def _build_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scan_interval_milliseconds: int,
    client=None,
):
    coordinator_module = _load_coordinator_module(monkeypatch)
    hass = _FakeHass()
    catalog = coordinator_module.EntityCatalog()
    coordinator = coordinator_module.EpsnetDataCoordinator(
        hass,
        client=object() if client is None else client,
        catalog=catalog,
        name="EPSNET",
        scan_interval_milliseconds=scan_interval_milliseconds,
    )
    return coordinator, hass


class _FakeReadMetrics:
    def __init__(
        self,
        duration_seconds: float,
        *,
        span_count: int | None = None,
        block_min_seconds: float | None = None,
        block_avg_seconds: float | None = None,
        block_max_seconds: float | None = None,
    ) -> None:
        self.duration_seconds = duration_seconds
        self.span_count = span_count
        self.block_min_seconds = block_min_seconds
        self.block_avg_seconds = block_avg_seconds
        self.block_max_seconds = block_max_seconds


class _FakeClient:
    def __init__(self, duration_seconds: float | None = None) -> None:
        self.last_read_metrics = (
            None if duration_seconds is None else _FakeReadMetrics(duration_seconds)
        )


def test_zero_scan_interval_schedules_immediate_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, hass = _build_coordinator(monkeypatch, scan_interval_milliseconds=0)

    coordinator._schedule_refresh()

    assert coordinator.super_schedule_calls == 0
    assert len(hass.loop.soon_handles) == 1
    assert not hass.loop.soon_handles[0].cancelled

    coordinator._unsub_refresh()

    assert hass.loop.soon_handles[0].cancelled

    coordinator._schedule_refresh()

    assert len(hass.loop.soon_handles) == 2

    hass.loop.soon_handles[1].callback()

    assert hass.background_tasks == [{"name": "EPSNET - refresh", "eager_start": True}]


def test_subsecond_scan_interval_schedules_precise_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, hass = _build_coordinator(monkeypatch, scan_interval_milliseconds=200)

    coordinator._schedule_refresh()

    assert coordinator.super_schedule_calls == 0
    assert coordinator.update_interval.total_seconds() == 0.2
    assert hass.loop.soon_handles == []
    assert len(hass.loop.later_handles) == 1
    assert hass.loop.later_handles[0].delay == 0.2

    hass.loop.later_handles[0].callback()

    assert hass.background_tasks == [{"name": "EPSNET - refresh", "eager_start": True}]


def test_subsecond_retry_after_schedules_precise_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, hass = _build_coordinator(monkeypatch, scan_interval_milliseconds=0)
    coordinator._retry_after = 0.25

    coordinator._schedule_refresh()

    assert coordinator.super_schedule_calls == 0
    assert len(hass.loop.later_handles) == 1
    assert hass.loop.later_handles[0].delay == 0.25
    assert coordinator._retry_after is None


def test_one_second_or_more_uses_base_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, hass = _build_coordinator(monkeypatch, scan_interval_milliseconds=5_000)

    coordinator._schedule_refresh()

    assert coordinator.super_schedule_calls == 1
    assert hass.loop.soon_handles == []
    assert hass.loop.later_handles == []


def test_poll_metrics_publish_smoothed_read_and_cycle_durations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(duration_seconds=0.10)
    coordinator, _ = _build_coordinator(
        monkeypatch,
        scan_interval_milliseconds=500,
        client=client,
    )

    coordinator._record_poll_metrics(poll_started=10.0, poll_finished=10.1)

    assert coordinator.poll_metrics_version == 1
    assert coordinator.poll_metrics.average_read_duration_ms == pytest.approx(100.0)
    assert coordinator.poll_metrics.minimum_read_duration_ms == pytest.approx(100.0)
    assert coordinator.poll_metrics.maximum_read_duration_ms == pytest.approx(100.0)
    assert coordinator.poll_metrics.average_cycle_duration_ms is None
    assert coordinator.poll_metrics.minimum_cycle_duration_ms is None
    assert coordinator.poll_metrics.maximum_cycle_duration_ms is None

    client.last_read_metrics = _FakeReadMetrics(0.20)
    coordinator._record_poll_metrics(poll_started=11.0, poll_finished=11.2)

    assert coordinator.poll_metrics_version == 1
    assert coordinator.poll_metrics.average_read_duration_ms == pytest.approx(100.0)

    client.last_read_metrics = _FakeReadMetrics(0.30)
    coordinator._record_poll_metrics(poll_started=16.0, poll_finished=16.3)

    assert coordinator.poll_metrics_version == 2
    assert coordinator.poll_metrics.average_read_duration_ms == pytest.approx(156.0)
    assert coordinator.poll_metrics.minimum_read_duration_ms == pytest.approx(200.0)
    assert coordinator.poll_metrics.maximum_read_duration_ms == pytest.approx(300.0)
    assert coordinator.poll_metrics.average_cycle_duration_ms == pytest.approx(1_800.0)
    assert coordinator.poll_metrics.minimum_cycle_duration_ms == pytest.approx(1_000.0)
    assert coordinator.poll_metrics.maximum_cycle_duration_ms == pytest.approx(5_000.0)

    client.last_read_metrics = _FakeReadMetrics(0.40)
    coordinator._record_poll_metrics(poll_started=22.0, poll_finished=22.4)

    assert coordinator.poll_metrics_version == 3
    assert coordinator.poll_metrics.average_read_duration_ms == pytest.approx(204.8)
    assert coordinator.poll_metrics.minimum_read_duration_ms == pytest.approx(400.0)
    assert coordinator.poll_metrics.maximum_read_duration_ms == pytest.approx(400.0)
    assert coordinator.poll_metrics.average_cycle_duration_ms == pytest.approx(2_640.0)
    assert coordinator.poll_metrics.minimum_cycle_duration_ms == pytest.approx(6_000.0)
    assert coordinator.poll_metrics.maximum_cycle_duration_ms == pytest.approx(6_000.0)


@pytest.mark.asyncio
async def test_migrate_entry_converts_seconds_to_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_module = _load_init_module(monkeypatch)
    updates: list[dict[str, object]] = []

    class _ConfigEntriesManager:
        def async_update_entry(self, entry, *, data, version) -> None:
            entry.data = data
            entry.version = version
            updates.append({"data": data, "version": version})

    class _Hass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntriesManager()

    entry = types.SimpleNamespace(
        version=2,
        data={"scan_interval": 5, "host": "192.0.2.10", "port": 61682, "local_port": 61681, "name": "EPSNET"},
        title="EPSNET",
        entry_id="entry-1",
    )

    result = await init_module.async_migrate_entry(_Hass(), entry)

    assert result is True
    assert entry.version == 4
    assert entry.data["scan_interval"] == 5_000
    assert updates == [{"data": entry.data, "version": 4}]


@pytest.mark.asyncio
async def test_migrate_entry_preserves_zero_scan_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_module = _load_init_module(monkeypatch)

    class _ConfigEntriesManager:
        def async_update_entry(self, entry, *, data, version) -> None:
            entry.data = data
            entry.version = version

    class _Hass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntriesManager()

    entry = types.SimpleNamespace(
        version=2,
        data={"scan_interval": 0, "host": "192.0.2.10", "port": 61682, "local_port": 61681, "name": "EPSNET"},
        title="EPSNET",
        entry_id="entry-2",
    )

    result = await init_module.async_migrate_entry(_Hass(), entry)

    assert result is True
    assert entry.version == 4
    assert entry.data["scan_interval"] == 0


@pytest.mark.asyncio
async def test_migrate_entry_uses_legacy_default_when_scan_interval_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_module = _load_init_module(monkeypatch)

    class _ConfigEntriesManager:
        def async_update_entry(self, entry, *, data, version) -> None:
            entry.data = data
            entry.version = version

    class _Hass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntriesManager()

    entry = types.SimpleNamespace(
        version=2,
        data={"host": "192.0.2.10", "port": 61682, "local_port": 61681, "name": "EPSNET"},
        title="EPSNET",
        entry_id="entry-3",
    )

    result = await init_module.async_migrate_entry(_Hass(), entry)

    assert result is True
    assert entry.version == 4
    assert entry.data["scan_interval"] == 5_000
