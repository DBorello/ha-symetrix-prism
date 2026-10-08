"""Push-driven coordinator for one Symetrix DSP."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_PUSH_INTERVAL,
    CONF_RESYNC_INTERVAL,
    DEFAULTS,
    DOMAIN,
    ECHO_WINDOW_SECONDS,
    SUBENTRY_ZONE,
)
from .models import ZoneConfig, resolve_zone
from .protocol import PrismClient, PrismError

_LOGGER = logging.getLogger(__name__)

type SymetrixConfigEntry = ConfigEntry[SymetrixCoordinator]


class SymetrixCoordinator(DataUpdateCoordinator[dict[int, int]]):
    """Holds the latest raw value of every configured control.

    Values arrive by push; a full read runs on connect and every resync
    interval as a heartbeat and drift fix.
    """

    config_entry: SymetrixConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SymetrixConfigEntry) -> None:
        """Initialise the coordinator and its (not yet connected) client."""
        options = entry.options
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(
                seconds=int(
                    options.get(CONF_RESYNC_INTERVAL, DEFAULTS[CONF_RESYNC_INTERVAL])
                )
            ),
            always_update=False,
        )
        self.zones: dict[str, ZoneConfig] = {
            subentry_id: resolve_zone(options, subentry.data, subentry.title)
            for subentry_id, subentry in entry.subentries.items()
            if subentry.subentry_type == SUBENTRY_ZONE
        }
        self.control_numbers: set[int] = {
            cn for zone in self.zones.values() for cn in zone.controls.values()
        }
        self.client = PrismClient(
            entry.data[CONF_HOST],
            int(entry.data[CONF_PORT]),
            on_values=self._handle_values,
            on_connection=self._handle_connection,
            push_interval=int(
                options.get(CONF_PUSH_INTERVAL, DEFAULTS[CONF_PUSH_INTERVAL])
            ),
        )
        self._lost_logged = False
        # Control number -> (value written, monotonic deadline) for writes
        # whose push echo has not arrived yet.
        self._pending_writes: dict[int, tuple[int, float]] = {}
        self._unsub_echo_check: CALLBACK_TYPE | None = None
        # Device registry id of the DSP device, set during setup.
        self.dsp_device_id: str | None = None

    async def _async_update_data(self) -> dict[int, int]:
        """Read every configured control (resync)."""
        if not self.client.connected:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="not_connected"
            )
        try:
            values = await self.client.read_many(self.control_numbers)
        except PrismError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="read_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        data = dict(self.data or {})
        # A control reading -0001 keeps its last known value: selectors read
        # -0001 transiently, so one bad read must not make it unknown.
        data.update({cn: v for cn, v in values.items() if v is not None})
        return data

    @callback
    def _handle_values(self, values: dict[int, int | None]) -> None:
        """Apply pushed (or read) values."""
        updates = {
            cn: v
            for cn, v in values.items()
            if v is not None and cn in self.control_numbers
        }
        self._drop_stale_echoes(updates)
        if not updates or self.data is None:
            return
        if all(self.data.get(cn) == v for cn, v in updates.items()):
            return
        self.async_set_updated_data({**self.data, **updates})

    @callback
    def _handle_connection(self, connected: bool) -> None:
        if connected:
            if self._lost_logged:
                _LOGGER.info("Reconnected to %s", self.config_entry.title)
                self._lost_logged = False
            if self.data is not None:
                # Resync whatever changed while disconnected.
                self.config_entry.async_create_background_task(
                    self.hass, self.async_refresh(), "symetrix resync"
                )
            return
        if not self._lost_logged:
            _LOGGER.warning(
                "Lost connection to %s; reconnecting", self.config_entry.title
            )
            self._lost_logged = True
        self.async_set_update_error(
            UpdateFailed(translation_domain=DOMAIN, translation_key="not_connected")
        )

    @callback
    def async_set_optimistic(self, cn: int, value: int) -> None:
        """Record a value just written, until the DSP confirms it."""
        if self.data is None or self.data.get(cn) == value:
            return
        self._pending_writes[cn] = (value, time.monotonic() + ECHO_WINDOW_SECONDS)
        if self._unsub_echo_check is None:
            self._unsub_echo_check = async_call_later(
                self.hass, ECHO_WINDOW_SECONDS, self._async_check_echoes
            )
        self.data[cn] = value
        self.async_update_listeners()

    def _drop_stale_echoes(self, updates: dict[int, int]) -> None:
        """Remove pushes that are echoes of values older than our last write.

        The DSP pushes each value it held at push time, so while a burst of
        writes is in flight it can report an earlier value after a newer one
        was written. Applying it would make the volume jump back and break
        stepping from the current value.
        """
        now = time.monotonic()
        for cn, value in list(updates.items()):
            if (pending := self._pending_writes.get(cn)) is None:
                continue
            written, deadline = pending
            if value == written or now >= deadline:
                del self._pending_writes[cn]
            else:
                del updates[cn]

    @callback
    def _async_check_echoes(self, _now: datetime) -> None:
        """Resync if a write was never confirmed by a push."""
        self._unsub_echo_check = None
        now = time.monotonic()
        expired = [cn for cn, (_, end) in self._pending_writes.items() if end <= now]
        for cn in expired:
            del self._pending_writes[cn]
        if expired:
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), "symetrix echo resync"
            )
        if self._pending_writes:
            next_deadline = min(end for _, end in self._pending_writes.values())
            self._unsub_echo_check = async_call_later(
                self.hass, max(next_deadline - now, 0.05), self._async_check_echoes
            )

    async def async_shutdown(self) -> None:
        """Close the connection."""
        if self._unsub_echo_check is not None:
            self._unsub_echo_check()
            self._unsub_echo_check = None
        await super().async_shutdown()
        await self.client.close()
