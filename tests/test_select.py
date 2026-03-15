"""Tests for Bermuda select entities."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ble_trilateration.bermuda_device import BermudaDevice
from custom_components.ble_trilateration.const import DOMAIN, NAME

from .const import MOCK_CONFIG


async def setup_integration(hass):
    """Set up the Bermuda config entry with coordinator refresh mocked."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-select", title=NAME)
    entry.add_to_hass(hass)

    with patch("custom_components.ble_trilateration.BermudaDataUpdateCoordinator.async_refresh"):
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


async def test_scanners_skip_legacy_anchor_select_entity(hass) -> None:
    """Ensure scanners do not recreate the removed anchor select entity."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:03")
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    assert ent_reg.async_get_entity_id("select", DOMAIN, f"{scanner.unique_id}_trilat_anchor_enabled") is None


async def test_legacy_anchor_select_removed_on_setup(hass) -> None:
    """Ensure stale scanner anchor select entities are pruned on startup."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-select-cleanup", title=NAME)
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    stale_entry = ent_reg.async_get_or_create(
        "select",
        DOMAIN,
        "aa:bb:cc:dd:ee:04_trilat_anchor_enabled",
        config_entry=entry,
        suggested_object_id="legacy_trilat_anchor_enabled",
    )

    with patch("custom_components.ble_trilateration.BermudaDataUpdateCoordinator.async_refresh"):
        assert await async_setup_component(hass, DOMAIN, {})

    await hass.async_block_till_done()

    assert ent_reg.async_get(stale_entry.entity_id) is None
