"""Diagnostics for Symetrix Prism."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .coordinator import SymetrixConfigEntry

TO_REDACT = {CONF_HOST, "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SymetrixConfigEntry
) -> dict[str, Any]:
    """Return redacted config, last values and connection stats."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(
            {
                "title": entry.title,
                "unique_id": entry.unique_id,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            TO_REDACT,
        ),
        "zones": {
            subentry_id: asdict(zone) for subentry_id, zone in coordinator.zones.items()
        },
        "values": {
            str(cn): value for cn, value in sorted((coordinator.data or {}).items())
        },
        "connected": coordinator.client.connected,
        "last_update_success": coordinator.last_update_success,
        "connection_stats": coordinator.client.stats.as_dict(),
    }
