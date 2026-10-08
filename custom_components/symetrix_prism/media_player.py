"""Media player entities: one per Symetrix zone."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
import logging

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
)
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONTROL_MUTE,
    CONTROL_POWER,
    CONTROL_SOURCE,
    CONTROL_VOLUME,
    DOMAIN,
    MANUFACTURER,
    VOLUME_DEBOUNCE_SECONDS,
    zone_device_identifier,
)
from .coordinator import SymetrixConfigEntry, SymetrixCoordinator
from .models import ZoneConfig
from .protocol import PrismError

_LOGGER = logging.getLogger(__name__)

# Commands are serialised by the client; no parallel-update limit needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SymetrixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one media player per zone subentry."""
    coordinator = entry.runtime_data
    for subentry_id, zone in coordinator.zones.items():
        async_add_entities(
            [SymetrixZoneMediaPlayer(coordinator, subentry_id, zone)],
            config_subentry_id=subentry_id,
        )


class SymetrixZoneMediaPlayer(
    CoordinatorEntity[SymetrixCoordinator], MediaPlayerEntity
):
    """A Symetrix zone with power, source, volume and mute."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_translation_key = "zone"

    def __init__(
        self, coordinator: SymetrixCoordinator, subentry_id: str, zone: ZoneConfig
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._zone = zone
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{subentry_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={zone_device_identifier(entry.entry_id, subentry_id)},
            name=zone.name,
            manufacturer=MANUFACTURER,
            model=f"Zone {zone.zone}",
        )
        if coordinator.dsp_device_id is not None:
            self._attr_device_info["via_device_id"] = coordinator.dsp_device_id
        features = MediaPlayerEntityFeature(0)
        if zone.cn(CONTROL_POWER) is not None:
            features |= MediaPlayerEntityFeature.TURN_ON
            features |= MediaPlayerEntityFeature.TURN_OFF
        if zone.cn(CONTROL_SOURCE) is not None and zone.sources:
            features |= MediaPlayerEntityFeature.SELECT_SOURCE
            self._attr_source_list = [name for _, name in zone.sources]
        if zone.cn(CONTROL_VOLUME) is not None:
            features |= MediaPlayerEntityFeature.VOLUME_SET
            features |= MediaPlayerEntityFeature.VOLUME_STEP
        if zone.cn(CONTROL_MUTE) is not None:
            features |= MediaPlayerEntityFeature.VOLUME_MUTE
        self._attr_supported_features = features
        self._volume_target: int | None = None
        self._volume_task: asyncio.Task[None] | None = None

    def _raw(self, control: str) -> int | None:
        cn = self._zone.cn(control)
        if cn is None or self.coordinator.data is None:
            return None
        return self.coordinator.data.get(cn)

    @property
    def available(self) -> bool:
        """Return False while the DSP is disconnected."""
        return super().available and self.coordinator.client.connected

    @property
    def state(self) -> MediaPlayerState | None:
        """Return OFF when the zone's power is off, otherwise ON."""
        if self._zone.cn(CONTROL_POWER) is None:
            return MediaPlayerState.ON
        if (raw := self._raw(CONTROL_POWER)) is None:
            return None
        return (
            MediaPlayerState.ON if self._zone.is_power_on(raw) else MediaPlayerState.OFF
        )

    @property
    def volume_db(self) -> float | None:
        """Return the zone volume in dB."""
        if (raw := self._raw(CONTROL_VOLUME)) is None:
            return None
        return self._zone.scale.raw_to_db(raw)

    @property
    def volume_level(self) -> float | None:
        """Return the volume level (0-1, linear in dB)."""
        if (db := self.volume_db) is None:
            return None
        return self._zone.scale.db_to_level(db)

    @property
    def is_volume_muted(self) -> bool | None:
        """Return True if the zone is muted."""
        if (raw := self._raw(CONTROL_MUTE)) is None:
            return None
        return self._zone.is_muted(raw)

    @property
    def source(self) -> str | None:
        """Return the selected source name."""
        if (raw := self._raw(CONTROL_SOURCE)) is None:
            return None
        return next((name for index, name in self._zone.sources if index == raw), None)

    @property
    def extra_state_attributes(self) -> dict[str, float | int | None]:
        """Expose the zone number and volume in dB."""
        db = self.volume_db
        return {
            "zone": self._zone.zone,
            "volume_db": None if db is None else round(db, 1),
        }

    async def _write(self, control: str, value: int) -> None:
        cn = self._zone.cn(control)
        if cn is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="control_disabled",
                translation_placeholders={"control": control},
            )
        await self._call(self.coordinator.client.set(cn, value))
        self.coordinator.async_set_optimistic(cn, value)

    async def _call(self, command: Awaitable[None]) -> None:
        try:
            await command
        except PrismError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_turn_on(self) -> None:
        """Power on, first lowering the volume to the turn-on cap if needed.

        The cap only applies when the zone is off, so calling turn_on on a
        zone that is already playing never changes its volume.
        """
        cap = self._zone.turn_on_cap_db
        db = self.volume_db
        if (
            self.state is MediaPlayerState.OFF
            and cap is not None
            and db is not None
            and db > cap
        ):
            await self._write(CONTROL_VOLUME, self._zone.scale.db_to_raw(cap))
        await self._write(CONTROL_POWER, self._zone.power_on_value)

    async def async_turn_off(self) -> None:
        """Power off (source, volume and mute are left alone)."""
        await self._write(CONTROL_POWER, self._zone.power_off_value)

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute."""
        await self._write(
            CONTROL_MUTE,
            self._zone.mute_on_value if mute else self._zone.mute_off_value,
        )

    async def async_select_source(self, source: str) -> None:
        """Select a source by name."""
        index = next(
            (index for index, name in self._zone.sources if name == source), None
        )
        if index is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_source",
                translation_placeholders={"source": source},
            )
        await self._write(CONTROL_SOURCE, index)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the volume; slider bursts are coalesced (last value wins)."""
        await self._set_volume_db(self._zone.scale.level_to_db(volume))

    async def async_volume_up(self) -> None:
        """Raise the volume by one step, never above the maximum."""
        if (db := self._current_db()) is None:
            return
        await self._set_volume_db(min(db + self._zone.step_db, self._zone.scale.max_db))

    async def async_volume_down(self) -> None:
        """Lower the volume by one step, not below the minimum."""
        if (db := self._current_db()) is None:
            return
        floor = min(db, self._zone.scale.min_db)
        await self._set_volume_db(max(db - self._zone.step_db, floor))

    def _current_db(self) -> float | None:
        """Return the volume to step from, rounded to 0.1 dB.

        Rounding stops raw-value quantisation accumulating across steps.
        """
        if self._volume_target is not None:
            db: float | None = self._zone.scale.raw_to_db(self._volume_target)
        else:
            db = self.volume_db
        return None if db is None else round(db, 1)

    async def _set_volume_db(self, db: float) -> None:
        cn = self._zone.cn(CONTROL_VOLUME)
        if cn is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="control_disabled",
                translation_placeholders={"control": CONTROL_VOLUME},
            )
        self._volume_target = self._zone.scale.db_to_raw(db)
        self.coordinator.async_set_optimistic(cn, self._volume_target)
        if self._volume_task is None or self._volume_task.done():
            self._volume_task = self.hass.async_create_task(
                self._async_flush_volume(cn), eager_start=False
            )
        await asyncio.shield(self._volume_task)

    async def _async_flush_volume(self, cn: int) -> None:
        await asyncio.sleep(VOLUME_DEBOUNCE_SECONDS)
        try:
            while (target := self._volume_target) is not None:
                await self._call(self.coordinator.client.set(cn, target))
                if self._volume_target == target:
                    self._volume_target = None
        finally:
            self._volume_target = None
        self.coordinator.async_update_listeners()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a pending volume write."""
        if self._volume_task is not None:
            self._volume_task.cancel()
        await super().async_will_remove_from_hass()
