"""Tests for Bermuda sensor entities."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ble_trilateration.bermuda_device import BermudaDevice
from custom_components.ble_trilateration.const import DOMAIN, NAME
from custom_components.ble_trilateration.sensor import (
    BermudaSensorFingerprintBestRoom,
    BermudaSensorFingerprintBlendWeight,
    BermudaSensorFingerprintCoverage,
    BermudaSensorFingerprintMargin,
    BermudaSensorFingerprintRoomScore,
    BermudaSensorGeometryConditionNumber,
    BermudaSensorGeometryGdop,
    BermudaSensorGeometryQuality,
    BermudaSensorGeometryRoomScore,
    BermudaSensorHorizontalSpeed,
    BermudaSensorNoAdvertAnchorCount,
    BermudaSensorNormalizedResidualRms,
    BermudaSensorPositionUncertaintyXBand,
    BermudaSensorPositionUncertaintyYBand,
    BermudaSensorPositionConfidence,
    BermudaSensorResidualConsistency,
    BermudaSensorResidualRms,
    BermudaSensorRoomCandidate,
    BermudaSensorRoomChallenger,
    BermudaSensorRoomChallengerEvidence,
    BermudaSensorRoomDecisionReason,
    BermudaSensorRoomHoldReason,
    BermudaSensorRoomSampleCount,
    BermudaSensorRoomScoreMargin,
    BermudaSensorScannerAdvertStatus,
    BermudaSensorScannerTimestampSync,
    BermudaSensorStaleAnchorCount,
    BermudaSensorTrackedDeviceAdvertStatus,
    BermudaSensorTrackingConfidence,
    BermudaSensorTrilatAnchorCount,
    BermudaSensorTrilatFloor,
    BermudaSensorTrilatX,
    BermudaSensorTrilatY,
    BermudaSensorTrilatZ,
    BermudaSensorValidAnchorCount,
    BermudaSensorValidOtherFloorAnchorCount,
    BermudaSensorVerticalSpeed,
    async_setup_entry as sensor_async_setup_entry,
    _remove_retired_sensor_entities,
)

from .const import MOCK_CONFIG


async def setup_integration(hass):
    """Set up the Bermuda config entry with coordinator refresh mocked."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-sensor", title=NAME)
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

    sensor = BermudaSensorScannerTimestampSync(coordinator, entry, scanner.address)

    assert sensor.native_value == "unstable"
    assert sensor.extra_state_attributes["recent_regressions"] == 1
    assert sensor.extra_state_attributes["recent_stale_drops"] == 1
    assert sensor.extra_state_attributes["recent_max_backward_s"] == 3.2


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

    assert horizontal.native_value == 1.23
    assert vertical.native_value == 0.46
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
    assert geometry_quality.name == "Geometry - Quality"
    assert geometry_quality.extra_state_attributes is None
    assert residual_consistency.native_value == 7.7
    assert residual_consistency.name == "Residual - Consistency"
    assert residual_consistency.extra_state_attributes is None
    assert raw_confidence.entity_category is None
    assert tracking_confidence.entity_category is None
    assert geometry_quality.entity_category is None
    assert residual_consistency.entity_category == EntityCategory.DIAGNOSTIC


async def test_room_classifier_diagnostic_sensors_expose_compact_values(hass) -> None:
    """Room-classifier diagnostics should expose compact rate-limited values."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    device = BermudaDevice("AA:BB:CC:DD:EE:78", coordinator)
    device.create_sensor = True
    device.room_decision_reason = "ok"
    device.room_candidate_name = "Living Room"
    device.room_challenger_name = "Kitchen"
    device.room_challenger_evidence = 0.345
    device.room_score_margin = 0.12345
    device.room_geometry_score = 0.45678
    device.room_fingerprint_score = 0.56789
    device.room_fingerprint_best_name = "Living Room"
    device.room_fingerprint_margin = 0.23456
    device.room_fingerprint_coverage = 0.78901
    device.room_fingerprint_blend_weight = 0.65
    device.room_sample_count = 7
    device.room_hold_reason = "room_evidence(0.35/0.50)"
    device.trilat_geometry_gdop = 1.234
    device.trilat_geometry_condition = 12.345
    device.trilat_normalized_residual_rms = 0.98765
    device.trilat_residual_m = 1.234
    device.trilat_anchor_statuses = {
        "scanner-1": {"status": "valid"},
        "scanner-2": {"status": "rejected_stale"},
        "scanner-3": {"status": "no_advert"},
        "scanner-4": {"status": "valid_other_floor"},
        "scanner-5": {"status": "valid"},
    }
    coordinator.devices[device.address] = device

    assert BermudaSensorRoomDecisionReason(coordinator, entry, device.address).native_value == "ok"
    room_candidate = BermudaSensorRoomCandidate(coordinator, entry, device.address)
    assert room_candidate.name == "Room - Candidate"
    assert room_candidate.native_value == "Living Room"
    assert BermudaSensorRoomChallenger(coordinator, entry, device.address).native_value == "Kitchen"
    assert BermudaSensorRoomChallengerEvidence(coordinator, entry, device.address).native_value == 0.34
    assert BermudaSensorRoomScoreMargin(coordinator, entry, device.address).native_value == 0.123
    assert BermudaSensorGeometryRoomScore(coordinator, entry, device.address).native_value == 0.457
    fingerprint_score = BermudaSensorFingerprintRoomScore(coordinator, entry, device.address)
    assert fingerprint_score.name == "Fingerprint - Room Score"
    assert fingerprint_score.native_value == 0.568
    assert BermudaSensorFingerprintBestRoom(coordinator, entry, device.address).native_value == "Living Room"
    assert BermudaSensorFingerprintMargin(coordinator, entry, device.address).native_value == 0.235
    assert BermudaSensorFingerprintCoverage(coordinator, entry, device.address).native_value == 0.789
    assert BermudaSensorFingerprintBlendWeight(coordinator, entry, device.address).native_value == 0.65
    assert BermudaSensorRoomSampleCount(coordinator, entry, device.address).native_value == 7
    assert BermudaSensorRoomHoldReason(coordinator, entry, device.address).native_value == "room_evidence(0.35/0.50)"
    geometry_gdop = BermudaSensorGeometryGdop(coordinator, entry, device.address)
    assert geometry_gdop.name == "Geometry - GDOP"
    assert geometry_gdop.native_value == 1.23
    assert BermudaSensorGeometryConditionNumber(coordinator, entry, device.address).native_value == 12.35
    assert BermudaSensorNormalizedResidualRms(coordinator, entry, device.address).native_value == 0.988
    residual_rms = BermudaSensorResidualRms(coordinator, entry, device.address)
    assert residual_rms.native_value == 1.23
    assert residual_rms.native_unit_of_measurement == UnitOfLength.METERS
    valid_anchor_count = BermudaSensorValidAnchorCount(coordinator, entry, device.address)
    assert valid_anchor_count.name == "Anchor - Valid Count"
    assert valid_anchor_count.native_value == 2
    assert BermudaSensorStaleAnchorCount(coordinator, entry, device.address).native_value == 1
    assert BermudaSensorNoAdvertAnchorCount(coordinator, entry, device.address).native_value == 1
    assert BermudaSensorValidOtherFloorAnchorCount(coordinator, entry, device.address).native_value == 1

    margin_sensor = BermudaSensorRoomScoreMargin(coordinator, entry, device.address)
    assert margin_sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert margin_sensor.entity_registry_enabled_default is False
    assert margin_sensor.native_value == 0.123
    device.room_score_margin = 0.999
    assert margin_sensor.native_value == 0.123


async def test_promoted_trilat_sensors_are_normal_sensors(hass) -> None:
    """Core trilat sensors should no longer live in the diagnostic category."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    device = BermudaDevice("AA:BB:CC:DD:EE:69", coordinator)
    device.create_sensor = True
    device.trilat_x_m = 1.1
    device.trilat_y_m = 2.2
    device.trilat_z_m = 3.3
    coordinator.devices[device.address] = device

    trilat_x = BermudaSensorTrilatX(coordinator, entry, device.address)
    trilat_y = BermudaSensorTrilatY(coordinator, entry, device.address)
    trilat_z = BermudaSensorTrilatZ(coordinator, entry, device.address)

    assert trilat_x.entity_category is None
    assert trilat_y.entity_category is None
    assert trilat_z.entity_category is None
    assert BermudaSensorTrilatFloor(coordinator, entry, device.address).entity_category is None
    assert BermudaSensorTrilatAnchorCount(coordinator, entry, device.address).entity_category is None
    assert trilat_x.native_unit_of_measurement == UnitOfLength.METERS
    assert trilat_y.native_unit_of_measurement == UnitOfLength.METERS
    assert trilat_z.native_unit_of_measurement == UnitOfLength.METERS


async def test_position_uncertainty_band_sensors_expose_band_widths(hass) -> None:
    """Tracked devices should expose empirical XY uncertainty bands with correction attrs."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    device = BermudaDevice("AA:BB:CC:DD:EE:6B", coordinator)
    device.create_sensor = True
    device.position_uncertainty_x_band_m = 5.4321
    device.position_uncertainty_y_band_m = 3.2109
    device.position_uncertainty_source = "mixed"
    device.trilat_position_correction_x_m = 0.3456
    device.trilat_position_correction_y_m = -0.1234
    device.trilat_x_raw_m = 1.2345
    device.trilat_y_raw_m = 6.7891
    coordinator.devices[device.address] = device

    sensor_x = BermudaSensorPositionUncertaintyXBand(coordinator, entry, device.address)
    sensor_y = BermudaSensorPositionUncertaintyYBand(coordinator, entry, device.address)

    assert sensor_x.native_value == 5.43
    assert sensor_y.native_value == 3.21
    assert sensor_x.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor_y.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor_x.extra_state_attributes == {"source": "mixed"}
    assert sensor_y.extra_state_attributes == {"source": "mixed"}


async def test_trilat_floor_sensor_exposes_phase0_diagnostics(hass) -> None:
    """Trilat Floor should expose floor evidence and floor-switch diagnostics."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    device = BermudaDevice("AA:BB:CC:DD:EE:6A", coordinator)
    device.create_sensor = True
    device.trilat_floor_id = "ground_floor"
    device.trilat_floor_name = "Ground floor"
    device.trilat_floor_diagnostics = {
        "reason": "floor_switch_cold_reset",
        "selected_floor_id": "ground_floor",
        "selected_floor_name": "Ground floor",
        "best_floor_id": "ground_floor",
        "best_floor_name": "Ground floor",
        "challenger_floor_id": "street_level",
        "challenger_floor_name": "Street level",
        "floor_switch_reset_count": 2,
    }
    device.trilat_floor_evidence = {
        "ground_floor": 10.1234,
        "street_level": 8.4321,
    }
    device.trilat_floor_evidence_names = {
        "ground_floor": "Ground floor",
        "street_level": "Street level",
    }
    coordinator.devices[device.address] = device

    sensor = BermudaSensorTrilatFloor(coordinator, entry, device.address)

    assert sensor.native_value == "Ground floor"
    assert sensor.extra_state_attributes == {"floor_id": "ground_floor"}


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
            "status": "valid_other_floor",
            "sync_state": "drifting",
            "affects_position": True,
        }
    }
    coordinator.devices[tracked.address] = tracked

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:09")
    scanner.name = "Kitchen Proxy"

    tracked_side = BermudaSensorScannerAdvertStatus(coordinator, entry, tracked.address, scanner.address)
    scanner_side = BermudaSensorTrackedDeviceAdvertStatus(coordinator, entry, tracked.address, scanner.address)

    assert tracked_side.native_value == "valid_other_floor"
    assert tracked_side.extra_state_attributes == {
        "scanner_address": "aa:bb:cc:dd:ee:09",
        "scanner_name": "Kitchen Proxy",
    }
    assert scanner_side.native_value == "valid_other_floor"
    assert scanner_side.extra_state_attributes == {
        "tracked_device_name": tracked.name,
        "tracked_device_address": tracked.address,
        "scanner_address": "aa:bb:cc:dd:ee:09",
        "scanner_name": "Kitchen Proxy",
    }


async def test_sensor_platform_catches_up_existing_tracked_devices_and_scanners(hass) -> None:
    """Sensor setup should create BLE-status entities for already-known devices/scanners."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    tracked = BermudaDevice("AA:BB:CC:DD:EE:71", coordinator)
    tracked.create_sensor = True
    coordinator.devices[tracked.address] = tracked

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:72")

    added_entities = []

    def _async_add_entities(entities, _update_before_add=False):
        added_entities.extend(entities)

    await sensor_async_setup_entry(hass, entry, _async_add_entities)

    assert any(
        isinstance(entity, BermudaSensorScannerAdvertStatus)
        and entity.address == tracked.address
        and entity._scanner.address == scanner.address
        for entity in added_entities
    )
    assert any(
        isinstance(entity, BermudaSensorTrackedDeviceAdvertStatus)
        and entity.address == scanner.address
        and entity._tracked_device.address == tracked.address
        for entity in added_entities
    )


async def test_trilat_anchor_count_sensor_exposes_anchor_status_lines(hass) -> None:
    """Anchor count diagnostics should expose one status line per scanner."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    device = BermudaDevice("AA:BB:CC:DD:EE:77", coordinator)
    device.create_sensor = True
    device.trilat_anchor_count = 2
    device.trilat_cross_floor_anchor_count = 1
    device.trilat_anchor_diagnostics = [
        "Living room light switch 1: valid",
        "Oven: rejected_no_range (sync=drifting)",
    ]
    device.trilat_cross_floor_anchor_diagnostics = [
        "Garage proxy: valid_other_floor (selected=ground_floor, scanner=street_level, other_floor_sigma=6.40m)",
    ]
    coordinator.devices[device.address] = device

    sensor = BermudaSensorTrilatAnchorCount(coordinator, entry, device.address)

    assert sensor.native_value == 2
    assert sensor.name == "Anchor - Used Count"
    assert sensor.extra_state_attributes == {
        "cross_floor_candidate_count": 1,
    }
