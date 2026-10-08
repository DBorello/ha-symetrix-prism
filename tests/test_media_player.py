"""Tests for the zone media players."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_INPUT_SOURCE_LIST,
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    DOMAIN as MP_DOMAIN,
    SERVICE_SELECT_SOURCE,
)
from homeassistant.components.media_player.const import MediaPlayerEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SERVICE_VOLUME_DOWN,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
    SERVICE_VOLUME_UP,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.symetrix_prism.const import (
    CONF_CN_VOLUME,
    CONF_CONTROLS,
    CONF_TURN_ON_CAP_DB,
    CONF_VOLUME_MAX_DB,
    CONF_ZONE_SOURCES,
    DOMAIN,
)
from tools.mock_prism import MockPrism

from .conftest import TEST_OPTIONS, zone_subentry

KITCHEN = "media_player.kitchen"
OFFICE = "media_player.office"
RAW_0DB = 56173
RAW_M20 = 40569
RAW_M30 = 32768
RAW_M40 = 24966
RAW_M36 = 28086
RAW_M24 = 37449
RAW_MIN = 0


async def until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    """Wait until predicate() is true."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def call(hass: HomeAssistant, service: str, **data: Any) -> None:
    """Call a media_player service on Kitchen."""
    await hass.services.async_call(
        MP_DOMAIN, service, {ATTR_ENTITY_ID: KITCHEN, **data}, blocking=True
    )


def state_of(hass: HomeAssistant, entity_id: str = KITCHEN) -> Any:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def test_initial_state(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    state = state_of(hass)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_INPUT_SOURCE] == "alpha"
    assert state.attributes[ATTR_INPUT_SOURCE_LIST] == ["alpha", "beta"]
    # -60 dB on a -72..0 dB scale.
    assert state.attributes[ATTR_MEDIA_VOLUME_LEVEL] == pytest.approx(1 / 6, abs=1e-3)
    assert state.attributes[ATTR_MEDIA_VOLUME_MUTED] is False
    assert state.attributes["volume_db"] == -60.0
    assert state.attributes["zone"] == 1
    assert state.attributes["device_class"] == "speaker"
    features = MediaPlayerEntityFeature(state.attributes[ATTR_SUPPORTED_FEATURES])
    assert features == (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
    )


async def test_devices_and_unique_ids(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    entry = setup_integration
    dsp = device_registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert dsp is not None
    assert dsp.manufacturer == "Symetrix"
    assert dsp.model == "Prism 4x4 (Dante)"
    entity = entity_registry.async_get(KITCHEN)
    assert entity is not None
    subentry_id = next(
        sid for sid, s in entry.subentries.items() if s.title == "Kitchen"
    )
    assert entity.unique_id == f"{entry.entry_id}_{subentry_id}"
    assert entity.config_subentry_id == subentry_id
    zone = device_registry.async_get(entity.device_id)
    assert zone is not None
    assert zone.name == "Kitchen"
    assert zone.via_device_id == dsp.id


async def test_turn_off_and_on(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    mock_prism.values[2201] = RAW_M40
    await call(hass, SERVICE_TURN_OFF)
    assert mock_prism.values[2001] == 65535
    assert state_of(hass).state == STATE_OFF
    # Turning off leaves source, volume and mute alone.
    assert mock_prism.commands("CS") == ["CS 2001 65535"]

    await call(hass, SERVICE_TURN_ON)
    assert mock_prism.values[2001] == 0
    assert state_of(hass).state == STATE_ON
    # Volume -40 dB is below the -30 dB cap, so it is not touched.
    assert mock_prism.commands("CS") == ["CS 2001 65535", "CS 2001 0"]


async def test_turn_on_lowers_volume_to_cap(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    mock_prism.set_value(2201, RAW_M20)
    mock_prism.set_value(2001, 65535)
    await until(lambda: state_of(hass).state == STATE_OFF)
    await call(hass, SERVICE_TURN_ON)
    assert mock_prism.commands("CS") == [f"CS 2201 {RAW_M30}", "CS 2001 0"]
    assert state_of(hass).attributes["volume_db"] == -30.0


async def test_turn_on_cap_ignored_when_already_on(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    mock_prism.set_value(2201, RAW_M20)
    await until(lambda: state_of(hass).attributes["volume_db"] == -20.0)
    await call(hass, SERVICE_TURN_ON)
    assert mock_prism.commands("CS") == ["CS 2001 0"]
    assert mock_prism.values[2201] == RAW_M20


async def test_turn_on_cap_disabled(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_prism: MockPrism
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_TURN_ON_CAP_DB: None}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_prism.set_value(2201, RAW_M20)
    mock_prism.set_value(2001, 65535)
    await until(lambda: state_of(hass).state == STATE_OFF)
    await call(hass, SERVICE_TURN_ON)
    assert mock_prism.commands("CS") == ["CS 2001 0"]


async def test_select_source(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    await call(hass, SERVICE_SELECT_SOURCE, source="beta")
    assert mock_prism.values[2101] == 1
    assert state_of(hass).attributes[ATTR_INPUT_SOURCE] == "beta"
    with pytest.raises(ServiceValidationError):
        await call(hass, SERVICE_SELECT_SOURCE, source="gamma")


async def test_unknown_source_index(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    mock_prism.selectors[2101] = 4
    mock_prism.set_value(2101, 3)
    await until(lambda: ATTR_INPUT_SOURCE not in state_of(hass).attributes)


async def test_mute(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    await call(hass, SERVICE_VOLUME_MUTE, is_volume_muted=True)
    assert mock_prism.values[2301] == 65535
    assert state_of(hass).attributes[ATTR_MEDIA_VOLUME_MUTED] is True
    # Mute is separate from power.
    assert state_of(hass).state == STATE_ON
    await call(hass, SERVICE_VOLUME_MUTE, is_volume_muted=False)
    assert mock_prism.values[2301] == 0


@pytest.mark.parametrize(
    ("level", "raw"),
    [(0.0, RAW_MIN), (1.0, RAW_0DB), (0.5, RAW_M36), (2 / 3, RAW_M24)],
)
async def test_set_volume(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_prism: MockPrism,
    level: float,
    raw: int,
) -> None:
    await call(hass, SERVICE_VOLUME_SET, volume_level=level)
    assert mock_prism.values[2201] == raw
    assert state_of(hass).attributes[ATTR_MEDIA_VOLUME_LEVEL] == pytest.approx(
        level, abs=1e-3
    )


async def test_volume_slider_burst_is_coalesced(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    await asyncio.gather(
        *(
            call(hass, SERVICE_VOLUME_SET, volume_level=level)
            for level in (0.1, 0.2, 0.3, 0.4, 0.5)
        )
    )
    assert mock_prism.commands("CS") == [f"CS 2201 {RAW_M36}"]


async def test_volume_never_above_max(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    mock_prism.set_value(2201, RAW_0DB)
    await until(lambda: state_of(hass).attributes["volume_db"] == 0.0)
    await call(hass, SERVICE_VOLUME_UP)
    await call(hass, SERVICE_VOLUME_SET, volume_level=1.0)
    assert all(int(cmd.split()[2]) <= RAW_0DB for cmd in mock_prism.commands("CS 2201"))


async def test_volume_step(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    mock_prism.set_value(2201, RAW_M30)
    await until(lambda: state_of(hass).attributes["volume_db"] == -30.0)
    await call(hass, SERVICE_VOLUME_UP)
    assert state_of(hass).attributes["volume_db"] == -28.0
    await call(hass, SERVICE_VOLUME_DOWN)
    await call(hass, SERVICE_VOLUME_DOWN)
    assert state_of(hass).attributes["volume_db"] == -32.0
    await until(lambda: mock_prism.values[2201] == 31207)


async def test_volume_step_limits(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    # One step below the 0 dB maximum goes to exactly 0 dB.
    mock_prism.set_value(2201, 55393)  # -1 dB
    await until(lambda: state_of(hass).attributes["volume_db"] == -1.0)
    await call(hass, SERVICE_VOLUME_UP)
    assert mock_prism.values[2201] == RAW_0DB
    # Below the 0% level, volume down does not move it further.
    mock_prism.set_value(2201, 0)  # -72 dB
    await until(lambda: state_of(hass).attributes["volume_db"] == -72.0)
    await call(hass, SERVICE_VOLUME_DOWN)
    assert mock_prism.values[2201] == 0
    # Volume up from below the 0% level steps up rather than jumping.
    await call(hass, SERVICE_VOLUME_UP)
    assert state_of(hass).attributes["volume_db"] == -70.0


async def test_push_updates_state(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    mock_prism.set_value(2102, 1)
    mock_prism.set_value(2302, 65535)
    mock_prism.set_value(2202, RAW_M40)
    await until(lambda: state_of(hass, OFFICE).attributes["volume_db"] == -40.0)
    state = state_of(hass, OFFICE)
    assert state.attributes[ATTR_INPUT_SOURCE] == "beta"
    assert state.attributes[ATTR_MEDIA_VOLUME_MUTED] is True
    mock_prism.set_value(2002, 65535)
    await until(lambda: state_of(hass, OFFICE).state == STATE_OFF)
    assert state_of(hass).state == STATE_ON


async def test_disconnect_and_recover(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    await mock_prism.drop_clients()
    await until(lambda: state_of(hass).state == STATE_UNAVAILABLE)
    # Changed while disconnected; picked up by the resync on reconnect.
    mock_prism.values[2201] = RAW_M40
    await until(lambda: state_of(hass).state == STATE_ON, timeout=5)
    await until(lambda: state_of(hass).attributes["volume_db"] == -40.0)


async def test_nak_raises(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    del mock_prism.values[2301]
    with pytest.raises(HomeAssistantError, match="did not accept"):
        await call(hass, SERVICE_VOLUME_MUTE, is_volume_muted=True)


async def test_zone_overrides(hass: HomeAssistant, mock_prism: MockPrism) -> None:
    mock_prism.values[2250] = RAW_M20
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="DSP-1",
        data={"host": "127.0.0.1", "port": mock_prism.port},
        options=TEST_OPTIONS,
        subentries_data=[
            zone_subentry(
                1,
                "Kitchen",
                **{
                    CONF_CONTROLS: ["source", "volume"],
                    CONF_ZONE_SOURCES: ["beta"],
                    CONF_CN_VOLUME: 2250,
                    CONF_VOLUME_MAX_DB: -10.0,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = state_of(hass)
    # No power control: always on, no turn on/off.
    assert state.state == STATE_ON
    features = MediaPlayerEntityFeature(state.attributes[ATTR_SUPPORTED_FEATURES])
    assert features == (
        MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
    )
    assert state.attributes[ATTR_INPUT_SOURCE_LIST] == ["beta"]
    # Source index 0 (alpha) is not in this zone's list.
    assert ATTR_INPUT_SOURCE not in state.attributes
    assert state.attributes["volume_db"] == -20.0
    await call(hass, SERVICE_VOLUME_SET, volume_level=1.0)
    assert mock_prism.values[2250] == 48371  # -10 dB
    await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
