"""Home Assistant data coordinator for the EPSNET integration."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
import logging
from time import monotonic

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import EpsnetClient
from .epsnet_udp import EpsnetError
from typing import TYPE_CHECKING

from .models import EntityCatalog, MathFormula, ScalarValue

if TYPE_CHECKING:  # PubEntry is only used in annotations (keeps the test stubs happy)
    from .models import PubEntry

LOGGER = logging.getLogger(__name__)
_POLL_METRICS_EMA_ALPHA = 0.2
# With that alpha the first read's share of the average is below 2 % after this many.
_TIMING_SETTLED_AFTER_READS = 20
_POLL_METRICS_PUBLISH_INTERVAL_SECONDS = 5.0
# After a press, how long an output is given to change and how often it is read.
# A program reacts within a few of its cycles (measured: 70 to 210 ms).
PRESS_CONFIRM_SECONDS = 1.5
PRESS_POLL_SECONDS = 0.05
# After a value is written into an output, how long until it is read back: a
# program that assigns the output itself has taken it back by then.
OUTPUT_CONFIRM_SECONDS = 0.3


def output_is_on(value: ScalarValue | None) -> bool:
    """On for an on/off output; for a level (a dimmer), anything above zero."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value > 0
    return bool(value)


@dataclass(frozen=True, slots=True)
class EpsnetPollMetricsSnapshot:
    """Smoothed timing metrics exposed to Home Assistant."""

    average_read_duration_ms: float | None = None
    minimum_read_duration_ms: float | None = None
    maximum_read_duration_ms: float | None = None
    average_cycle_duration_ms: float | None = None
    minimum_cycle_duration_ms: float | None = None
    maximum_cycle_duration_ms: float | None = None
    # Per-block (per-span) request->reply timing and the block count per cycle.
    block_count: int | None = None
    average_block_duration_ms: float | None = None
    minimum_block_duration_ms: float | None = None
    maximum_block_duration_ms: float | None = None


class EpsnetDataCoordinator(
    DataUpdateCoordinator[dict[str, ScalarValue]]
):
    """Poll values for all configured EPSNET entities."""

    def __init__(
        self,
        hass,
        *,
        client: EpsnetClient,
        catalog: EntityCatalog,
        name: str,
        scan_interval_milliseconds: int,
        config_entry: object | None = None,
        fast_scan_interval_milliseconds: int = 0,
        fast_entries: tuple[PubEntry, ...] = (),
    ) -> None:
        self._continuous_polling = scan_interval_milliseconds == 0
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=name,
            update_interval=timedelta(milliseconds=scan_interval_milliseconds),
        )
        self.client = client
        self.catalog = catalog
        self._poll_sequence = 0
        self._timing_samples = 0
        self._average_read_duration_ms: float | None = None
        self._average_cycle_duration_ms: float | None = None
        self._last_poll_started: float | None = None
        self._last_metrics_published_at: float | None = None
        self._window_minimum_read_duration_ms: float | None = None
        self._window_maximum_read_duration_ms: float | None = None
        self._window_minimum_cycle_duration_ms: float | None = None
        self._window_maximum_cycle_duration_ms: float | None = None
        self._average_block_duration_ms: float | None = None
        self._window_minimum_block_duration_ms: float | None = None
        self._window_maximum_block_duration_ms: float | None = None
        self._last_block_count: int | None = None
        self._poll_metrics = EpsnetPollMetricsSnapshot()
        self._poll_metrics_version = 0
        # Fast lane (binary inputs polled every few 100 ms so short button presses and
        # PIR pulses are not lost between full cycles). Off unless interval > 0.
        self._fast_interval_seconds = max(0, int(fast_scan_interval_milliseconds)) / 1000.0
        self._fast_entries: tuple[PubEntry, ...] = tuple(fast_entries) if fast_scan_interval_milliseconds > 0 else ()
        self._fast_names = frozenset(e.name for e in self._fast_entries)
        self._fast_values: dict[str, ScalarValue] = {}
        self._fast_task: asyncio.Task | None = None
        self._fast_started = False
        self._fast_sequence = 0
        self._fast_changes = 0
        self._fast_failures = 0
        # Entities that start disabled are polled only once the user enables them
        # (an enabled entity registers its state name when it is added).
        enabled = {e.state_name for e in catalog.entities if e.enabled_by_default}
        self._opt_in_state_names = frozenset(
            e.state_name
            for e in catalog.entities
            if not e.enabled_by_default and e.state_name not in enabled
        )
        self._added_state_names: Counter[str] = Counter()
        # True while a changed PLC program has not been re-read: values placed by
        # the old program layout may be wrong, so those entities are unavailable.
        self.program_layout_stale = False

    @callback
    def add_state_name(self, state_name: str) -> None:
        self._added_state_names[state_name] += 1

    @callback
    def remove_state_name(self, state_name: str) -> None:
        self._added_state_names[state_name] -= 1
        if self._added_state_names[state_name] <= 0:
            del self._added_state_names[state_name]

    def polled_state_entries(self):
        """State entries to read: enabled-by-default entities plus the ones enabled since."""
        skipped = self._opt_in_state_names - self._added_state_names.keys()
        return tuple(e for e in self.catalog.state_entries() if e.name not in skipped)

    # ------------------------------------------------------------------ fast lane ----
    @property
    def fast_lane_active(self) -> bool:
        """True when binary inputs are polled on the fast lane."""
        return bool(self._fast_entries) and self._fast_interval_seconds > 0

    @property
    def fast_lane_stats(self) -> dict[str, int | float]:
        return {
            "interval_ms": int(self._fast_interval_seconds * 1000),
            "inputs": len(self._fast_entries),
            "polls": self._fast_sequence,
            "changes": self._fast_changes,
            "failures": self._fast_failures,
        }

    def start_fast_lane(self) -> None:
        """Start the fast-lane poller (call once, after the first full refresh)."""
        if not self.fast_lane_active or self._fast_started:
            return
        self._fast_started = True
        LOGGER.info(
            "EPSNET fast lane: %d binary inputs every %d ms (full cycle keeps the rest)",
            len(self._fast_entries),
            int(self._fast_interval_seconds * 1000),
        )
        if self.config_entry:
            self._fast_task = self.config_entry.async_create_background_task(
                self.hass, self._fast_loop(), name=f"{self.name} - fast lane", eager_start=True
            )
        else:
            self._fast_task = self.hass.async_create_background_task(
                self._fast_loop(), name=f"{self.name} - fast lane", eager_start=True
            )

    async def async_shutdown(self) -> None:
        if self._fast_task is not None:
            self._fast_task.cancel()
            self._fast_task = None
        await super().async_shutdown()

    def _formula_for(self, state_name: str) -> MathFormula:
        for e in self.catalog.entities:
            if e.state_name == state_name:
                return e.transform_read
        return MathFormula.NONE

    async def _fast_loop(self) -> None:
        formulas = {name: self._formula_for(name) for name in self._fast_names}
        while True:
            await asyncio.sleep(self._fast_interval_seconds)
            if self.data is None:
                continue
            self._fast_sequence += 1
            try:
                raw = await self.client.get_values(self._fast_entries)
            except EpsnetError as err:
                self._fast_failures += 1
                LOGGER.debug("EPSNET fast lane poll failed: %s", err)
                await asyncio.sleep(1.0)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — never let the fast lane die silently
                self._fast_failures += 1
                LOGGER.warning("EPSNET fast lane error: %s", err)
                await asyncio.sleep(1.0)
                continue
            changed: dict[str, ScalarValue] = {}
            for name, val in raw.items():
                value = self._apply_formula(val, formulas.get(name, MathFormula.NONE))
                self._fast_values[name] = value
                if self.data.get(name) != value:
                    changed[name] = value
            if changed:
                self._fast_changes += 1
                LOGGER.debug("EPSNET fast lane: %d input(s) changed: %s", len(changed), changed)
                self.async_set_updated_data({**self.data, **changed})

    @property
    def poll_metrics(self) -> EpsnetPollMetricsSnapshot:
        """Return the latest published timing metrics snapshot."""
        return self._poll_metrics

    @property
    def full_read_count(self) -> int:
        """How many values one full read covers (the fast lane's inputs are not in it)."""
        entries = self.polled_state_entries()
        if self._fast_task is not None and self._fast_values:
            return sum(1 for entry in entries if entry.name not in self._fast_names)
        return len(entries)

    @property
    def current_timing_ms(self) -> tuple[float, float] | None:
        """Running (read, cycle) averages in ms, or ``None`` until they have settled.

        Unlike ``poll_metrics`` (published to the sensors at intervals) this is
        always current. The first read after a start is often much slower than
        the rest and weighs on the averages for a while, so they are held back
        for the first ``_TIMING_SETTLED_AFTER_READS`` reads.
        """
        read, cycle = self._average_read_duration_ms, self._average_cycle_duration_ms
        if read is None or cycle is None:
            return None
        if self._timing_samples < _TIMING_SETTLED_AFTER_READS:
            return None
        return read, cycle

    @property
    def poll_metrics_version(self) -> int:
        """Return a monotonically increasing metrics snapshot version."""
        return self._poll_metrics_version

    def _update_ewma(self, current: float | None, sample: float) -> float:
        """Blend a sample into a running exponential moving average."""
        if current is None:
            return sample
        return current + (_POLL_METRICS_EMA_ALPHA * (sample - current))

    def _update_range(
        self,
        minimum: float | None,
        maximum: float | None,
        sample: float,
    ) -> tuple[float, float]:
        """Expand a min/max pair with one new sample."""
        next_minimum = sample if minimum is None else min(minimum, sample)
        next_maximum = sample if maximum is None else max(maximum, sample)
        return next_minimum, next_maximum

    def _record_poll_metrics(self, *, poll_started: float, poll_finished: float) -> None:
        """Capture smoothed read/cycle timing and throttle HA publication."""
        self._timing_samples += 1
        read_metrics = self.client.last_read_metrics
        read_duration_ms = (
            read_metrics.duration_seconds * 1000.0
            if read_metrics is not None
            else (poll_finished - poll_started) * 1000.0
        )
        self._average_read_duration_ms = self._update_ewma(
            self._average_read_duration_ms,
            read_duration_ms,
        )
        (
            self._window_minimum_read_duration_ms,
            self._window_maximum_read_duration_ms,
        ) = self._update_range(
            self._window_minimum_read_duration_ms,
            self._window_maximum_read_duration_ms,
            read_duration_ms,
        )

        # Per-block (per-span) timing: EWMA the mean, window the fastest/slowest
        # single block, and remember the block count for this cycle.
        if read_metrics is not None and read_metrics.block_avg_seconds is not None:
            self._last_block_count = read_metrics.span_count
            self._average_block_duration_ms = self._update_ewma(
                self._average_block_duration_ms,
                read_metrics.block_avg_seconds * 1000.0,
            )
            if read_metrics.block_min_seconds is not None:
                sample = read_metrics.block_min_seconds * 1000.0
                self._window_minimum_block_duration_ms = (
                    sample
                    if self._window_minimum_block_duration_ms is None
                    else min(self._window_minimum_block_duration_ms, sample)
                )
            if read_metrics.block_max_seconds is not None:
                sample = read_metrics.block_max_seconds * 1000.0
                self._window_maximum_block_duration_ms = (
                    sample
                    if self._window_maximum_block_duration_ms is None
                    else max(self._window_maximum_block_duration_ms, sample)
                )

        if self._last_poll_started is not None:
            cycle_duration_ms = (poll_started - self._last_poll_started) * 1000.0
            self._average_cycle_duration_ms = self._update_ewma(
                self._average_cycle_duration_ms,
                cycle_duration_ms,
            )
            (
                self._window_minimum_cycle_duration_ms,
                self._window_maximum_cycle_duration_ms,
            ) = self._update_range(
                self._window_minimum_cycle_duration_ms,
                self._window_maximum_cycle_duration_ms,
                cycle_duration_ms,
            )
        self._last_poll_started = poll_started

        should_publish = (
            self._last_metrics_published_at is None
            or (poll_finished - self._last_metrics_published_at)
            >= _POLL_METRICS_PUBLISH_INTERVAL_SECONDS
        )
        if not should_publish:
            return

        self._poll_metrics = EpsnetPollMetricsSnapshot(
            average_read_duration_ms=self._average_read_duration_ms,
            minimum_read_duration_ms=self._window_minimum_read_duration_ms,
            maximum_read_duration_ms=self._window_maximum_read_duration_ms,
            average_cycle_duration_ms=self._average_cycle_duration_ms,
            minimum_cycle_duration_ms=self._window_minimum_cycle_duration_ms,
            maximum_cycle_duration_ms=self._window_maximum_cycle_duration_ms,
            block_count=self._last_block_count,
            average_block_duration_ms=self._average_block_duration_ms,
            minimum_block_duration_ms=self._window_minimum_block_duration_ms,
            maximum_block_duration_ms=self._window_maximum_block_duration_ms,
        )
        self._last_metrics_published_at = poll_finished
        self._poll_metrics_version += 1
        self._window_minimum_read_duration_ms = None
        self._window_maximum_read_duration_ms = None
        self._window_minimum_cycle_duration_ms = None
        self._window_maximum_cycle_duration_ms = None
        self._window_minimum_block_duration_ms = None
        self._window_maximum_block_duration_ms = None

    @callback
    def _schedule_refresh(self) -> None:
        """Schedule the next poll.

        Home Assistant's built-in scheduler quantizes periodic refreshes around
        whole-second boundaries. EPSNET exposes millisecond intervals and a
        special zero-immediate mode, so sub-second delays are scheduled
        precisely on the event loop instead of using the default coordinator
        timing.
        """
        effective_delay = (
            self._retry_after
            if self._retry_after is not None
            else 0 if self._continuous_polling else self._update_interval_seconds
        )
        if effective_delay is None:
            return

        if effective_delay >= 1:
            super()._schedule_refresh()
            return

        if self.config_entry and self.config_entry.pref_disable_polling:
            return

        self._async_unsub_refresh()
        if self._retry_after is not None:
            self._retry_after = None

        if effective_delay == 0:
            self._unsub_refresh = self.hass.loop.call_soon(
                self._async_schedule_precise_refresh
            ).cancel
            return

        self._unsub_refresh = self.hass.loop.call_later(
            effective_delay, self._async_schedule_precise_refresh
        ).cancel

    @callback
    def _async_schedule_precise_refresh(self) -> None:
        """Start the next refresh from a precise loop callback."""
        if self.config_entry:
            self.config_entry.async_create_background_task(
                self.hass,
                self._handle_refresh_interval(),
                name=f"{self.name} - {self.config_entry.title} - refresh",
                eager_start=True,
            )
            return

        self.hass.async_create_background_task(
            self._handle_refresh_interval(),
            name=f"{self.name} - refresh",
            eager_start=True,
        )

    def _apply_formula(self, value: ScalarValue, formula: MathFormula) -> ScalarValue:
        if formula == MathFormula.NONE or not isinstance(value, (int, float)):
            return value
        if formula == MathFormula.DIV_10: return value / 10.0
        if formula == MathFormula.DIV_100: return value / 100.0
        if formula == MathFormula.MUL_10: return value * 10
        if formula == MathFormula.MUL_100: return value * 100
        return value

    async def _async_update_data(self) -> dict[str, ScalarValue]:
        self._poll_sequence += 1
        poll_started = monotonic()
        state_entries = self.polled_state_entries()
        fast_running = self._fast_task is not None and self._fast_values
        if fast_running:
            # The fast lane owns the binary inputs: don't re-read them here (shorter cycle) and,
            # more importantly, never overwrite a fresh fast value with an older full-cycle one.
            state_entries = tuple(e for e in state_entries if e.name not in self._fast_names)
        LOGGER.debug(
            "EPSNET poll #%d starting: states=%d",
            self._poll_sequence,
            len(state_entries),
        )
        try:
            raw_values = await self.client.get_values(state_entries)
            
            # Apply transformations
            final_values: dict[str, ScalarValue] = {}
            formula_by_state: dict[str, MathFormula] = {
                e.state_name: e.transform_read for e in self.catalog.entities
            }
            
            for entry_name, val in raw_values.items():
                formula = formula_by_state.get(entry_name, MathFormula.NONE)
                final_values[entry_name] = self._apply_formula(val, formula)
            if fast_running:
                final_values.update(self._fast_values)

            poll_finished = monotonic()
            self._record_poll_metrics(
                poll_started=poll_started,
                poll_finished=poll_finished,
            )
            LOGGER.debug(
                "EPSNET poll #%d finished: values=%d duration=%.3fs",
                self._poll_sequence,
                len(final_values),
                poll_finished - poll_started,
            )
            return final_values
        except EpsnetError as err:
            LOGGER.warning(
                "EPSNET poll #%d failed after %.3fs while reading %d states: %s",
                self._poll_sequence,
                monotonic() - poll_started,
                len(state_entries),
                err,
            )
            raise UpdateFailed(str(err)) from err

    async def async_press_command(self, command_name: str) -> None:
        """Write a one-shot boolean command variable."""
        LOGGER.debug("EPSNET command press requested: %s", command_name)
        await self.client.set_value(self.catalog.entries_by_name[command_name], True)

    async def _async_read_output(self, state_name: str) -> ScalarValue:
        entry = self.catalog.entries_by_name[state_name]
        raw = (await self.client.get_values((entry,)))[state_name]
        return self._apply_formula(raw, self._formula_for(state_name))

    async def async_switch_output(
        self,
        state_name: str,
        on: bool,
        *,
        press_name: str | None = None,
        on_value: ScalarValue = True,
    ) -> ScalarValue | None:
        """Bring an output without command variables to ``on``.

        The output is read first: already as wanted, nothing is done. With a
        front button (``press_name``) that is pressed, which flips the output
        in most programs, and the output is read until it changed. When it did
        not (or there is no such button), ``on_value`` or its zero is written
        into the output itself. Returns the output's value, or ``None`` when
        it is still not as wanted: the PLC program controls it itself.
        """
        value = await self._async_read_output(state_name)
        if output_is_on(value) == on:
            return value
        if press_name is not None:
            LOGGER.debug("EPSNET press requested: %s to switch %s", press_name, state_name)
            await self.client.set_value(self.catalog.entries_by_name[press_name], True)
            deadline = monotonic() + PRESS_CONFIRM_SECONDS
            while monotonic() < deadline:
                await asyncio.sleep(PRESS_POLL_SECONDS)
                value = await self._async_read_output(state_name)
                if output_is_on(value) == on:
                    return value
        off_value: ScalarValue = False if isinstance(on_value, bool) else 0.0
        return await self.async_write_output(state_name, on_value if on else off_value)

    async def async_write_output(self, state_name: str, value: ScalarValue) -> ScalarValue | None:
        """Write a value into an output and read it back a moment later.

        Returns what the output holds then, or ``None`` when it is not what
        was written: the PLC program assigns that output itself.
        """
        LOGGER.debug("EPSNET output write requested: %s value=%r", state_name, value)
        await self.client.set_value(self.catalog.entries_by_name[state_name], value)
        await asyncio.sleep(OUTPUT_CONFIRM_SECONDS)
        now = await self._async_read_output(state_name)
        if isinstance(value, bool):
            stayed = output_is_on(now) == value
        else:
            stayed = abs(float(now) - float(value)) <= 0.5
        return now if stayed else None

    async def async_set_scalar(self, command_name: str, value: float | int | str) -> None:
        """Write a scalar value variable."""
        # Find formula
        formula = MathFormula.NONE
        for entity in self.catalog.entities:
            if entity.set_value_name == command_name:
                formula = entity.transform_write
                break
        
        final_value = self._apply_formula(value, formula)
        LOGGER.debug(
            "EPSNET scalar write requested: %s raw=%r final=%r",
            command_name,
            value,
            final_value,
        )
        await self.client.set_value(self.catalog.entries_by_name[command_name], final_value)
