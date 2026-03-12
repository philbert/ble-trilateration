"""Tests for Bermuda sensor entities."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import DOMAIN, NAME
from custom_components.bermuda.sensor import (
    BermudaSensorGeometryQuality,
    BermudaSensorPositionConfidence,
    BermudaSensorResidualConsistency,
    BermudaSensorScannerAdvertStatus,
    BermudaSensorTrilatFloor,
    BermudaSensorTrilatX,
    BermudaSensorTrilatY,
    BermudaSensorTrilatZ,
    BermudaSensorTrackedDeviceAdvertStatus,
    BermudaSensorTrackingConfidence,
    BermudaSensorHorizontalSpeed,
    BermudaSensorTrilatAnchorCount,
    BermudaSensorVerticalSpeed,
    _remove_retired_sensor_entities,
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
    assert horizontal.name == "Speed Horizontal"
    assert vertical.name == "Speed Vertical"


async def test_trilat_confidence_sensors_expose_numeric_confidence(hass) -> None:
    """Tracked devices should expose raw and tracking confidence as numeric sensors."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    device = BermudaDevice("AA:BB:CC:DD:EE:67", coordinator)
    device.create_sensor = True
    device.trilat_confidence = 2.234
    device.trilat_confidence_level = "low"
    device.trilat_tracking_confidence = 6.789
    device.trilat_tracking_confidence_level = "medium"
    device.trilat_geometry_quality = 4.321
    device.trilat_geometry_gdop = 1.8
    device.trilat_geometry_condition = 12.5
    device.trilat_residual_consistency = 7.654
    device.trilat_normalized_residual_rms = 1.234
    device.trilat_residual_m = 0.9
    coordinator.devices[device.address] = device

    raw_confidence = BermudaSensorPositionConfidence(coordinator, entry, device.address)
    tracking_confidence = BermudaSensorTrackingConfidence(coordinator, entry, device.address)
    geometry_quality = BermudaSensorGeometryQuality(coordinator, entry, device.address)
    residual_consistency = BermudaSensorResidualConsistency(coordinator, entry, device.address)

    assert raw_confidence.native_value == 2.2
    assert raw_confidence.state_class == SensorStateClass.MEASUREMENT
    assert tracking_confidence.native_value == 6.8
    assert tracking_confidence.state_class == SensorStateClass.MEASUREMENT
    assert geometry_quality.native_value == 4.3
    assert geometry_quality.extra_state_attributes == {"gdop": 1.8, "condition_number": 12.5}
    assert residual_consistency.native_value == 7.7
    assert residual_consistency.extra_state_attributes == {
        "normalized_residual_rms": 1.234,
        "residual_m": 0.9,
    }
    assert raw_confidence.entity_category is None
    assert tracking_confidence.entity_category is None
    assert geometry_quality.entity_category is None
    assert residual_consistency.entity_category == EntityCategory.DIAGNOSTIC


async def test_promoted_trilat_sensors_are_normal_sensors(hass) -> None:
    """Core trilat sensors should no longer live in the diagnostic category."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    device = BermudaDevice("AA:BB:CC:DD:EE:69", coordinator)
    device.create_sensor = True
    coordinator.devices[device.address] = device

    assert BermudaSensorTrilatX(coordinator, entry, device.address).entity_category is None
    assert BermudaSensorTrilatY(coordinator, entry, device.address).entity_category is None
    assert BermudaSensorTrilatZ(coordinator, entry, device.address).entity_category is None
    assert BermudaSensorTrilatFloor(coordinator, entry, device.address).entity_category is None
    assert BermudaSensorTrilatAnchorCount(coordinator, entry, device.address).entity_category is None


async def test_retired_legacy_sensor_entities_are_pruned(hass) -> None:
    """Retired legacy sensor entities should be removed from the entity registry."""
    entry = await setup_integration(hass)
    ent_reg = er.async_get(hass)

    distance = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        "AA:BB:CC:DD:EE:70_range",
        config_entry=entry,
        suggested_object_id="legacy_distance",
    )
    scanner = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        "AA:BB:CC:DD:EE:70_scanner",
        config_entry=entry,
        suggested_object_id="legacy_scanner",
    )
    keep = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        "AA:BB:CC:DD:EE:70_tracking_confidence",
        config_entry=entry,
        suggested_object_id="tracking_confidence",
    )

    _remove_retired_sensor_entities(hass, entry.entry_id)

    assert ent_reg.async_get(distance.entity_id) is None
    assert ent_reg.async_get(scanner.entity_id) is None
    assert ent_reg.async_get(keep.entity_id) is not None


async def test_per_scanner_ble_status_sensors_expose_structured_status(hass) -> None:
    """Tracked-device and scanner-side BLE status sensors should expose per-pair status."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    tracked = BermudaDevice("AA:BB:CC:DD:EE:68", coordinator)
    tracked.create_sensor = True
    tracked.trilat_anchor_statuses = {
        "aa:bb:cc:dd:ee:09": {
            "scanner_address": "AA:BB:CC:DD:EE:09",
            "scanner_name": "Kitchen Proxy",
            "status": "rejected_wrong_floor",
            "sync_state": "drifting",
            "affects_position": False,
        }
    }
    coordinator.devices[tracked.address] = tracked

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:09")
    scanner.name = "Kitchen Proxy"

    tracked_side = BermudaSensorScannerAdvertStatus(coordinator, entry, tracked.address, scanner.address)
    scanner_side = BermudaSensorTrackedDeviceAdvertStatus(coordinator, entry, tracked.address, scanner.address)

    assert tracked_side.native_value == "rejected_wrong_floor"
    assert tracked_side.extra_state_attributes == {
        "scanner_address": "AA:BB:CC:DD:EE:09",
        "scanner_name": "Kitchen Proxy",
        "status": "rejected_wrong_floor",
        "sync_state": "drifting",
        "affects_position": False,
    }
    assert scanner_side.native_value == "rejected_wrong_floor"
    assert scanner_side.extra_state_attributes == {
        "scanner_address": "AA:BB:CC:DD:EE:09",
        "scanner_name": "Kitchen Proxy",
        "status": "rejected_wrong_floor",
        "sync_state": "drifting",
        "affects_position": False,
        "tracked_device_name": tracked.name,
        "tracked_device_address": tracked.address,
    }


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
        "1": "Living room light switch 1: valid",
        "2": "Oven: rejected_no_range (sync=drifting)",
    }
