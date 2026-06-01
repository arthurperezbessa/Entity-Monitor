"""Core monitoring logic for the Entity Monitor integration."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_RESET_DAYS,
    CONF_COALESCE_SECONDS,
    CONF_ENTITIES,
    CONF_EXCLUDED_ENTITIES,
    CONF_INTEGRATIONS,
    CONF_N1_BURST_WINDOW_MINUTES,
    CONF_N3_MINUTES_THRESHOLD,
    CONF_NOTIFY_SERVICE,
    CONF_ONLY_PRIMARY,
    CONF_REPORT_TIME_HOUR,
    CONF_SECONDS_THRESHOLD,
    DEFAULT_AUTO_RESET_DAYS,
    DEFAULT_COALESCE_SECONDS,
    DEFAULT_N1_BURST_WINDOW_MINUTES,
    DEFAULT_N3_MINUTES_THRESHOLD,
    DEFAULT_ONLY_PRIMARY,
    DEFAULT_REPORT_TIME_HOUR,
    DEFAULT_SECONDS_THRESHOLD,
    DOMAIN,
    EVENT_NOTIFICATION,
    EVENT_RECOVERED,
    EVENT_UNAVAILABLE,
    LEVEL_MINUTES,
    LEVEL_SECONDS,
    NOTIFY_N1_1,
    NOTIFY_N1_2,
    NOTIFY_N2,
    NOTIFY_N3_1,
    NOTIFY_N3_2,
    NOTIFY_TEST,
    PRIMARY_DOMAIN_ORDER,
    SCOPE_ENTITY,
    SCOPE_INTEGRATION,
    SIGNAL_UPDATE,
    STATE_ACTIVE_DAY1,
    STATE_QUIET,
    STATE_SILENT,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


def format_duration(seconds: float) -> str:
    """Return a human friendly representation of a duration in seconds."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO timestamp coming back from storage."""
    if not value:
        return None
    return dt_util.parse_datetime(value)


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as an ISO timestamp (or None)."""
    return value.isoformat() if value is not None else None


@dataclass
class EntityStats:
    """Persisted outage statistics for a single entity."""

    outage_count: int = 0
    total_downtime: float = 0.0
    longest_outage: float = 0.0
    last_outage_start: str | None = None
    last_outage_end: str | None = None

    def as_dict(self) -> dict:
        """Serialise for storage."""
        return {
            "outage_count": self.outage_count,
            "total_downtime": self.total_downtime,
            "longest_outage": self.longest_outage,
            "last_outage_start": self.last_outage_start,
            "last_outage_end": self.last_outage_end,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EntityStats":
        """Restore from storage."""
        return cls(
            outage_count=data.get("outage_count", 0),
            total_downtime=data.get("total_downtime", 0.0),
            longest_outage=data.get("longest_outage", 0.0),
            last_outage_start=data.get("last_outage_start"),
            last_outage_end=data.get("last_outage_end"),
        )


@dataclass
class IntegrationStats:
    """Persisted outage statistics aggregated per integration."""

    burst_count: int = 0
    total_downtime: float = 0.0
    last_burst_start: str | None = None
    last_burst_end: str | None = None

    def as_dict(self) -> dict:
        """Serialise for storage."""
        return {
            "burst_count": self.burst_count,
            "total_downtime": self.total_downtime,
            "last_burst_start": self.last_burst_start,
            "last_burst_end": self.last_burst_end,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IntegrationStats":
        """Restore from storage."""
        return cls(
            burst_count=data.get("burst_count", 0),
            total_downtime=data.get("total_downtime", 0.0),
            last_burst_start=data.get("last_burst_start"),
            last_burst_end=data.get("last_burst_end"),
        )


@dataclass
class OngoingOutage:
    """Tracks an outage that is currently in progress."""

    started: datetime
    timers: list[CALLBACK_TYPE] = field(default_factory=list)
    alerts_fired: set[str] = field(default_factory=set)


@dataclass
class IntegrationBurst:
    """A group of entities of one integration that went down together."""

    integration: str
    started: datetime
    entities: list[str] = field(default_factory=list)
    active: set[str] = field(default_factory=set)
    ended: datetime | None = None

    def as_dict(self) -> dict:
        """Serialise for storage (only what's needed for daily reports)."""
        return {
            "integration": self.integration,
            "started": _iso(self.started),
            "entities": list(self.entities),
            "ended": _iso(self.ended),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IntegrationBurst":
        """Restore from storage."""
        started = _parse_dt(data.get("started")) or dt_util.utcnow()
        ended = _parse_dt(data.get("ended"))
        entities = list(data.get("entities", []))
        return cls(
            integration=data.get("integration", ""),
            started=started,
            entities=entities,
            active=set(),  # all entities of a restored burst are considered closed
            ended=ended,
        )


@dataclass
class OutageInterval:
    """An outage segment contributing to the current daily cycle."""

    entity_id: str
    start: datetime
    end: datetime | None = None  # None = still ongoing in this cycle

    def as_dict(self) -> dict:
        """Serialise for storage."""
        return {
            "entity_id": self.entity_id,
            "start": _iso(self.start),
            "end": _iso(self.end),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OutageInterval":
        """Restore from storage."""
        start = _parse_dt(data.get("start")) or dt_util.utcnow()
        return cls(
            entity_id=data.get("entity_id", ""),
            start=start,
            end=_parse_dt(data.get("end")),
        )


@dataclass
class IntegrationCycleState:
    """Per-integration state machine + buffer for the current daily cycle."""

    state: str = STATE_QUIET
    last_drop_at: datetime | None = None
    first_drop_at_in_period: datetime | None = None
    n11_fired: bool = False
    n12_fired: bool = False
    n31_fired: bool = False
    cycle_bursts: list[IntegrationBurst] = field(default_factory=list)
    cycle_intervals: list[OutageInterval] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Serialise for storage."""
        return {
            "state": self.state,
            "last_drop_at": _iso(self.last_drop_at),
            "first_drop_at_in_period": _iso(self.first_drop_at_in_period),
            "n11_fired": self.n11_fired,
            "n12_fired": self.n12_fired,
            "n31_fired": self.n31_fired,
            "cycle_bursts": [b.as_dict() for b in self.cycle_bursts],
            "cycle_intervals": [i.as_dict() for i in self.cycle_intervals],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IntegrationCycleState":
        """Restore from storage."""
        return cls(
            state=data.get("state", STATE_QUIET),
            last_drop_at=_parse_dt(data.get("last_drop_at")),
            first_drop_at_in_period=_parse_dt(
                data.get("first_drop_at_in_period")
            ),
            n11_fired=data.get("n11_fired", False),
            n12_fired=data.get("n12_fired", False),
            n31_fired=data.get("n31_fired", False),
            cycle_bursts=[
                IntegrationBurst.from_dict(b)
                for b in data.get("cycle_bursts", [])
            ],
            cycle_intervals=[
                OutageInterval.from_dict(i)
                for i in data.get("cycle_intervals", [])
            ],
        )


def _union_seconds(
    intervals: list[tuple[datetime, datetime]],
) -> float:
    """Return the total duration covered by a union of (start, end) intervals."""
    if not intervals:
        return 0.0
    sorted_intervals = sorted(intervals, key=lambda i: i[0])
    total = 0.0
    cur_start, cur_end = sorted_intervals[0]
    for start, end in sorted_intervals[1:]:
        if start <= cur_end:
            if end > cur_end:
                cur_end = end
        else:
            total += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = start, end
    total += (cur_end - cur_start).total_seconds()
    return total


class EntityMonitor:
    """Watches a set of entities and records when they become unavailable."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the monitor."""
        self.hass = hass
        self.entry = entry
        self.stats: dict[str, EntityStats] = {}
        self.integration_stats: dict[str, IntegrationStats] = {}
        self._ongoing: dict[str, OngoingOutage] = {}
        self._stored_outage_starts: dict[str, datetime] = {}
        self._integration_state: dict[str, IntegrationCycleState] = {}
        self._unsub_state: CALLBACK_TYPE | None = None
        self._integration_names: dict[str, str] = {}
        self._last_reset_at: datetime | None = None
        self._auto_reset_cancel: CALLBACK_TYPE | None = None
        self._unsub_report_tick: CALLBACK_TYPE | None = None
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )

    # -- Configuration helpers -------------------------------------------------

    @property
    def entities(self) -> list[str]:
        """Return the entity ids the user picked explicitly."""
        return list(self.entry.options.get(CONF_ENTITIES, []))

    @property
    def integrations(self) -> list[str]:
        """Return the integrations whose entities are auto-monitored."""
        return list(self.entry.options.get(CONF_INTEGRATIONS, []))

    @property
    def excluded_entities(self) -> set[str]:
        """Return the entity ids that must be ignored entirely."""
        return set(self.entry.options.get(CONF_EXCLUDED_ENTITIES, []))

    @property
    def only_primary_entity(self) -> bool:
        """Return whether to keep only one entity per device per integration."""
        return bool(
            self.entry.options.get(CONF_ONLY_PRIMARY, DEFAULT_ONLY_PRIMARY)
        )

    @property
    def seconds_threshold(self) -> int:
        """Return the seconds an entity must stay offline to confirm a drop."""
        return int(
            self.entry.options.get(
                CONF_SECONDS_THRESHOLD, DEFAULT_SECONDS_THRESHOLD
            )
        )

    @property
    def coalesce_seconds(self) -> int:
        """Return the window within which simultaneous drops count as one."""
        return int(
            self.entry.options.get(
                CONF_COALESCE_SECONDS, DEFAULT_COALESCE_SECONDS
            )
        )

    @property
    def n1_burst_window_minutes(self) -> int:
        """Return how many minutes the N1.2 escalation window lasts (0 disables)."""
        return int(
            self.entry.options.get(
                CONF_N1_BURST_WINDOW_MINUTES,
                DEFAULT_N1_BURST_WINDOW_MINUTES,
            )
        )

    @property
    def n3_minutes_threshold(self) -> int:
        """Return the minutes used by both N3.1 and N3.2 (0 disables N3)."""
        return int(
            self.entry.options.get(
                CONF_N3_MINUTES_THRESHOLD, DEFAULT_N3_MINUTES_THRESHOLD
            )
        )

    @property
    def report_time_hour(self) -> int:
        """Return the local hour at which daily reports fire (0-23)."""
        value = int(
            self.entry.options.get(
                CONF_REPORT_TIME_HOUR, DEFAULT_REPORT_TIME_HOUR
            )
        )
        return max(0, min(23, value))

    @property
    def notify_service(self) -> str:
        """Return the notify service to call, e.g. ``notify.mobile_app_x``."""
        return str(self.entry.options.get(CONF_NOTIFY_SERVICE, "")).strip()

    @property
    def auto_reset_days(self) -> int:
        """Return after how many days stats and silent state auto-reset."""
        return int(
            self.entry.options.get(
                CONF_AUTO_RESET_DAYS, DEFAULT_AUTO_RESET_DAYS
            )
        )

    @property
    def last_reset_at(self) -> datetime | None:
        """Return when statistics were last cleared (if ever)."""
        return self._last_reset_at

    # -- Public state ----------------------------------------------------------

    @property
    def ongoing_entities(self) -> list[str]:
        """Return entity ids that are currently in an outage."""
        return list(self._ongoing)

    @property
    def total_outages(self) -> int:
        """Return the total number of recorded outage events (bursts)."""
        return sum(s.burst_count for s in self.integration_stats.values())

    @property
    def total_downtime(self) -> float:
        """Return the total recorded integration downtime in seconds."""
        return sum(s.total_downtime for s in self.integration_stats.values())

    # -- Lifecycle -------------------------------------------------------------

    async def async_load(self) -> None:
        """Load persisted statistics from disk."""
        data = await self._store.async_load()
        if not data:
            return
        if "stats" in data:
            self.stats = {
                eid: EntityStats.from_dict(raw)
                for eid, raw in data["stats"].items()
            }
        if "integration_stats" in data:
            self.integration_stats = {
                integration: IntegrationStats.from_dict(raw)
                for integration, raw in data["integration_stats"].items()
            }
        last_reset = data.get("last_reset_at")
        if last_reset:
            parsed = dt_util.parse_datetime(last_reset)
            if parsed is not None:
                self._last_reset_at = parsed
        if "integration_state" in data:
            self._integration_state = {
                integration: IntegrationCycleState.from_dict(raw)
                for integration, raw in data["integration_state"].items()
            }
        if "ongoing_outages" in data:
            for entity_id, raw in data["ongoing_outages"].items():
                started = _parse_dt(raw)
                if started is not None:
                    self._stored_outage_starts[entity_id] = started

    async def async_start(self) -> None:
        """Begin watching the configured entities."""
        entities = self._resolved_entities()
        for eid in entities:
            self.stats.setdefault(eid, EntityStats())

        if entities:
            self._unsub_state = async_track_state_change_event(
                self.hass, entities, self._handle_state_change
            )

        # Restore any outage that was in progress before HA went down so the
        # duration counts from the original drop, not from boot time.
        for eid in entities:
            state = self.hass.states.get(eid)
            if state is None or state.state != STATE_UNAVAILABLE:
                self._stored_outage_starts.pop(eid, None)
                continue
            started = self._stored_outage_starts.pop(eid, None)
            self._start_outage(eid, started_override=started)

        self._stored_outage_starts.clear()
        self._schedule_auto_reset()
        self._schedule_report_tick()
        self._store.async_delay_save(self._data_for_storage, 5)

    def _resolved_entities(self) -> list[str]:
        """Expand integrations + explicit entities into the watch list."""
        explicit = self.entities
        integrations = set(self.integrations)
        excluded = self.excluded_entities

        ids: list[str] = []
        seen: set[str] = set()

        def add(eid: str) -> None:
            if eid in excluded:
                return
            if eid not in seen:
                seen.add(eid)
                ids.append(eid)

        for eid in explicit:
            add(eid)

        if not integrations:
            return ids

        registry = er.async_get(self.hass)
        candidates = [
            entry
            for entry in registry.entities.values()
            if entry.platform in integrations
            and entry.entity_category is None
            and entry.disabled_by is None
            and entry.hidden_by is None
            and entry.entity_id not in excluded
        ]

        if self.only_primary_entity:
            candidates = self._pick_primary_per_device(candidates)

        for entry in candidates:
            add(entry.entity_id)
        return ids

    @staticmethod
    def _pick_primary_per_device(entries: list) -> list:
        """Keep one entity per device, preferring the device's main one."""
        by_device: dict[str, list] = {}
        loose: list = []
        for entry in entries:
            if entry.device_id:
                by_device.setdefault(entry.device_id, []).append(entry)
            else:
                loose.append(entry)

        domain_rank = {d: i for i, d in enumerate(PRIMARY_DOMAIN_ORDER)}
        unknown_domain = len(PRIMARY_DOMAIN_ORDER)

        def rank(entry) -> tuple:
            # Entities whose original_name is None inherit the device's name,
            # which is the convention for the "main" entity of a device.
            named_after_device = 0 if entry.original_name is None else 1
            return (
                named_after_device,
                domain_rank.get(entry.domain, unknown_domain),
                entry.entity_id,
            )

        primaries = []
        for device_entries in by_device.values():
            device_entries.sort(key=rank)
            primaries.append(device_entries[0])
        primaries.extend(loose)
        return primaries

    @callback
    def async_stop(self) -> None:
        """Stop watching entities and cancel pending timers."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_report_tick is not None:
            self._unsub_report_tick()
            self._unsub_report_tick = None
        for outage in self._ongoing.values():
            for cancel in outage.timers:
                cancel()
        self._ongoing.clear()
        if self._auto_reset_cancel is not None:
            self._auto_reset_cancel()
            self._auto_reset_cancel = None

    # -- State change handling -------------------------------------------------

    @callback
    def _handle_state_change(self, event: Event) -> None:
        """React to a monitored entity changing state."""
        entity_id: str = event.data["entity_id"]
        new_state = event.data.get("new_state")
        state = new_state.state if new_state is not None else STATE_UNAVAILABLE

        if state == STATE_UNAVAILABLE:
            if entity_id not in self._ongoing:
                self._start_outage(entity_id)
        elif entity_id in self._ongoing:
            self._end_outage(entity_id)

    @callback
    def _start_outage(
        self, entity_id: str, started_override: datetime | None = None
    ) -> None:
        """Record the start of an outage and schedule the alert timers."""
        now = dt_util.utcnow()
        started = started_override or now
        elapsed = (now - started).total_seconds()

        integration = self._integration_of(entity_id)
        state = self._integration_state.setdefault(
            integration, IntegrationCycleState()
        )

        # Levels already settled before the restart shouldn't fire again.
        pre_fired: set[str] = set()
        if any(entity_id in b.entities for b in state.cycle_bursts):
            pre_fired.add(LEVEL_SECONDS)
        if state.n31_fired:
            pre_fired.add(LEVEL_MINUTES)

        timers: list[CALLBACK_TYPE] = []

        def schedule(level: str, target_seconds: int) -> None:
            if level in pre_fired:
                return
            remaining = max(target_seconds - elapsed, 0)
            timers.append(
                async_call_later(
                    self.hass,
                    remaining,
                    partial(self._fire_alert, entity_id, level),
                )
            )

        schedule(LEVEL_SECONDS, self.seconds_threshold)
        if self.n3_minutes_threshold > 0:
            schedule(LEVEL_MINUTES, self.n3_minutes_threshold * 60)

        self._ongoing[entity_id] = OngoingOutage(
            started=started, timers=timers, alerts_fired=pre_fired
        )

        # Reuse the persisted open interval if it already represents this drop.
        existing_open = any(
            iv.entity_id == entity_id and iv.end is None
            for iv in state.cycle_intervals
        )
        if not existing_open:
            state.cycle_intervals.append(
                OutageInterval(entity_id=entity_id, start=started, end=None)
            )

        _LOGGER.debug(
            "Entity %s became unavailable (started=%s, restored=%s)",
            entity_id,
            started.isoformat(),
            started_override is not None,
        )
        self._store.async_delay_save(self._data_for_storage, 5)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    @callback
    def _fire_alert(self, entity_id: str, level: str, _now: datetime) -> None:
        """Fire an event because an entity stayed unavailable past a threshold."""
        outage = self._ongoing.get(entity_id)
        if outage is None:
            return

        outage.alerts_fired.add(level)
        now = dt_util.utcnow()
        duration = (now - outage.started).total_seconds()
        threshold = self._threshold_seconds(level)
        self.hass.bus.async_fire(
            EVENT_UNAVAILABLE,
            {
                "entity_id": entity_id,
                "friendly_name": self._friendly_name(entity_id),
                "integration": self._integration_of(entity_id),
                "level": level,
                "threshold_seconds": threshold,
                "unavailable_since": outage.started.isoformat(),
                "duration_seconds": round(duration, 1),
            },
        )
        _LOGGER.warning(
            "%s has been unavailable for more than %s (%s alert)",
            entity_id,
            format_duration(threshold),
            level,
        )
        if level == LEVEL_SECONDS:
            self._on_confirmed_drop(entity_id, outage)
        elif level == LEVEL_MINUTES:
            self._evaluate_n3_1(entity_id)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    def _threshold_seconds(self, level: str) -> int:
        """Return the configured threshold for the given alert level."""
        if level == LEVEL_SECONDS:
            return self.seconds_threshold
        if level == LEVEL_MINUTES:
            return self.n3_minutes_threshold * 60
        return 0

    @callback
    def _end_outage(self, entity_id: str) -> None:
        """Record the end of an outage and update the statistics."""
        outage = self._ongoing.pop(entity_id, None)
        if outage is None:
            return

        for cancel in outage.timers:
            cancel()

        now = dt_util.utcnow()
        duration = (now - outage.started).total_seconds()

        stats = self.stats.setdefault(entity_id, EntityStats())
        stats.outage_count += 1
        stats.total_downtime += duration
        stats.longest_outage = max(stats.longest_outage, duration)
        stats.last_outage_start = outage.started.isoformat()
        stats.last_outage_end = now.isoformat()

        integration = self._integration_of(entity_id)
        self._close_cycle_interval(integration, entity_id, now)
        self._close_integration_burst(integration, entity_id, now)

        # Clear the N3.1 latch once every entity of the integration is back so
        # a future outage can fire its own notification.
        if not any(
            self._integration_of(e) == integration for e in self._ongoing
        ):
            state = self._integration_state.get(integration)
            if state is not None:
                state.n31_fired = False

        self.hass.bus.async_fire(
            EVENT_RECOVERED,
            {
                "entity_id": entity_id,
                "friendly_name": self._friendly_name(entity_id),
                "integration": integration,
                "duration_seconds": round(duration, 1),
                "duration": format_duration(duration),
                "outage_count": stats.outage_count,
            },
        )
        _LOGGER.info(
            "%s recovered after %s", entity_id, format_duration(duration)
        )
        self._store.async_delay_save(self._data_for_storage, 5)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # -- Burst tracking --------------------------------------------------------

    @callback
    def _on_confirmed_drop(
        self, entity_id: str, outage: OngoingOutage
    ) -> None:
        """Handle the seconds-threshold being reached for an entity."""
        integration = self._integration_of(entity_id)
        state = self._integration_state.setdefault(
            integration, IntegrationCycleState()
        )

        now = dt_util.utcnow()
        window = self.coalesce_seconds
        bursts = state.cycle_bursts
        current = bursts[-1] if bursts else None

        if (
            current is not None
            and current.ended is None
            and (now - current.started).total_seconds() <= window
        ):
            # Joins the burst that's still inside its coalesce window.
            if entity_id not in current.entities:
                current.entities.append(entity_id)
            current.active.add(entity_id)
            self._update_integration_stats(integration, new_burst=False)
            self._store.async_delay_save(self._data_for_storage, 5)
            async_dispatcher_send(self.hass, SIGNAL_UPDATE)
            return

        burst = IntegrationBurst(
            integration=integration,
            started=now,
            entities=[entity_id],
            active={entity_id},
        )
        bursts.append(burst)
        self._update_integration_stats(integration, new_burst=True)
        self._process_new_burst(integration, burst, state)
        self._store.async_delay_save(self._data_for_storage, 5)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    def _update_integration_stats(
        self, integration: str, *, new_burst: bool
    ) -> None:
        """Bump the cumulative integration statistics."""
        istats = self.integration_stats.setdefault(
            integration, IntegrationStats()
        )
        now = dt_util.utcnow()
        if new_burst:
            istats.burst_count += 1
            istats.last_burst_start = now.isoformat()

    @callback
    def _close_integration_burst(
        self, integration: str, entity_id: str, now: datetime
    ) -> None:
        """Mark an entity as recovered within its burst."""
        state = self._integration_state.get(integration)
        if state is None:
            return
        for burst in reversed(state.cycle_bursts):
            if burst.ended is not None:
                continue
            if entity_id in burst.active:
                burst.active.discard(entity_id)
                if not burst.active:
                    burst.ended = now
                    istats = self.integration_stats.setdefault(
                        integration, IntegrationStats()
                    )
                    istats.total_downtime += (
                        now - burst.started
                    ).total_seconds()
                    istats.last_burst_end = now.isoformat()
                return

    @callback
    def _close_cycle_interval(
        self, integration: str, entity_id: str, now: datetime
    ) -> None:
        """Close the open outage interval associated with an entity."""
        state = self._integration_state.get(integration)
        if state is None:
            return
        for iv in reversed(state.cycle_intervals):
            if iv.entity_id == entity_id and iv.end is None:
                iv.end = now
                return

    # -- N1 state machine ------------------------------------------------------

    @callback
    def _process_new_burst(
        self,
        integration: str,
        burst: IntegrationBurst,
        state: IntegrationCycleState,
    ) -> None:
        """Run the N1 state machine when a new burst has just opened."""
        now = dt_util.utcnow()
        state.last_drop_at = now

        if state.state == STATE_QUIET:
            state.state = STATE_ACTIVE_DAY1
            state.first_drop_at_in_period = now
            state.n11_fired = True
            state.n12_fired = False
            self._dispatch_n1_1(integration, burst)
            return

        if state.state == STATE_ACTIVE_DAY1 and not state.n12_fired:
            if state.first_drop_at_in_period is None:
                state.first_drop_at_in_period = now
            window = self.n1_burst_window_minutes * 60
            elapsed = (
                now - state.first_drop_at_in_period
            ).total_seconds()
            burst_count = len(state.cycle_bursts)
            if (
                window > 0
                and elapsed <= window
                and burst_count >= 2
            ):
                state.n12_fired = True
                self._dispatch_n1_2(integration, state, burst_count)

        # In STATE_SILENT (or after N1.2 fired) drops simply accumulate.

    @callback
    def _dispatch_n1_1(
        self, integration: str, burst: IntegrationBurst
    ) -> None:
        """Fire the N1.1 notification for the first drop of a fresh period."""
        scope, entity_id, entity_name, has_others = self._scope_for_entities(
            burst.entities
        )
        self._dispatch_notification(
            integration=integration,
            kind=NOTIFY_N1_1,
            scope=scope,
            entity_id=entity_id,
            entity_name=entity_name,
            has_others=has_others,
        )

    @callback
    def _dispatch_n1_2(
        self,
        integration: str,
        state: IntegrationCycleState,
        burst_count: int,
    ) -> None:
        """Fire the N1.2 notification for repeated drops."""
        most_eid = self._most_frequent_entity(state.cycle_bursts)
        unique = self._unique_entities(state.cycle_bursts)
        scope = SCOPE_INTEGRATION if len(unique) > 1 else SCOPE_ENTITY
        has_others = len(unique) > 1
        self._dispatch_notification(
            integration=integration,
            kind=NOTIFY_N1_2,
            scope=scope,
            entity_id=most_eid,
            entity_name=self._friendly_name(most_eid) if most_eid else "",
            has_others=has_others,
            outage_count=burst_count,
            window_minutes=self.n1_burst_window_minutes,
        )

    # -- N3.1: sustained outage -----------------------------------------------

    @callback
    def _evaluate_n3_1(self, entity_id: str) -> None:
        """Fire N3.1 when an entity crosses the sustained-outage threshold."""
        threshold_seconds = self.n3_minutes_threshold * 60
        if threshold_seconds <= 0:
            return

        integration = self._integration_of(entity_id)
        state = self._integration_state.setdefault(
            integration, IntegrationCycleState()
        )
        if state.n31_fired:
            return

        now = dt_util.utcnow()
        qualified = [
            e
            for e, outage in self._ongoing.items()
            if self._integration_of(e) == integration
            and (now - outage.started).total_seconds() >= threshold_seconds
        ]
        if not qualified:
            return

        state.n31_fired = True
        most_eid = self._most_long_offline(qualified)
        if not most_eid:
            most_eid = entity_id
        has_others = len(qualified) > 1
        scope = SCOPE_INTEGRATION if has_others else SCOPE_ENTITY
        self._dispatch_notification(
            integration=integration,
            kind=NOTIFY_N3_1,
            scope=scope,
            entity_id=most_eid,
            entity_name=self._friendly_name(most_eid),
            has_others=has_others,
            threshold_seconds=threshold_seconds,
        )

    # -- Daily report tick -----------------------------------------------------

    @callback
    def _schedule_report_tick(self) -> None:
        """Subscribe to the report-time clock event."""
        if self._unsub_report_tick is not None:
            self._unsub_report_tick()
            self._unsub_report_tick = None
        hour = self.report_time_hour
        self._unsub_report_tick = async_track_time_change(
            self.hass,
            self._on_report_tick,
            hour=hour,
            minute=0,
            second=0,
        )

    @callback
    def _on_report_tick(self, _now: datetime) -> None:
        """Fire daily reports and advance the state machine for every integration."""
        now = dt_util.utcnow()
        # Run for every integration that has any state, even if it has no
        # current drops, because we still need to evaluate the quiet reset.
        for integration in list(self._integration_state):
            self._close_cycle(integration, now)
        self._store.async_delay_save(self._data_for_storage, 5)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    @callback
    def _close_cycle(self, integration: str, now: datetime) -> None:
        """Emit the daily reports for an integration and roll the cycle."""
        state = self._integration_state.get(integration)
        if state is None:
            return

        # Materialise the just-ended cycle's data, treating still-open outages
        # as closing right at this tick so their cycle contribution counts.
        closed_intervals = [
            (iv.entity_id, iv.start, iv.end if iv.end is not None else now)
            for iv in state.cycle_intervals
        ]
        bursts = list(state.cycle_bursts)

        self._maybe_fire_n2(integration, bursts)
        self._maybe_fire_n3_2(integration, closed_intervals)

        # Reset the cycle data, keeping any still-ongoing outages alive in a
        # fresh interval that starts at the tick.
        state.cycle_bursts = []
        state.cycle_intervals = [
            OutageInterval(entity_id=iv.entity_id, start=now, end=None)
            for iv in state.cycle_intervals
            if iv.end is None
        ]

        # State transitions
        if state.state == STATE_ACTIVE_DAY1:
            state.state = STATE_SILENT
        elif state.state == STATE_SILENT:
            reset_days = self.auto_reset_days
            if (
                reset_days > 0
                and state.last_drop_at is not None
                and (now - state.last_drop_at).days >= reset_days
            ):
                state.state = STATE_QUIET
                state.first_drop_at_in_period = None
                state.n11_fired = False
                state.n12_fired = False

    @callback
    def _maybe_fire_n2(
        self, integration: str, bursts: list[IntegrationBurst]
    ) -> None:
        """Fire the N2 daily report when the cycle saw at least one drop."""
        if not bursts:
            return
        most_eid = self._most_frequent_entity(bursts)
        unique = self._unique_entities(bursts)
        scope = SCOPE_INTEGRATION if len(unique) > 1 else SCOPE_ENTITY
        has_others = len(unique) > 1
        self._dispatch_notification(
            integration=integration,
            kind=NOTIFY_N2,
            scope=scope,
            entity_id=most_eid,
            entity_name=self._friendly_name(most_eid) if most_eid else "",
            has_others=has_others,
            outage_count=len(bursts),
        )

    @callback
    def _maybe_fire_n3_2(
        self,
        integration: str,
        intervals: list[tuple[str, datetime, datetime]],
    ) -> None:
        """Fire the N3.2 daily offline report when applicable."""
        threshold_seconds = self.n3_minutes_threshold * 60
        if threshold_seconds <= 0:
            return

        # Per-entity longest single outage and per-entity total downtime.
        per_entity_longest: dict[str, float] = {}
        per_entity_total: dict[str, float] = {}
        for entity_id, start, end in intervals:
            duration = (end - start).total_seconds()
            if duration <= 0:
                continue
            if duration > per_entity_longest.get(entity_id, 0):
                per_entity_longest[entity_id] = duration
            per_entity_total[entity_id] = (
                per_entity_total.get(entity_id, 0) + duration
            )

        # Inclusion rule: at least one entity must have had a single outage
        # that lasted >= threshold during the cycle.
        qualifying_entities = [
            eid
            for eid, longest in per_entity_longest.items()
            if longest >= threshold_seconds
        ]
        if not qualifying_entities:
            return

        # Title/body duration is the union of the integration's offline time.
        union_intervals = [(start, end) for _, start, end in intervals]
        union_seconds = _union_seconds(union_intervals)

        # Pick the entity that was offline the longest (cumulative) for the
        # body. Ties resolved by entity_id for determinism.
        most_eid = max(
            per_entity_total.items(), key=lambda kv: (kv[1], kv[0])
        )[0]
        has_others = len(qualifying_entities) > 1
        scope = SCOPE_INTEGRATION if has_others else SCOPE_ENTITY
        self._dispatch_notification(
            integration=integration,
            kind=NOTIFY_N3_2,
            scope=scope,
            entity_id=most_eid,
            entity_name=self._friendly_name(most_eid),
            has_others=has_others,
            duration_seconds=union_seconds,
        )

    # -- Notification dispatch -------------------------------------------------

    @callback
    def _dispatch_notification(
        self,
        *,
        integration: str,
        kind: str,
        scope: str,
        entity_id: str | None = None,
        entity_name: str = "",
        has_others: bool = False,
        outage_count: int = 0,
        window_minutes: int = 0,
        threshold_seconds: int = 0,
        duration_seconds: float = 0.0,
    ) -> None:
        """Build, fire the event for, and deliver a notification."""
        integration_name = self._integration_name(integration)
        title, message = self._build_message(
            kind=kind,
            scope=scope,
            integration_name=integration_name,
            entity_name=entity_name,
            has_others=has_others,
            outage_count=outage_count,
            window_minutes=window_minutes,
            threshold_seconds=threshold_seconds,
            duration_seconds=duration_seconds,
        )
        self.hass.bus.async_fire(
            EVENT_NOTIFICATION,
            {
                "integration": integration,
                "integration_name": integration_name,
                "kind": kind,
                "scope": scope,
                "entity_id": entity_id,
                "entity_name": entity_name,
                "has_others": has_others,
                "outage_count": outage_count,
                "window_minutes": window_minutes,
                "threshold_seconds": threshold_seconds,
                "duration_seconds": round(duration_seconds, 1),
                "title": title,
                "message": message,
            },
        )
        self._send_notification(title, message)
        _LOGGER.info(
            "Entity Monitor notification (%s/%s): %s", kind, scope, message
        )

    def _build_message(
        self,
        *,
        kind: str,
        scope: str,
        integration_name: str,
        entity_name: str,
        has_others: bool,
        outage_count: int,
        window_minutes: int,
        threshold_seconds: int,
        duration_seconds: float,
    ) -> tuple[str, str]:
        """Return the (title, body) pair for a notification."""
        subject = (
            f"{entity_name} e outras" if has_others else entity_name
        )
        verb_single = "caiu" if not has_others else "caíram"

        if kind == NOTIFY_N1_1:
            return (
                f"{integration_name} instável",
                f"{subject} {verb_single}.",
            )
        if kind == NOTIFY_N1_2:
            return (
                f"{integration_name} instável",
                f"{subject} {verb_single} {outage_count} vezes nos últimos "
                f"{window_minutes} minutos.",
            )
        if kind == NOTIFY_N2:
            return (
                f"Relatório {integration_name}",
                f"{subject} {verb_single} {outage_count} vezes ontem.",
            )
        if kind == NOTIFY_N3_1:
            minutes = threshold_seconds // 60
            offline_word = "offline"
            return (
                f"{integration_name} offline por mais de {minutes} minutos",
                f"{subject} {offline_word} por {minutes} minutos.",
            )
        if kind == NOTIFY_N3_2:
            duration = format_duration(duration_seconds)
            verb_n32 = "ficaram" if has_others else "ficou"
            return (
                f"{integration_name} offline por {duration}",
                f"{subject} {verb_n32} offline por {duration} ontem.",
            )
        return ("", "")

    @callback
    def async_send_test_notification(self) -> bool:
        """Fire a sample notification so the user can validate the setup."""
        title = "Entity Monitor — teste"
        message = (
            "Notificação de teste do Entity Monitor. Se você está vendo "
            "isto, o serviço de notificação está configurado corretamente."
        )
        self.hass.bus.async_fire(
            EVENT_NOTIFICATION,
            {
                "integration": "_test_",
                "integration_name": "Entity Monitor",
                "kind": NOTIFY_TEST,
                "scope": SCOPE_INTEGRATION,
                "entity_id": None,
                "entity_name": "",
                "has_others": False,
                "outage_count": 0,
                "window_minutes": 0,
                "threshold_seconds": 0,
                "duration_seconds": 0,
                "title": title,
                "message": message,
                "test": True,
            },
        )
        if not self.notify_service:
            _LOGGER.warning(
                "Test notification requested but no notify_service is "
                "configured — only the entity_monitor_notification event "
                "was fired."
            )
            return False
        self._send_notification(title, message)
        return True

    @callback
    def _send_notification(self, title: str, message: str) -> None:
        """Call the configured notify service, if any."""
        service = self.notify_service
        if not service:
            return
        if "." in service:
            domain, name = service.split(".", 1)
        else:
            domain, name = "notify", service
        self.hass.async_create_task(
            self.hass.services.async_call(
                domain,
                name,
                {"title": title, "message": message},
                blocking=False,
            )
        )

    # -- Statistics / reporting ------------------------------------------------

    @callback
    def async_reset_statistics(self) -> None:
        """Clear cumulative statistics (kept buckets, restart counters)."""
        for eid in list(self.stats):
            self.stats[eid] = EntityStats()
        self.integration_stats.clear()
        self._last_reset_at = dt_util.utcnow()
        self._store.async_delay_save(self._data_for_storage, 1)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        _LOGGER.info("Entity Monitor statistics reset")
        self._schedule_auto_reset()

    @callback
    def async_reset_all(self) -> None:
        """Zero everything: stats, integration state, ongoing outages."""
        self.stats.clear()
        self.integration_stats.clear()
        self._integration_state.clear()
        for outage in self._ongoing.values():
            for cancel in outage.timers:
                cancel()
        self._ongoing.clear()
        self._stored_outage_starts.clear()
        self._last_reset_at = dt_util.utcnow()
        self._store.async_delay_save(self._data_for_storage, 1)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        _LOGGER.info("Entity Monitor full reset performed")
        # Re-seed stats for the currently watched entities.
        for eid in self._resolved_entities():
            self.stats.setdefault(eid, EntityStats())
        self._schedule_auto_reset()

    @callback
    def _schedule_auto_reset(self) -> None:
        """(Re)schedule the next periodic reset, if enabled."""
        if self._auto_reset_cancel is not None:
            self._auto_reset_cancel()
            self._auto_reset_cancel = None

        days = self.auto_reset_days
        if days <= 0:
            return

        period = timedelta(days=days)
        now = dt_util.utcnow()
        if self._last_reset_at is None:
            self._last_reset_at = now
            self._store.async_delay_save(self._data_for_storage, 1)

        next_at = self._last_reset_at + period
        if next_at <= now:
            self.async_reset_statistics()
            return

        delay = (next_at - now).total_seconds()
        self._auto_reset_cancel = async_call_later(
            self.hass, delay, self._auto_reset_fire
        )

    @callback
    def _auto_reset_fire(self, _now: datetime) -> None:
        """Callback fired by async_call_later when the period elapses."""
        self._auto_reset_cancel = None
        _LOGGER.info(
            "Entity Monitor auto-reset after %s days", self.auto_reset_days
        )
        self.async_reset_statistics()

    def build_report(self) -> dict:
        """Build a report ranking entities and integrations by downtime."""
        now = dt_util.utcnow()
        by_entity: list[dict] = []

        for eid, stats in self.stats.items():
            ongoing = self._ongoing.get(eid)
            current = (
                (now - ongoing.started).total_seconds() if ongoing else 0.0
            )
            by_entity.append(
                {
                    "entity_id": eid,
                    "friendly_name": self._friendly_name(eid),
                    "integration": self._integration_of(eid),
                    "outage_count": stats.outage_count,
                    "total_downtime_seconds": round(stats.total_downtime, 1),
                    "total_downtime": format_duration(stats.total_downtime),
                    "longest_outage_seconds": round(stats.longest_outage, 1),
                    "longest_outage": format_duration(stats.longest_outage),
                    "currently_unavailable": ongoing is not None,
                    "current_outage": (
                        format_duration(current) if ongoing else None
                    ),
                    "last_outage_end": stats.last_outage_end,
                }
            )

        by_entity.sort(
            key=lambda e: (e["outage_count"], e["total_downtime_seconds"]),
            reverse=True,
        )
        by_integration = self._build_integration_report()

        return {
            "generated_at": dt_util.now().isoformat(),
            "monitored_entities": len(self.stats),
            "total_outages": self.total_outages,
            "total_downtime_seconds": round(self.total_downtime, 1),
            "total_downtime": format_duration(self.total_downtime),
            "currently_unavailable": [
                e["entity_id"] for e in by_entity if e["currently_unavailable"]
            ],
            "seconds_threshold": self.seconds_threshold,
            "n1_burst_window_minutes": self.n1_burst_window_minutes,
            "n3_minutes_threshold": self.n3_minutes_threshold,
            "report_time_hour": self.report_time_hour,
            "coalesce_seconds": self.coalesce_seconds,
            "auto_reset_days": self.auto_reset_days,
            "last_reset_at": (
                self._last_reset_at.isoformat()
                if self._last_reset_at is not None
                else None
            ),
            "by_entity": by_entity,
            "by_integration": by_integration,
        }

    def _build_integration_report(self) -> list[dict]:
        """Rank integrations by coalesced outage events and downtime."""
        integrations: dict[str, dict] = {}

        def agg_for(integration: str) -> dict:
            return integrations.setdefault(
                integration,
                {
                    "integration": integration,
                    "integration_name": self._integration_name(integration),
                    "monitored_entities": 0,
                    "outage_count": 0,
                    "total_downtime_seconds": 0.0,
                    "currently_unavailable": 0,
                },
            )

        for eid in self.stats:
            agg_for(self._integration_of(eid))["monitored_entities"] += 1

        for integration, istats in self.integration_stats.items():
            agg = agg_for(integration)
            agg["outage_count"] = istats.burst_count
            agg["total_downtime_seconds"] = round(istats.total_downtime, 1)

        for eid in self._ongoing:
            agg_for(self._integration_of(eid))["currently_unavailable"] += 1

        ranked = sorted(
            integrations.values(),
            key=lambda i: (i["outage_count"], i["total_downtime_seconds"]),
            reverse=True,
        )
        for agg in ranked:
            agg["total_downtime"] = format_duration(
                agg["total_downtime_seconds"]
            )
        return ranked

    # -- Internal helpers ------------------------------------------------------

    @staticmethod
    def _most_frequent_entity(bursts: list[IntegrationBurst]) -> str:
        """Return the entity that appeared in the most bursts."""
        counter: Counter[str] = Counter()
        for burst in bursts:
            for entity_id in burst.entities:
                counter[entity_id] += 1
        if not counter:
            return ""
        most_common = counter.most_common()
        # Resolve ties by entity_id for determinism.
        top_count = most_common[0][1]
        ties = sorted([eid for eid, c in most_common if c == top_count])
        return ties[0]

    @staticmethod
    def _unique_entities(bursts: list[IntegrationBurst]) -> set[str]:
        """Return the set of unique entity ids across bursts."""
        result: set[str] = set()
        for burst in bursts:
            result.update(burst.entities)
        return result

    def _most_long_offline(self, entity_ids: list[str]) -> str:
        """Return the entity from the list that's been offline the longest."""
        now = dt_util.utcnow()
        best_eid = ""
        best_seconds = -1.0
        for eid in sorted(entity_ids):
            outage = self._ongoing.get(eid)
            if outage is None:
                continue
            elapsed = (now - outage.started).total_seconds()
            if elapsed > best_seconds:
                best_seconds = elapsed
                best_eid = eid
        return best_eid

    def _scope_for_entities(
        self, entity_ids: list[str]
    ) -> tuple[str, str | None, str, bool]:
        """Pick the citation entity and decide the notification scope."""
        if not entity_ids:
            return (SCOPE_INTEGRATION, None, "", False)
        primary = entity_ids[0]
        has_others = len(entity_ids) > 1
        scope = SCOPE_INTEGRATION if has_others else SCOPE_ENTITY
        return (scope, primary, self._friendly_name(primary), has_others)

    @callback
    def _data_for_storage(self) -> dict:
        """Return the data structure persisted to disk."""
        return {
            "stats": {eid: s.as_dict() for eid, s in self.stats.items()},
            "integration_stats": {
                integration: s.as_dict()
                for integration, s in self.integration_stats.items()
            },
            "last_reset_at": (
                self._last_reset_at.isoformat()
                if self._last_reset_at is not None
                else None
            ),
            "integration_state": {
                integration: state.as_dict()
                for integration, state in self._integration_state.items()
            },
            "ongoing_outages": {
                eid: outage.started.isoformat()
                for eid, outage in self._ongoing.items()
            },
        }

    def _friendly_name(self, entity_id: str) -> str:
        """Return the friendly name of an entity, falling back to its id."""
        if not entity_id:
            return ""
        state = self.hass.states.get(entity_id)
        if state is not None:
            return state.attributes.get("friendly_name", entity_id)
        return entity_id

    def _integration_of(self, entity_id: str) -> str:
        """Return the integration (platform) that provides an entity."""
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if entry is not None and entry.platform:
            return entry.platform
        return entity_id.split(".", 1)[0]

    def _integration_name(self, integration: str) -> str:
        """Return the friendly integration name (e.g. ``Local Tuya``)."""
        cached = self._integration_names.get(integration)
        if cached is not None:
            return cached
        try:
            from homeassistant.loader import async_get_loaded_integration

            loaded = async_get_loaded_integration(self.hass, integration)
            name = loaded.name if loaded is not None else integration
        except Exception:  # pylint: disable=broad-except
            name = integration
        self._integration_names[integration] = name
        return name
