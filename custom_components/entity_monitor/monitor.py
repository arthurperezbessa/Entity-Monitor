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
    CONF_COALESCE_SECONDS,
    CONF_ENTITIES,
    CONF_INTEGRATIONS,
    CONF_MINUTES_THRESHOLD,
    CONF_NOTIFY_SERVICE,
    CONF_ONLY_PRIMARY,
    CONF_RENOTIFY_HOURS,
    CONF_SECONDS_THRESHOLD,
    DEFAULT_COALESCE_SECONDS,
    DEFAULT_MINUTES_THRESHOLD,
    DEFAULT_ONLY_PRIMARY,
    DEFAULT_RENOTIFY_HOURS,
    DEFAULT_SECONDS_THRESHOLD,
    DOMAIN,
    EVENT_NOTIFICATION,
    EVENT_RECOVERED,
    EVENT_UNAVAILABLE,
    LEVEL_MINUTES,
    LEVEL_SECONDS,
    PRIMARY_DOMAIN_ORDER,
    SIGNAL_UPDATE,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Cap how many entity names are spelled out in a notification body.
_MAX_NAMES_IN_MESSAGE = 8


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
        self._last_notified: dict[str, datetime] = {}
        self._flush_timers: dict[str, CALLBACK_TYPE] = {}
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
    def minutes_threshold(self) -> int:
        """Return the long (minutes) alert threshold."""
        return int(
            self.entry.options.get(
                CONF_MINUTES_THRESHOLD, DEFAULT_MINUTES_THRESHOLD
            )
        )

    @property
    def notify_service(self) -> str:
        """Return the notify service to call, e.g. ``notify.mobile_app_x``."""
        return str(self.entry.options.get(CONF_NOTIFY_SERVICE, "")).strip()

    @property
    def renotify_hours(self) -> int:
        """Return how many hours to wait before notifying again."""
        return int(
            self.entry.options.get(
                CONF_RENOTIFY_HOURS, DEFAULT_RENOTIFY_HOURS
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
        timers = [
            async_call_later(
                self.hass,
                self.seconds_threshold,
                partial(self._fire_alert, entity_id, LEVEL_SECONDS),
            ),
            async_call_later(
                self.hass,
                self.minutes_threshold * 60,
                partial(self._fire_alert, entity_id, LEVEL_MINUTES),
            ),
        ]
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
        threshold = (
            self.seconds_threshold
            if level == LEVEL_SECONDS
            else self.minutes_threshold * 60
        )
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
        # Treat the seconds threshold as the "confirmed offline" moment and
        # attach the entity to a burst for its integration.
        if level == LEVEL_SECONDS:
            self._assign_to_burst(entity_id)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

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

        self._close_burst_entity(entity_id, now)

        self.hass.bus.async_fire(
            EVENT_RECOVERED,
            {
                "entity_id": entity_id,
                "friendly_name": self._friendly_name(entity_id),
                "integration": self._integration_of(entity_id),
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
            self.hass, window, partial(self._flush_burst, integration)
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
        """Drop bursts that are finished and already notified."""
        last = self._last_notified.get(integration)
        bursts = self._bursts.get(integration, [])
        self._bursts[integration] = [
            b
            for b in bursts
            if b.ended is None or last is None or b.started > last
        ]

    # -- Notifications ---------------------------------------------------------

    @callback
    def _flush_burst(self, integration: str, _now: datetime) -> None:
        """Run once the coalesce window closed for a freshly opened burst."""
        self._flush_timers.pop(integration, None)
        self._maybe_notify(integration)

    @callback
    def _maybe_notify(self, integration: str) -> None:
        """Notify about an integration unless it is still within cooldown."""
        now = dt_util.utcnow()
        last = self._last_notified.get(integration)
        if last is not None and now < last + timedelta(
            hours=self.renotify_hours
        ):
            _LOGGER.debug(
                "Notification for %s suppressed (cooldown active)",
                integration,
            )
            return

        pending = [
            b
            for b in self._bursts.get(integration, [])
            if last is None or b.started > last
        ]
        if not pending:
            return

        entity_ids: list[str] = []
        for burst in pending:
            for eid in burst.entities:
                if eid not in entity_ids:
                    entity_ids.append(eid)

        burst_count = len(pending)
        title, message = self._build_message(
            integration, entity_ids, burst_count
        )

        self.hass.bus.async_fire(
            EVENT_NOTIFICATION,
            {
                "integration": integration,
                "entity_ids": entity_ids,
                "entity_names": [
                    self._friendly_name(eid) for eid in entity_ids
                ],
                "outage_events": burst_count,
                "entity_count": len(entity_ids),
                "title": title,
                "message": message,
            },
        )
        self._send_notification(title, message)

        self._last_notified[integration] = now
        self._prune_bursts(integration)
        _LOGGER.info("Entity Monitor notification: %s", message)

    def _build_message(
        self, integration: str, entity_ids: list[str], burst_count: int
    ) -> tuple[str, str]:
        """Build the notification title and body (one line per integration)."""
        if len(entity_ids) == 1:
            name = self._friendly_name(entity_ids[0])
            if burst_count == 1:
                message = f"{name} ficou indisponível."
            else:
                message = f"{name} ficou indisponível {burst_count} vezes."
            return "Entidade indisponível", message

        names = self._join_names(entity_ids)
        count = len(entity_ids)
        if burst_count == 1:
            message = (
                f"Integração {integration}: {count} entidades caíram "
                f"juntas. {names}."
            )
        else:
            message = (
                f"Integração {integration}: {burst_count} quedas "
                f"envolvendo {count} entidades. {names}."
            )
        return f"Integração {integration} instável", message

    def _join_names(self, entity_ids: list[str]) -> str:
        """Join entity names for a message, trimming very long lists."""
        names = [self._friendly_name(eid) for eid in entity_ids]
        if len(names) <= _MAX_NAMES_IN_MESSAGE:
            return ", ".join(names)
        shown = ", ".join(names[:_MAX_NAMES_IN_MESSAGE])
        return f"{shown} e mais {len(names) - _MAX_NAMES_IN_MESSAGE}"

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
                "entity_ids": [],
                "entity_names": [],
                "outage_events": 0,
                "entity_count": 0,
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
        self._store.async_delay_save(self._data_for_storage, 1)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        _LOGGER.info("Entity Monitor statistics reset")

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
            "minutes_threshold": self.minutes_threshold,
            "coalesce_seconds": self.coalesce_seconds,
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
