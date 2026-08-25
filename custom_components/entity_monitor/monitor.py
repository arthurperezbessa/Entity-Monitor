"""Core monitoring logic for the Entity Monitor integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CENTRAL_SEND_TIMEOUT,
    CENTRAL_TOKEN_HEADER,
    CONF_AUTO_RESET_DAYS,
    CONF_CENTRAL_CLIENT_ID,
    CONF_CENTRAL_TOKEN,
    CONF_CENTRAL_URL,
    CONF_COALESCE_SECONDS,
    CONF_ENTITIES,
    CONF_EXCLUDED_ENTITIES,
    CONF_INTEGRATIONS,
    CONF_N3_MINUTES_THRESHOLD,
    CONF_NOTIFY_SERVICE,
    CONF_ONLY_PRIMARY,
    CONF_REPORT_TIME_HOUR,
    CONF_SECONDS_THRESHOLD,
    DEFAULT_AUTO_RESET_DAYS,
    DEFAULT_COALESCE_SECONDS,
    DEFAULT_N3_MINUTES_THRESHOLD,
    DEFAULT_ONLY_PRIMARY,
    DEFAULT_REPORT_TIME_HOUR,
    DEFAULT_SECONDS_THRESHOLD,
    DOMAIN,
    EVENT_NOTIFICATION,
    EVENT_RECOVERED,
    EVENT_UNAVAILABLE,
    LEVEL_SECONDS,
    NOTIFY_N1,
    NOTIFY_N2,
    NOTIFY_N3,
    NOTIFY_SNAPSHOT,
    NOTIFY_TEST,
    PRIMARY_DOMAIN_ORDER,
    SCOPE_ENTITY,
    SCOPE_INTEGRATION,
    SIGNAL_UPDATE,
    SNAPSHOT_DELAY_SECONDS,
    STATE_ACTIVE_TODAY,
    STATE_QUIET,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


def format_duration(seconds: float) -> str:
    """Return a short technical duration used in report attributes."""
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


def format_duration_pt(seconds: float) -> str:
    """Return a compact duration for notification bodies.

    Format:
    - `< 60s`  →  `Xs`
    - `< 3600s` →  `Xm`  (minutes truncated, seconds dropped)
    - `>= 3600s` →  `XhYm` (or just `Xh` when minutes = 0)
    """
    total = int(seconds)
    if total <= 0:
        return "0s"
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if remaining_minutes:
        return f"{hours}h{remaining_minutes}m"
    return f"{hours}h"


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
        return {
            "outage_count": self.outage_count,
            "total_downtime": self.total_downtime,
            "longest_outage": self.longest_outage,
            "last_outage_start": self.last_outage_start,
            "last_outage_end": self.last_outage_end,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EntityStats":
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
        return {
            "burst_count": self.burst_count,
            "total_downtime": self.total_downtime,
            "last_burst_start": self.last_burst_start,
            "last_burst_end": self.last_burst_end,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IntegrationStats":
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
        return {
            "integration": self.integration,
            "started": _iso(self.started),
            "entities": list(self.entities),
            "ended": _iso(self.ended),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IntegrationBurst":
        started = _parse_dt(data.get("started")) or dt_util.utcnow()
        ended = _parse_dt(data.get("ended"))
        entities = list(data.get("entities", []))
        return cls(
            integration=data.get("integration", ""),
            started=started,
            entities=entities,
            active=set(),
            ended=ended,
        )


@dataclass
class OutageInterval:
    """An outage segment contributing to the current daily cycle."""

    entity_id: str
    start: datetime
    end: datetime | None = None  # None = still ongoing in this cycle

    def as_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "start": _iso(self.start),
            "end": _iso(self.end),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OutageInterval":
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
    n1_fired: bool = False
    n2_fired: bool = False
    cycle_bursts: list[IntegrationBurst] = field(default_factory=list)
    cycle_intervals: list[OutageInterval] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "last_drop_at": _iso(self.last_drop_at),
            "n1_fired": self.n1_fired,
            "n2_fired": self.n2_fired,
            "cycle_bursts": [b.as_dict() for b in self.cycle_bursts],
            "cycle_intervals": [i.as_dict() for i in self.cycle_intervals],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IntegrationCycleState":
        # Old v0.6.x layouts persisted the previous state machine; map them
        # so upgrading users keep their in-flight cycle data.
        legacy_state = data.get("state", STATE_QUIET)
        if legacy_state in ("active_day1", "silent"):
            new_state = STATE_ACTIVE_TODAY
        elif legacy_state == STATE_ACTIVE_TODAY:
            new_state = STATE_ACTIVE_TODAY
        else:
            new_state = STATE_QUIET
        n1_fired = data.get("n1_fired", data.get("n11_fired", False))
        n2_fired = data.get("n2_fired", data.get("n31_fired", False))
        return cls(
            state=new_state,
            last_drop_at=_parse_dt(data.get("last_drop_at")),
            n1_fired=n1_fired,
            n2_fired=n2_fired,
            cycle_bursts=[
                IntegrationBurst.from_dict(b)
                for b in data.get("cycle_bursts", [])
            ],
            cycle_intervals=[
                OutageInterval.from_dict(i)
                for i in data.get("cycle_intervals", [])
            ],
        )


class EntityMonitor:
    """Watches a set of entities and records when they become unavailable."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.stats: dict[str, EntityStats] = {}
        self.integration_stats: dict[str, IntegrationStats] = {}
        self._ongoing: dict[str, OngoingOutage] = {}
        self._stored_outage_starts: dict[str, datetime] = {}
        self._integration_state: dict[str, IntegrationCycleState] = {}
        self._n2_timers: dict[str, CALLBACK_TYPE] = {}
        self._unsub_state: CALLBACK_TYPE | None = None
        self._integration_names: dict[str, str] = {}
        self._last_reset_at: datetime | None = None
        self._auto_reset_cancel: CALLBACK_TYPE | None = None
        self._snapshot_cancel: CALLBACK_TYPE | None = None
        self._unsub_report_tick: CALLBACK_TYPE | None = None
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )

    # -- Configuration helpers -------------------------------------------------

    @property
    def entities(self) -> list[str]:
        return list(self.entry.options.get(CONF_ENTITIES, []))

    @property
    def integrations(self) -> list[str]:
        return list(self.entry.options.get(CONF_INTEGRATIONS, []))

    @property
    def excluded_entities(self) -> set[str]:
        return set(self.entry.options.get(CONF_EXCLUDED_ENTITIES, []))

    @property
    def only_primary_entity(self) -> bool:
        return bool(
            self.entry.options.get(CONF_ONLY_PRIMARY, DEFAULT_ONLY_PRIMARY)
        )

    @property
    def seconds_threshold(self) -> int:
        return int(
            self.entry.options.get(
                CONF_SECONDS_THRESHOLD, DEFAULT_SECONDS_THRESHOLD
            )
        )

    @property
    def coalesce_seconds(self) -> int:
        return int(
            self.entry.options.get(
                CONF_COALESCE_SECONDS, DEFAULT_COALESCE_SECONDS
            )
        )

    @property
    def n3_minutes_threshold(self) -> int:
        """Minutes of cumulative offline that trigger N2 (0 disables N2)."""
        return int(
            self.entry.options.get(
                CONF_N3_MINUTES_THRESHOLD, DEFAULT_N3_MINUTES_THRESHOLD
            )
        )

    @property
    def report_time_hour(self) -> int:
        value = int(
            self.entry.options.get(
                CONF_REPORT_TIME_HOUR, DEFAULT_REPORT_TIME_HOUR
            )
        )
        return max(0, min(23, value))

    @property
    def notify_service(self) -> str:
        return str(self.entry.options.get(CONF_NOTIFY_SERVICE, "")).strip()

    @property
    def central_url(self) -> str:
        return str(self.entry.options.get(CONF_CENTRAL_URL, "")).strip()

    @property
    def central_client_id(self) -> str:
        return str(self.entry.options.get(CONF_CENTRAL_CLIENT_ID, "")).strip()

    @property
    def central_token(self) -> str:
        return str(self.entry.options.get(CONF_CENTRAL_TOKEN, "")).strip()

    @property
    def central_enabled(self) -> bool:
        """True quando URL, client_id e token do central estão preenchidos."""
        return bool(
            self.central_url and self.central_client_id and self.central_token
        )

    @property
    def auto_reset_days(self) -> int:
        return int(
            self.entry.options.get(
                CONF_AUTO_RESET_DAYS, DEFAULT_AUTO_RESET_DAYS
            )
        )

    @property
    def last_reset_at(self) -> datetime | None:
        return self._last_reset_at

    # -- Public state ----------------------------------------------------------

    @property
    def ongoing_entities(self) -> list[str]:
        return list(self._ongoing)

    @property
    def total_outages(self) -> int:
        return sum(s.burst_count for s in self.integration_stats.values())

    @property
    def total_downtime(self) -> float:
        return sum(s.total_downtime for s in self.integration_stats.values())

    # -- Lifecycle -------------------------------------------------------------

    async def async_load(self) -> None:
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
        entities = self._resolved_entities()
        for eid in entities:
            self.stats.setdefault(eid, EntityStats())

        if entities:
            self._unsub_state = async_track_state_change_event(
                self.hass, entities, self._handle_state_change
            )

        # Restore any outage that was in progress before HA went down so its
        # duration counts from the original drop, not from boot time.
        for eid in entities:
            state = self.hass.states.get(eid)
            if state is None or state.state != STATE_UNAVAILABLE:
                self._stored_outage_starts.pop(eid, None)
                continue
            started = self._stored_outage_starts.pop(eid, None)
            self._start_outage(eid, started_override=started)

        self._stored_outage_starts.clear()

        # After outages are restored, arm the N2 timer for any integration
        # that has an open interval so a still-ongoing outage keeps growing
        # its union toward the threshold.
        for integration in list(self._integration_state):
            self._reevaluate_n2_timer(integration)

        self._schedule_auto_reset()
        self._schedule_report_tick()
        self._store.async_delay_save(self._data_for_storage, 5)

        # Snapshot do estado atual para o central (após um atraso, para não
        # reportar entidades que ainda estão carregando no boot).
        if self.central_enabled:
            self._snapshot_cancel = async_call_later(
                self.hass, SNAPSHOT_DELAY_SECONDS, self._send_snapshot_to_central
            )

    def _resolved_entities(self) -> list[str]:
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
        for cancel in self._n2_timers.values():
            cancel()
        self._n2_timers.clear()
        if self._auto_reset_cancel is not None:
            self._auto_reset_cancel()
            self._auto_reset_cancel = None
        if self._snapshot_cancel is not None:
            self._snapshot_cancel()
            self._snapshot_cancel = None

    # -- State change handling -------------------------------------------------

    @callback
    def _handle_state_change(self, event: Event) -> None:
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
        now = dt_util.utcnow()
        started = started_override or now
        elapsed = (now - started).total_seconds()

        integration = self._integration_of(entity_id)
        state = self._integration_state.setdefault(
            integration, IntegrationCycleState()
        )

        # The seconds-threshold event shouldn't re-fire if a persisted burst
        # already contains this entity (survives HA restart).
        pre_fired: set[str] = set()
        if any(entity_id in b.entities for b in state.cycle_bursts):
            pre_fired.add(LEVEL_SECONDS)

        timers: list[CALLBACK_TYPE] = []
        if LEVEL_SECONDS not in pre_fired:
            remaining = max(self.seconds_threshold - elapsed, 0)
            timers.append(
                async_call_later(
                    self.hass,
                    remaining,
                    partial(self._fire_alert, entity_id, LEVEL_SECONDS),
                )
            )

        self._ongoing[entity_id] = OngoingOutage(
            started=started, timers=timers, alerts_fired=pre_fired
        )

        # Reuse a persisted open interval if it already tracks this drop.
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
        self._reevaluate_n2_timer(integration)
        self._store.async_delay_save(self._data_for_storage, 5)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    @callback
    def _fire_alert(self, entity_id: str, level: str, _now: datetime) -> None:
        outage = self._ongoing.get(entity_id)
        if outage is None:
            return

        outage.alerts_fired.add(level)
        now = dt_util.utcnow()
        duration = (now - outage.started).total_seconds()
        self.hass.bus.async_fire(
            EVENT_UNAVAILABLE,
            {
                "entity_id": entity_id,
                "friendly_name": self._friendly_name(entity_id),
                "integration": self._integration_of(entity_id),
                "level": level,
                "threshold_seconds": self.seconds_threshold,
                "unavailable_since": outage.started.isoformat(),
                "duration_seconds": round(duration, 1),
            },
        )
        _LOGGER.warning(
            "%s has been unavailable for more than %s (%s alert)",
            entity_id,
            format_duration(self.seconds_threshold),
            level,
        )
        if level == LEVEL_SECONDS:
            self._on_confirmed_drop(entity_id, outage)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    @callback
    def _end_outage(self, entity_id: str) -> None:
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
        # The union stopped growing — usually just cancels the pending N2
        # timer if this was the last open interval.
        self._reevaluate_n2_timer(integration)
        self._store.async_delay_save(self._data_for_storage, 5)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # -- Burst tracking --------------------------------------------------------

    @callback
    def _on_confirmed_drop(
        self, entity_id: str, outage: OngoingOutage
    ) -> None:
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
            if entity_id not in current.entities:
                current.entities.append(entity_id)
            current.active.add(entity_id)
            self._update_integration_stats(integration, new_burst=False)
        else:
            burst = IntegrationBurst(
                integration=integration,
                started=now,
                entities=[entity_id],
                active={entity_id},
            )
            bursts.append(burst)
            self._update_integration_stats(integration, new_burst=True)
            self._maybe_fire_n1(integration, burst, state)

        # A new confirmed drop might already cross the N2 union threshold
        # even if the outage that started it was the very last to join.
        self._reevaluate_n2_timer(integration)
        self._store.async_delay_save(self._data_for_storage, 5)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    def _update_integration_stats(
        self, integration: str, *, new_burst: bool
    ) -> None:
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
        state = self._integration_state.get(integration)
        if state is None:
            return
        for iv in reversed(state.cycle_intervals):
            if iv.entity_id == entity_id and iv.end is None:
                iv.end = now
                return

    # -- N1: first drop of the day --------------------------------------------

    @callback
    def _maybe_fire_n1(
        self,
        integration: str,
        burst: IntegrationBurst,
        state: IntegrationCycleState,
    ) -> None:
        now = dt_util.utcnow()
        state.last_drop_at = now

        if state.state != STATE_QUIET or state.n1_fired:
            return

        state.state = STATE_ACTIVE_TODAY
        state.n1_fired = True

        # All entities in the first burst fell once — sort by entity_id so
        # the citation stays deterministic across restarts.
        ranked = sorted(set(burst.entities))
        self._dispatch_notification(
            integration=integration,
            kind=NOTIFY_N1,
            ranked_entity_ids=ranked,
        )

    # -- N2: accumulated offline crosses threshold ----------------------------

    def _current_per_entity_totals(
        self, state: IntegrationCycleState, now: datetime
    ) -> dict[str, float]:
        """Return per-entity cumulative offline seconds in the cycle so far."""
        per_entity_total: dict[str, float] = {}
        for iv in state.cycle_intervals:
            end = iv.end if iv.end is not None else now
            if end <= iv.start:
                continue
            per_entity_total[iv.entity_id] = per_entity_total.get(
                iv.entity_id, 0.0
            ) + (end - iv.start).total_seconds()
        return per_entity_total

    @callback
    def _reevaluate_n2_timer(self, integration: str) -> None:
        """(Re)schedule the N2 timer for an integration.

        N2 fires when the *single entity* with the most cumulative offline
        time in the cycle crosses the threshold. Timer picks the shortest
        remaining time across all currently-offline entities (whichever
        will hit the threshold first).
        """
        cancel = self._n2_timers.pop(integration, None)
        if cancel is not None:
            cancel()

        threshold_seconds = self.n3_minutes_threshold * 60
        if threshold_seconds <= 0:
            return

        state = self._integration_state.get(integration)
        if state is None or state.n2_fired:
            return

        now = dt_util.utcnow()
        per_entity = self._current_per_entity_totals(state, now)
        if per_entity and max(per_entity.values()) >= threshold_seconds:
            self._fire_n2(integration)
            return

        currently_offline = {
            iv.entity_id for iv in state.cycle_intervals if iv.end is None
        }
        if not currently_offline:
            return

        remaining_per_entity = [
            threshold_seconds - per_entity.get(eid, 0.0)
            for eid in currently_offline
        ]
        min_remaining = min(remaining_per_entity)
        if min_remaining <= 0:
            self._fire_n2(integration)
            return

        self._n2_timers[integration] = async_call_later(
            self.hass,
            max(min_remaining, 1.0),
            partial(self._fire_n2_if_qualified, integration),
        )

    @callback
    def _fire_n2_if_qualified(
        self, integration: str, _now: datetime
    ) -> None:
        self._n2_timers.pop(integration, None)
        state = self._integration_state.get(integration)
        if state is None or state.n2_fired:
            return
        threshold_seconds = self.n3_minutes_threshold * 60
        if threshold_seconds <= 0:
            return
        now = dt_util.utcnow()
        per_entity = self._current_per_entity_totals(state, now)
        if per_entity and max(per_entity.values()) >= threshold_seconds:
            self._fire_n2(integration)
        else:
            # The leading entity recovered a hair before the timer, so its
            # total stopped growing. Re-arm from what's still open.
            self._reevaluate_n2_timer(integration)

    @callback
    def _fire_n2(self, integration: str) -> None:
        state = self._integration_state.get(integration)
        if state is None or state.n2_fired:
            return

        now = dt_util.utcnow()
        per_entity = self._current_per_entity_totals(state, now)
        if not per_entity:
            return

        state.n2_fired = True
        # Rank by cumulative offline (desc), tie-break by entity_id (asc).
        ranked = [
            eid
            for eid, _ in sorted(
                per_entity.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ]
        self._dispatch_notification(
            integration=integration,
            kind=NOTIFY_N2,
            ranked_entity_ids=ranked,
            per_entity_seconds=per_entity,
            threshold_seconds=self.n3_minutes_threshold * 60,
        )
        self._store.async_delay_save(self._data_for_storage, 5)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # -- Daily report tick -----------------------------------------------------

    @callback
    def _schedule_report_tick(self) -> None:
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
        now = dt_util.utcnow()
        for integration in list(self._integration_state):
            self._close_cycle(integration, now)
        self._store.async_delay_save(self._data_for_storage, 5)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    @callback
    def _close_cycle(self, integration: str, now: datetime) -> None:
        state = self._integration_state.get(integration)
        if state is None:
            return

        # Materialise the just-ended cycle, treating still-open outages as
        # closing at this tick so their cycle contribution counts.
        closed_intervals = [
            (iv.entity_id, iv.start, iv.end if iv.end is not None else now)
            for iv in state.cycle_intervals
        ]
        bursts = list(state.cycle_bursts)

        self._maybe_fire_n3(integration, bursts, closed_intervals)

        state.cycle_bursts = []
        state.cycle_intervals = [
            OutageInterval(entity_id=iv.entity_id, start=now, end=None)
            for iv in state.cycle_intervals
            if iv.end is None
        ]

        cancel = self._n2_timers.pop(integration, None)
        if cancel is not None:
            cancel()

        # Daily reset: state machine returns to quiet so tomorrow's first
        # drop dispatches a fresh N1, and N2 rearms from zero.
        state.n1_fired = False
        state.n2_fired = False
        state.state = STATE_QUIET

        # Long-standing state cleanup: drop the last_drop_at marker so
        # nothing sticks around past auto_reset_days.
        reset_days = self.auto_reset_days
        if (
            reset_days > 0
            and state.last_drop_at is not None
            and (now - state.last_drop_at).days >= reset_days
        ):
            state.last_drop_at = None

        # Re-arm N2 if an outage is still ongoing across the boundary.
        self._reevaluate_n2_timer(integration)

    @callback
    def _maybe_fire_n3(
        self,
        integration: str,
        bursts: list[IntegrationBurst],
        intervals: list[tuple[str, datetime, datetime]],
    ) -> None:
        """Fire the N3 daily report if the cycle recorded any drop."""
        if not bursts:
            return

        per_entity: dict[str, float] = {}
        for entity_id, start, end in intervals:
            duration = (end - start).total_seconds()
            if duration <= 0:
                continue
            per_entity[entity_id] = per_entity.get(entity_id, 0.0) + duration
        if not per_entity:
            return
        ranked = [
            eid
            for eid, _ in sorted(
                per_entity.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ]
        self._dispatch_notification(
            integration=integration,
            kind=NOTIFY_N3,
            ranked_entity_ids=ranked,
            per_entity_seconds=per_entity,
            outage_count=len(bursts),
        )

    # -- Notification dispatch -------------------------------------------------

    @callback
    def _dispatch_notification(
        self,
        *,
        integration: str,
        kind: str,
        ranked_entity_ids: list[str],
        per_entity_seconds: dict[str, float] | None = None,
        outage_count: int = 0,
        threshold_seconds: int = 0,
    ) -> None:
        integration_name = self._integration_name(integration)
        total_affected = len(ranked_entity_ids)
        top_ids = ranked_entity_ids[:3]
        top_names = [self._friendly_name(eid) for eid in top_ids]
        top_seconds = [
            (per_entity_seconds.get(eid, 0.0) if per_entity_seconds else 0.0)
            for eid in top_ids
        ]
        scope = SCOPE_INTEGRATION if total_affected > 1 else SCOPE_ENTITY

        title, message = self._build_message(
            kind=kind,
            integration_name=integration_name,
            top_names=top_names,
            top_seconds=top_seconds,
            total_affected=total_affected,
            outage_count=outage_count,
            show_times=per_entity_seconds is not None,
        )
        duration_seconds = top_seconds[0] if top_seconds else 0.0
        self.hass.bus.async_fire(
            EVENT_NOTIFICATION,
            {
                "integration": integration,
                "integration_name": integration_name,
                "kind": kind,
                "scope": scope,
                "entity_ids": top_ids,
                "entity_names": top_names,
                "entity_seconds": [round(s, 1) for s in top_seconds],
                "total_affected": total_affected,
                "outage_count": outage_count,
                "threshold_seconds": threshold_seconds,
                "duration_seconds": round(duration_seconds, 1),
                "title": title,
                "message": message,
            },
        )
        self._send_notification(title, message)
        self._send_to_central(
            kind=kind,
            integration=integration,
            integration_name=integration_name,
            entity_names=top_names,
            entity_seconds=[round(s, 1) for s in top_seconds],
            total_affected=total_affected,
            outage_count=outage_count,
            threshold_seconds=threshold_seconds,
            title=title,
            message=message,
        )
        _LOGGER.info(
            "Entity Monitor notification (%s/%s): %s", kind, scope, message
        )

    def _build_message(
        self,
        *,
        kind: str,
        integration_name: str,
        top_names: list[str],
        top_seconds: list[float],
        total_affected: int,
        outage_count: int,
        show_times: bool,
    ) -> tuple[str, str]:
        subject = self._format_subject(
            top_names, top_seconds, total_affected, show_times
        )
        plural = total_affected > 1
        verb_caiu = "caíram" if plural else "caiu"
        verb_ficaram = "ficaram" if plural else "ficou"
        title = f"{integration_name} instável"

        if kind == NOTIFY_N1:
            return (title, f"{subject} {verb_caiu}.")
        if kind == NOTIFY_N2:
            return (title, f"{subject} {verb_ficaram} offline hoje.")
        if kind == NOTIFY_N3:
            vezes = "vezes" if outage_count != 1 else "vez"
            return (
                title,
                f"{subject} {verb_caiu} {outage_count} {vezes} nas "
                f"últimas 24 horas.",
            )
        return ("", "")

    @staticmethod
    def _format_subject(
        top_names: list[str],
        top_seconds: list[float],
        total_affected: int,
        show_times: bool,
    ) -> str:
        """Format 'A (10m), B (5m), C (2m)' — or without times for N1."""
        if not top_names:
            return ""
        if show_times:
            parts = [
                f"{name} ({format_duration_pt(sec)})"
                for name, sec in zip(top_names, top_seconds)
            ]
        else:
            parts = list(top_names)
        joined = ", ".join(parts)
        extra = total_affected - len(top_names)
        if extra > 0:
            return f"{joined} (+{extra})"
        return joined

    @callback
    def async_send_test_notification(self) -> bool:
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
                "entity_ids": [],
                "entity_names": [],
                "entity_seconds": [],
                "total_affected": 0,
                "outage_count": 0,
                "threshold_seconds": 0,
                "duration_seconds": 0,
                "title": title,
                "message": message,
                "test": True,
            },
        )
        self._send_to_central(
            kind=NOTIFY_TEST,
            integration="_test_",
            integration_name="Entity Monitor",
            entity_names=[],
            entity_seconds=[],
            total_affected=0,
            outage_count=0,
            threshold_seconds=0,
            title=title,
            message=message,
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

    # -- Central (Home360 Feedback Central) ------------------------------------

    @callback
    def _send_to_central(
        self,
        *,
        kind: str,
        integration: str,
        integration_name: str,
        entity_names: list[str],
        entity_seconds: list[float],
        total_affected: int,
        outage_count: int,
        threshold_seconds: int,
        title: str,
        message: str,
    ) -> None:
        """Envia o alerta ao HA central, se configurado."""
        if not self.central_enabled:
            return
        payload = {
            "tipo": "monitor",
            "client_id": self.central_client_id,
            "token": self.central_token,
            "kind": kind,
            "integracao": integration_name,
            "integracao_slug": integration,
            "entidades": entity_names,
            "entidades_segundos": entity_seconds,
            "total_afetadas": total_affected,
            "quedas": outage_count,
            "limiar_segundos": threshold_seconds,
            "titulo": title,
            "mensagem": message,
        }
        self.hass.async_create_task(self._async_send_to_central(payload))

    async def _async_send_to_central(self, payload: dict) -> None:
        """POST do alerta para o webhook do central."""
        session = async_get_clientsession(self.hass)
        headers = {CENTRAL_TOKEN_HEADER: self.central_token}
        timeout = aiohttp.ClientTimeout(total=CENTRAL_SEND_TIMEOUT)
        try:
            async with session.post(
                self.central_url, json=payload, headers=headers, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    _LOGGER.warning(
                        "Entity Monitor: central respondeu HTTP %s", resp.status
                    )
        except Exception as err:  # noqa: BLE001 - logar qualquer falha de rede
            _LOGGER.warning(
                "Entity Monitor: falha ao enviar ao central: %s", err
            )

    @callback
    def _send_snapshot_to_central(self, _now: datetime | None = None) -> None:
        """Envia o estado atual (entidades caídas agora) ao central.

        Agrupado por integração, um alerta kind="snapshot" por integração.
        """
        self._snapshot_cancel = None
        if not self.central_enabled or not self._ongoing:
            return
        by_integration: dict[str, list[str]] = {}
        for eid in self._ongoing:
            by_integration.setdefault(self._integration_of(eid), []).append(eid)
        for integration, eids in by_integration.items():
            names = [self._friendly_name(eid) for eid in sorted(eids)]
            top = names[:3]
            extra = len(names) - len(top)
            subject = ", ".join(top) + (f" (+{extra})" if extra > 0 else "")
            verb = "estão" if len(names) > 1 else "está"
            self._send_to_central(
                kind=NOTIFY_SNAPSHOT,
                integration=integration,
                integration_name=self._integration_name(integration),
                entity_names=top,
                entity_seconds=[],
                total_affected=len(names),
                outage_count=0,
                threshold_seconds=0,
                title=f"{self._integration_name(integration)} instável",
                message=f"{subject} {verb} offline agora.",
            )

    # -- Statistics / reporting ------------------------------------------------

    @callback
    def async_reset_statistics(self) -> None:
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
        self.stats.clear()
        self.integration_stats.clear()
        self._integration_state.clear()
        for outage in self._ongoing.values():
            for cancel in outage.timers:
                cancel()
        self._ongoing.clear()
        self._stored_outage_starts.clear()
        for cancel in self._n2_timers.values():
            cancel()
        self._n2_timers.clear()
        self._last_reset_at = dt_util.utcnow()
        self._store.async_delay_save(self._data_for_storage, 1)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        _LOGGER.info("Entity Monitor full reset performed")
        for eid in self._resolved_entities():
            self.stats.setdefault(eid, EntityStats())
        self._schedule_auto_reset()

    @callback
    def _schedule_auto_reset(self) -> None:
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
        self._auto_reset_cancel = None
        _LOGGER.info(
            "Entity Monitor auto-reset after %s days", self.auto_reset_days
        )
        self.async_reset_statistics()

    def build_report(self) -> dict:
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

    @callback
    def _data_for_storage(self) -> dict:
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
        if not entity_id:
            return ""
        state = self.hass.states.get(entity_id)
        if state is not None:
            return state.attributes.get("friendly_name", entity_id)
        return entity_id

    def _integration_of(self, entity_id: str) -> str:
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if entry is not None and entry.platform:
            return entry.platform
        return entity_id.split(".", 1)[0]

    def _integration_name(self, integration: str) -> str:
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
