"""Acceptance tests for canonical layout identity, strict repair, and data classification."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ble_trilateration.bermuda_device import BermudaDevice
from custom_components.ble_trilateration.transition_zone_store import (
    TransitionZone,
    TransitionZoneCapture,
)
from custom_components.ble_trilateration.trilat_bootstrap_store import TrilatBootstrapRecord


def _add_scanner(coordinator, address, name, x_m, y_m, z_m, ble_mac=None, wifi_mac=None):
    scanner = BermudaDevice(address, coordinator)
    scanner.name = name
    if ble_mac is not None:
        scanner.address_ble_mac = ble_mac
    if wifi_mac is not None:
        scanner.address_wifi_mac = wifi_mac
    scanner.anchor_x_m = x_m
    scanner.anchor_y_m = y_m
    scanner.anchor_z_m = z_m
    coordinator.devices[scanner.address] = scanner
    coordinator._scanner_list.add(scanner.address)
    return scanner


def _sample(sample_id, anchors, layout_hash="old_layout_hash", **overrides):
    payload = {
        "id": sample_id,
        "created_at": "2026-03-06T12:00:00+00:00",
        "device_id": "device_one",
        "device_name": "Device One",
        "device_address": "aa:bb:cc:dd:ee:01",
        "room_area_id": "garage",
        "room_name": "Garage",
        "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
        "sample_radius_m": 1.0,
        "anchor_layout_hash": layout_hash,
        "anchors": anchors,
        "quality": {"status": "accepted", "eligible_anchor_count": len(anchors), "reason": None},
    }
    payload.update(overrides)
    return payload


def _anchor(name, x_m, y_m, z_m, rssi_median=-70.0, packet_count=42):
    return {
        "scanner_name": name,
        "anchor_position": {"x_m": x_m, "y_m": y_m, "z_m": z_m},
        "rssi_median": rssi_median,
        "rssi_mean": rssi_median + 0.5,
        "rssi_mad": 1.5,
        "packet_count": packet_count,
        "first_seen_at": "2026-03-06T11:59:00+00:00",
        "last_seen_at": "2026-03-06T12:00:00+00:00",
    }


async def test_canonical_layout_hash_stable_across_ble_wifi_alias_flip(
    hass: HomeAssistant, setup_bermuda_entry
):
    """The layout hash must not change when a scanner re-registers under another MAC."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator

    wifi_mac = "aa:bb:cc:dd:20:02"
    ble_mac = "aa:bb:cc:dd:20:00"
    scanner = _add_scanner(coordinator, wifi_mac, "Flip Proxy", 8.0, 2.0, 1.0, ble_mac=ble_mac)
    await coordinator.scanner_anchor_store.async_save_scanner(scanner)
    coordinator.calibration.refresh_layout_identity()
    hash_under_wifi = coordinator.calibration.current_anchor_layout_hash

    # The same physical scanner now shows up under its BLE MAC instead.
    coordinator._scanner_list.discard(wifi_mac)
    del coordinator.devices[wifi_mac]
    _add_scanner(coordinator, ble_mac, "Flip Proxy", 8.0, 2.0, 1.0, wifi_mac=wifi_mac)
    coordinator.calibration.refresh_layout_identity()
    hash_under_ble = coordinator.calibration.current_anchor_layout_hash

    assert hash_under_wifi == hash_under_ble


async def test_classify_stored_samples_reports_explicit_buckets(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Ordinary, transition, and bootstrap data get explicit current/stale classifications."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    scanner = _add_scanner(coordinator, "aa:bb:cc:dd:21:01", "Bucket Proxy", 8.0, 2.0, 1.0)
    coordinator.calibration.refresh_layout_identity()
    current_hash = coordinator.calibration.current_anchor_layout_hash

    await coordinator.calibration_store.async_replace_samples(
        [
            _sample("s_hash_only", {scanner.address: _anchor("Bucket Proxy", 8.0, 2.0, 1.0)}),
            _sample("s_moved", {scanner.address: _anchor("Bucket Proxy", 9.5, 2.0, 1.0)}),
            _sample("s_unknown", {"aa:bb:cc:dd:99:99": _anchor("Ghost Proxy", 1.0, 1.0, 1.0)}),
            _sample("s_corrupt", {scanner.address: _anchor("Bucket Proxy", None, 2.0, 1.0)}),
        ]
    )
    await coordinator.calibration_store.async_replace_transition_samples(
        [
            _sample(
                "t_null_z",
                {scanner.address: _anchor("Bucket Proxy", 8.0, 2.0, None)},
                transition_name="stairs",
                transition_floor_ids=["f2"],
            ),
        ]
    )
    coordinator.trilat_bootstrap_store.schedule_save(
        "11:22:33:44:55:66",
        TrilatBootstrapRecord(
            saved_at="2026-03-06T12:00:00+00:00",
            floor_id="f1",
            area_id="garage",
            x_m=1.0,
            y_m=2.0,
            z_m=1.0,
            layout_hash="some_stale_hash",
            floor_confidence=0.2,
            geometry_quality_01=0.3,
        ),
    )

    summary = coordinator.calibration.classify_stored_samples()
    assert summary["current_layout_hash"] == current_hash
    assert summary["samples"] == {
        "hash_only_alias_churn": 1,
        "coordinate_correction": 1,
        "physical_layout_changed": 1,
        "corrupted_stored_geometry": 1,
    }
    assert summary["transition_samples"] == {"repairable_null_z": 1}
    assert summary["bootstrap_records"] == {"current": 0, "stale_layout": 1}
    assert summary["current_geometry_complete"] is True


async def test_repair_not_offered_when_blockers_present(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Unknown scanners or corrupted geometry block the coordinate-correction repair."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    scanner = _add_scanner(coordinator, "aa:bb:cc:dd:22:01", "Blocker Proxy", 8.0, 2.0, 1.0)
    coordinator.calibration.refresh_layout_identity()

    await coordinator.calibration_store.async_replace_samples(
        [
            _sample("s_moved", {scanner.address: _anchor("Blocker Proxy", 9.5, 2.0, 1.0)}),
            _sample("s_unknown", {"aa:bb:cc:dd:99:98": _anchor("Ghost Proxy", 1.0, 1.0, 1.0)}),
        ]
    )
    assert coordinator.calibration.get_layout_mismatch_summary() is None

    # Without the unknown-scanner sample the same moved sample is repair-eligible.
    await coordinator.calibration_store.async_replace_samples(
        [
            _sample("s_moved", {scanner.address: _anchor("Blocker Proxy", 9.5, 2.0, 1.0)}),
        ]
    )
    assert coordinator.calibration.get_layout_mismatch_summary() is not None


async def test_repair_not_offered_when_current_geometry_incomplete(
    hass: HomeAssistant, setup_bermuda_entry
):
    """A configured anchor missing its Z blocks the repair offer entirely."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    scanner = _add_scanner(coordinator, "aa:bb:cc:dd:23:01", "NoZ Proxy", 8.0, 2.0, None)
    coordinator.calibration.refresh_layout_identity()

    await coordinator.calibration_store.async_replace_samples(
        [
            _sample("s_moved", {scanner.address: _anchor("NoZ Proxy", 9.5, 2.0, 1.0)}),
        ]
    )
    assert coordinator.calibration.incomplete_current_anchor_scanners() == ["NoZ Proxy"]
    assert coordinator.calibration.get_layout_mismatch_summary() is None


async def test_repair_mutation_refuses_missing_z_and_unknown_scanners(
    hass: HomeAssistant, setup_bermuda_entry
):
    """The repair mutation must never run against unprovable geometry."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    scanner = _add_scanner(coordinator, "aa:bb:cc:dd:24:01", "Refuse Proxy", 8.0, 2.0, None)
    coordinator.calibration.refresh_layout_identity()

    await coordinator.calibration_store.async_replace_samples(
        [
            _sample("s_moved", {scanner.address: _anchor("Refuse Proxy", 9.5, 2.0, 1.0)}),
        ]
    )
    with pytest.raises(HomeAssistantError, match="missing coordinates"):
        await coordinator.calibration.async_update_samples_to_current_geometry()

    scanner.anchor_z_m = 1.0
    coordinator.calibration.refresh_layout_identity()
    await coordinator.calibration_store.async_replace_samples(
        [
            _sample("s_unknown", {"aa:bb:cc:dd:99:97": _anchor("Ghost Proxy", 1.0, 1.0, 1.0)}),
        ]
    )
    with pytest.raises(HomeAssistantError, match="missing from the current anchor set"):
        await coordinator.calibration.async_update_samples_to_current_geometry()


async def test_repair_mutation_updates_all_stores_and_preserves_statistics(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Repair rewrites ordinary samples, transition samples, and zone hashes; stats survive."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    scanner = _add_scanner(coordinator, "aa:bb:cc:dd:25:01", "Repair Proxy", 8.0, 2.0, 1.0)
    coordinator.calibration.refresh_layout_identity()
    current_hash = coordinator.calibration.current_anchor_layout_hash

    await coordinator.calibration_store.async_replace_samples(
        [
            _sample(
                "s_moved",
                {scanner.address: _anchor("Repair Proxy", 9.5, 2.0, 1.0, rssi_median=-66.0, packet_count=58)},
                layout_hash="hash_ordinary_old",
            ),
        ]
    )
    await coordinator.calibration_store.async_replace_transition_samples(
        [
            _sample(
                "t_moved",
                {scanner.address: _anchor("Repair Proxy", 9.5, 2.0, None, rssi_median=-71.0)},
                layout_hash="hash_transition_old",
                transition_name="stairs",
                transition_floor_ids=["f2"],
            ),
        ]
    )
    await coordinator.transition_zone_store.async_save_zone(
        TransitionZone(
            zone_id="zone1",
            name="stairs",
            captures=[TransitionZoneCapture(x_m=1.0, y_m=2.0, z_m=1.0, sigma_m=1.0)],
            floor_pairs=[("f1", "f2"), ("f2", "f1")],
            anchor_layout_hash="hash_transition_old",
            created_at="2026-03-06T12:00:00+00:00",
        )
    )

    updated = await coordinator.calibration.async_update_samples_to_current_geometry()
    assert updated == 2

    samples = coordinator.calibration.samples()
    assert samples[0]["anchor_layout_hash"] == current_hash
    repaired_anchor = samples[0]["anchors"][scanner.address]
    assert repaired_anchor["anchor_position"] == {"x_m": 8.0, "y_m": 2.0, "z_m": 1.0}
    assert repaired_anchor["rssi_median"] == -66.0
    assert repaired_anchor["packet_count"] == 58
    assert samples[0]["position"] == {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0}

    transition_samples = coordinator.calibration.transition_samples()
    assert transition_samples[0]["anchor_layout_hash"] == current_hash
    t_anchor = transition_samples[0]["anchors"][scanner.address]
    assert t_anchor["anchor_position"] == {"x_m": 8.0, "y_m": 2.0, "z_m": 1.0}
    assert t_anchor["rssi_median"] == -71.0
    assert all(
        value is not None
        for sample in [*samples, *transition_samples]
        for anchor in sample["anchors"].values()
        for value in anchor["anchor_position"].values()
    )

    zones = coordinator.transition_zone_store.zones
    assert zones[0].anchor_layout_hash == current_hash


async def test_transition_null_z_repaired_or_flagged(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Null-z transition anchors are backfilled when x/y prove identity, else flagged."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    scanner = _add_scanner(coordinator, "aa:bb:cc:dd:26:01", "NullZ Proxy", 8.0, 2.0, 1.0)
    coordinator.calibration.refresh_layout_identity()
    current_hash = coordinator.calibration.current_anchor_layout_hash

    await coordinator.calibration_store.async_replace_transition_samples(
        [
            _sample(
                "t_repairable",
                {scanner.address: _anchor("NullZ Proxy", 8.0, 2.0, None)},
                transition_name="stairs",
                transition_floor_ids=["f2"],
            ),
            _sample(
                "t_corrupted",
                {scanner.address: _anchor("NullZ Proxy", 4.0, 4.0, None)},
                transition_name="stairs",
                transition_floor_ids=["f2"],
            ),
        ]
    )

    # A zone already migrated from these samples under their old hash must be
    # re-keyed by the repair so it stays active.
    await coordinator.transition_zone_store.async_save_zone(
        TransitionZone(
            zone_id="zone_old_hash",
            name="stairs",
            captures=[TransitionZoneCapture(x_m=1.0, y_m=2.0, z_m=1.0, sigma_m=1.0)],
            floor_pairs=[("f1", "f2"), ("f2", "f1")],
            anchor_layout_hash="old_layout_hash",
            created_at="2026-03-06T12:00:00+00:00",
        )
    )

    result = await coordinator.calibration.async_repair_transition_sample_null_z()
    assert result == {"repaired": 1, "corrupted": 1, "unchanged": 0}
    assert coordinator.transition_zone_store.zones[0].anchor_layout_hash == current_hash

    transition_samples = {s["id"]: s for s in coordinator.calibration.transition_samples()}
    repaired = transition_samples["t_repairable"]
    assert repaired["anchors"][scanner.address]["anchor_position"]["z_m"] == 1.0
    assert repaired["anchor_layout_hash"] == current_hash

    corrupted = transition_samples["t_corrupted"]
    assert corrupted["anchors"][scanner.address]["anchor_position"]["z_m"] is None
    assert corrupted["anchor_layout_hash"] == "old_layout_hash"

    summary = coordinator.calibration.classify_stored_samples()
    assert summary["transition_samples"] == {
        "current": 1,
        "corrupted_stored_geometry": 1,
    }


async def test_transition_support_accepts_geometry_equivalent_old_hash(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Transition samples under an alias-churned hash still provide transition support."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    scanner = _add_scanner(coordinator, "aa:bb:cc:dd:27:01", "Support Proxy", 8.0, 2.0, 1.0)
    coordinator.calibration.refresh_layout_identity()
    current_hash = coordinator.calibration.current_anchor_layout_hash

    await coordinator.calibration_store.async_replace_transition_samples(
        [
            _sample(
                "t_old_hash",
                {scanner.address: _anchor("Support Proxy", 8.0, 2.0, 1.0)},
                layout_hash="hash_from_before_alias_flip",
                transition_name="stairs",
                room_floor_id="f1",
                transition_floor_ids=["f2"],
            ),
        ]
    )

    diagnostics = coordinator.calibration.transition_support_diagnostics(
        layout_hash=current_hash,
        x_m=1.0,
        y_m=2.0,
        z_m=1.0,
        room_area_id="garage",
        challenger_floor_id="f2",
        geometry_quality_01=0.5,
    )
    assert diagnostics["transition_layout_sample_count"] == 1
    assert diagnostics["transition_support_01"] == 1.0


async def test_equivalent_layout_hashes_require_full_geometry_match(
    hass: HomeAssistant, setup_bermuda_entry
):
    """A hash carried by any geometry-mismatched sample is not treated as equivalent."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    scanner = _add_scanner(coordinator, "aa:bb:cc:dd:28:01", "Equiv Proxy", 8.0, 2.0, 1.0)
    coordinator.calibration.refresh_layout_identity()
    current_hash = coordinator.calibration.current_anchor_layout_hash

    await coordinator.calibration_store.async_replace_samples(
        [
            _sample("s_match", {scanner.address: _anchor("Equiv Proxy", 8.0, 2.0, 1.0)}, layout_hash="hash_a"),
            _sample("s_moved", {scanner.address: _anchor("Equiv Proxy", 9.5, 2.0, 1.0)}, layout_hash="hash_b"),
            _sample("s_mixed_ok", {scanner.address: _anchor("Equiv Proxy", 8.0, 2.0, 1.0)}, layout_hash="hash_c"),
            _sample("s_mixed_bad", {scanner.address: _anchor("Equiv Proxy", 9.5, 2.0, 1.0)}, layout_hash="hash_c"),
        ]
    )
    coordinator.calibration.refresh_layout_identity()

    equivalent = coordinator.calibration.equivalent_layout_hashes()
    assert current_hash in equivalent
    assert "hash_a" in equivalent
    assert "hash_b" not in equivalent
    assert "hash_c" not in equivalent


async def test_calibration_capture_refuses_misconfigured_anchor(
    hass: HomeAssistant, setup_bermuda_entry
):
    """A capture must fail to start while any scanner anchor lacks an axis value."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    _add_scanner(coordinator, "aa:bb:cc:dd:30:01", "Good Proxy", 8.0, 2.0, 1.0)
    _add_scanner(coordinator, "aa:bb:cc:dd:30:02", "Half Proxy", 4.0, None, None)
    coordinator.calibration.refresh_layout_identity()

    assert coordinator.calibration.misconfigured_anchor_scanners() == ["Half Proxy (missing y/z)"]

    with pytest.raises(HomeAssistantError, match=r"Half Proxy \(missing y/z\)"):
        await coordinator.calibration.async_start_session(
            device_id="any-device",
            room_area_id="garage",
            x_m=1.0,
            y_m=2.0,
            z_m=1.0,
        )

    # Completing the configuration unblocks the precondition.
    half = coordinator.devices["aa:bb:cc:dd:30:02"]
    half.anchor_y_m = 5.0
    half.anchor_z_m = 1.5
    assert coordinator.calibration.misconfigured_anchor_scanners() == []


async def test_transition_capture_refuses_misconfigured_anchor(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Transition captures hard-fail the same way when a scanner anchor Z is missing."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    _add_scanner(coordinator, "aa:bb:cc:dd:31:01", "NoZ Capture Proxy", 8.0, 2.0, None)
    coordinator.calibration.refresh_layout_identity()

    with pytest.raises(HomeAssistantError, match=r"NoZ Capture Proxy \(missing z\)"):
        await coordinator.calibration.async_start_transition_session(
            device_id="any-device",
            room_area_id="entrance",
            transition_name="stairwell",
            x_m=12.5,
            y_m=0.5,
            z_m=3.7,
            transition_floor_ids=["basement", "top_floor"],
        )


async def test_zone_migration_pairs_room_floor_with_transition_floors(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Zone migration must pair the capture room's floor with each transition floor."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    scanner = _add_scanner(coordinator, "aa:bb:cc:dd:32:01", "Zone Proxy", 8.0, 2.0, 1.0)
    coordinator.calibration.refresh_layout_identity()

    await coordinator.calibration_store.async_replace_transition_samples(
        [
            _sample(
                "t_door",
                {scanner.address: _anchor("Zone Proxy", 8.0, 2.0, 1.0)},
                transition_name="laundry_door",
                room_floor_id="ground_floor",
                transition_floor_ids=["street_level"],
            ),
            _sample(
                "t_stair_down",
                {scanner.address: _anchor("Zone Proxy", 8.0, 2.0, 1.0)},
                transition_name="stairwell",
                room_floor_id="ground_floor",
                transition_floor_ids=["basement"],
            ),
            _sample(
                "t_stair_up",
                {scanner.address: _anchor("Zone Proxy", 8.0, 2.0, 1.0)},
                transition_name="stairwell",
                room_floor_id="ground_floor",
                transition_floor_ids=["top_floor"],
            ),
        ]
    )

    migrated = await coordinator.calibration.async_migrate_transition_samples_to_zones(
        coordinator.transition_zone_store
    )
    assert migrated == 2

    zones = {zone.name: zone for zone in coordinator.transition_zone_store.zones}
    door = zones["laundry_door"]
    assert door.covers_pair("ground_floor", "street_level")
    assert door.covers_pair("street_level", "ground_floor")

    stairwell = zones["stairwell"]
    assert stairwell.covers_pair("ground_floor", "basement")
    assert stairwell.covers_pair("basement", "ground_floor")
    assert stairwell.covers_pair("ground_floor", "top_floor")
    assert stairwell.covers_pair("top_floor", "ground_floor")


async def test_null_z_repair_retries_when_scanners_arrive_after_startup(
    hass: HomeAssistant, setup_bermuda_entry
):
    """The repair must succeed on a later pass once scanner anchors become available."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    coordinator.calibration.refresh_layout_identity()

    await coordinator.calibration_store.async_replace_transition_samples(
        [
            _sample(
                "t_late",
                {"aa:bb:cc:dd:33:01": _anchor("Late Proxy", 8.0, 2.0, None)},
                transition_name="stairs",
                transition_floor_ids=["f2"],
            ),
        ]
    )

    # First pass: scanner not discovered yet, nothing can be proven.
    result = await coordinator.calibration.async_repair_transition_sample_null_z()
    assert result == {"repaired": 0, "corrupted": 0, "unchanged": 1}

    # Scanner appears with its anchors; the geometry-changed hook re-runs the repair.
    _add_scanner(coordinator, "aa:bb:cc:dd:33:01", "Late Proxy", 8.0, 2.0, 1.0)
    await coordinator.async_handle_anchor_geometry_changed(reason="test_scanner_arrived")

    repaired = coordinator.calibration.transition_samples()[0]
    assert repaired["anchors"]["aa:bb:cc:dd:33:01"]["anchor_position"]["z_m"] == 1.0
    assert repaired["anchor_layout_hash"] == coordinator.calibration.current_anchor_layout_hash


async def test_null_z_repair_skips_while_anchor_restore_is_partial(
    hass: HomeAssistant, setup_bermuda_entry
):
    """Mid-restore partial geometry must not classify healthy samples as corrupted."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    scanner = _add_scanner(coordinator, "aa:bb:cc:dd:34:01", "Restore Proxy", 8.0, 2.0, None)
    coordinator.calibration.refresh_layout_identity()

    await coordinator.calibration_store.async_replace_transition_samples(
        [
            _sample(
                "t_mid_restore",
                {"aa:bb:cc:dd:34:01": _anchor("Restore Proxy", 8.0, 2.0, None)},
                transition_name="stairs",
                transition_floor_ids=["f2"],
            ),
        ]
    )

    # Z has not restored yet: the runner must skip without touching the sample.
    await coordinator._async_run_transition_null_z_repair()
    untouched = coordinator.calibration.transition_samples()[0]
    assert untouched["anchors"]["aa:bb:cc:dd:34:01"]["anchor_position"]["z_m"] is None
    assert untouched["anchor_layout_hash"] == "old_layout_hash"

    # Once Z restores, the same runner repairs the sample.
    scanner.anchor_z_m = 1.0
    coordinator.calibration.refresh_layout_identity()
    await coordinator._async_run_transition_null_z_repair()
    repaired = coordinator.calibration.transition_samples()[0]
    assert repaired["anchors"]["aa:bb:cc:dd:34:01"]["anchor_position"]["z_m"] == 1.0
    assert repaired["anchor_layout_hash"] == coordinator.calibration.current_anchor_layout_hash
