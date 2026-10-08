"""Fixtures for Symetrix Prism tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.symetrix_prism.const import (
    CONF_CONTROLS,
    CONF_ZONE,
    CONTROLS,
    DOMAIN,
    SUBENTRY_ZONE,
    default_options,
)
from tools.mock_prism import MockPrism

# Default options with two named sources, as most tests select by name.
TEST_OPTIONS = {**default_options(), "sources": "0: alpha\n1: beta"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable custom integrations in every test."""


@pytest.fixture(autouse=True)
def fast_debounce() -> Generator[None]:
    """Shorten the volume debounce and setup connect timeout."""
    with (
        patch(
            "custom_components.symetrix_prism.media_player.VOLUME_DEBOUNCE_SECONDS",
            0.02,
        ),
        patch("custom_components.symetrix_prism.SETUP_CONNECT_TIMEOUT", 0.5),
    ):
        yield


@pytest.fixture
async def mock_prism(socket_enabled: None) -> AsyncIterator[MockPrism]:
    """Start a mock DSP with the default layout on localhost."""
    prism = MockPrism(push_interval_ms=20)
    await prism.start()
    yield prism
    await prism.stop()


def zone_subentry(zone: int, name: str, **data: Any) -> ConfigSubentryData:
    """Build zone subentry data."""
    return ConfigSubentryData(
        data={CONF_ZONE: zone, CONF_CONTROLS: list(CONTROLS), **data},
        subentry_type=SUBENTRY_ZONE,
        title=name,
        unique_id=f"zone_{zone}",
    )


@pytest.fixture
def config_entry(mock_prism: MockPrism) -> MockConfigEntry:
    """Config entry for the mock DSP with zones Kitchen and Office."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="DSP-1",
        unique_id=f"127.0.0.1:{mock_prism.port}",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: mock_prism.port},
        options=TEST_OPTIONS,
        subentries_data=[zone_subentry(1, "Kitchen"), zone_subentry(2, "Office")],
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
