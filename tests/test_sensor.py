"""Tests for Bermuda sensor entities."""

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
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-sensor", title=NAME)
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
    scanner._is_remote_scanner = True  # noqa: SLF001 - test helper to mark as remote proxy
    scanner.address_wifi_mac = address
    scanner.address_ble_mac = address
    coordinator.devices[scanner.address] = scanner
    coordinator.scanner_list_add(scanner)
    return scanner


async def test_scanner_timestamp_sync_sensor_exposes_runtime_health(hass) -> None:
    """Scanner proxies should expose timestamp sync diagnostics in HA."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:05")
    scanner.record_scanner_timestamp_regression(3.2)
    scanner.record_stale_advert_drop(1.1)

    await hass.async_block_till_done()
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{scanner.address_ble_mac}_timestamp_sync")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "unstable"
    assert state.attributes["recent_scanner_regressions"] == 1
    assert state.attributes["recent_stale_advert_drops"] == 1
    assert state.attributes["recent_max_backward_s"] == 3.2


async def test_scanner_timestamp_sync_removes_stale_legacy_unique_id(hass) -> None:
    """Old timestamp-sync entities should be pruned when scanner identity stabilizes."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-sensor-cleanup", title=NAME)
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    stale_entry = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        "11:22:33:44:55:66_timestamp_sync",
        config_entry=entry,
        suggested_object_id="legacy_timestamp_sync",
    )

    with patch("custom_components.bermuda.BermudaDataUpdateCoordinator.async_refresh"):
        assert await async_setup_component(hass, DOMAIN, {})

    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator

    scanner = BermudaDevice("AA:BB:CC:DD:EE:99", coordinator)
    scanner._is_scanner = True  # noqa: SLF001 - test helper
    scanner._is_remote_scanner = True  # noqa: SLF001 - test helper
    scanner.address_ble_mac = "AA:BB:CC:DD:EE:99"
    scanner.address_wifi_mac = "11:22:33:44:55:66"
    scanner.unique_id = scanner.address_wifi_mac
    coordinator.devices[scanner.address] = scanner
    coordinator.scanner_list_add(scanner)

    await hass.async_block_till_done()

    assert ent_reg.async_get(stale_entry.entity_id) is None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{scanner.address_ble_mac}_timestamp_sync") is not None
