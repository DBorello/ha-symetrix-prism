"""Config, options and zone subentry flows for Symetrix Prism."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    AreaSelector,
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
import voluptuous as vol

from .const import (
    CONF_AREA,
    CONF_BASE_MUTE,
    CONF_BASE_POWER,
    CONF_BASE_SOURCE,
    CONF_BASE_VOLUME,
    CONF_CN_MUTE,
    CONF_CN_POWER,
    CONF_CN_SOURCE,
    CONF_CN_VOLUME,
    CONF_CONTROLS,
    CONF_CREATE_DEFAULTS,
    CONF_FADER_MAX_DB,
    CONF_FADER_MIN_DB,
    CONF_MODEL,
    CONF_MUTE_OFF_VALUE,
    CONF_MUTE_ON_VALUE,
    CONF_POWER_OFF_VALUE,
    CONF_POWER_ON_VALUE,
    CONF_PUSH_INTERVAL,
    CONF_RESYNC_INTERVAL,
    CONF_SOURCES,
    CONF_TURN_ON_CAP_DB,
    CONF_VOLUME_MAX_DB,
    CONF_VOLUME_MIN_DB,
    CONF_VOLUME_STEP_DB,
    CONF_ZONE,
    CONF_ZONE_SOURCES,
    CONTROLS,
    DEFAULTS,
    DOMAIN,
    MAX_ZONE,
    MIN_ZONE,
    OPTION_KEYS,
    SUBENTRY_ZONE,
    default_options,
    zone_device_identifier,
)
from .models import parse_sources, zone_control_numbers
from .protocol import (
    MAX_CONTROL,
    MAX_VALUE,
    MIN_CONTROL,
    PrismClient,
    PrismError,
)

_LOGGER = logging.getLogger(__name__)

SECTION_LAYOUT = "layout"
SECTION_VALUES = "values"
SECTION_VOLUME = "volume"
SECTION_ADVANCED = "advanced"

OPTION_SECTIONS: dict[str, tuple[str, ...]] = {
    SECTION_LAYOUT: (
        CONF_BASE_POWER,
        CONF_BASE_SOURCE,
        CONF_BASE_VOLUME,
        CONF_BASE_MUTE,
    ),
    SECTION_VALUES: (
        CONF_POWER_ON_VALUE,
        CONF_POWER_OFF_VALUE,
        CONF_MUTE_ON_VALUE,
        CONF_MUTE_OFF_VALUE,
    ),
    SECTION_VOLUME: (
        CONF_FADER_MIN_DB,
        CONF_FADER_MAX_DB,
        CONF_VOLUME_MIN_DB,
        CONF_VOLUME_MAX_DB,
        CONF_VOLUME_STEP_DB,
        CONF_TURN_ON_CAP_DB,
    ),
    SECTION_ADVANCED: (CONF_MODEL, CONF_RESYNC_INTERVAL, CONF_PUSH_INTERVAL),
}
ZONE_ADVANCED_KEYS = (
    CONF_CN_POWER,
    CONF_CN_SOURCE,
    CONF_CN_VOLUME,
    CONF_CN_MUTE,
    CONF_VOLUME_MIN_DB,
    CONF_VOLUME_MAX_DB,
    CONF_TURN_ON_CAP_DB,
)
# Keys that may be left blank.
OPTIONAL_KEYS = {
    CONF_TURN_ON_CAP_DB,
    CONF_CN_POWER,
    CONF_CN_SOURCE,
    CONF_CN_VOLUME,
    CONF_CN_MUTE,
    CONF_VOLUME_MIN_DB,
    CONF_VOLUME_MAX_DB,
}


def _number(
    minimum: float,
    maximum: float,
    step: float = 1,
    unit: str | None = None,
) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=NumberSelectorMode.BOX,
            **({"unit_of_measurement": unit} if unit else {}),
        )
    )


def _cn() -> NumberSelector:
    return _number(MIN_CONTROL, MAX_CONTROL)


def _raw() -> NumberSelector:
    return _number(0, MAX_VALUE)


def _db() -> NumberSelector:
    return _number(-120, 24, 0.5, "dB")


SELECTORS: dict[str, Any] = {
    CONF_BASE_POWER: _number(0, MAX_CONTROL - MAX_ZONE),
    CONF_BASE_SOURCE: _number(0, MAX_CONTROL - MAX_ZONE),
    CONF_BASE_VOLUME: _number(0, MAX_CONTROL - MAX_ZONE),
    CONF_BASE_MUTE: _number(0, MAX_CONTROL - MAX_ZONE),
    CONF_POWER_ON_VALUE: _raw(),
    CONF_POWER_OFF_VALUE: _raw(),
    CONF_MUTE_ON_VALUE: _raw(),
    CONF_MUTE_OFF_VALUE: _raw(),
    CONF_FADER_MIN_DB: _db(),
    CONF_FADER_MAX_DB: _db(),
    CONF_VOLUME_MIN_DB: _db(),
    CONF_VOLUME_MAX_DB: _db(),
    CONF_VOLUME_STEP_DB: _number(0.5, 20, 0.5, "dB"),
    CONF_TURN_ON_CAP_DB: _db(),
    CONF_MODEL: TextSelector(),
    CONF_RESYNC_INTERVAL: _number(30, 3600, 1, "s"),
    CONF_PUSH_INTERVAL: _number(20, 30000, 10, "ms"),
    CONF_SOURCES: TextSelector(TextSelectorConfig(multiline=True)),
    CONF_CN_POWER: _cn(),
    CONF_CN_SOURCE: _cn(),
    CONF_CN_VOLUME: _cn(),
    CONF_CN_MUTE: _cn(),
}
INT_KEYS = {
    CONF_BASE_POWER,
    CONF_BASE_SOURCE,
    CONF_BASE_VOLUME,
    CONF_BASE_MUTE,
    CONF_POWER_ON_VALUE,
    CONF_POWER_OFF_VALUE,
    CONF_MUTE_ON_VALUE,
    CONF_MUTE_OFF_VALUE,
    CONF_RESYNC_INTERVAL,
    CONF_PUSH_INTERVAL,
    CONF_CN_POWER,
    CONF_CN_SOURCE,
    CONF_CN_VOLUME,
    CONF_CN_MUTE,
    CONF_ZONE,
}


def _field(key: str, values: Mapping[str, Any]) -> vol.Marker:
    """Build a schema key pre-filled from values."""
    value = values.get(key)
    if key in OPTIONAL_KEYS:
        return vol.Optional(key, description={"suggested_value": value})
    return vol.Required(key, default=value)


def _section_schema(keys: Iterable[str], values: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema({_field(key, values): SELECTORS[key] for key in keys})


def _flatten(user_input: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """Merge section dicts into one flat dict, normalising types and blanks."""
    flat: dict[str, Any] = {}
    for key, value in user_input.items():
        if isinstance(value, Mapping):
            flat.update(value)
        else:
            flat[key] = value
    result: dict[str, Any] = {}
    for key in keys:
        value = flat.get(key)
        if value in (None, ""):
            result[key] = None
        elif key in INT_KEYS:
            result[key] = int(value)
        elif isinstance(value, (int, float)):
            result[key] = float(value)
        else:
            result[key] = value
    return result


def _validate_volume(
    values: Mapping[str, Any], options: Mapping[str, Any], errors: dict[str, str]
) -> None:
    """Check a dB range against the fader range."""
    fader_min = float(options[CONF_FADER_MIN_DB])
    fader_max = float(options[CONF_FADER_MAX_DB])
    if fader_min >= fader_max:
        errors["base"] = "invalid_fader_range"
        return
    min_db = values.get(CONF_VOLUME_MIN_DB)
    max_db = values.get(CONF_VOLUME_MAX_DB)
    min_db = float(options[CONF_VOLUME_MIN_DB]) if min_db is None else min_db
    max_db = float(options[CONF_VOLUME_MAX_DB]) if max_db is None else max_db
    if not fader_min <= min_db < max_db <= fader_max:
        errors["base"] = "invalid_volume_range"
        return
    cap = values.get(CONF_TURN_ON_CAP_DB)
    if cap is not None and not fader_min <= cap <= max_db:
        errors["base"] = "invalid_turn_on_cap"


def validate_options(options: Mapping[str, Any]) -> dict[str, str]:
    """Validate DSP options; returns form errors."""
    errors: dict[str, str] = {}
    try:
        if not parse_sources(options[CONF_SOURCES] or ""):
            errors[CONF_SOURCES] = "no_sources"
    except ValueError:
        errors[CONF_SOURCES] = "invalid_sources"
    if (
        options[CONF_POWER_ON_VALUE] == options[CONF_POWER_OFF_VALUE]
        or options[CONF_MUTE_ON_VALUE] == options[CONF_MUTE_OFF_VALUE]
    ):
        errors["base"] = "on_equals_off"
    _validate_volume(options, options, errors)
    return errors


async def async_check_controls(
    host: str, port: int, controls: Iterable[int]
) -> list[int]:
    """Connect to a DSP and return the controls that read as unassigned.

    Raises PrismError if the DSP cannot be reached.
    """
    client = PrismClient(host, port)
    try:
        await client.connect()
        values = await client.read_many(controls) if controls else {}
    finally:
        await client.close()
    return sorted(cn for cn, value in values.items() if value is None)


def _default_zone_subentries() -> list[ConfigSubentryData]:
    return [
        ConfigSubentryData(
            data={CONF_ZONE: zone[CONF_ZONE], CONF_CONTROLS: list(CONTROLS)},
            subentry_type=SUBENTRY_ZONE,
            title=zone[CONF_NAME],
            unique_id=f"zone_{zone[CONF_ZONE]}",
        )
        for zone in DEFAULTS["zones"]
    ]


class SymetrixConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Symetrix DSP."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._pending: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SymetrixOptionsFlow:
        """Return the options flow."""
        return SymetrixOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentry types (zones)."""
        return {SUBENTRY_ZONE: ZoneSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the DSP's name and address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            options = default_options()
            subentries = (
                _default_zone_subentries() if user_input[CONF_CREATE_DEFAULTS] else []
            )
            controls = [
                cn
                for subentry in subentries
                for cn in zone_control_numbers(options, subentry["data"]).values()
            ]
            try:
                unassigned = await async_check_controls(host, port, controls)
            except PrismError as err:
                _LOGGER.debug("Cannot connect to %s:%s: %s", host, port, err)
                errors["base"] = "cannot_connect"
            else:
                self._pending = {
                    "title": user_input[CONF_NAME],
                    "data": {CONF_HOST: host, CONF_PORT: port},
                    "options": options,
                    "subentries": subentries,
                }
                if unassigned:
                    return await self.async_step_unassigned(unassigned=unassigned)
                return self._create_pending()

        values = user_input or {
            CONF_NAME: DEFAULTS[CONF_NAME],
            CONF_HOST: DEFAULTS[CONF_HOST],
            CONF_PORT: DEFAULTS[CONF_PORT],
            CONF_CREATE_DEFAULTS: True,
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=values[CONF_NAME]): TextSelector(),
                    vol.Required(
                        CONF_HOST, description={"suggested_value": values[CONF_HOST]}
                    ): TextSelector(),
                    vol.Required(CONF_PORT, default=values[CONF_PORT]): _number(
                        1, 65535
                    ),
                    vol.Required(
                        CONF_CREATE_DEFAULTS, default=values[CONF_CREATE_DEFAULTS]
                    ): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_unassigned(
        self,
        user_input: dict[str, Any] | None = None,
        unassigned: list[int] | None = None,
    ) -> ConfigFlowResult:
        """Warn that some zone controls are not assigned on the DSP."""
        if user_input is not None:
            return self._create_pending()
        return self.async_show_form(
            step_id="unassigned",
            description_placeholders={
                "controls": ", ".join(str(cn) for cn in unassigned or [])
            },
        )

    def _create_pending(self) -> ConfigFlowResult:
        pending = self._pending
        return self.async_create_entry(
            title=pending["title"],
            data=pending["data"],
            options=pending["options"],
            subentries=pending["subentries"],
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the DSP's address."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            unique_id = f"{host}:{port}"
            if any(
                other.unique_id == unique_id and other.entry_id != entry.entry_id
                for other in self._async_current_entries(include_ignore=False)
            ):
                return self.async_abort(reason="already_configured")
            try:
                await async_check_controls(host, port, [])
            except PrismError:
                errors["base"] = "cannot_connect"
            else:
                # The entry's update listener reloads it.
                return self.async_update_and_abort(
                    entry,
                    unique_id=unique_id,
                    data_updates={CONF_HOST: host, CONF_PORT: port},
                )
        values = user_input or entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=values[CONF_HOST]): TextSelector(),
                    vol.Required(CONF_PORT, default=values[CONF_PORT]): _number(
                        1, 65535
                    ),
                }
            ),
            errors=errors,
        )


class SymetrixOptionsFlow(OptionsFlow):
    """Edit a DSP's control layout, value mappings and sources."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        errors: dict[str, str] = {}
        values: dict[str, Any] = {**default_options(), **self.config_entry.options}
        if user_input is not None:
            values = _flatten(user_input, OPTION_KEYS)
            errors = validate_options(values)
            if not errors:
                return self.async_create_entry(data=values)

        schema: dict[vol.Marker, Any] = {
            vol.Required(CONF_SOURCES, default=values[CONF_SOURCES]): SELECTORS[
                CONF_SOURCES
            ]
        }
        for name, keys in OPTION_SECTIONS.items():
            schema[vol.Required(name)] = section(
                _section_schema(keys, values),
                {"collapsed": name == SECTION_ADVANCED},
            )
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema), errors=errors
        )


class ZoneSubentryFlow(ConfigSubentryFlow):
    """Add or edit a zone."""

    def __init__(self) -> None:
        """Initialise the flow."""
        self._pending: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a zone."""
        return await self._async_zone_form("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a zone."""
        return await self._async_zone_form("reconfigure", user_input)

    def _existing_zones(self, exclude: str | None) -> set[int]:
        return {
            int(subentry.data[CONF_ZONE])
            for subentry_id, subentry in self._get_entry().subentries.items()
            if subentry.subentry_type == SUBENTRY_ZONE and subentry_id != exclude
        }

    async def _async_zone_form(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        options = {**default_options(), **entry.options}
        source_names = [name for _, name in parse_sources(options[CONF_SOURCES])]
        editing = self.source == SOURCE_RECONFIGURE
        subentry = self._get_reconfigure_subentry() if editing else None
        existing = self._existing_zones(subentry.subentry_id if subentry else None)

        errors: dict[str, str] = {}
        if user_input is not None:
            data = _flatten(user_input, ZONE_ADVANCED_KEYS)
            data[CONF_ZONE] = int(user_input[CONF_ZONE])
            data[CONF_CONTROLS] = list(user_input.get(CONF_CONTROLS, []))
            data[CONF_ZONE_SOURCES] = list(user_input.get(CONF_ZONE_SOURCES, []))
            data[CONF_AREA] = user_input.get(CONF_AREA) or None
            name = user_input[CONF_NAME].strip()
            if data[CONF_ZONE] in existing:
                errors[CONF_ZONE] = "zone_exists"
            _validate_volume(data, options, errors)
            if not errors:
                self._pending = {"title": name, "data": data}
                unassigned = await self._async_unassigned(options, data)
                if unassigned:
                    return await self.async_step_unassigned(unassigned=unassigned)
                return self._save_pending()
            values: dict[str, Any] = {**data, CONF_NAME: name}
        elif subentry is not None:
            values = {**subentry.data, CONF_NAME: subentry.title}
        else:
            zone = next(z for z in range(MIN_ZONE, MAX_ZONE + 1) if z not in existing)
            values = {
                CONF_NAME: f"Zone {zone}",
                CONF_ZONE: zone,
                CONF_CONTROLS: list(CONTROLS),
                CONF_ZONE_SOURCES: [],
            }

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=values[CONF_NAME]): TextSelector(),
                vol.Required(CONF_ZONE, default=values[CONF_ZONE]): _number(
                    MIN_ZONE, MAX_ZONE
                ),
                vol.Optional(
                    CONF_CONTROLS, default=values.get(CONF_CONTROLS, list(CONTROLS))
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(CONTROLS),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key="control",
                    )
                ),
                vol.Optional(
                    CONF_ZONE_SOURCES, default=values.get(CONF_ZONE_SOURCES) or []
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=source_names,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(
                    CONF_AREA, description={"suggested_value": values.get(CONF_AREA)}
                ): AreaSelector(),
                vol.Required(SECTION_ADVANCED): section(
                    vol.Schema(
                        {
                            vol.Optional(
                                key, description={"suggested_value": values.get(key)}
                            ): SELECTORS[key]
                            for key in ZONE_ADVANCED_KEYS
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders={"dsp": entry.title},
        )

    async def _async_unassigned(
        self, options: Mapping[str, Any], data: Mapping[str, Any]
    ) -> list[int]:
        """Return the zone's controls that read as unassigned on the DSP."""
        entry = self._get_entry()
        controls = zone_control_numbers(options, data).values()
        try:
            return await async_check_controls(
                entry.data[CONF_HOST], int(entry.data[CONF_PORT]), controls
            )
        except PrismError as err:
            # Zones can be set up while the DSP is offline.
            _LOGGER.debug("Cannot check zone controls: %s", err)
            return []

    async def async_step_unassigned(
        self,
        user_input: dict[str, Any] | None = None,
        unassigned: list[int] | None = None,
    ) -> SubentryFlowResult:
        """Warn that some of the zone's controls are not assigned on the DSP."""
        if user_input is not None:
            return self._save_pending()
        return self.async_show_form(
            step_id="unassigned",
            description_placeholders={
                "controls": ", ".join(str(cn) for cn in unassigned or [])
            },
        )

    def _save_pending(self) -> SubentryFlowResult:
        title: str = self._pending["title"]
        data: dict[str, Any] = self._pending["data"]
        unique_id = f"zone_{data[CONF_ZONE]}"
        if self.source == SOURCE_RECONFIGURE:
            subentry = self._get_reconfigure_subentry()
            if data.get(CONF_AREA) != subentry.data.get(CONF_AREA):
                self._move_zone_device(subentry.subentry_id, data.get(CONF_AREA))
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=title,
                data=data,
                unique_id=unique_id,
            )
        return self.async_create_entry(title=title, data=data, unique_id=unique_id)

    def _move_zone_device(self, subentry_id: str, area_id: str | None) -> None:
        """Apply an area changed in the zone form to the zone's device."""
        entry = self._get_entry()
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device_by_identifier(
            zone_device_identifier(entry.entry_id, subentry_id), entry.entry_id
        )
        if device is not None:
            device_registry.async_update_device(device.id, area_id=area_id)
