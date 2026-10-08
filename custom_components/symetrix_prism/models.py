"""Zone configuration and value mapping (no Home Assistant imports)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from .const import (
    CONF_AREA,
    CONF_CONTROLS,
    CONF_FADER_MAX_DB,
    CONF_FADER_MIN_DB,
    CONF_MUTE_OFF_VALUE,
    CONF_MUTE_ON_VALUE,
    CONF_POWER_OFF_VALUE,
    CONF_POWER_ON_VALUE,
    CONF_SOURCES,
    CONF_TURN_ON_CAP_DB,
    CONF_VOLUME_MAX_DB,
    CONF_VOLUME_MIN_DB,
    CONF_VOLUME_STEP_DB,
    CONF_ZONE,
    CONF_ZONE_SOURCES,
    CONTROL_BASE_KEYS,
    CONTROL_CN_KEYS,
    CONTROLS,
    MAX_SOURCE_INDEX,
)
from .protocol import MAX_CONTROL, MAX_VALUE, MIN_CONTROL

_SOURCE_LINE_RE = re.compile(r"^\s*(\d+)\s*[:=]\s*(.+?)\s*$")


def parse_sources(text: str) -> list[tuple[int, str]]:
    """Parse a source list of ``index: name`` lines.

    Raises ValueError on a malformed line, an index out of range or a
    duplicate index or name.
    """
    sources: list[tuple[int, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if not (match := _SOURCE_LINE_RE.match(line)):
            raise ValueError(f"Expected 'index: name', got {line.strip()!r}")
        index, name = int(match.group(1)), match.group(2)
        if index > MAX_SOURCE_INDEX:
            raise ValueError(f"Source index {index} above {MAX_SOURCE_INDEX}")
        if any(index == i or name == n for i, n in sources):
            raise ValueError(f"Duplicate source {index}: {name}")
        sources.append((index, name))
    return sources


@dataclass(frozen=True, slots=True)
class VolumeScale:
    """Maps between raw fader values, dB and HA volume levels."""

    fader_min_db: float
    fader_max_db: float
    min_db: float
    max_db: float

    def raw_to_db(self, raw: int) -> float:
        """Convert a raw fader value to dB."""
        span = self.fader_max_db - self.fader_min_db
        return self.fader_min_db + raw * span / MAX_VALUE

    def db_to_raw(self, db: float) -> int:
        """Convert dB to a raw fader value, never above ``max_db``."""
        db = min(db, self.max_db, self.fader_max_db)
        db = max(db, self.fader_min_db)
        span = self.fader_max_db - self.fader_min_db
        raw = round((db - self.fader_min_db) * MAX_VALUE / span)
        return max(0, min(raw, self.max_raw))

    @property
    def max_raw(self) -> int:
        """Return the highest raw value ever sent (``max_db``)."""
        db = max(min(self.max_db, self.fader_max_db), self.fader_min_db)
        span = self.fader_max_db - self.fader_min_db
        # Rounded like the DSP's own calibration (0 dB on -72..+12 = 56173).
        return round((db - self.fader_min_db) * MAX_VALUE / span)

    def db_to_level(self, db: float) -> float:
        """Convert dB to an HA volume level (0-1)."""
        level = (db - self.min_db) / (self.max_db - self.min_db)
        return max(0.0, min(1.0, level))

    def level_to_db(self, level: float) -> float:
        """Convert an HA volume level (0-1) to dB."""
        level = max(0.0, min(1.0, level))
        return self.min_db + level * (self.max_db - self.min_db)


@dataclass(frozen=True, slots=True)
class ZoneConfig:
    """Everything a zone entity needs, resolved from options and subentry."""

    name: str
    zone: int
    # Control number per control type; missing = control disabled.
    controls: dict[str, int]
    sources: list[tuple[int, str]]
    scale: VolumeScale
    step_db: float
    turn_on_cap_db: float | None
    power_on_value: int
    power_off_value: int
    mute_on_value: int
    mute_off_value: int
    # Area applied to the zone's device (None = leave unassigned).
    area_id: str | None = None

    def cn(self, control: str) -> int | None:
        """Return the control number for a control type, if enabled."""
        return self.controls.get(control)

    def is_power_on(self, raw: int) -> bool:
        """Decode a raw power value (nearest of the on/off values)."""
        return abs(raw - self.power_on_value) < abs(raw - self.power_off_value)

    def is_muted(self, raw: int) -> bool:
        """Decode a raw mute value (nearest of the on/off values)."""
        return abs(raw - self.mute_on_value) < abs(raw - self.mute_off_value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def zone_control_numbers(
    options: Mapping[str, Any], zone_data: Mapping[str, Any]
) -> dict[str, int]:
    """Return the control number of every enabled control of a zone."""
    enabled = zone_data.get(CONF_CONTROLS, list(CONTROLS))
    zone = int(zone_data[CONF_ZONE])
    controls: dict[str, int] = {}
    for control in CONTROLS:
        if control not in enabled:
            continue
        explicit = zone_data.get(CONTROL_CN_KEYS[control])
        if explicit not in (None, ""):
            cn = int(explicit)
        else:
            cn = int(options[CONTROL_BASE_KEYS[control]]) + zone
        if MIN_CONTROL <= cn <= MAX_CONTROL:
            controls[control] = cn
    return controls


def resolve_zone(
    options: Mapping[str, Any], zone_data: Mapping[str, Any], name: str
) -> ZoneConfig:
    """Combine DSP options and zone subentry data into a ZoneConfig."""
    all_sources = parse_sources(options[CONF_SOURCES])
    subset = zone_data.get(CONF_ZONE_SOURCES) or []
    sources = [s for s in all_sources if not subset or s[1] in subset]

    min_db = _optional_float(zone_data.get(CONF_VOLUME_MIN_DB))
    max_db = _optional_float(zone_data.get(CONF_VOLUME_MAX_DB))
    cap = _optional_float(zone_data.get(CONF_TURN_ON_CAP_DB))
    if cap is None:
        cap = _optional_float(options.get(CONF_TURN_ON_CAP_DB))

    return ZoneConfig(
        name=name,
        zone=int(zone_data[CONF_ZONE]),
        controls=zone_control_numbers(options, zone_data),
        sources=sources,
        scale=VolumeScale(
            fader_min_db=float(options[CONF_FADER_MIN_DB]),
            fader_max_db=float(options[CONF_FADER_MAX_DB]),
            min_db=float(options[CONF_VOLUME_MIN_DB]) if min_db is None else min_db,
            max_db=float(options[CONF_VOLUME_MAX_DB]) if max_db is None else max_db,
        ),
        step_db=float(options[CONF_VOLUME_STEP_DB]),
        turn_on_cap_db=cap,
        power_on_value=int(options[CONF_POWER_ON_VALUE]),
        power_off_value=int(options[CONF_POWER_OFF_VALUE]),
        mute_on_value=int(options[CONF_MUTE_ON_VALUE]),
        mute_off_value=int(options[CONF_MUTE_OFF_VALUE]),
        area_id=zone_data.get(CONF_AREA) or None,
    )
