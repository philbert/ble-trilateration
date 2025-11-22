"""Tests for Bermuda number entities."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import (
    CONF_ATTENUATION,
    CONF_MAX_RADIUS,
    CONF_RSSI_OFFSETS,
    DEFAULT_ATTENUATION,
    DEFAULT_MAX_RADIUS,
    DOMAIN,
    NAME,
)

from .const import MOCK_CONFIG


async def setup_integration(hass):
    """Set up the Bermuda config entry with coordinator refresh mocked."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-number", title=NAME)
    entry.add_to_hass(hass)

    with patch("custom_components.bermuda.BermudaDataUpdateCoordinator.async_refresh"):
        assert await async_setup_component(hass, DOMAIN, {})

    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    return entry


def _create_scanner(coordinator, address: str) -> BermudaDevice:
    """Helper to register a scanner device with the coordinator."""
    scanner = BermudaDevice(address, coordinator)
    scanner._is_scanner = True  # noqa: SLF001 - test helper to mark as scanner
    coordinator.devices[scanner.address] = scanner
    coordinator.scanner_list_add(scanner)
    return scanner


async def test_scanner_config_numbers_created(hass) -> None:
    """Ensure RSSI offset, attenuation and max radius entities appear for scanners."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:01")
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    rssi_unique = f"{scanner.unique_id}_rssi_offset"
    attenuation_unique = f"{scanner.unique_id}_attenuation"
    radius_unique = f"{scanner.unique_id}_max_radius"

    rssi_entity = ent_reg.async_get_entity_id("number", DOMAIN, rssi_unique)
    attenuation_entity = ent_reg.async_get_entity_id("number", DOMAIN, attenuation_unique)
    radius_entity = ent_reg.async_get_entity_id("number", DOMAIN, radius_unique)

    assert rssi_entity is not None
    assert attenuation_entity is not None
    assert radius_entity is not None

    rssi_state = hass.states.get(rssi_entity)
    attenuation_state = hass.states.get(attenuation_entity)
    radius_state = hass.states.get(radius_entity)

    assert rssi_state is not None
    assert attenuation_state is not None
    assert radius_state is not None

    assert float(rssi_state.state) == pytest.approx(0)
    assert float(attenuation_state.state) == pytest.approx(DEFAULT_ATTENUATION)
    assert float(radius_state.state) == pytest.approx(DEFAULT_MAX_RADIUS)


async def test_scanner_config_numbers_use_existing_settings(hass) -> None:
    """Ensure entities inherit coordinator option defaults and legacy offsets."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    coordinator.options[CONF_ATTENUATION] = 4.2
    coordinator.options[CONF_MAX_RADIUS] = 12.5
    coordinator.options[CONF_RSSI_OFFSETS] = {}
    scanner_address = "aa:bb:cc:dd:ee:02"
    coordinator.options[CONF_RSSI_OFFSETS][scanner_address] = 7
    scanner = _create_scanner(coordinator, scanner_address)

    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    rssi_entity = ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_rssi_offset")
    attenuation_entity = ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_attenuation")
    radius_entity = ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_max_radius")

    assert rssi_entity is not None
    assert attenuation_entity is not None
    assert radius_entity is not None

    rssi_state = hass.states.get(rssi_entity)
    attenuation_state = hass.states.get(attenuation_entity)
    radius_state = hass.states.get(radius_entity)

    assert rssi_state is not None
    assert attenuation_state is not None
    assert radius_state is not None

    assert float(rssi_state.state) == pytest.approx(7)
    assert float(attenuation_state.state) == pytest.approx(4.2)
    assert float(radius_state.state) == pytest.approx(12.5)
