"""Constants for the Symetrix Prism integration."""

from __future__ import annotations

from typing import Any, Final

DOMAIN: Final = "symetrix_prism"
MANUFACTURER: Final = "Symetrix"

SUBENTRY_ZONE: Final = "zone"

# Config entry data
CONF_CREATE_DEFAULTS: Final = "create_defaults"

# Options (per DSP)
CONF_MODEL: Final = "model"
CONF_BASE_POWER: Final = "base_power"
CONF_BASE_SOURCE: Final = "base_source"
CONF_BASE_VOLUME: Final = "base_volume"
CONF_BASE_MUTE: Final = "base_mute"
CONF_POWER_ON_VALUE: Final = "power_on_value"
CONF_POWER_OFF_VALUE: Final = "power_off_value"
CONF_MUTE_ON_VALUE: Final = "mute_on_value"
CONF_MUTE_OFF_VALUE: Final = "mute_off_value"
CONF_FADER_MIN_DB: Final = "fader_min_db"
CONF_FADER_MAX_DB: Final = "fader_max_db"
CONF_VOLUME_MIN_DB: Final = "volume_min_db"
CONF_VOLUME_MAX_DB: Final = "volume_max_db"
CONF_VOLUME_STEP_DB: Final = "volume_step_db"
CONF_TURN_ON_CAP_DB: Final = "turn_on_cap_db"
CONF_RESYNC_INTERVAL: Final = "resync_interval"
CONF_PUSH_INTERVAL: Final = "push_interval"
CONF_SOURCES: Final = "sources"

# Zone subentry data
CONF_ZONE: Final = "zone"
CONF_CONTROLS: Final = "controls"
CONF_ZONE_SOURCES: Final = "zone_sources"
CONF_CN_POWER: Final = "cn_power"
CONF_CN_SOURCE: Final = "cn_source"
CONF_CN_VOLUME: Final = "cn_volume"
CONF_CN_MUTE: Final = "cn_mute"
CONF_AREA: Final = "area_id"

# Control types, in the order they are numbered on the DSP.
CONTROL_POWER: Final = "power"
CONTROL_SOURCE: Final = "source"
CONTROL_VOLUME: Final = "volume"
CONTROL_MUTE: Final = "mute"
CONTROLS: Final = (CONTROL_POWER, CONTROL_SOURCE, CONTROL_VOLUME, CONTROL_MUTE)
CONTROL_BASE_KEYS: Final = {
    CONTROL_POWER: CONF_BASE_POWER,
    CONTROL_SOURCE: CONF_BASE_SOURCE,
    CONTROL_VOLUME: CONF_BASE_VOLUME,
    CONTROL_MUTE: CONF_BASE_MUTE,
}
CONTROL_CN_KEYS: Final = {
    CONTROL_POWER: CONF_CN_POWER,
    CONTROL_SOURCE: CONF_CN_SOURCE,
    CONTROL_VOLUME: CONF_CN_VOLUME,
    CONTROL_MUTE: CONF_CN_MUTE,
}

MIN_ZONE: Final = 1
MAX_ZONE: Final = 100
MAX_SOURCE_INDEX: Final = 99

# Time to collect volume slider moves before writing the last one.
VOLUME_DEBOUNCE_SECONDS: Final = 0.1
# Time to wait for the first connection while setting up an entry.
SETUP_CONNECT_TIMEOUT: Final = 10.0

# Only used to pre-fill forms.
DEFAULTS: Final[dict[str, Any]] = {
    "name": "Symetrix DSP",
    "host": "",
    "port": 48631,
    CONF_MODEL: "Prism 4x4 (Dante)",
    CONF_BASE_POWER: 2000,
    CONF_BASE_SOURCE: 2100,
    CONF_BASE_VOLUME: 2200,
    CONF_BASE_MUTE: 2300,
    CONF_POWER_ON_VALUE: 0,
    CONF_POWER_OFF_VALUE: 65535,
    CONF_MUTE_ON_VALUE: 65535,
    CONF_MUTE_OFF_VALUE: 0,
    CONF_FADER_MIN_DB: -72.0,
    CONF_FADER_MAX_DB: 12.0,
    # Fader minimum: silent, so HA volume 0% is off.
    CONF_VOLUME_MIN_DB: -72.0,
    CONF_VOLUME_MAX_DB: 0.0,
    CONF_VOLUME_STEP_DB: 2.0,
    CONF_TURN_ON_CAP_DB: -30.0,
    CONF_RESYNC_INTERVAL: 300,
    CONF_PUSH_INTERVAL: 100,
    CONF_SOURCES: "0: Input 1\n1: Input 2",
    "zones": [
        {CONF_ZONE: 1, "name": "Zone 1"},
        {CONF_ZONE: 2, "name": "Zone 2"},
    ],
}

# The option keys (everything a DSP options form edits).
OPTION_KEYS: Final = (
    CONF_MODEL,
    CONF_BASE_POWER,
    CONF_BASE_SOURCE,
    CONF_BASE_VOLUME,
    CONF_BASE_MUTE,
    CONF_POWER_ON_VALUE,
    CONF_POWER_OFF_VALUE,
    CONF_MUTE_ON_VALUE,
    CONF_MUTE_OFF_VALUE,
    CONF_FADER_MIN_DB,
    CONF_FADER_MAX_DB,
    CONF_VOLUME_MIN_DB,
    CONF_VOLUME_MAX_DB,
    CONF_VOLUME_STEP_DB,
    CONF_TURN_ON_CAP_DB,
    CONF_RESYNC_INTERVAL,
    CONF_PUSH_INTERVAL,
    CONF_SOURCES,
)


def zone_device_identifier(entry_id: str, subentry_id: str) -> tuple[str, str]:
    """Return the device registry identifier of a zone device."""
    return (DOMAIN, f"{entry_id}_{subentry_id}")


def default_options() -> dict[str, Any]:
    """Return the default DSP options."""
    return {key: DEFAULTS[key] for key in OPTION_KEYS}
