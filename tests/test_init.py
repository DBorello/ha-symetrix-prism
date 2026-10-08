"""Tests for setup, unload, models and diagnostics."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.symetrix_prism.const import default_options
from custom_components.symetrix_prism.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.symetrix_prism.models import (
    VolumeScale,
    parse_sources,
    resolve_zone,
    zone_control_numbers,
)
from tools.mock_prism import MockPrism

from .conftest import TEST_OPTIONS

SCALE = VolumeScale(fader_min_db=-72, fader_max_db=12, min_db=-60, max_db=0)


@pytest.mark.parametrize(
    ("db", "raw"),
    [(-60, 9362), (-40, 24966), (-20, 40569), (0, 56173), (-72, 0)],
)
def test_calibration(db: float, raw: int) -> None:
    """The fader calibration measured on the real Prism."""
    assert SCALE.db_to_raw(db) == raw
    assert SCALE.raw_to_db(raw) == pytest.approx(db, abs=0.01)


def test_volume_ceiling() -> None:
    assert SCALE.max_raw == 56173
    assert SCALE.db_to_raw(12) == 56173
    assert SCALE.db_to_raw(-100) == 0
    assert SCALE.db_to_level(-72) == 0
    assert SCALE.db_to_level(6) == 1
    assert SCALE.level_to_db(0.5) == -30


def test_parse_sources() -> None:
    assert parse_sources("0: alpha\n\n 1 = beta two \n") == [
        (0, "alpha"),
        (1, "beta two"),
    ]
    for bad in ("alpha", "0: a\n1: a", "0: a\n0: b", "100: x"):
        with pytest.raises(ValueError):  # noqa: PT011
            parse_sources(bad)


def test_zone_control_numbers() -> None:
    options = default_options()
    assert zone_control_numbers(options, {"zone": 2}) == {
        "power": 2002,
        "source": 2102,
        "volume": 2202,
        "mute": 2302,
    }
    assert zone_control_numbers(
        options, {"zone": 2, "controls": ["volume", "mute"], "cn_mute": 77}
    ) == {"volume": 2202, "mute": 77}


def test_resolve_zone_power_decoding() -> None:
    zone = resolve_zone(default_options(), {"zone": 1}, "Kitchen")
    assert zone.is_power_on(0)
    assert zone.is_power_on(1)
    assert not zone.is_power_on(65535)
    assert zone.is_muted(65535)
    assert not zone.is_muted(0)
    assert zone.turn_on_cap_db == -30


async def test_setup_and_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    assert setup_integration.state is ConfigEntryState.LOADED
    assert mock_prism.received[0] == "PUE"
    assert "PUI 100" in mock_prism.received
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert setup_integration.state is ConfigEntryState.NOT_LOADED
    await hass.async_block_till_done()
    assert mock_prism.clients == []


async def test_setup_retry_when_offline(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_prism: MockPrism
) -> None:
    await mock_prism.stop()
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_transient_unassigned_keeps_value(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    coordinator = setup_integration.runtime_data
    mock_prism.values[2101] = 1
    await coordinator.async_refresh()
    assert coordinator.data[2101] == 1
    # A selector that keeps reading -0001 keeps its last known value.
    mock_prism.transient_reads = 10
    for client in mock_prism.clients:
        client.transient[2101] = 10
    await coordinator.async_refresh()
    assert coordinator.data[2101] == 1
    assert coordinator.last_update_success


async def test_diagnostics(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    diag = await async_get_config_entry_diagnostics(hass, setup_integration)
    assert diag["entry"]["data"]["host"] == "**REDACTED**"
    assert diag["entry"]["options"] == TEST_OPTIONS
    assert diag["values"]["2201"] == 9362
    assert diag["connected"] is True
    assert diag["connection_stats"]["connects"] == 1
    assert sorted(zone["name"] for zone in diag["zones"].values()) == [
        "Kitchen",
        "Office",
    ]
