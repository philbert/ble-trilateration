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
from homeassistant.helpers import issue_registry as ir

from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import (
    CALIBRATION_EVENT_SAMPLE_CAPTURED,
    DOMAIN,
    REPAIR_CALIBRATION_LAYOUT_MISMATCH,
)


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
                    "x_m": 4.2,
                    "y_m": 1.8,
                    "z_m": 1.1,
                    "duration_s": 1,
                    "notes": "Near sofa",
                },
                blocking=True,
                return_response=True,
            )

            assert response["device_id"] == device_entry.id
            assert isinstance(response["expected_complete_at"], str)
            assert "T" in response["expected_complete_at"]
            assert response["sample_radius_m"] == 1.0
            session_id = response["session_id"]

            coordinator.calibration.capture_update()
            await coordinator.calibration._async_finalize_session(session_id)

            task = coordinator.calibration._session_tasks.pop(session_id)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await hass.async_block_till_done()

            notify_mock.assert_called_once()
            assert "Bermuda calibration sample complete" == notify_mock.call_args.kwargs["title"]
            assert notify_mock.call_args.kwargs["notification_id"] == f"bermuda_calibration_{session_id}"
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
