"""Core monitoring logic for the Entity Monitor integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
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
    CONF_ENTITIES,
    CONF_MINUTES_THRESHOLD,
    CONF_SECONDS_THRESHOLD,
    DEFAULT_MINUTES_THRESHOLD,
    DEFAULT_SECONDS_THRESHOLD,
    DOMAIN,
    EVENT_RECOVERED,
    EVENT_UNAVAILABLE,
    LEVEL_MINUTES,
    LEVEL_SECONDS,
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
class OngoingOutage:
    """Tracks an outage that is currently in progress."""

    started: datetime
    timers: list[CALLBACK_TYPE] = field(default_factory=list)
    alerts_fired: set[str] = field(default_factory=set)


class EntityMonitor:
    """Watches a set of entities and records when they become unavailable."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the monitor."""
        self.hass = hass
        self.entry = entry
        self.stats: dict[str, EntityStats] = {}
        self._ongoing: dict[str, OngoingOutage] = {}
        self._unsub_state: CALLBACK_TYPE | None = None
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )

    # -- Configuration helpers -------------------------------------------------

    @property
    def entities(self) -> list[str]:
        """Return the list of monitored entity ids."""
        return list(self.entry.options.get(CONF_ENTITIES, []))

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

    # -- Public state ----------------------------------------------------------

    @property
    def ongoing_entities(self) -> list[str]:
        """Return entity ids that are currently in an outage."""
        return list(self._ongoing)

    @property
    def total_outages(self) -> int:
        """Return the total number of recorded outages."""
        return sum(s.outage_count for s in self.stats.values())

    @property
    def total_downtime(self) -> float:
        """Return the total recorded downtime in seconds."""
        return sum(s.total_downtime for s in self.stats.values())

    # -- Lifecycle -------------------------------------------------------------

    async def async_load(self) -> None:
        """Load persisted statistics from disk."""
        data = await self._store.async_load()
        if data and "stats" in data:
            self.stats = {
                eid: EntityStats.from_dict(raw)
                for eid, raw in data["stats"].items()
            }

    async def async_start(self) -> None:
        """Begin watching the configured entities."""
        entities = self.entities
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

    # -- Statistics / reporting ------------------------------------------------

    @callback
    def async_reset_statistics(self) -> None:
        """Clear all recorded statistics."""
        for eid in list(self.stats):
            self.stats[eid] = EntityStats()
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

        integrations: dict[str, dict] = {}
        for item in by_entity:
            agg = integrations.setdefault(
                item["integration"],
                {
                    "integration": item["integration"],
                    "monitored_entities": 0,
                    "outage_count": 0,
                    "total_downtime_seconds": 0.0,
                },
            )
            agg["monitored_entities"] += 1
            agg["outage_count"] += item["outage_count"]
            agg["total_downtime_seconds"] += item["total_downtime_seconds"]

        by_integration = sorted(
            integrations.values(),
            key=lambda i: (i["outage_count"], i["total_downtime_seconds"]),
            reverse=True,
        )
        for agg in by_integration:
            agg["total_downtime_seconds"] = round(
                agg["total_downtime_seconds"], 1
            )
            agg["total_downtime"] = format_duration(
                agg["total_downtime_seconds"]
            )

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
            "by_entity": by_entity,
            "by_integration": by_integration,
        }

    # -- Internal helpers ------------------------------------------------------

    @callback
    def _data_for_storage(self) -> dict:
        """Return the data structure persisted to disk."""
        return {
            "stats": {eid: s.as_dict() for eid, s in self.stats.items()}
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
