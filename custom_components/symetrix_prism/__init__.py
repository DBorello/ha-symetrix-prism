"""The Symetrix Prism integration."""

from __future__ import annotations

import asyncio

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import area_registry as ar, device_registry as dr

from .const import (
    CONF_MODEL,
    DEFAULTS,
    DOMAIN,
    MANUFACTURER,
    SETUP_CONNECT_TIMEOUT,
    zone_device_identifier,
)
from .coordinator import SymetrixConfigEntry, SymetrixCoordinator

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]


async def async_setup_entry(hass: HomeAssistant, entry: SymetrixConfigEntry) -> bool:
    """Connect to the DSP and set up its zones."""
    coordinator = SymetrixCoordinator(hass, entry)
    await coordinator.client.start()
    try:
        async with asyncio.timeout(SETUP_CONNECT_TIMEOUT):
            await coordinator.client.wait_connected()
    except TimeoutError as err:
        await coordinator.client.close()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": coordinator.client.host},
        ) from err
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        await coordinator.client.close()
        raise

    entry.runtime_data = coordinator
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=MANUFACTURER,
        model=entry.options.get(CONF_MODEL, DEFAULTS[CONF_MODEL]),
        name=entry.title,
    )
    coordinator.dsp_device_id = device.id
    _async_create_zone_devices(hass, entry, device.id)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_create_zone_devices(
    hass: HomeAssistant, entry: SymetrixConfigEntry, dsp_device_id: str
) -> None:
    """Create a device per zone and give it the zone's area if it has none.

    An area chosen on the device page is kept; the zone form changes the
    area of an existing device itself (see ZoneSubentryFlow).
    """
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    for subentry_id, zone in entry.runtime_data.zones.items():
        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=subentry_id,
            identifiers={zone_device_identifier(entry.entry_id, subentry_id)},
            manufacturer=MANUFACTURER,
            model=f"Zone {zone.zone}",
            name=zone.name,
            via_device_id=dsp_device_id,
        )
        if (
            zone.area_id
            and device.area_id is None
            and area_registry.async_get_area(zone.area_id) is not None
        ):
            device_registry.async_update_device(device.id, area_id=zone.area_id)


async def async_unload_entry(hass: HomeAssistant, entry: SymetrixConfigEntry) -> bool:
    """Unload a config entry (the coordinator closes its connection on unload)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: SymetrixConfigEntry
) -> None:
    """Reload when options or zones change."""
    hass.config_entries.async_schedule_reload(entry.entry_id)
