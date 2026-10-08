"""Tests for zone areas."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.symetrix_prism.const import (
    CONF_AREA,
    CONF_ZONE,
    DOMAIN,
    SUBENTRY_ZONE,
    zone_device_identifier,
)
from tools.mock_prism import MockPrism

from .conftest import TEST_OPTIONS, zone_subentry


def zone_area(hass: HomeAssistant, entry: MockConfigEntry, title: str) -> str | None:
    """Return the area of a zone's device."""
    subentry_id = next(sid for sid, s in entry.subentries.items() if s.title == title)
    device = dr.async_get(hass).async_get_device_by_identifier(
        zone_device_identifier(entry.entry_id, subentry_id), entry.entry_id
    )
    assert device is not None
    return device.area_id


async def setup_with_area(
    hass: HomeAssistant, mock_prism: MockPrism, area_id: str
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="DSP-1",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: mock_prism.port},
        options=TEST_OPTIONS,
        subentries_data=[
            zone_subentry(1, "Kitchen", **{CONF_AREA: area_id}),
            zone_subentry(2, "Office"),
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_area_applied_on_setup(
    hass: HomeAssistant, mock_prism: MockPrism
) -> None:
    bedroom = ar.async_get(hass).async_create("Main Floor")
    entry = await setup_with_area(hass, mock_prism, bedroom.id)
    assert zone_area(hass, entry, "Kitchen") == bedroom.id
    assert zone_area(hass, entry, "Office") is None


async def test_area_set_on_device_page_is_kept(
    hass: HomeAssistant, mock_prism: MockPrism
) -> None:
    areas = ar.async_get(hass)
    bedroom = areas.async_create("Main Floor")
    office = areas.async_create("Office")
    entry = await setup_with_area(hass, mock_prism, bedroom.id)
    subentry_id = next(s for s, z in entry.subentries.items() if z.title == "Kitchen")
    devices = dr.async_get(hass)
    device = devices.async_get_device_by_identifier(
        zone_device_identifier(entry.entry_id, subentry_id), entry.entry_id
    )
    assert device is not None
    devices.async_update_device(device.id, area_id=office.id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert zone_area(hass, entry, "Kitchen") == office.id


async def test_unknown_area_ignored(hass: HomeAssistant, mock_prism: MockPrism) -> None:
    entry = await setup_with_area(hass, mock_prism, "deleted_area")
    assert zone_area(hass, entry, "Kitchen") is None


async def test_add_zone_with_area(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_prism: MockPrism
) -> None:
    patio = ar.async_get(hass).async_create("Patio")
    for cn in (2003, 2103, 2203, 2303):
        mock_prism.values[cn] = 0
    result = await hass.config_entries.subentries.async_init(
        (setup_integration.entry_id, SUBENTRY_ZONE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Patio", CONF_ZONE: 3, CONF_AREA: patio.id, "advanced": {}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert zone_area(hass, setup_integration, "Patio") == patio.id


async def test_zone_form_moves_device(
    hass: HomeAssistant, mock_prism: MockPrism
) -> None:
    areas = ar.async_get(hass)
    bedroom = areas.async_create("Main Floor")
    office = areas.async_create("Office")
    entry = await setup_with_area(hass, mock_prism, bedroom.id)
    subentry_id = next(s for s, z in entry.subentries.items() if z.title == "Kitchen")

    async def reconfigure(area: str | None) -> None:
        result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
        user_input = {CONF_NAME: "Kitchen", CONF_ZONE: 1, "advanced": {}}
        if area is not None:
            user_input[CONF_AREA] = area
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], user_input
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()

    await reconfigure(office.id)
    assert zone_area(hass, entry, "Kitchen") == office.id
    assert entry.subentries[subentry_id].data[CONF_AREA] == office.id
    # Clearing the area in the form clears it on the device.
    await reconfigure(None)
    assert zone_area(hass, entry, "Kitchen") is None
