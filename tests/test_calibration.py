"""Tests for BLE Trilateration calibration sample capture and management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from types import SimpleNamespace
from unittest.mock import patch

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir

from custom_components.ble_trilateration.bermuda_device import BermudaDevice
from custom_components.ble_trilateration.const import (
    CALIBRATION_EVENT_SAMPLE_CAPTURED,
    DOMAIN,
    REPAIR_CALIBRATION_LAYOUT_MISMATCH,
)
from custom_components.ble_trilateration.repairs import async_create_fix_flow


async def test_record_calibration_sample_service(hass: HomeAssistant, setup_bermuda_entry):
    """Service should start a session, persist a sample, and fire completion event."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    area = ar.async_get(hass).async_create("Living Room")
    devreg = dr.async_get(hass)
    device_entry = devreg.async_get_or_create(
        config_entry_id=setup_bermuda_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:01")},
        name="Phil Phone",
    )

    target = BermudaDevice("aa:bb:cc:dd:ee:01", coordinator)
    target.name = "Phil Phone"
    coordinator.devices[target.address] = target

    for idx in range(3):
        scanner = BermudaDevice(f"aa:bb:cc:dd:10:0{idx}", coordinator)
        scanner.name = f"Scanner {idx}"
        scanner.anchor_enabled = True
        scanner.anchor_x_m = float(idx)
        scanner.anchor_y_m = float(idx + 1)
        scanner.anchor_z_m = 2.0
        coordinator.devices[scanner.address] = scanner
        coordinator._scanner_list.add(scanner.address)
        target.adverts[(target.address, scanner.address)] = SimpleNamespace(
            scanner_address=scanner.address,
            stamp=monotonic_time_coarse(),
            rssi=-65.0 - idx,
        )

    events = []

    @callback
    def _capture_event(event):
        events.append(event)

    unsub = hass.bus.async_listen(CALIBRATION_EVENT_SAMPLE_CAPTURED, _capture_event)
    try:
        with patch("custom_components.ble_trilateration.calibration.persistent_notification.async_create") as notify_mock:
            response = await hass.services.async_call(
                DOMAIN,
                "record_calibration_sample",
                {
                    "device_id": device_entry.id,
                    "room_area_id": area.id,
                    "x_y_z_m": "4.2, 1.8, 1.1",
                    "duration_s": 1,
                    "notes": "Near sofa",
                },
                blocking=True,
                return_response=True,
            )

            assert response["device_id"] == device_entry.id
            assert isinstance(response["expected_complete_at"], str)
            assert "T" in response["expected_complete_at"]
            assert response["x_m"] == 4.2
            assert response["y_m"] == 1.8
            assert response["z_m"] == 1.1
            assert response["sample_radius_m"] == 1.0
            session_id = response["session_id"]

            coordinator.calibration.capture_update()
            await coordinator.calibration._async_finalize_session(session_id)

            task = coordinator.calibration._session_tasks.pop(session_id)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await hass.async_block_till_done()

            assert notify_mock.call_count == 2
            start_call = notify_mock.call_args_list[0]
            finish_call = notify_mock.call_args_list[1]

            assert "BLE Trilateration calibration sample" == start_call.kwargs["title"]
            assert start_call.kwargs["notification_id"] == f"ble_trilateration_calibration_{session_id}"
            start_message = start_call.args[1]
            assert "Room: Living Room" in start_message
            assert "Position: x=4.200, y=1.800, z=1.100" in start_message
            assert "Status: started" in start_message
            assert "Expected complete at:" in start_message
            assert "Notes: Near sofa" in start_message

            assert "BLE Trilateration calibration sample" == finish_call.kwargs["title"]
            assert finish_call.kwargs["notification_id"] == f"ble_trilateration_calibration_{session_id}"
            finish_message = finish_call.args[1]
            assert "Position: x=4.200, y=1.800, z=1.100" in finish_message
            assert "Status: accepted" in finish_message
            assert "Sample ID:" in finish_message
            assert "Quality: " in finish_message
            assert "Quality details: anchors=3" in finish_message
            assert "Notes: Near sofa" in finish_message
    finally:
        unsub()

    assert len(events) == 1
    event = events[0]
    assert event.data["quality_status"] == "accepted"
    assert event.data["sample_id"] is not None

    samples = coordinator.calibration.samples()
    assert len(samples) == 1
    sample = samples[0]
    assert sample["room_area_id"] == area.id
    assert sample["position"] == {"x_m": 4.2, "y_m": 1.8, "z_m": 1.1}
    assert sample["sample_radius_m"] == 1.0
    assert sample["quality"]["status"] == "accepted"
    assert sample["quality"]["level"] in {"high", "medium", "low"}
    assert isinstance(sample["quality"]["score_01"], float)
    assert sample["quality"]["eligible_anchor_count"] == 3
    assert sample["quality"]["total_packet_count"] == 3
    assert "median_rssi_mad_db" in sample["quality"]
    assert "geometry_quality_01" in sample["quality"]
    assert len(sample["anchors"]) == 3
    first_anchor = next(iter(sample["anchors"].values()))
    assert "buckets_1s" not in first_anchor


async def test_record_calibration_sample_service_accepts_legacy_room_radius(hass: HomeAssistant, setup_bermuda_entry):
    """Legacy room_radius_m service input should still be accepted temporarily."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    area = ar.async_get(hass).async_create("Living Room")
    devreg = dr.async_get(hass)
    device_entry = devreg.async_get_or_create(
        config_entry_id=setup_bermuda_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:11")},
        name="Phil Phone",
    )

    target = BermudaDevice("aa:bb:cc:dd:ee:11", coordinator)
    target.name = "Phil Phone"
    coordinator.devices[target.address] = target

    scanner = BermudaDevice("aa:bb:cc:dd:10:11", coordinator)
    scanner.name = "Scanner"
    scanner.anchor_enabled = True
    scanner.anchor_x_m = 0.0
    scanner.anchor_y_m = 1.0
    scanner.anchor_z_m = 2.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)
    target.adverts[(target.address, scanner.address)] = SimpleNamespace(
        scanner_address=scanner.address,
        stamp=monotonic_time_coarse(),
        rssi=-65.0,
    )

    response = await hass.services.async_call(
        DOMAIN,
        "record_calibration_sample",
        {
            "device_id": device_entry.id,
            "room_area_id": area.id,
            "x_m": 4.2,
            "y_m": 1.8,
            "z_m": 1.1,
            "room_radius_m": 1.4,
            "duration_s": 1,
        },
        blocking=True,
        return_response=True,
    )

    session_id = response["session_id"]
    coordinator.calibration.capture_update()
    await coordinator.calibration._async_finalize_session(session_id)

    task = coordinator.calibration._session_tasks.pop(session_id)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    sample = coordinator.calibration.samples()[0]
    assert sample["sample_radius_m"] == 1.4
    assert "room_radius_m" not in sample


async def test_calibration_sample_records_trilat_capture_summary(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Calibration captures should persist compact raw-trilat behavior summaries."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    floor = fr.async_get(hass).async_create("Ground floor", level=0)
    area = ar.async_get(hass).async_create("Living Room", floor_id=floor.floor_id)
    devreg = dr.async_get(hass)
    device_entry = devreg.async_get_or_create(
        config_entry_id=setup_bermuda_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:13")},
        name="Phil Phone",
    )

    target = BermudaDevice("aa:bb:cc:dd:ee:13", coordinator)
    target.name = "Phil Phone"
    coordinator.devices[target.address] = target

    for idx in range(3):
        scanner = BermudaDevice(f"aa:bb:cc:dd:10:1{idx}", coordinator)
        scanner.name = f"Scanner {idx}"
        scanner.anchor_enabled = True
        scanner.anchor_x_m = float(idx)
        scanner.anchor_y_m = float(idx + 1)
        scanner.anchor_z_m = 2.0
        coordinator.devices[scanner.address] = scanner
        coordinator._scanner_list.add(scanner.address)
        target.adverts[(target.address, scanner.address)] = SimpleNamespace(
            scanner_address=scanner.address,
            stamp=monotonic_time_coarse(),
            rssi=-65.0 - idx,
        )

    response = await coordinator.calibration.async_start_session(
        device_id=device_entry.id,
        room_area_id=area.id,
        x_m=4.2,
        y_m=1.8,
        z_m=1.1,
        duration_s=1,
    )
    session_id = response["session_id"]

    for x_val, y_val, z_val, residual_m in (
        (4.0, 2.0, 1.0, 0.40),
        (4.4, 1.6, 1.1, 0.60),
        (4.2, 1.8, 1.2, 0.50),
    ):
        target.trilat_x_raw_m = x_val
        target.trilat_y_raw_m = y_val
        target.trilat_z_raw_m = z_val
        target.trilat_residual_m = residual_m
        target.trilat_geometry_quality = 6.0
        target.trilat_tracking_confidence = 8.0
        coordinator.calibration.capture_update()

    await coordinator.calibration._async_finalize_session(session_id)
    task = coordinator.calibration._session_tasks.pop(session_id)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    sample = coordinator.calibration.samples()[0]
    trilat_capture = sample["trilat_capture"]
    assert sample["room_floor_id"] == floor.floor_id
    assert trilat_capture["position_source"] == "raw_filtered"
    assert trilat_capture["observed_count"] == 3
    assert trilat_capture["x_mean_m"] == 4.2
    assert trilat_capture["y_mean_m"] == 1.8
    assert trilat_capture["z_mean_m"] == 1.1
    assert trilat_capture["x_stddev_m"] > 0.0
    assert trilat_capture["y_stddev_m"] > 0.0
    assert trilat_capture["x_rmse_from_target_m"] > 0.0
    assert trilat_capture["y_rmse_from_target_m"] > 0.0
    # Post-correction spread fields (p95 from mean, not from target)
    assert "x_p95_spread_m" in trilat_capture
    assert "y_p95_spread_m" in trilat_capture
    assert trilat_capture["x_p95_spread_m"] <= trilat_capture["x_p95_abs_error_m"]
    assert trilat_capture["y_p95_spread_m"] <= trilat_capture["y_p95_abs_error_m"]
    assert trilat_capture["residual_mean_m"] == 0.5


async def test_existing_calibration_samples_bootstrap_trilat_position_model(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Existing calibration samples without trilat summaries should still seed correction lookups."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    floor = fr.async_get(hass).async_create("Ground floor", level=0)
    area = ar.async_get(hass).async_create("Office", floor_id=floor.floor_id)

    scanner_positions = [
        ("aa:bb:cc:dd:10:31", 0.0, 0.0, 1.0),
        ("aa:bb:cc:dd:10:32", 4.0, 0.0, 1.0),
        ("aa:bb:cc:dd:10:33", 0.0, 4.0, 1.0),
    ]
    for address, x_m, y_m, z_m in scanner_positions:
        scanner = BermudaDevice(address, coordinator)
        scanner.name = address
        scanner.anchor_enabled = True
        scanner.anchor_x_m = x_m
        scanner.anchor_y_m = y_m
        scanner.anchor_z_m = z_m
        coordinator.devices[scanner.address] = scanner
        coordinator._scanner_list.add(scanner.address)

    layout_hash = coordinator.calibration.current_anchor_layout_hash

    def _sample_anchors(sample_x: float, sample_y: float, sample_z: float) -> dict[str, dict[str, object]]:
        anchors: dict[str, dict[str, object]] = {}
        for address, anchor_x, anchor_y, anchor_z in scanner_positions:
            distance = ((sample_x - anchor_x) ** 2 + (sample_y - anchor_y) ** 2 + (sample_z - anchor_z) ** 2) ** 0.5
            anchors[address] = {
                "scanner_name": address,
                "anchor_position": {"x_m": anchor_x, "y_m": anchor_y, "z_m": anchor_z},
                "packet_count": 5,
                "rssi_median": -55.0 - (20.0 * math.log10(max(distance, 0.2))),
                "rssi_mad": 0.5,
                "rssi_min": -56.0 - (20.0 * math.log10(max(distance, 0.2))),
                "rssi_max": -54.0 - (20.0 * math.log10(max(distance, 0.2))),
            }
        return anchors

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_bootstrap_1",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:41",
            "room_area_id": area.id,
            "room_name": area.name,
            "room_floor_id": floor.floor_id,
            "position": {"x_m": 1.0, "y_m": 1.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": layout_hash,
            "anchors": _sample_anchors(1.0, 1.0, 1.0),
            "quality": {"status": "accepted", "score_01": 0.8, "eligible_anchor_count": 3, "reason": None},
        }
    )
    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_bootstrap_2",
            "created_at": "2026-03-06T12:05:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:41",
            "room_area_id": area.id,
            "room_name": area.name,
            "room_floor_id": floor.floor_id,
            "position": {"x_m": 2.0, "y_m": 1.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": layout_hash,
            "anchors": _sample_anchors(2.0, 1.0, 1.0),
            "quality": {"status": "accepted", "score_01": 0.8, "eligible_anchor_count": 3, "reason": None},
        }
    )

    await coordinator.async_handle_calibration_samples_changed()

    adjustment = coordinator.calibration.trilat_position_adjustment(
        layout_hash=layout_hash,
        floor_id=floor.floor_id,
        x_m=1.0,
        y_m=1.0,
        residual_m=0.5,
    )
    assert adjustment is not None
    assert adjustment.source == "bootstrap"
    assert adjustment.sample_count >= 1
    assert adjustment.uncertainty_x_band_m is not None
    assert adjustment.uncertainty_y_band_m is not None


async def test_record_calibration_sample_service_accepts_split_xyz(hass: HomeAssistant, setup_bermuda_entry):
    """Split x_m/y_m/z_m service inputs should remain accepted."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    area = ar.async_get(hass).async_create("Living Room")
    devreg = dr.async_get(hass)
    device_entry = devreg.async_get_or_create(
        config_entry_id=setup_bermuda_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:12")},
        name="Phil Phone",
    )

    target = BermudaDevice("aa:bb:cc:dd:ee:12", coordinator)
    target.name = "Phil Phone"
    coordinator.devices[target.address] = target

    scanner = BermudaDevice("aa:bb:cc:dd:10:12", coordinator)
    scanner.name = "Scanner"
    scanner.anchor_enabled = True
    scanner.anchor_x_m = 1.0
    scanner.anchor_y_m = 2.0
    scanner.anchor_z_m = 3.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)
    target.adverts[(target.address, scanner.address)] = SimpleNamespace(
        scanner_address=scanner.address,
        stamp=monotonic_time_coarse(),
        rssi=-65.0,
    )

    response = await hass.services.async_call(
        DOMAIN,
        "record_calibration_sample",
        {
            "device_id": device_entry.id,
            "room_area_id": area.id,
            "x_m": 4.2,
            "y_m": 1.8,
            "z_m": 1.1,
            "duration_s": 1,
        },
        blocking=True,
        return_response=True,
    )

    session_id = response["session_id"]
    coordinator.calibration.capture_update()
    await coordinator.calibration._async_finalize_session(session_id)

    task = coordinator.calibration._session_tasks.pop(session_id)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    sample = coordinator.calibration.samples()[0]
    assert sample["position"] == {"x_m": 4.2, "y_m": 1.8, "z_m": 1.1}


async def test_record_transition_sample_service_keeps_repeated_captures_separate(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Transition samples should remain separate even with the same room and name."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    floor_registry = fr.async_get(hass)
    ground_floor = floor_registry.async_create("Ground floor", level=0)
    basement = floor_registry.async_create("Basement", level=-1)
    top_floor = floor_registry.async_create("Top floor", level=1)
    area = ar.async_get(hass).async_create("Entrance", floor_id=ground_floor.floor_id)

    devreg = dr.async_get(hass)
    device_entry = devreg.async_get_or_create(
        config_entry_id=setup_bermuda_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:21")},
        name="Phil Phone",
    )

    target = BermudaDevice("aa:bb:cc:dd:ee:21", coordinator)
    target.name = "Phil Phone"
    coordinator.devices[target.address] = target

    for idx in range(3):
        scanner = BermudaDevice(f"aa:bb:cc:dd:21:0{idx}", coordinator)
        scanner.name = f"Scanner {idx}"
        scanner.anchor_enabled = True
        scanner.anchor_x_m = float(idx)
        scanner.anchor_y_m = float(idx + 1)
        scanner.anchor_z_m = 2.0
        coordinator.devices[scanner.address] = scanner
        coordinator._scanner_list.add(scanner.address)
        target.adverts[(target.address, scanner.address)] = SimpleNamespace(
            scanner_address=scanner.address,
            stamp=monotonic_time_coarse(),
            rssi=-65.0 - idx,
        )

    response = await hass.services.async_call(
        DOMAIN,
        "record_transition_sample",
        {
            "device_id": device_entry.id,
            "room_area_id": area.id,
            "transition_name": "stairwell",
            "x_y_z_m": "1.0, 2.0, 3.0",
            "sample_radius_m": 1.0,
            "capture_duration_s": 60,
            "transition_floor_ids": [basement.floor_id],
        },
        blocking=True,
        return_response=True,
    )

    assert response["session_id"].startswith("transition_")
    assert isinstance(response["expected_complete_at"], str)
    assert response["transition_name"] == "stairwell"
    assert response["transition_floor_ids"] == [basement.floor_id]
    first_session_id = response["session_id"]

    coordinator.calibration.capture_update()
    await coordinator.calibration._async_finalize_session(first_session_id)
    task = coordinator.calibration._session_tasks.pop(first_session_id)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    response = await hass.services.async_call(
        DOMAIN,
        "record_transition_sample",
        {
            "device_id": device_entry.id,
            "room_area_id": area.id,
            "transition_name": "stairwell",
            "x_m": 3.0,
            "y_m": 4.0,
            "z_m": 5.0,
            "sample_radius_m": 1.5,
            "capture_duration_s": 45,
            "transition_floor_ids": [basement.floor_id, top_floor.floor_id],
        },
        blocking=True,
        return_response=True,
    )

    assert response["session_id"].startswith("transition_")
    assert response["x_m"] == 3.0
    assert response["y_m"] == 4.0
    assert response["z_m"] == 5.0
    assert response["sample_radius_m"] == 1.5
    assert response["transition_floor_ids"] == sorted([basement.floor_id, top_floor.floor_id])
    second_session_id = response["session_id"]

    coordinator.calibration.capture_update()
    await coordinator.calibration._async_finalize_session(second_session_id)
    task = coordinator.calibration._session_tasks.pop(second_session_id)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    transition_samples = coordinator.calibration.transition_samples()
    assert len(transition_samples) == 2
    stored_positions = {tuple(sample["position"].values()) for sample in transition_samples}
    assert stored_positions == {(1.0, 2.0, 3.0), (3.0, 4.0, 5.0)}
    for stored in transition_samples:
        assert stored["id"].startswith("transition_sample_")
        assert stored["room_area_id"] == area.id
        assert stored["room_floor_id"] == ground_floor.floor_id
        assert stored["transition_name"] == "stairwell"
        assert stored["anchor_layout_hash"] == coordinator.calibration.current_anchor_layout_hash
        assert stored["capture_duration_s"] in {45, 60}
        assert stored["quality"]["status"] == "accepted"
        assert len(stored["anchors"]) == 3
        assert "transition_key" not in stored


async def test_record_transition_sample_service_updates_persistent_notification(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Transition sample capture should emit started and stored notifications."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    floor_registry = fr.async_get(hass)
    ground_floor = floor_registry.async_create("Ground floor", level=0)
    basement = floor_registry.async_create("Basement", level=-1)
    area = ar.async_get(hass).async_create("Entrance", floor_id=ground_floor.floor_id)

    devreg = dr.async_get(hass)
    device_entry = devreg.async_get_or_create(
        config_entry_id=setup_bermuda_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:23")},
        name="Phil Phone",
    )

    target = BermudaDevice("aa:bb:cc:dd:ee:23", coordinator)
    target.name = "Phil Phone"
    coordinator.devices[target.address] = target

    for idx in range(3):
        scanner = BermudaDevice(f"aa:bb:cc:dd:23:0{idx}", coordinator)
        scanner.name = f"Scanner {idx}"
        scanner.anchor_enabled = True
        scanner.anchor_x_m = float(idx)
        scanner.anchor_y_m = float(idx + 1)
        scanner.anchor_z_m = 2.0
        coordinator.devices[scanner.address] = scanner
        coordinator._scanner_list.add(scanner.address)
        target.adverts[(target.address, scanner.address)] = SimpleNamespace(
            scanner_address=scanner.address,
            stamp=monotonic_time_coarse(),
            rssi=-65.0 - idx,
        )

    with patch("custom_components.ble_trilateration.calibration.persistent_notification.async_create") as notify_mock:
        response = await hass.services.async_call(
            DOMAIN,
            "record_transition_sample",
            {
                "device_id": device_entry.id,
                "room_area_id": area.id,
                "transition_name": "stairwell",
                "x_y_z_m": "1.0, 2.0, 3.0",
                "sample_radius_m": 1.0,
                "capture_duration_s": 60,
                "transition_floor_ids": [basement.floor_id],
            },
            blocking=True,
            return_response=True,
        )

        session_id = response["session_id"]
        assert notify_mock.call_count == 1

        start_call = notify_mock.call_args_list[0]
        assert start_call.kwargs["title"] == "BLE Trilateration transition sample"
        assert start_call.kwargs["notification_id"] == f"ble_trilateration_transition_{session_id}"
        start_message = start_call.args[1]
        assert "Room: Entrance" in start_message
        assert "Room floor: Ground floor" in start_message
        assert "Transition: stairwell" in start_message
        assert "Transition floors: Basement" in start_message
        assert "Status: started" in start_message
        assert "Expected complete at:" in start_message

        coordinator.calibration.capture_update()
        await coordinator.calibration._async_finalize_session(session_id)
        task = coordinator.calibration._session_tasks.pop(session_id)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await hass.async_block_till_done()

        assert notify_mock.call_count == 2
        finish_call = notify_mock.call_args_list[1]
        assert finish_call.kwargs["notification_id"] == f"ble_trilateration_transition_{session_id}"

        assert finish_call.kwargs["title"] == "BLE Trilateration transition sample"
        finish_message = finish_call.args[1]
        assert "Position: x=1.000, y=2.000, z=3.000" in finish_message
        assert "Radius: 1.000 m" in finish_message
        assert "Capture duration: 60 s" in finish_message
        assert "Transition floors: Basement" in finish_message
        assert "Status: accepted" in finish_message
        assert "Sample ID:" in finish_message
        assert "Quality: " in finish_message
        assert "Quality details: anchors=3" in finish_message


async def test_transition_sample_diagnostics_are_exposed_without_affecting_assignment(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Stored transition samples should surface proximity diagnostics in Trilat Floor attrs."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    floor_registry = fr.async_get(hass)
    ground_floor = floor_registry.async_create("Ground floor", level=0)
    street_level = floor_registry.async_create("Street level", level=1)
    area = ar.async_get(hass).async_create("Entrance", floor_id=ground_floor.floor_id)

    devreg = dr.async_get(hass)
    device_entry = devreg.async_get_or_create(
        config_entry_id=setup_bermuda_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:22")},
        name="Phil Phone",
    )

    target = BermudaDevice("aa:bb:cc:dd:ee:22", coordinator)
    target.name = "Phil Phone"
    target.area_id = area.id
    target.area_name = area.name
    target.area_last_seen_id = area.id
    target.trilat_x_m = 1.1
    target.trilat_y_m = 2.0
    target.trilat_z_m = 3.0
    target.trilat_geometry_quality = 6.0
    target.trilat_floor_diagnostics = {"selected_floor_id": ground_floor.floor_id}
    coordinator.devices[target.address] = target

    scanner = BermudaDevice("aa:bb:cc:dd:22:01", coordinator)
    scanner.name = "Scanner"
    scanner.anchor_enabled = True
    scanner.anchor_x_m = 1.0
    scanner.anchor_y_m = 2.0
    scanner.anchor_z_m = 3.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)
    target.adverts[(target.address, scanner.address)] = SimpleNamespace(
        scanner_address=scanner.address,
        stamp=monotonic_time_coarse(),
        rssi=-65.0,
    )

    response = await hass.services.async_call(
        DOMAIN,
        "record_transition_sample",
        {
            "device_id": device_entry.id,
            "room_area_id": area.id,
            "transition_name": "front_stairs",
            "x_y_z_m": "1.0, 2.0, 3.0",
            "sample_radius_m": 1.0,
            "capture_duration_s": 30,
            "transition_floor_ids": [street_level.floor_id],
        },
        blocking=True,
        return_response=True,
    )
    session_id = response["session_id"]
    coordinator.calibration.capture_update()
    await coordinator.calibration._async_finalize_session(session_id)
    task = coordinator.calibration._session_tasks.pop(session_id)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    state = coordinator._get_trilat_decision_state(target)
    state.floor_challenger_id = street_level.floor_id
    coordinator._refresh_transition_sample_diagnostics(target, coordinator.current_anchor_layout_hash())

    diagnostics = target.trilat_floor_diagnostics
    assert diagnostics["transition_sample_count"] == 1
    assert diagnostics["transition_layout_sample_count"] == 1
    assert diagnostics["transition_support_01"] == 1.0
    assert diagnostics["transition_room_context_area_id"] == area.id
    assert diagnostics["transition_challenger_floor_id"] == street_level.floor_id
    assert diagnostics["transition_challenger_floor_name"] == "Street level"
    assert diagnostics["transition_best_name"] == "front_stairs"
    assert diagnostics["transition_best_room_area_id"] == area.id
    assert diagnostics["transition_best_room_name"] == "Entrance"
    assert diagnostics["transition_best_floor_ids"] == [street_level.floor_id]
    assert diagnostics["transition_best_floor_names"] == ["Street level"]
    assert diagnostics["transition_best_distance_mode"] == "3d"
    assert diagnostics["transition_best_within_radius"] is True
    assert diagnostics["transition_best_room_context_match"] is True
    assert diagnostics["transition_best_supports_challenger"] is True


async def test_calibration_store_management(hass: HomeAssistant, setup_bermuda_entry):
    """Calibration store should support deleting by device and layout."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    layout_hash = coordinator.calibration.current_anchor_layout_hash

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_one",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "living_room",
            "room_name": "Living Room",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": layout_hash,
            "anchors": {},
            "quality": {"status": "accepted", "eligible_anchor_count": 3, "reason": None},
        }
    )
    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_two",
            "created_at": "2026-03-06T13:00:00+00:00",
            "device_id": "device_two",
            "device_name": "Device Two",
            "device_address": "aa:bb:cc:dd:ee:02",
            "room_area_id": "office",
            "room_name": "Office",
            "position": {"x_m": 3.0, "y_m": 4.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "different_layout",
            "anchors": {},
            "quality": {"status": "accepted", "eligible_anchor_count": 3, "reason": None},
        }
    )

    removed = await coordinator.calibration.async_clear_device("device_two")
    assert removed == 1
    assert [sample["id"] for sample in coordinator.calibration.samples()] == ["sample_one"]

    removed = await coordinator.calibration.async_clear_current_anchor_layout()
    assert removed == 1
    assert coordinator.calibration.samples() == []


async def test_calibration_store_uses_stable_shared_key(hass: HomeAssistant, setup_bermuda_entry):
    """Calibration samples should use the integration storage key."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    assert coordinator.calibration_store._store.key == "ble_trilateration/calibration_samples"


async def test_calibration_layout_mismatch_can_update_samples(hass: HomeAssistant, setup_bermuda_entry):
    """Stored samples can be adopted to the current anchor geometry."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    scanner = BermudaDevice("aa:bb:cc:dd:10:21", coordinator)
    scanner.name = "Kitchen Proxy"
    scanner.anchor_x_m = 2.0
    scanner.anchor_y_m = 3.0
    scanner.anchor_z_m = 1.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    current_layout_hash = coordinator.calibration.current_anchor_layout_hash
    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_old",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "kitchen",
            "room_name": "Kitchen",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {
                scanner.address: {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 1.5, "y_m": 2.5, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    mismatch = coordinator.calibration.get_layout_mismatch_summary()
    assert mismatch is not None
    assert mismatch["sample_count"] == 1
    assert mismatch["total_sample_count"] == 1
    assert mismatch["current_layout_count"] == 0
    assert mismatch["mismatched_sample_count"] == 1
    assert mismatch["current_layout_hash"] == current_layout_hash

    updated = await coordinator.calibration.async_update_samples_to_current_geometry()
    assert updated == 1

    sample = coordinator.calibration.samples()[0]
    assert sample["anchor_layout_hash"] == current_layout_hash
    assert sample["anchors"][scanner.address]["anchor_position"] == {
        "x_m": 2.0,
        "y_m": 3.0,
        "z_m": 1.0,
    }
    assert coordinator.calibration.get_layout_mismatch_summary() is None


async def test_calibration_layout_mismatch_can_be_acknowledged(hass: HomeAssistant, setup_bermuda_entry):
    """Acknowledging a layout mismatch suppresses the repair condition for the current geometry."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    scanner = BermudaDevice("aa:bb:cc:dd:10:31", coordinator)
    scanner.name = "Living Room Proxy"
    scanner.anchor_x_m = 5.0
    scanner.anchor_y_m = 6.0
    scanner.anchor_z_m = 1.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_ack",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "living_room",
            "room_name": "Living Room",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {
                scanner.address: {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 4.5, "y_m": 6.0, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    assert coordinator.calibration.get_layout_mismatch_summary() is not None
    await coordinator.calibration.async_acknowledge_current_layout_mismatch()
    assert coordinator.calibration.get_layout_mismatch_summary() is None


async def test_calibration_layout_mismatch_raises_repair(
    hass: HomeAssistant, setup_bermuda_entry, caplog
):
    """A layout mismatch should create a fixable repair issue."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    coordinator._cancel_calibration_layout_mismatch_grace()
    coordinator._calibration_layout_mismatch_grace_active = False
    coordinator._calibration_layout_mismatch_grace_deadline = None

    scanner = BermudaDevice("aa:bb:cc:dd:10:41", coordinator)
    scanner.name = "Garage Proxy"
    scanner.anchor_x_m = 8.0
    scanner.anchor_y_m = 2.0
    scanner.anchor_z_m = 1.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_issue",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "garage",
            "room_name": "Garage",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {
                scanner.address: {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 8.5, "y_m": 2.0, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.ble_trilateration"):
        await coordinator.async_handle_calibration_samples_changed()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
    assert issue is not None
    assert issue.is_fixable is True
    assert "Calibration anchor mismatch detected" in caplog.text
    assert "Garage Proxy: moved 0.50 m" in caplog.text
    assert issue.translation_placeholders == {
        "sample_count": "1",
        "total_sample_count": "1",
        "current_layout_count": "0",
        "mismatched_sample_count": "1",
        "mismatched_layout_count": "1",
        "current_layout_hash": coordinator.calibration.current_anchor_layout_hash[:8],
        "dominant_layout_hash": "old_layo",
        "dominant_layout_count": "1",
        "changed_anchor_lines": "- Garage Proxy: moved 0.50 m",
    }


async def test_calibration_layout_mismatch_raises_repair_for_mixed_current_and_stale_samples(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Keep the repair visible when some samples match and some samples are stale."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    coordinator._cancel_calibration_layout_mismatch_grace()
    coordinator._calibration_layout_mismatch_grace_active = False
    coordinator._calibration_layout_mismatch_grace_deadline = None

    scanner = BermudaDevice("aa:bb:cc:dd:10:4a", coordinator)
    scanner.name = "Bosgame Proxy"
    scanner.anchor_x_m = 3.0
    scanner.anchor_y_m = 7.0
    scanner.anchor_z_m = 1.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    current_layout_hash = coordinator.calibration.current_anchor_layout_hash
    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_current",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "living_room",
            "room_name": "Living Room",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": current_layout_hash,
            "anchors": {
                scanner.address: {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 3.0, "y_m": 7.0, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )
    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_stale",
            "created_at": "2026-03-06T12:05:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "living_room",
            "room_name": "Living Room",
            "position": {"x_m": 2.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {
                scanner.address: {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 3.5, "y_m": 7.0, "z_m": 1.0},
                    "rssi_median": -71.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    mismatch = coordinator.calibration.get_layout_mismatch_summary()
    assert mismatch is not None
    assert mismatch["sample_count"] == 1
    assert mismatch["total_sample_count"] == 2
    assert mismatch["current_layout_count"] == 1
    assert mismatch["mismatched_sample_count"] == 1
    assert mismatch["mismatched_layout_count"] == 1

    await coordinator.async_handle_calibration_samples_changed()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
    assert issue is not None
    assert issue.translation_placeholders == {
        "sample_count": "1",
        "total_sample_count": "2",
        "current_layout_count": "1",
        "mismatched_sample_count": "1",
        "mismatched_layout_count": "1",
        "current_layout_hash": current_layout_hash[:8],
        "dominant_layout_hash": "old_layo",
        "dominant_layout_count": "1",
        "changed_anchor_lines": "- Bosgame Proxy: moved 0.50 m",
    }


async def test_calibration_layout_mismatch_updates_issue_without_recreating(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Updating mismatch details should replace the issue in place without deleting it first."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    coordinator._cancel_calibration_layout_mismatch_grace()
    coordinator._calibration_layout_mismatch_grace_active = False
    coordinator._calibration_layout_mismatch_grace_deadline = None

    scanner = BermudaDevice("aa:bb:cc:dd:10:4b", coordinator)
    scanner.name = "Garage Proxy"
    scanner.anchor_x_m = 8.5
    scanner.anchor_y_m = 2.0
    scanner.anchor_z_m = 1.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_issue_update",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "garage",
            "room_name": "Garage",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {
                scanner.address: {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 8.0, "y_m": 2.0, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    await coordinator.async_handle_calibration_samples_changed()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
    assert issue is not None
    assert issue.translation_placeholders["changed_anchor_lines"] == "- Garage Proxy: moved 0.50 m"

    with patch("custom_components.ble_trilateration.coordinator.ir.async_delete_issue") as delete_issue:
        scanner.anchor_y_m = 3.0
        await coordinator.async_handle_anchor_geometry_changed(reason="test_update")

    delete_issue.assert_not_called()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
    assert issue is not None
    assert issue.translation_placeholders["changed_anchor_lines"] == "- Garage Proxy: moved 1.12 m"


async def test_calibration_layout_mismatch_not_raised_without_current_anchor_geometry(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Do not raise a mismatch repair while current anchor geometry is still unavailable."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_startup_gap",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "garage",
            "room_name": "Garage",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {},
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    await coordinator.async_handle_calibration_samples_changed()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
    assert issue is None


async def test_calibration_layout_mismatch_not_raised_for_hash_only_alias_change(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Do not raise a mismatch repair when only the scanner identity alias changes."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    scanner = BermudaDevice("aa:bb:cc:dd:10:0e", coordinator)
    scanner.name = "Fridge Proxy"
    scanner.address_ble_mac = "aa:bb:cc:dd:10:0c"
    scanner.anchor_x_m = 8.0
    scanner.anchor_y_m = 2.0
    scanner.anchor_z_m = 1.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_alias_only",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "garage",
            "room_name": "Garage",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {
                "aa:bb:cc:dd:10:0c": {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 8.0, "y_m": 2.0, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    await coordinator.async_handle_calibration_samples_changed()

    assert coordinator.calibration.get_layout_mismatch_summary() is None
    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
    assert issue is None


async def test_calibration_layout_mismatch_not_raised_for_hash_only_same_geometry(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Do not raise a mismatch repair when only the stored layout hash differs."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    scanner = BermudaDevice("aa:bb:cc:dd:10:0f", coordinator)
    scanner.name = "Hall Proxy"
    scanner.anchor_x_m = 8.0
    scanner.anchor_y_m = 2.0
    scanner.anchor_z_m = 1.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_same_geometry",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "hall",
            "room_name": "Hall",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {
                scanner.address: {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 8.0, "y_m": 2.0, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    assert coordinator.calibration.get_layout_mismatch_summary() is None
    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
    assert issue is None


async def test_hash_only_same_geometry_samples_build_current_runtime_models(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Geometry-matching samples should feed the current runtime model even with an old stored hash."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    area = ar.async_get(hass).async_create("Hall")

    scanner = BermudaDevice("aa:bb:cc:dd:10:10", coordinator)
    scanner.name = "Hall Proxy"
    scanner.anchor_x_m = 0.0
    scanner.anchor_y_m = 0.0
    scanner.anchor_z_m = 0.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    for idx, (distance_m, rssi_dbm) in enumerate(
        ((1.0, -52.0), (2.0, -58.0), (3.0, -61.0), (4.0, -65.0), (5.0, -68.0)),
        start=1,
    ):
        await coordinator.calibration_store.async_add_sample(
            {
                "id": f"sample_layout_same_geometry_runtime_{idx}",
                "created_at": f"2026-03-06T12:0{idx}:00+00:00",
                "device_id": "device_one",
                "device_name": "Device One",
                "device_address": "aa:bb:cc:dd:ee:01",
                "room_area_id": area.id,
                "room_name": area.name,
                "position": {"x_m": distance_m, "y_m": 0.0, "z_m": 0.0},
                "sample_radius_m": 1.0,
                "anchor_layout_hash": "old_layout_hash",
                "anchors": {
                    scanner.address: {
                        "scanner_name": scanner.name,
                        "anchor_position": {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0},
                        "rssi_median": rssi_dbm,
                    }
                },
                "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
            }
        )

    await coordinator.async_handle_calibration_samples_changed()

    current_layout_hash = coordinator.calibration.current_anchor_layout_hash
    assert coordinator.calibration.get_layout_mismatch_summary() is None
    assert coordinator.ranging_model.has_model(current_layout_hash) is True
    assert coordinator.room_classifier.has_trained_rooms(current_layout_hash, None) is True


async def test_calibration_layout_mismatch_repair_is_suppressed_during_startup_grace(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Delay mismatch repairs until the startup grace period has elapsed."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    scanner = BermudaDevice("aa:bb:cc:dd:10:42", coordinator)
    scanner.name = "Office Proxy"
    scanner.anchor_x_m = 8.0
    scanner.anchor_y_m = 2.0
    scanner.anchor_z_m = 1.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_grace",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "office",
            "room_name": "Office",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {
                scanner.address: {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 7.5, "y_m": 2.0, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    coordinator._arm_calibration_layout_mismatch_grace()
    await coordinator.async_handle_calibration_samples_changed()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
    assert issue is None

    coordinator._cancel_calibration_layout_mismatch_grace()
    coordinator._calibration_layout_mismatch_grace_active = False
    coordinator._calibration_layout_mismatch_grace_deadline = None
    await coordinator.async_handle_calibration_samples_changed()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
    assert issue is not None


async def test_calibration_layout_mismatch_repair_flow_updates_samples(
    hass: HomeAssistant, setup_bermuda_entry
):
    """The repair flow should update stored sample geometry on confirmation."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    scanner = BermudaDevice("aa:bb:cc:dd:10:51", coordinator)
    scanner.name = "Hall Proxy"
    scanner.anchor_x_m = 4.0
    scanner.anchor_y_m = 5.0
    scanner.anchor_z_m = 1.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_flow",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "hall",
            "room_name": "Hall",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {
                scanner.address: {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 3.0, "y_m": 5.0, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    await coordinator.async_handle_calibration_samples_changed()

    flow = await async_create_fix_flow(
        hass,
        REPAIR_CALIBRATION_LAYOUT_MISMATCH,
        {"entry_id": setup_bermuda_entry.entry_id},
    )
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"
    assert "update_stored_sample_geometry" in result["data_schema"].schema

    result = await flow.async_step_init({"update_stored_sample_geometry": False})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "confirm_required"}

    result = await flow.async_step_init({"update_stored_sample_geometry": True})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert coordinator.calibration.samples()[0]["anchor_layout_hash"] == coordinator.calibration.current_anchor_layout_hash
    assert coordinator.calibration.get_layout_mismatch_summary() is None


async def test_calibration_layout_mismatch_repair_flow_renders_without_runtime_data(
    hass: HomeAssistant, setup_bermuda_entry
):
    """The repair flow should still render its confirm dialog without runtime data."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    scanner = BermudaDevice("aa:bb:cc:dd:10:52", coordinator)
    scanner.name = "Office Proxy"
    scanner.anchor_x_m = 7.0
    scanner.anchor_y_m = 8.0
    scanner.anchor_z_m = 1.0
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_layout_runtime_missing",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "office",
            "room_name": "Office",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": "old_layout_hash",
            "anchors": {
                scanner.address: {
                    "scanner_name": scanner.name,
                    "anchor_position": {"x_m": 6.0, "y_m": 8.0, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    await coordinator.async_handle_calibration_samples_changed()

    runtime_data = setup_bermuda_entry.runtime_data
    del setup_bermuda_entry.runtime_data
    try:
        flow = await async_create_fix_flow(
            hass,
            REPAIR_CALIBRATION_LAYOUT_MISMATCH,
            {"entry_id": setup_bermuda_entry.entry_id},
        )
        flow.hass = hass
        result = await flow.async_step_init()
    finally:
        setup_bermuda_entry.runtime_data = runtime_data

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"
    assert "update_stored_sample_geometry" in result["data_schema"].schema


async def test_calibration_samples_options_flow(hass: HomeAssistant, setup_bermuda_entry):
    """Options flow should expose and manage calibration samples."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_delete_me",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": "living_room",
            "room_name": "Living Room",
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": coordinator.calibration.current_anchor_layout_hash,
            "anchors": {},
            "quality": {"status": "accepted", "eligible_anchor_count": 3, "reason": None},
        }
    )

    result = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "calibration_samples"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == "calibration_samples"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "calibration_samples_delete_one"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "calibration_samples_delete_one"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"sample_id": "sample_delete_me"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == "calibration_samples"
    assert coordinator.calibration.samples() == []
