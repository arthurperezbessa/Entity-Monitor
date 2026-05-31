"""Core monitoring logic for the Entity Monitor integration."""

from __future__ import annotations

import logging
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
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_RESET_DAYS,
    CONF_COALESCE_SECONDS,
    CONF_ENTITIES,
    CONF_INTEGRATIONS,
    CONF_NOTIFY_COOLDOWN_HOURS,
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_UPGRADE_WINDOW_HOURS,
    CONF_ONLY_PRIMARY,
    CONF_SECONDS_THRESHOLD,
    CONF_SUSTAINED_OUTAGE_LONG_HOURS,
    CONF_SUSTAINED_OUTAGE_SHORT_MINUTES,
    DEFAULT_AUTO_RESET_DAYS,
    DEFAULT_COALESCE_SECONDS,
    DEFAULT_NOTIFY_COOLDOWN_HOURS,
    DEFAULT_NOTIFY_UPGRADE_WINDOW_HOURS,
    DEFAULT_ONLY_PRIMARY,
    DEFAULT_SECONDS_THRESHOLD,
    DEFAULT_SUSTAINED_OUTAGE_LONG_HOURS,
    DEFAULT_SUSTAINED_OUTAGE_SHORT_MINUTES,
    DOMAIN,
    EVENT_NOTIFICATION,
    EVENT_RECOVERED,
    EVENT_UNAVAILABLE,
    LEVEL_HOURS,
    LEVEL_MINUTES,
    LEVEL_SECONDS,
    NOTIFY_N1,
    NOTIFY_N1_UPGRADE,
    NOTIFY_N2,
    NOTIFY_N3_LONG,
    NOTIFY_N3_SHORT,
    NOTIFY_TEST,
    PRIMARY_DOMAIN_ORDER,
    SCOPE_ENTITY,
    SCOPE_INTEGRATION,
    SIGNAL_UPDATE,
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
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


@dataclass
class EntityStats:
    """Persisted outage statistics for a single entity."""

    outage_count: int = 0
    total_downtime: float = 0.0  # seconds
    longest_outage: float = 0.0  # seconds
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
    """Persisted outage statistics aggregated per integration.

    Outages here are counted as *bursts*: when several entities of the same
    integration drop together within the coalesce window it counts as one.
    """

    burst_count: int = 0
    total_downtime: float = 0.0  # seconds
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
    """A group of entities of one integration that went down together.

    Entities that become unavailable within the coalesce window of the burst
    start belong to the same burst, so a hub taking 30 entities offline at
    once counts as a single outage event.
    """

    integration: str
    started: datetime
    entities: list[str] = field(default_factory=list)
    active: set[str] = field(default_factory=set)
    ended: datetime | None = None


@dataclass
class NotificationCycle:
    """Tracks the N1/N2 notification state of one integration.

    A cycle opens with an N1, then rolls forward in fixed windows of
    ``notify_cooldown_hours``. At the end of each window an N2 summary fires
    (when there were drops) and the window restarts silently. A full window
    with no drops closes the cycle so the next drop can fire a fresh N1.
    """

    started_at: datetime
    kind: str  # SCOPE_ENTITY or SCOPE_INTEGRATION
    notified_entity_id: str | None
    upgraded: bool = False
    bursts: list[IntegrationBurst] = field(default_factory=list)
    summaries_fired: int = 0
    summary_cancel: CALLBACK_TYPE | None = None


@dataclass
class IntegrationN3State:
    """Tracks which sustained-outage notifications have fired."""

    short_state: str | None = None  # None / "entity:<id>" / "integration"
    long_state: str | None = None


class EntityMonitor:
    """Watches a set of entities and records when they become unavailable."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the monitor."""
        self.hass = hass
        self.entry = entry
        self.stats: dict[str, EntityStats] = {}
        self.integration_stats: dict[str, IntegrationStats] = {}
        self._ongoing: dict[str, OngoingOutage] = {}
        self._unsub_state: CALLBACK_TYPE | None = None
        # Burst / notification bookkeeping, kept in memory only. A restart
        # simply allows the next outage to start a fresh burst.
        self._bursts: dict[str, list[IntegrationBurst]] = {}
        self._entity_burst: dict[str, IntegrationBurst] = {}
        self._flush_timers: dict[str, CALLBACK_TYPE] = {}
        self._cycle: dict[str, NotificationCycle] = {}
        self._n3_state: dict[str, IntegrationN3State] = {}
        self._integration_names: dict[str, str] = {}
        self._last_reset_at: datetime | None = None
        self._auto_reset_cancel: CALLBACK_TYPE | None = None
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
    def only_primary_entity(self) -> bool:
        """Return whether to keep only one entity per device per integration."""
        return bool(
            self.entry.options.get(CONF_ONLY_PRIMARY, DEFAULT_ONLY_PRIMARY)
        )

    @property
    def seconds_threshold(self) -> int:
        """Return the short (seconds) alert threshold."""
        return int(
            self.entry.options.get(
                CONF_SECONDS_THRESHOLD, DEFAULT_SECONDS_THRESHOLD
            )
        )

    @property
    def sustained_outage_short_minutes(self) -> int:
        """Return the first sustained-outage threshold, in minutes."""
        return int(
            self.entry.options.get(
                CONF_SUSTAINED_OUTAGE_SHORT_MINUTES,
                DEFAULT_SUSTAINED_OUTAGE_SHORT_MINUTES,
            )
        )

    @property
    def sustained_outage_long_hours(self) -> int:
        """Return the second sustained-outage threshold, in hours."""
        return int(
            self.entry.options.get(
                CONF_SUSTAINED_OUTAGE_LONG_HOURS,
                DEFAULT_SUSTAINED_OUTAGE_LONG_HOURS,
            )
        )

    @property
    def notify_service(self) -> str:
        """Return the notify service to call, e.g. ``notify.mobile_app_x``."""
        return str(self.entry.options.get(CONF_NOTIFY_SERVICE, "")).strip()

    @property
    def notify_cooldown_hours(self) -> int:
        """Return the N1 cooldown window, also the N2-long summary window."""
        return int(
            self.entry.options.get(
                CONF_NOTIFY_COOLDOWN_HOURS, DEFAULT_NOTIFY_COOLDOWN_HOURS
            )
        )

    @property
    def notify_upgrade_window_hours(self) -> int:
        """Return the window during which a 2nd different entity escalates the
        N1 cycle to integration scope (0 disables the upgrade)."""
        return int(
            self.entry.options.get(
                CONF_NOTIFY_UPGRADE_WINDOW_HOURS,
                DEFAULT_NOTIFY_UPGRADE_WINDOW_HOURS,
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
    def auto_reset_days(self) -> int:
        """Return after how many days statistics auto-reset (0 = never)."""
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

    async def async_start(self) -> None:
        """Begin watching the configured entities."""
        entities = self._resolved_entities()
        for eid in entities:
            self.stats.setdefault(eid, EntityStats())

        if entities:
            self._unsub_state = async_track_state_change_event(
                self.hass, entities, self._handle_state_change
            )

        # Capture entities that are already unavailable at startup.
        for eid in entities:
            state = self.hass.states.get(eid)
            if state is not None and state.state == STATE_UNAVAILABLE:
                self._start_outage(eid)

        self._schedule_auto_reset()

    def _resolved_entities(self) -> list[str]:
        """Expand integrations + explicit entities into the watch list."""
        explicit = self.entities
        integrations = set(self.integrations)

        ids: list[str] = []
        seen: set[str] = set()

        def add(eid: str) -> None:
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
        for outage in self._ongoing.values():
            for cancel in outage.timers:
                cancel()
        self._ongoing.clear()
        for cancel in self._flush_timers.values():
            cancel()
        self._flush_timers.clear()
        for cycle in self._cycle.values():
            if cycle.summary_cancel is not None:
                cycle.summary_cancel()
        self._cycle.clear()
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
    def _start_outage(self, entity_id: str) -> None:
        """Record the start of an outage and schedule the alert timers."""
        now = dt_util.utcnow()
        timers: list[CALLBACK_TYPE] = [
            async_call_later(
                self.hass,
                self.seconds_threshold,
                partial(self._fire_alert, entity_id, LEVEL_SECONDS),
            ),
        ]
        if self.sustained_outage_short_minutes > 0:
            timers.append(
                async_call_later(
                    self.hass,
                    self.sustained_outage_short_minutes * 60,
                    partial(self._fire_alert, entity_id, LEVEL_MINUTES),
                )
            )
        if self.sustained_outage_long_hours > 0:
            timers.append(
                async_call_later(
                    self.hass,
                    self.sustained_outage_long_hours * 3600,
                    partial(self._fire_alert, entity_id, LEVEL_HOURS),
                )
            )
        self._ongoing[entity_id] = OngoingOutage(started=now, timers=timers)
        _LOGGER.debug("Entity %s became unavailable", entity_id)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    @callback
    def _fire_alert(self, entity_id: str, level: str, _now: datetime) -> None:
        """Fire an event because an entity stayed unavailable past a threshold."""
        outage = self._ongoing.get(entity_id)
        if outage is None:
            return

        outage.alerts_fired.add(level)
        duration = (dt_util.utcnow() - outage.started).total_seconds()
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
        # Hook the alert into the higher-level notification system.
        if level == LEVEL_SECONDS:
            # Treat the seconds threshold as the "confirmed offline" moment
            # and attach the entity to a burst for its integration.
            self._assign_to_burst(entity_id)
        elif level == LEVEL_MINUTES:
            self._evaluate_n3(entity_id, "short")
        elif level == LEVEL_HOURS:
            self._evaluate_n3(entity_id, "long")
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    def _threshold_seconds(self, level: str) -> int:
        """Return the configured threshold for the given alert level."""
        if level == LEVEL_SECONDS:
            return self.seconds_threshold
        if level == LEVEL_MINUTES:
            return self.sustained_outage_short_minutes * 60
        if level == LEVEL_HOURS:
            return self.sustained_outage_long_hours * 3600
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
        self._close_burst_entity(entity_id, now)

        # Clear the N3 state once every entity of the integration is back.
        if not any(
            self._integration_of(e) == integration for e in self._ongoing
        ):
            self._n3_state.pop(integration, None)

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
        self._store.async_delay_save(self._data_for_storage, 10)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # -- Burst tracking --------------------------------------------------------

    @callback
    def _assign_to_burst(self, entity_id: str) -> None:
        """Attach a confirmed outage to a burst, creating one when needed."""
        integration = self._integration_of(entity_id)
        now = dt_util.utcnow()
        window = self.coalesce_seconds
        bursts = self._bursts.setdefault(integration, [])
        current = bursts[-1] if bursts else None

        if (
            current is not None
            and current.ended is None
            and (now - current.started).total_seconds() <= window
        ):
            # Joins the open burst: simultaneous drop, not a new event.
            if entity_id not in current.entities:
                current.entities.append(entity_id)
            current.active.add(entity_id)
            self._entity_burst[entity_id] = current
            return

        # A new outage event for this integration.
        burst = IntegrationBurst(
            integration=integration,
            started=now,
            entities=[entity_id],
            active={entity_id},
        )
        bursts.append(burst)
        self._entity_burst[entity_id] = burst

        istats = self.integration_stats.setdefault(
            integration, IntegrationStats()
        )
        istats.burst_count += 1
        istats.last_burst_start = now.isoformat()
        self._store.async_delay_save(self._data_for_storage, 10)
        self._prune_bursts(integration)

        # Wait the coalesce window so the notification can include every
        # entity that drops together, then evaluate the notification.
        if integration in self._flush_timers:
            self._flush_timers[integration]()
        self._flush_timers[integration] = async_call_later(
            self.hass, window, partial(self._flush_burst, integration, burst)
        )

    @callback
    def _close_burst_entity(self, entity_id: str, now: datetime) -> None:
        """Mark an entity as recovered within its burst."""
        burst = self._entity_burst.pop(entity_id, None)
        if burst is None:
            return
        burst.active.discard(entity_id)
        if burst.active or burst.ended is not None:
            return
        # Every entity of the burst recovered: the integration is back.
        burst.ended = now
        istats = self.integration_stats.setdefault(
            burst.integration, IntegrationStats()
        )
        istats.total_downtime += (now - burst.started).total_seconds()
        istats.last_burst_end = now.isoformat()
        self._store.async_delay_save(self._data_for_storage, 10)

    @callback
    def _prune_bursts(self, integration: str) -> None:
        """Drop bursts that are finished and not referenced by an active cycle."""
        cycle = self._cycle.get(integration)
        cycle_bursts: set[int] = (
            {id(b) for b in cycle.bursts} if cycle is not None else set()
        )
        bursts = self._bursts.get(integration, [])
        self._bursts[integration] = [
            b
            for b in bursts
            if b.ended is None or id(b) in cycle_bursts
        ]

    # -- Notifications ---------------------------------------------------------

    @callback
    def _flush_burst(
        self,
        integration: str,
        burst: IntegrationBurst,
        _now: datetime,
    ) -> None:
        """Run once the coalesce window closed for a freshly opened burst."""
        self._flush_timers.pop(integration, None)
        self._process_burst(integration, burst)

    @callback
    def _process_burst(
        self, integration: str, burst: IntegrationBurst
    ) -> None:
        """Either open a new notification cycle or fold the burst into one."""
        cycle = self._cycle.get(integration)

        if cycle is None:
            self._start_cycle(integration, burst)
            return

        # Cooldown is active: accumulate the burst for the next summary.
        if burst not in cycle.bursts:
            cycle.bursts.append(burst)

        # Upgrade the cycle to integration-scope if a different entity drops
        # within the upgrade window from the original N1.
        if cycle.kind == SCOPE_ENTITY and not cycle.upgraded:
            upgrade_window = self.notify_upgrade_window_hours * 3600
            elapsed = (
                dt_util.utcnow() - cycle.started_at
            ).total_seconds()
            if elapsed < upgrade_window:
                others = [
                    e
                    for e in burst.entities
                    if e != cycle.notified_entity_id
                ]
                if others or len(burst.entities) > 1:
                    cycle.kind = SCOPE_INTEGRATION
                    cycle.upgraded = True
                    self._dispatch_notification(
                        integration=integration,
                        kind=NOTIFY_N1_UPGRADE,
                        scope=SCOPE_INTEGRATION,
                    )

    @callback
    def _start_cycle(
        self, integration: str, burst: IntegrationBurst
    ) -> None:
        """Open a fresh notification cycle for an integration and fire N1."""
        now = dt_util.utcnow()
        if len(burst.entities) == 1:
            scope = SCOPE_ENTITY
            notified_eid: str | None = burst.entities[0]
        else:
            scope = SCOPE_INTEGRATION
            notified_eid = None

        cycle = NotificationCycle(
            started_at=now,
            kind=scope,
            notified_entity_id=notified_eid,
            bursts=[burst],
        )
        self._cycle[integration] = cycle

        self._dispatch_notification(
            integration=integration,
            kind=NOTIFY_N1,
            scope=scope,
            entity_id=notified_eid,
        )

        cycle.summary_cancel = async_call_later(
            self.hass,
            self.notify_cooldown_hours * 3600,
            partial(self._fire_summary, integration),
        )

    @callback
    def _fire_summary(self, integration: str, _now: datetime) -> None:
        """Fire the rolling N2 summary and decide whether to keep rolling."""
        cycle = self._cycle.get(integration)
        if cycle is None:
            return
        cycle.summary_cancel = None

        burst_count = len(cycle.bursts)
        # On the first summary N1 already announced the trigger drop, so the
        # summary only adds value when there is more than one. From the second
        # summary onwards there was no N1, so any drop is worth reporting.
        threshold = 2 if cycle.summaries_fired == 0 else 1

        if burst_count >= threshold:
            scope = cycle.kind
            if scope == SCOPE_ENTITY and any(
                any(e != cycle.notified_entity_id for e in b.entities)
                for b in cycle.bursts
            ):
                scope = SCOPE_INTEGRATION
            self._dispatch_notification(
                integration=integration,
                kind=NOTIFY_N2,
                scope=scope,
                entity_id=(
                    cycle.notified_entity_id
                    if scope == SCOPE_ENTITY
                    else None
                ),
                outage_count=burst_count,
                window_hours=self.notify_cooldown_hours,
            )

        if burst_count >= 1:
            # Integration is still unstable: roll into a fresh silent window.
            cycle.summaries_fired += 1
            cycle.bursts = []
            cycle.summary_cancel = async_call_later(
                self.hass,
                self.notify_cooldown_hours * 3600,
                partial(self._fire_summary, integration),
            )
        else:
            self._end_cycle(integration)

    @callback
    def _end_cycle(self, integration: str) -> None:
        """Close out an integration's notification cycle."""
        cycle = self._cycle.pop(integration, None)
        if cycle is None:
            return
        if cycle.summary_cancel is not None:
            cycle.summary_cancel()
        self._prune_bursts(integration)

    # -- N3: sustained-outage notifications -----------------------------------

    @callback
    def _evaluate_n3(self, entity_id: str, level: str) -> None:
        """Decide whether the sustained-outage threshold notification fires."""
        integration = self._integration_of(entity_id)
        state = self._n3_state.setdefault(integration, IntegrationN3State())

        if level == "short":
            threshold_seconds = self.sustained_outage_short_minutes * 60
            current = state.short_state
            notify_kind = NOTIFY_N3_SHORT
        else:
            threshold_seconds = self.sustained_outage_long_hours * 3600
            current = state.long_state
            notify_kind = NOTIFY_N3_LONG

        if threshold_seconds <= 0 or current == "integration":
            return

        now = dt_util.utcnow()
        qualified = [
            e
            for e, outage in self._ongoing.items()
            if self._integration_of(e) == integration
            and (now - outage.started).total_seconds() >= threshold_seconds
        ]
        count = len(qualified)

        if count == 0:
            return

        if current is None:
            if count >= 2:
                new_state = "integration"
                self._dispatch_notification(
                    integration=integration,
                    kind=notify_kind,
                    scope=SCOPE_INTEGRATION,
                    threshold_seconds=threshold_seconds,
                )
            else:
                new_state = f"entity:{entity_id}"
                self._dispatch_notification(
                    integration=integration,
                    kind=notify_kind,
                    scope=SCOPE_ENTITY,
                    entity_id=entity_id,
                    threshold_seconds=threshold_seconds,
                )
        else:
            prev = current.split(":", 1)[1] if current.startswith("entity:") else None
            if prev == entity_id:
                return
            if count >= 2:
                new_state = "integration"
                self._dispatch_notification(
                    integration=integration,
                    kind=notify_kind,
                    scope=SCOPE_INTEGRATION,
                    threshold_seconds=threshold_seconds,
                )
            else:
                # Previous entity has recovered; a fresh one just crossed it.
                new_state = f"entity:{entity_id}"
                self._dispatch_notification(
                    integration=integration,
                    kind=notify_kind,
                    scope=SCOPE_ENTITY,
                    entity_id=entity_id,
                    threshold_seconds=threshold_seconds,
                )

        if level == "short":
            state.short_state = new_state
        else:
            state.long_state = new_state

    # -- Notification dispatch -------------------------------------------------

    @callback
    def _dispatch_notification(
        self,
        *,
        integration: str,
        kind: str,
        scope: str,
        entity_id: str | None = None,
        outage_count: int = 0,
        window_hours: int = 0,
        threshold_seconds: int = 0,
    ) -> None:
        """Build, fire the event for, and deliver a notification."""
        integration_name = self._integration_name(integration)
        entity_name = (
            self._friendly_name(entity_id) if entity_id is not None else ""
        )

        title, message = self._build_message(
            kind=kind,
            scope=scope,
            integration_name=integration_name,
            entity_name=entity_name,
            outage_count=outage_count,
            window_hours=window_hours,
            threshold_seconds=threshold_seconds,
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
                "outage_count": outage_count,
                "window_hours": window_hours,
                "threshold_seconds": threshold_seconds,
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
        outage_count: int,
        window_hours: int,
        threshold_seconds: int,
    ) -> tuple[str, str]:
        """Return the (title, body) pair for a notification."""
        if kind == NOTIFY_N1:
            if scope == SCOPE_ENTITY:
                return (
                    integration_name,
                    f"{entity_name} ficou indisponível.",
                )
            return (
                f"Integração {integration_name} instável",
                "Várias entidades caíram juntas.",
            )

        if kind == NOTIFY_N1_UPGRADE:
            return (
                f"Integração {integration_name} instável",
                "Outras entidades caíram, escalando para a integração.",
            )

        if kind == NOTIFY_N2:
            if scope == SCOPE_ENTITY:
                return (
                    integration_name,
                    f"{entity_name} indisponível {outage_count} vezes nas "
                    f"últimas {window_hours}h.",
                )
            return (
                f"Integração {integration_name} instável",
                f"{outage_count} quedas nas últimas {window_hours}h.",
            )

        if kind == NOTIFY_N3_SHORT:
            minutes = threshold_seconds // 60
            if scope == SCOPE_ENTITY:
                return (
                    integration_name,
                    f"{entity_name} indisponível há mais de {minutes} "
                    "minutos.",
                )
            return (
                f"Integração {integration_name}",
                f"Indisponível há mais de {minutes} minutos.",
            )

        if kind == NOTIFY_N3_LONG:
            hours = threshold_seconds // 3600
            if scope == SCOPE_ENTITY:
                return (
                    integration_name,
                    f"{entity_name} indisponível há mais de {hours} horas.",
                )
            return (
                f"Integração {integration_name}",
                f"Indisponível há mais de {hours} horas.",
            )

        return ("", "")

    @callback
    def async_send_test_notification(self) -> bool:
        """Fire a sample notification so the user can validate the setup.

        Returns whether the configured notify service was invoked.
        """
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
                "outage_count": 0,
                "window_hours": 0,
                "threshold_seconds": 0,
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
        """Clear all recorded statistics."""
        for eid in list(self.stats):
            self.stats[eid] = EntityStats()
        self.integration_stats.clear()
        self._bursts.clear()
        self._entity_burst.clear()
        for integration in list(self._cycle):
            self._end_cycle(integration)
        self._n3_state.clear()
        self._last_reset_at = dt_util.utcnow()
        self._store.async_delay_save(self._data_for_storage, 1)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        _LOGGER.info("Entity Monitor statistics reset")
        # Restart the auto-reset countdown from this moment.
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
            # First time we see this period: anchor it to now so users get a
            # full window before the first auto-reset.
            self._last_reset_at = now
            self._store.async_delay_save(self._data_for_storage, 1)

        next_at = self._last_reset_at + period
        if next_at <= now:
            # The window already elapsed (e.g. HA was off): reset right away.
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
            "sustained_outage_short_minutes": (
                self.sustained_outage_short_minutes
            ),
            "sustained_outage_long_hours": self.sustained_outage_long_hours,
            "notify_cooldown_hours": self.notify_cooldown_hours,
            "notify_upgrade_window_hours": self.notify_upgrade_window_hours,
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
        }

    def _friendly_name(self, entity_id: str) -> str:
        """Return the friendly name of an entity, falling back to its id."""
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
