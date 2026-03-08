"""Tests for Bermuda number entities."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import DOMAIN, NAME

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
    scanner.scanner_entity_key = address.lower()
    coordinator.devices[scanner.address] = scanner
    coordinator.scanner_list_add(scanner)
    return scanner


async def test_scanner_anchor_numbers_created_without_legacy_numbers(hass) -> None:
    """Ensure scanners only expose anchor coordinates, not legacy calibration numbers."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:01")
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    assert ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_rssi_offset") is None
    assert ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_attenuation") is None
    assert ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_max_radius") is None

    anchor_x = ent_reg.async_get_entity_id("number", DOMAIN, f"scanner:{scanner.scanner_entity_key}:anchor_x")
    anchor_y = ent_reg.async_get_entity_id("number", DOMAIN, f"scanner:{scanner.scanner_entity_key}:anchor_y")
    anchor_z = ent_reg.async_get_entity_id("number", DOMAIN, f"scanner:{scanner.scanner_entity_key}:anchor_z")

    assert anchor_x is not None
    assert anchor_y is not None
    assert anchor_z is not None


async def test_legacy_scanner_number_entities_removed_on_setup(hass) -> None:
    """Ensure stale legacy per-scanner number entities are pruned on startup."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-number-cleanup", title=NAME)
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    stale_entry = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        "aa:bb:cc:dd:ee:02_rssi_offset",
        config_entry=entry,
        suggested_object_id="legacy_scanner_rssi_offset",
    )

    with patch("custom_components.bermuda.BermudaDataUpdateCoordinator.async_refresh"):
        assert await async_setup_component(hass, DOMAIN, {})

    await hass.async_block_till_done()

    assert ent_reg.async_get(stale_entry.entity_id) is None


async def test_legacy_scanner_anchor_entities_removed_on_setup(hass) -> None:
    """Old mutable-ID anchor entities should be pruned when canonical scanner IDs exist."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-number-anchor-cleanup", title=NAME)
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    stale_entry = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        "11:22:33:44:55:66_anchor_x_m",
        config_entry=entry,
        suggested_object_id="legacy_scanner_anchor_x",
    )

    with patch("custom_components.bermuda.BermudaDataUpdateCoordinator.async_refresh"):
        assert await async_setup_component(hass, DOMAIN, {})

    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator

    scanner = BermudaDevice("AA:BB:CC:DD:EE:66", coordinator)
    scanner._is_scanner = True  # noqa: SLF001 - test helper
    scanner.address_wifi_mac = "11:22:33:44:55:66"
    scanner.address_ble_mac = "aa:bb:cc:dd:ee:66"
    scanner.unique_id = scanner.address_wifi_mac
    scanner.scanner_entity_key = "aa:bb:cc:dd:ee:66"
    coordinator.devices[scanner.address] = scanner
    coordinator.scanner_list_add(scanner)

    await hass.async_block_till_done()

    assert ent_reg.async_get(stale_entry.entity_id) is None
    assert ent_reg.async_get_entity_id("number", DOMAIN, "scanner:aa:bb:cc:dd:ee:66:anchor_x") is not None
