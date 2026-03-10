"""Tests for Bermuda sensor entities."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import DOMAIN, NAME
from custom_components.bermuda.sensor import (
    BermudaSensorHorizontalSpeed,
    BermudaSensorTrilatAnchorCount,
    BermudaSensorVerticalSpeed,
)

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
    entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{scanner.unique_id}_timestamp_sync")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "unstable"
    assert state.attributes["recent_scanner_regressions"] == 1
    assert state.attributes["recent_stale_advert_drops"] == 1
    assert state.attributes["recent_max_backward_s"] == 3.2


async def test_trilat_speed_sensors_expose_filtered_motion(hass) -> None:
    """Tracked devices should expose horizontal and vertical speed diagnostics."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    device = BermudaDevice("AA:BB:CC:DD:EE:66", coordinator)
    device.create_sensor = True
    device.trilat_horizontal_speed_mps = 1.23456
    device.trilat_vertical_speed_mps = 0.45678
    coordinator.devices[device.address] = device

    horizontal = BermudaSensorHorizontalSpeed(coordinator, entry, device.address)
    vertical = BermudaSensorVerticalSpeed(coordinator, entry, device.address)

    assert horizontal.native_value == 1.235
    assert vertical.native_value == 0.457


async def test_trilat_anchor_count_sensor_exposes_anchor_status_lines(hass) -> None:
    """Anchor count diagnostics should expose one status line per scanner."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    device = BermudaDevice("AA:BB:CC:DD:EE:77", coordinator)
    device.create_sensor = True
    device.trilat_anchor_count = 2
    device.trilat_anchor_diagnostics = [
        "Living room light switch 1: valid",
        "Oven: rejected_no_range (sync=drifting)",
    ]
    coordinator.devices[device.address] = device

    sensor = BermudaSensorTrilatAnchorCount(coordinator, entry, device.address)

    assert sensor.native_value == 2
    assert sensor.extra_state_attributes == {
        "used_anchors": 2,
        "anchors": "Living room light switch 1: valid\nOven: rejected_no_range (sync=drifting)",
    }
