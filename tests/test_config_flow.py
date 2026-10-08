"""Tests for the config, options and zone subentry flows."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.symetrix_prism.const import (
    CONF_CN_VOLUME,
    CONF_CONTROLS,
    CONF_CREATE_DEFAULTS,
    CONF_SOURCES,
    CONF_TURN_ON_CAP_DB,
    CONF_VOLUME_MAX_DB,
    CONF_ZONE,
    CONF_ZONE_SOURCES,
    DOMAIN,
    SUBENTRY_ZONE,
    default_options,
)
from tools.mock_prism import MockPrism

from .test_protocol import free_port


async def _user_flow(
    hass: HomeAssistant, port: int, create_defaults: bool = True
) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return dict(
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "DSP-1",
                CONF_HOST: "127.0.0.1",
                CONF_PORT: port,
                CONF_CREATE_DEFAULTS: create_defaults,
            },
        )
    )


async def test_user_form_prefilled(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    schema = result["data_schema"]
    defaults = {
        str(key): key.default() for key in schema.schema if str(key) != CONF_HOST
    }
    assert defaults == {
        CONF_NAME: "Symetrix DSP",
        CONF_PORT: 48631,
        CONF_CREATE_DEFAULTS: True,
    }
    # The host must be entered.
    with pytest.raises(vol.Invalid):
        schema({CONF_NAME: "x", CONF_PORT: 48631, CONF_CREATE_DEFAULTS: True})


async def test_user_flow_creates_defaults(
    hass: HomeAssistant, mock_prism: MockPrism
) -> None:
    result = await _user_flow(hass, mock_prism.port)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == "DSP-1"
    assert entry.unique_id == f"127.0.0.1:{mock_prism.port}"
    assert entry.data == {CONF_HOST: "127.0.0.1", CONF_PORT: mock_prism.port}
    assert entry.options == default_options()
    zones = {s.title: dict(s.data) for s in entry.subentries.values()}
    assert zones == {
        "Zone 1": {CONF_ZONE: 1, CONF_CONTROLS: ["power", "source", "volume", "mute"]},
        "Zone 2": {CONF_ZONE: 2, CONF_CONTROLS: ["power", "source", "volume", "mute"]},
    }
    await hass.async_block_till_done()
    assert hass.states.get("media_player.zone_1") is not None
    assert hass.states.get("media_player.zone_2") is not None


async def test_user_flow_without_defaults(
    hass: HomeAssistant, mock_prism: MockPrism
) -> None:
    result = await _user_flow(hass, mock_prism.port, create_defaults=False)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].subentries == {}


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    result = await _user_flow(hass, free_port())
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_warns_on_unassigned(
    hass: HomeAssistant, mock_prism: MockPrism
) -> None:
    del mock_prism.values[2302]
    result = await _user_flow(hass, mock_prism.port)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "unassigned"
    assert result["description_placeholders"] == {"controls": "2302"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_user_flow_already_configured(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    result = await _user_flow(hass, mock_prism.port)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    other = MockPrism(push_interval_ms=20)
    await other.start()
    try:
        result = await setup_integration.start_reconfigure_flow(hass)
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: free_port()}
        )
        assert result["errors"] == {"base": "cannot_connect"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: other.port}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert setup_integration.data[CONF_PORT] == other.port
        assert setup_integration.unique_id == f"127.0.0.1:{other.port}"
        assert hass.states.get("media_player.kitchen").state == "on"
        assert other.connections == 2  # flow check + integration
    finally:
        await hass.config_entries.async_unload(setup_integration.entry_id)
        await other.stop()


async def test_reconfigure_rejects_other_entry(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="10.0.0.9:48631").add_to_hass(hass)
    result = await setup_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "10.0.0.9", CONF_PORT: 48631}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def _options_input(**changes: Any) -> dict[str, Any]:
    options = {**default_options(), **changes}
    return {
        CONF_SOURCES: options[CONF_SOURCES],
        "layout": {
            k: options[k]
            for k in ("base_power", "base_source", "base_volume", "base_mute")
        },
        "values": {
            k: options[k]
            for k in (
                "power_on_value",
                "power_off_value",
                "mute_on_value",
                "mute_off_value",
            )
        },
        "volume": {
            k: options[k]
            for k in (
                "fader_min_db",
                "fader_max_db",
                "volume_min_db",
                "volume_max_db",
                "volume_step_db",
                "turn_on_cap_db",
            )
            if options[k] is not None
        },
        "advanced": {
            k: options[k] for k in ("model", "resync_interval", "push_interval")
        },
    }


async def test_options_flow(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _options_input(sources="0: alpha\n1: beta\n2: gamma", turn_on_cap_db=None),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert setup_integration.options[CONF_SOURCES] == "0: alpha\n1: beta\n2: gamma"
    assert setup_integration.options[CONF_TURN_ON_CAP_DB] is None
    assert isinstance(setup_integration.options["base_power"], int)
    state = hass.states.get("media_player.kitchen")
    assert state.attributes["source_list"] == ["alpha", "beta", "gamma"]


@pytest.mark.parametrize(
    ("changes", "errors"),
    [
        ({"sources": "alpha\nbeta"}, {CONF_SOURCES: "invalid_sources"}),
        ({"sources": "0: a\n0: b"}, {CONF_SOURCES: "invalid_sources"}),
        ({"sources": " "}, {CONF_SOURCES: "no_sources"}),
        ({"power_on_value": 65535}, {"base": "on_equals_off"}),
        ({"fader_min_db": 12}, {"base": "invalid_fader_range"}),
        ({"volume_max_db": 20}, {"base": "invalid_volume_range"}),
        ({"volume_min_db": 0}, {"base": "invalid_volume_range"}),
        ({"turn_on_cap_db": 6}, {"base": "invalid_turn_on_cap"}),
    ],
)
async def test_options_flow_errors(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    changes: dict[str, Any],
    errors: dict[str, str],
) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _options_input(**changes)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors


async def _zone_flow(
    hass: HomeAssistant, entry: MockConfigEntry, user_input: dict[str, Any]
) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_ZONE), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return dict(
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input
        )
    )


async def test_add_zone(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    for cn in (2003, 2103, 2203, 2303):
        mock_prism.values[cn] = 0
    result = await hass.config_entries.subentries.async_init(
        (setup_integration.entry_id, SUBENTRY_ZONE), context={"source": SOURCE_USER}
    )
    # The next free zone number is suggested.
    assert result["data_schema"]({CONF_NAME: "x", "advanced": {}})[CONF_ZONE] == 3
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Patio",
            CONF_ZONE: 3,
            CONF_CONTROLS: ["power", "volume"],
            CONF_ZONE_SOURCES: ["beta"],
            "advanced": {CONF_VOLUME_MAX_DB: -10},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    subentry = next(
        s for s in setup_integration.subentries.values() if s.title == "Patio"
    )
    assert subentry.unique_id == "zone_3"
    assert subentry.data[CONF_VOLUME_MAX_DB] == -10
    assert subentry.data[CONF_CN_VOLUME] is None
    state = hass.states.get("media_player.patio")
    assert state is not None
    assert "source_list" not in state.attributes


async def test_add_zone_duplicate_number(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    result = await _zone_flow(
        hass, setup_integration, {CONF_NAME: "Again", CONF_ZONE: 1, "advanced": {}}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ZONE: "zone_exists"}


async def test_add_zone_bad_volume_range(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    result = await _zone_flow(
        hass,
        setup_integration,
        {CONF_NAME: "Loud", CONF_ZONE: 5, "advanced": {CONF_VOLUME_MAX_DB: 20}},
    )
    assert result["errors"] == {"base": "invalid_volume_range"}


async def test_add_zone_warns_on_unassigned(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    result = await _zone_flow(
        hass, setup_integration, {CONF_NAME: "Ghost", CONF_ZONE: 9, "advanced": {}}
    )
    assert result["step_id"] == "unassigned"
    assert result["description_placeholders"] == {"controls": "2009, 2109, 2209, 2309"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_reconfigure_zone(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    mock_prism.values[2250] = 9362
    subentry_id = next(
        sid for sid, s in setup_integration.subentries.items() if s.title == "Office"
    )
    result = await setup_integration.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Back office",
            CONF_ZONE: 2,
            CONF_CONTROLS: ["power", "source", "volume", "mute"],
            "advanced": {CONF_CN_VOLUME: 2250},
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    subentry = setup_integration.subentries[subentry_id]
    assert subentry.title == "Back office"
    assert subentry.data[CONF_CN_VOLUME] == 2250
    # Same entity, same unique id; the device keeps its original name.
    assert hass.states.get("media_player.office") is not None


async def test_zone_can_be_added_while_dsp_offline(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    await mock_prism.stop()
    result = await _zone_flow(
        hass, setup_integration, {CONF_NAME: "Later", CONF_ZONE: 4, "advanced": {}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    # The reload cannot connect, so the entry retries setup later.
    assert setup_integration.state is ConfigEntryState.SETUP_RETRY
