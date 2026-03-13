"""Tests for Bermuda calibration sample capture and management."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import patch

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir

from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import (
    CALIBRATION_EVENT_SAMPLE_CAPTURED,
    DOMAIN,
    REPAIR_CALIBRATION_LAYOUT_MISMATCH,
)
from custom_components.bermuda.repairs import async_create_fix_flow


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
        with patch("custom_components.bermuda.calibration.persistent_notification.async_create") as notify_mock:
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

            assert "Bermuda calibration sample" == start_call.kwargs["title"]
            assert start_call.kwargs["notification_id"] == f"bermuda_calibration_{session_id}"
            start_message = start_call.args[1]
            assert "Room: Living Room" in start_message
            assert "Position: x=4.200, y=1.800, z=1.100" in start_message
            assert "Status: started" in start_message
            assert "Expected complete at:" in start_message
            assert "Notes: Near sofa" in start_message

            assert "Bermuda calibration sample" == finish_call.kwargs["title"]
            assert finish_call.kwargs["notification_id"] == f"bermuda_calibration_{session_id}"
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
    assert "count" not in first_anchor["buckets_1s"][0]
    assert "rssi_median" not in first_anchor["buckets_1s"][0]
    assert "rssi" in first_anchor["buckets_1s"][0]


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

    assert response["merged"] is False
    assert response["transition_name"] == "stairwell"
    assert response["transition_floor_ids"] == [basement.floor_id]
    assert response["id"].startswith("transition_sample_")

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

    assert response["merged"] is False
    assert response["id"].startswith("transition_sample_")
    assert response["id"] != ""
    assert response["x_m"] == 3.0
    assert response["y_m"] == 4.0
    assert response["z_m"] == 5.0
    assert response["sample_radius_m"] == 1.5
    assert response["transition_floor_ids"] == sorted([basement.floor_id, top_floor.floor_id])

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
        assert "transition_key" not in stored
        assert "capture_count" not in stored
        assert "last_capture_duration_s" not in stored
        assert "total_capture_duration_s" not in stored


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

    with patch("custom_components.bermuda.calibration.persistent_notification.async_create") as notify_mock:
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

        assert response["merged"] is False
        assert notify_mock.call_count == 2

        start_call = notify_mock.call_args_list[0]
        finish_call = notify_mock.call_args_list[1]
        notification_id = start_call.kwargs["notification_id"]
        assert notification_id.startswith("bermuda_transition_")
        assert finish_call.kwargs["notification_id"] == notification_id

        assert start_call.kwargs["title"] == "Bermuda transition sample"
        start_message = start_call.args[1]
        assert "Room: Entrance" in start_message
        assert "Room floor: Ground floor" in start_message
        assert "Transition: stairwell" in start_message
        assert "Transition floors: Basement" in start_message
        assert "Status: started" in start_message

        assert finish_call.kwargs["title"] == "Bermuda transition sample"
        finish_message = finish_call.args[1]
        assert "Position: x=1.000, y=2.000, z=3.000" in finish_message
        assert "Radius: 1.000 m" in finish_message
        assert "Capture duration: 60 s" in finish_message
        assert "Transition floors: Basement" in finish_message
        assert "Status: stored" in finish_message
        assert "Capture count:" not in finish_message


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

    await hass.services.async_call(
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
    """Calibration samples should use the stable shared Bermuda storage key."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    assert coordinator.calibration_store._store.key == "bermuda/calibration_samples"


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
    """Acknowledging a layout mismatch suppresses the repair condition for the current hash."""
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
                    "anchor_position": {"x_m": 5.0, "y_m": 6.0, "z_m": 1.0},
                    "rssi_median": -70.0,
                }
            },
            "quality": {"status": "accepted", "eligible_anchor_count": 1, "reason": None},
        }
    )

    assert coordinator.calibration.get_layout_mismatch_summary() is not None
    await coordinator.calibration.async_acknowledge_current_layout_mismatch()
    assert coordinator.calibration.get_layout_mismatch_summary() is None


async def test_calibration_layout_mismatch_raises_repair(hass: HomeAssistant, setup_bermuda_entry):
    """A layout mismatch should create a fixable repair issue."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

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

    await coordinator.async_handle_calibration_samples_changed()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
    assert issue is not None
    assert issue.is_fixable is True


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
