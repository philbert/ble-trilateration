"""Tests for coordinator trilateration decision path."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.bermuda.const import DISTANCE_TIMEOUT
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator
from custom_components.bermuda.room_classifier import RoomClassification


class _DummyScanner(SimpleNamespace):
    def __hash__(self):
        return hash(self.address)


class _DummyDevice:
    def __init__(self, address: str, mobility_type: str = "moving"):
        self.address = address
        self.name = address
        self.prefname = address
        self.name_by_user = None
        self.name_devreg = None
        self.name_bt_local_name = None
        self.name_bt_serviceinfo = None
        self.mobility_type = mobility_type
        self.create_sensor = True
        self.adverts = {}
        self.trilat_status = "unknown"
        self.trilat_reason = "init"
        self.trilat_floor_id = None
        self.trilat_floor_name = None
        self.trilat_anchor_count = 0
        self.trilat_x_m = None
        self.trilat_y_m = None
        self.trilat_z_m = None
        self.trilat_residual_m = None
        self.trilat_confidence = 0.0
        self.trilat_confidence_level = "low"
        self.trilat_horizontal_speed_mps = None
        self.trilat_vertical_speed_mps = None
        self.area_id = None
        self.area_name = None
        self.area_last_seen_id = None
        self.area_is_unknown = False
        self.diag_area_switch = None

    def get_mobility_type(self):
        return self.mobility_type

    def set_trilat_unknown(self, reason, floor_id=None, floor_name=None, anchor_count=0):
        self.trilat_status = "unknown"
        self.trilat_reason = reason
        self.trilat_floor_id = floor_id
        self.trilat_floor_name = floor_name
        self.trilat_anchor_count = anchor_count
        self.trilat_x_m = None
        self.trilat_y_m = None
        self.trilat_z_m = None
        self.trilat_residual_m = None
        self.trilat_confidence = 0.0
        self.trilat_confidence_level = "low"
        self.trilat_horizontal_speed_mps = None
        self.trilat_vertical_speed_mps = None

    def set_trilat_solution(self, x_m, y_m, z_m, floor_id, floor_name, anchor_count, residual_m):
        self.trilat_status = "ok"
        self.trilat_reason = "ok"
        self.trilat_x_m = x_m
        self.trilat_y_m = y_m
        self.trilat_z_m = z_m
        self.trilat_floor_id = floor_id
        self.trilat_floor_name = floor_name
        self.trilat_anchor_count = anchor_count
        self.trilat_residual_m = residual_m

    def apply_position_classification(self, area_id, *, floor_id=None, floor_name=None, force_unknown=False):
        if area_id is not None:
            self.area_id = area_id
            self.area_name = area_id
            self.area_last_seen_id = area_id
            self.area_is_unknown = False
        else:
            self.area_id = None
            self.area_name = "Unknown" if force_unknown else None
            self.area_is_unknown = force_unknown
        self.trilat_floor_id = floor_id
        self.trilat_floor_name = floor_name


def _make_advert(scanner, stamp, rssi, distance_raw):
    return SimpleNamespace(
        scanner_address=scanner.address,
        stamp=stamp,
        scanner_device=scanner,
        rssi_filtered=rssi,
        rssi=rssi,
        conf_rssi_offset=0.0,
        rssi_distance_raw=distance_raw,
        rssi_distance=distance_raw,
        rssi_distance_sigma_m=0.8,
        trilat_range_ewma_m=None,
    )


def _make_coordinator():
    coordinator = object.__new__(BermudaDataUpdateCoordinator)
    coordinator.options = {}
    coordinator.devices = {}
    coordinator._scanners = set()
    coordinator._trilat_decision_state = {}
    coordinator._connector_groups_by_id = {}
    coordinator._connector_area_to_group_id = {}
    coordinator._connector_group_floor_ids = {}
    coordinator.fr = SimpleNamespace(async_get_floor=lambda floor_id: SimpleNamespace(name=f"Floor {floor_id}"))
    coordinator.ar = SimpleNamespace(async_get_area=lambda _area_id: None)
    coordinator.get_scanner_anchor_x = lambda scanner_addr: getattr(coordinator.devices.get(scanner_addr), "anchor_x_m", None)
    coordinator.get_scanner_anchor_y = lambda scanner_addr: getattr(coordinator.devices.get(scanner_addr), "anchor_y_m", None)
    coordinator.get_scanner_anchor_z = lambda scanner_addr: getattr(coordinator.devices.get(scanner_addr), "anchor_z_m", None)
    coordinator.trilat_cross_floor_penalty_db = lambda: 8.0
    return coordinator


def test_trilat_unknown_when_inputs_stale():
    """No fresh adverts should yield explicit stale_inputs."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-a")
    scanner = SimpleNamespace(address="scanner-a", floor_id="f1", anchor_x_m=0.0, anchor_y_m=0.0)
    coordinator.devices[scanner.address] = scanner
    old_stamp = time.monotonic() - DISTANCE_TIMEOUT - 5
    advert = _make_advert(scanner, old_stamp, -70.0, 4.0)
    device.adverts = {("dev-a", scanner.address): advert}

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "unknown"
    assert device.trilat_reason == "stale_inputs"


def _make_scanner(coordinator, address, floor_id, x_m, y_m, z_m=None):
    """Helper: register a scanner device in the coordinator."""
    sc = _DummyScanner(
        address=address,
        name=address,
        floor_id=floor_id,
        anchor_x_m=x_m,
        anchor_y_m=y_m,
        anchor_z_m=z_m,
        is_scanner=False,
    )
    coordinator.devices[address] = sc
    coordinator._scanners.add(sc)
    return sc


def _right_triangle_anchors(coordinator, device_addr, floor_id):
    """Three anchors forming a right triangle whose circumcenter is at (3, 4) at range 5 m."""
    sc_a = _make_scanner(coordinator, f"{device_addr}-a", floor_id, 0.0, 0.0)
    sc_b = _make_scanner(coordinator, f"{device_addr}-b", floor_id, 6.0, 0.0)
    sc_c = _make_scanner(coordinator, f"{device_addr}-c", floor_id, 0.0, 8.0)
    return sc_a, sc_b, sc_c


def test_trilat_low_confidence_with_insufficient_anchors():
    """With too few anchors, trilat should retain an estimate at low confidence."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-b")

    scanner = SimpleNamespace(
        address="scanner-a",
        floor_id="f1",
        anchor_x_m=0.0,
        anchor_y_m=0.0,
        name="Scanner A",
    )
    coordinator.devices[scanner.address] = scanner

    fresh_stamp = time.monotonic()
    advert = _make_advert(scanner, fresh_stamp, -72.0, 3.5)
    device.adverts = {("dev-b", scanner.address): advert}

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "low_confidence"
    assert device.trilat_reason == "insufficient_anchors_low_confidence"
    assert device.trilat_anchor_count == 1
    assert device.trilat_x_m is not None
    assert device.trilat_y_m is not None


def test_floor_evidence_cross_floor_penalty_selects_correct_floor():
    """Cross-floor penalty should steer floor selection to the floor with more scanners."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-fev")

    # 3 scanners on f1 (same RSSI as the 1 scanner on f2)
    sc_a = _make_scanner(coordinator, "fev-a", "f1", 0.0, 0.0)
    sc_b = _make_scanner(coordinator, "fev-b", "f1", 6.0, 0.0)
    sc_c = _make_scanner(coordinator, "fev-c", "f1", 0.0, 8.0)
    sc_f2 = _make_scanner(coordinator, "fev-d", "f2", 5.0, 5.0)

    fresh = time.monotonic()
    device.adverts = {
        ("dev-fev", sc_a.address): _make_advert(sc_a, fresh, -70.0, 5.0),
        ("dev-fev", sc_b.address): _make_advert(sc_b, fresh, -70.0, 5.0),
        ("dev-fev", sc_c.address): _make_advert(sc_c, fresh, -70.0, 5.0),
        ("dev-fev", sc_f2.address): _make_advert(sc_f2, fresh, -70.0, 5.0),
    }

    coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    # 3 same-floor scanners produce higher evidence for f1 than 1 same-floor scanner does
    # for f2 (the other three get penalised when scoring for f2).
    assert state.floor_id == "f1"
    # With valid triangle geometry the solver should succeed.
    assert device.trilat_status == "ok"


def test_anchor_qualification_requires_valid_coordinates():
    """Anchors missing x or y coordinates must be excluded from the solve."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-coords")

    sc_a = _make_scanner(coordinator, "ca", "f1", 0.0, 0.0)
    # Inject None coords directly on the scanner objects.
    sc_b = _make_scanner(coordinator, "cb", "f1", None, 0.0)
    sc_c = _make_scanner(coordinator, "cc", "f1", 0.0, None)

    fresh = time.monotonic()
    device.adverts = {
        ("dev-coords", sc_a.address): _make_advert(sc_a, fresh, -70.0, 5.0),
        ("dev-coords", sc_b.address): _make_advert(sc_b, fresh, -70.0, 5.0),
        ("dev-coords", sc_c.address): _make_advert(sc_c, fresh, -70.0, 5.0),
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "low_confidence"
    assert device.trilat_reason == "insufficient_anchors_low_confidence"
    assert device.trilat_anchor_count == 1


def test_trilat_anchor_diagnostics_describe_scanner_statuses():
    """Current-cycle trilat diagnostics should expose one status per scanner."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-anchor-diag")

    sc_valid = _make_scanner(coordinator, "diag-a", "f1", 0.0, 0.0)
    sc_wrong_floor = _make_scanner(coordinator, "diag-b", "f2", 6.0, 0.0)
    sc_no_range = _make_scanner(coordinator, "diag-c", "f1", 0.0, 8.0)

    fresh = time.monotonic()
    adv_valid = _make_advert(sc_valid, fresh, -70.0, 5.0)
    adv_wrong_floor = _make_advert(sc_wrong_floor, fresh, -70.0, 5.0)
    adv_no_range = _make_advert(sc_no_range, fresh, -70.0, 5.0)
    adv_no_range.rssi_distance_raw = None
    adv_no_range.rssi_distance = None
    device.adverts = {
        ("dev-anchor-diag", sc_valid.address): adv_valid,
        ("dev-anchor-diag", sc_wrong_floor.address): adv_wrong_floor,
        ("dev-anchor-diag", sc_no_range.address): adv_no_range,
    }

    coordinator._refresh_trilateration_for_device(device)

    assert any(line.endswith(": valid") for line in device.trilat_anchor_diagnostics)
    assert any(": rejected_wrong_floor" in line for line in device.trilat_anchor_diagnostics)
    assert any(": rejected_no_range" in line for line in device.trilat_anchor_diagnostics)


def test_high_sigma_anchor_is_downweighted_not_dropped():
    """Large sigma should reduce influence, not censor the anchor outright."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-high-sigma")
    sc_a, sc_b, sc_c = _right_triangle_anchors(coordinator, "dev-high-sigma", "f1")

    fresh = time.monotonic()
    adv_a = _make_advert(sc_a, fresh, -70.0, 5.0)
    adv_b = _make_advert(sc_b, fresh, -70.0, 5.0)
    adv_c = _make_advert(sc_c, fresh, -70.0, 5.0)
    adv_c.rssi_distance_sigma_m = 12.0
    device.adverts = {
        ("dev-high-sigma", sc_a.address): adv_a,
        ("dev-high-sigma", sc_b.address): adv_b,
        ("dev-high-sigma", sc_c.address): adv_c,
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "ok"
    assert device.trilat_anchor_count == 3


def test_missing_sigma_anchor_uses_default_uncertainty():
    """Anchors with missing sigma should still contribute with a weak default weight."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-missing-sigma")
    sc_a, sc_b, sc_c = _right_triangle_anchors(coordinator, "dev-missing-sigma", "f1")

    fresh = time.monotonic()
    adv_a = _make_advert(sc_a, fresh, -70.0, 5.0)
    adv_b = _make_advert(sc_b, fresh, -70.0, 5.0)
    adv_c = _make_advert(sc_c, fresh, -70.0, 5.0)
    adv_c.rssi_distance_sigma_m = None
    device.adverts = {
        ("dev-missing-sigma", sc_a.address): adv_a,
        ("dev-missing-sigma", sc_b.address): adv_b,
        ("dev-missing-sigma", sc_c.address): adv_c,
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "ok"
    assert device.trilat_anchor_count == 3


def test_trilat_age_sigma_multiplier_grows_for_older_adverts():
    """Older-but-not-stale adverts should be downweighted by inflating sigma."""
    assert BermudaDataUpdateCoordinator._trilat_age_sigma_multiplier(0.1) == 1.0
    assert BermudaDataUpdateCoordinator._trilat_age_sigma_multiplier(3.0) > 1.0
    assert BermudaDataUpdateCoordinator._trilat_age_sigma_multiplier(8.0) > BermudaDataUpdateCoordinator._trilat_age_sigma_multiplier(3.0)


def test_area_from_trilat_holds_previous_room_on_weak_evidence():
    """Weak room evidence should hold the last stable room instead of switching to Unknown."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-room-hold")
    device.trilat_status = "ok"
    device.trilat_x_m = 10.0
    device.trilat_y_m = 2.0
    device.trilat_z_m = 3.0
    device.trilat_floor_id = "f1"
    device.trilat_floor_name = "Floor f1"
    device.area_id = "kitchen"
    device.area_name = "kitchen"
    device.area_last_seen_id = "kitchen"
    coordinator.room_classifier = SimpleNamespace(
        has_trained_rooms=lambda _layout_hash, _floor_id: True,
        classify=lambda **_kwargs: RoomClassification(
            area_id=None,
            reason="weak_room_evidence",
            best_area_id="living_room",
            best_score=0.12,
            second_score=0.05,
            topk_used=1,
            geometry_score=0.03,
            fingerprint_score=0.11,
        ),
    )

    coordinator._refresh_area_from_trilat(device, "layout-a")

    assert device.area_id == "kitchen"
    assert "hold=weak_evidence" in device.diag_area_switch


def test_area_switch_requires_room_dwell_before_switching():
    """A challenger room should need sustained evidence before Bermuda switches."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-room-switch")
    device.trilat_status = "ok"
    device.trilat_x_m = 10.0
    device.trilat_y_m = 2.0
    device.trilat_z_m = 3.0
    device.trilat_floor_id = "f1"
    device.trilat_floor_name = "Floor f1"
    device.area_id = "kitchen"
    device.area_name = "kitchen"
    device.area_last_seen_id = "kitchen"
    coordinator.room_classifier = SimpleNamespace(
        has_trained_rooms=lambda _layout_hash, _floor_id: True,
        classify=lambda **_kwargs: RoomClassification(
            area_id="living_room",
            reason="ok",
            best_area_id="living_room",
            best_score=0.62,
            second_score=0.12,
            topk_used=3,
            geometry_score=0.41,
            fingerprint_score=0.73,
        ),
    )

    with patch("custom_components.bermuda.coordinator.monotonic_time_coarse", side_effect=[100.0, 101.0, 103.0]):
        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"
        assert "hold=room_switch_dwell" in device.diag_area_switch

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "living_room"


def test_trilat_ewma_resets_on_floor_change():
    """Switching floors must reset per-advert EWMA so stale cross-floor ranges are discarded."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-ewma")

    sc_a = _make_scanner(coordinator, "ew-a", "f1", 0.0, 0.0)
    sc_b1 = _make_scanner(coordinator, "ew-b1", "f2", 5.0, 0.0)
    sc_b2 = _make_scanner(coordinator, "ew-b2", "f2", 0.0, 5.0)

    fresh = time.monotonic()
    adv_a = _make_advert(sc_a, fresh, -70.0, 4.0)
    adv_b1 = _make_advert(sc_b1, fresh, -60.0, 3.0)
    adv_b2 = _make_advert(sc_b2, fresh, -60.0, 3.0)

    # First call: only f1 scanner visible → floor = f1, EWMA initialised.
    device.adverts = {("dev-ewma", sc_a.address): adv_a}
    coordinator._refresh_trilateration_for_device(device)
    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"
    assert adv_a.trilat_range_ewma_m is not None

    # Expose the f2 scanners and force the challenger dwell to be already expired.
    device.adverts = {
        ("dev-ewma", sc_a.address): adv_a,
        ("dev-ewma", sc_b1.address): adv_b1,
        ("dev-ewma", sc_b2.address): adv_b2,
    }
    state.floor_challenger_id = "f2"
    state.floor_challenger_since = time.monotonic() - 100.0

    coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f2", "floor should have switched to f2"
    # Cross-floor scanner (f1) must have its EWMA cleared so stale ranges are discarded.
    assert adv_a.trilat_range_ewma_m is None, "EWMA must be reset on floor change for cross-floor scanner"
    # New floor's scanners get freshly initialized to rssi_distance_raw in the same call.
    assert adv_b1.trilat_range_ewma_m == adv_b1.rssi_distance_raw
    assert adv_b2.trilat_range_ewma_m == adv_b2.rssi_distance_raw


def test_floor_switch_uses_base_policy_when_floors_share_connector_group():
    """Linked floors should use the existing floor-switch margin/dwell."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-topology-linked")
    device.area_id = "living"

    sc_f1 = _make_scanner(coordinator, "linked-f1", "f1", 0.0, 0.0)
    sc_f2a = _make_scanner(coordinator, "linked-f2a", "f2", 5.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "linked-f2b", "f2", 0.0, 5.0)

    coordinator.ar = SimpleNamespace(
        async_get_area=lambda area_id: {
            "living": SimpleNamespace(id="living", floor_id="f1"),
            "stairs_f1": SimpleNamespace(id="stairs_f1", floor_id="f1"),
            "stairs_f2": SimpleNamespace(id="stairs_f2", floor_id="f2"),
        }.get(area_id)
    )
    coordinator.options = {
        "connector_groups": [
            {"id": "stairs", "name": "Stairs", "area_ids": ["stairs_f1", "stairs_f2"]}
        ]
    }
    coordinator._rebuild_connector_topology()

    fresh = time.monotonic()
    device.adverts = {
        ("dev-topology-linked", sc_f1.address): _make_advert(sc_f1, fresh, -74.0, 4.0),
        ("dev-topology-linked", sc_f2a.address): _make_advert(sc_f2a, fresh, -70.0, 3.0),
        ("dev-topology-linked", sc_f2b.address): _make_advert(sc_f2b, fresh, -70.0, 3.0),
    }
    state = coordinator._get_trilat_decision_state(device)
    state.floor_id = "f1"
    state.floor_challenger_id = "f2"
    state.floor_challenger_since = time.monotonic() - 10.0

    coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f2"


def test_floor_switch_requires_extra_margin_and_dwell_without_connector_group():
    """Unlinked floors should need the topology penalty before switching."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-topology-unlinked")
    device.area_id = "living"

    sc_f1 = _make_scanner(coordinator, "unlinked-f1", "f1", 0.0, 0.0)
    sc_f2a = _make_scanner(coordinator, "unlinked-f2a", "f2", 5.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "unlinked-f2b", "f2", 0.0, 5.0)

    coordinator.ar = SimpleNamespace(
        async_get_area=lambda area_id: {
            "living": SimpleNamespace(id="living", floor_id="f1"),
            "stairs_f1": SimpleNamespace(id="stairs_f1", floor_id="f1"),
            "stairs_f3": SimpleNamespace(id="stairs_f3", floor_id="f3"),
        }.get(area_id)
    )
    coordinator.options = {
        "connector_groups": [
            {"id": "stairs", "name": "Stairs", "area_ids": ["stairs_f1", "stairs_f3"]}
        ]
    }
    coordinator._rebuild_connector_topology()

    fresh = time.monotonic()
    device.adverts = {
        ("dev-topology-unlinked", sc_f1.address): _make_advert(sc_f1, fresh, -74.0, 4.0),
        ("dev-topology-unlinked", sc_f2a.address): _make_advert(sc_f2a, fresh, -70.0, 3.0),
        ("dev-topology-unlinked", sc_f2b.address): _make_advert(sc_f2b, fresh, -70.0, 3.0),
    }
    state = coordinator._get_trilat_decision_state(device)
    state.floor_id = "f1"
    state.floor_challenger_id = "f2"
    state.floor_challenger_since = time.monotonic() - 10.0

    coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f1"
    assert state.floor_challenger_id == "f2"


def test_floor_switch_uses_area_last_seen_when_current_area_unknown():
    """Topology should fall back to area_last_seen_id when area_id is unknown."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-topology-last-seen")
    device.area_is_unknown = True
    device.area_last_seen_id = "stairs_f1"

    sc_f1 = _make_scanner(coordinator, "lastseen-f1", "f1", 0.0, 0.0)
    sc_f2a = _make_scanner(coordinator, "lastseen-f2a", "f2", 5.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "lastseen-f2b", "f2", 0.0, 5.0)

    coordinator.ar = SimpleNamespace(
        async_get_area=lambda area_id: {
            "stairs_f1": SimpleNamespace(id="stairs_f1", floor_id="f1"),
            "stairs_f2": SimpleNamespace(id="stairs_f2", floor_id="f2"),
        }.get(area_id)
    )
    coordinator.options = {
        "connector_groups": [
            {"id": "stairs", "name": "Stairs", "area_ids": ["stairs_f1", "stairs_f2"]}
        ]
    }
    coordinator._rebuild_connector_topology()

    fresh = time.monotonic()
    device.adverts = {
        ("dev-topology-last-seen", sc_f1.address): _make_advert(sc_f1, fresh, -74.0, 4.0),
        ("dev-topology-last-seen", sc_f2a.address): _make_advert(sc_f2a, fresh, -70.0, 3.0),
        ("dev-topology-last-seen", sc_f2b.address): _make_advert(sc_f2b, fresh, -70.0, 3.0),
    }
    state = coordinator._get_trilat_decision_state(device)
    state.floor_id = "f1"
    state.floor_challenger_id = "f2"
    state.floor_challenger_since = time.monotonic() - 10.0

    coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f2"


def test_solve_skips_when_inputs_unchanged():
    """Second call with identical ranges should reuse the cached solution."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-skip")
    sc_a, sc_b, sc_c = _right_triangle_anchors(coordinator, "dev-skip", "f1")

    fresh = time.monotonic()
    adverts = {
        ("dev-skip", sc_a.address): _make_advert(sc_a, fresh, -70.0, 5.0),
        ("dev-skip", sc_b.address): _make_advert(sc_b, fresh, -70.0, 5.0),
        ("dev-skip", sc_c.address): _make_advert(sc_c, fresh, -70.0, 5.0),
    }
    device.adverts = adverts

    coordinator._refresh_trilateration_for_device(device)
    assert device.trilat_status == "ok"
    first_x = device.trilat_x_m

    coordinator._refresh_trilateration_for_device(device)
    assert device.trilat_status == "ok"
    assert device.trilat_reason == "skip_unchanged_inputs"
    assert device.trilat_x_m == first_x


def test_solve_runs_when_delta_crosses_threshold():
    """When any EWMA range shifts by >= 0.2 m the solver must re-run."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-delta")
    sc_a, sc_b, sc_c = _right_triangle_anchors(coordinator, "dev-delta", "f1")

    fresh = time.monotonic()
    adv_a = _make_advert(sc_a, fresh, -70.0, 5.0)
    device.adverts = {
        ("dev-delta", sc_a.address): adv_a,
        ("dev-delta", sc_b.address): _make_advert(sc_b, fresh, -70.0, 5.0),
        ("dev-delta", sc_c.address): _make_advert(sc_c, fresh, -70.0, 5.0),
    }

    coordinator._refresh_trilateration_for_device(device)
    assert device.trilat_status == "ok"

    # Shift range by 0.6 m — moving alpha=0.40, so EWMA delta = 0.4*0.6 = 0.24 >= 0.2.
    adv_a.rssi_distance_raw = 5.6
    adv_a.rssi_distance = 5.6

    coordinator._refresh_trilateration_for_device(device)
    assert device.trilat_status == "ok"
    assert device.trilat_reason != "skip_unchanged_inputs"


def test_trilat_3d_solves_when_four_anchors_have_z():
    """Four same-floor anchors with z coordinates should produce trilat z output."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-3d")

    sc_a = _make_scanner(coordinator, "3d-a", "f1", 0.0, 0.0, z_m=0.0)
    sc_b = _make_scanner(coordinator, "3d-b", "f1", 2.0, 0.0, z_m=0.0)
    sc_c = _make_scanner(coordinator, "3d-c", "f1", 0.0, 2.0, z_m=0.0)
    sc_d = _make_scanner(coordinator, "3d-d", "f1", 0.0, 0.0, z_m=2.0)

    fresh = time.monotonic()
    # Target point is (1, 1, 1): distance to all anchors is sqrt(3).
    dist = 3.0**0.5
    device.adverts = {
        ("dev-3d", sc_a.address): _make_advert(sc_a, fresh, -70.0, dist),
        ("dev-3d", sc_b.address): _make_advert(sc_b, fresh, -70.0, dist),
        ("dev-3d", sc_c.address): _make_advert(sc_c, fresh, -70.0, dist),
        ("dev-3d", sc_d.address): _make_advert(sc_d, fresh, -70.0, dist),
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "ok"
    assert device.trilat_z_m is not None
    assert abs(device.trilat_x_m - 1.0) < 0.2
    assert abs(device.trilat_y_m - 1.0) < 0.2
    assert abs(device.trilat_z_m - 1.0) < 0.2


def test_trilat_falls_back_to_2d_when_any_anchor_z_missing():
    """When z is incomplete, solve should remain valid but z output should be None."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-2d-fallback")

    sc_a = _make_scanner(coordinator, "2d-a", "f1", 0.0, 0.0, z_m=0.0)
    sc_b = _make_scanner(coordinator, "2d-b", "f1", 2.0, 0.0, z_m=0.0)
    sc_c = _make_scanner(coordinator, "2d-c", "f1", 0.0, 2.0, z_m=0.0)
    sc_d = _make_scanner(coordinator, "2d-d", "f1", 2.0, 2.0, z_m=None)

    fresh = time.monotonic()
    # Target point is (1, 1) in 2D: distance to all corners is sqrt(2).
    dist = 2.0**0.5
    device.adverts = {
        ("dev-2d-fallback", sc_a.address): _make_advert(sc_a, fresh, -70.0, dist),
        ("dev-2d-fallback", sc_b.address): _make_advert(sc_b, fresh, -70.0, dist),
        ("dev-2d-fallback", sc_c.address): _make_advert(sc_c, fresh, -70.0, dist),
        ("dev-2d-fallback", sc_d.address): _make_advert(sc_d, fresh, -70.0, dist),
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "ok"
    assert device.trilat_z_m is None


def test_trilat_motion_filter_caps_unphysical_xy_jump():
    """Published XY coordinates should not jump faster than the motion cap."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-motion-cap")
    sc_a, sc_b, sc_c = _right_triangle_anchors(coordinator, "dev-motion-cap", "f1")

    with patch("custom_components.bermuda.coordinator.monotonic_time_coarse", return_value=100.0):
        device.adverts = {
            ("dev-motion-cap", sc_a.address): _make_advert(sc_a, 100.0, -70.0, 5.0),
            ("dev-motion-cap", sc_b.address): _make_advert(sc_b, 100.0, -70.0, 5.0),
            ("dev-motion-cap", sc_c.address): _make_advert(sc_c, 100.0, -70.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    first_xy = (device.trilat_x_m, device.trilat_y_m)
    assert first_xy[0] is not None
    assert first_xy[1] is not None

    far_x = 20.0
    far_y = 20.0
    dist_a = ((far_x - sc_a.anchor_x_m) ** 2 + (far_y - sc_a.anchor_y_m) ** 2) ** 0.5
    dist_b = ((far_x - sc_b.anchor_x_m) ** 2 + (far_y - sc_b.anchor_y_m) ** 2) ** 0.5
    dist_c = ((far_x - sc_c.anchor_x_m) ** 2 + (far_y - sc_c.anchor_y_m) ** 2) ** 0.5

    adv_a = _make_advert(sc_a, 101.0, -70.0, dist_a)
    adv_b = _make_advert(sc_b, 101.0, -70.0, dist_b)
    adv_c = _make_advert(sc_c, 101.0, -70.0, dist_c)
    adv_a.trilat_range_ewma_m = None
    adv_b.trilat_range_ewma_m = None
    adv_c.trilat_range_ewma_m = None
    device.adverts = {
        ("dev-motion-cap", sc_a.address): adv_a,
        ("dev-motion-cap", sc_b.address): adv_b,
        ("dev-motion-cap", sc_c.address): adv_c,
    }

    with patch("custom_components.bermuda.coordinator.monotonic_time_coarse", return_value=101.0):
        coordinator._refresh_trilateration_for_device(device)

    dx = float(device.trilat_x_m) - float(first_xy[0])
    dy = float(device.trilat_y_m) - float(first_xy[1])
    published_speed = ((dx * dx) + (dy * dy)) ** 0.5 / 1.0

    assert device.trilat_status == "ok"
    assert published_speed <= coordinator._TRILAT_MAX_POSITION_SPEED_MPS
    assert device.trilat_x_m is not None and device.trilat_x_m < far_x
    assert device.trilat_y_m is not None and device.trilat_y_m < far_y
    assert device.trilat_horizontal_speed_mps is not None
    assert device.trilat_horizontal_speed_mps <= coordinator._TRILAT_MAX_POSITION_SPEED_MPS


def test_trilat_motion_filter_caps_unphysical_xy_jump_after_long_gap():
    """Long update gaps must still respect the motion cap instead of publishing the raw solve."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-motion-gap")
    sc_a, sc_b, sc_c = _right_triangle_anchors(coordinator, "dev-motion-gap", "f1")

    with patch("custom_components.bermuda.coordinator.monotonic_time_coarse", return_value=100.0):
        device.adverts = {
            ("dev-motion-gap", sc_a.address): _make_advert(sc_a, 100.0, -70.0, 5.0),
            ("dev-motion-gap", sc_b.address): _make_advert(sc_b, 100.0, -70.0, 5.0),
            ("dev-motion-gap", sc_c.address): _make_advert(sc_c, 100.0, -70.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    first_xy = (float(device.trilat_x_m), float(device.trilat_y_m))

    far_x = -100.0
    far_y = 10.0
    dist_a = ((far_x - sc_a.anchor_x_m) ** 2 + (far_y - sc_a.anchor_y_m) ** 2) ** 0.5
    dist_b = ((far_x - sc_b.anchor_x_m) ** 2 + (far_y - sc_b.anchor_y_m) ** 2) ** 0.5
    dist_c = ((far_x - sc_c.anchor_x_m) ** 2 + (far_y - sc_c.anchor_y_m) ** 2) ** 0.5

    adv_a = _make_advert(sc_a, 106.0, -70.0, dist_a)
    adv_b = _make_advert(sc_b, 106.0, -70.0, dist_b)
    adv_c = _make_advert(sc_c, 106.0, -70.0, dist_c)
    adv_a.trilat_range_ewma_m = None
    adv_b.trilat_range_ewma_m = None
    adv_c.trilat_range_ewma_m = None
    device.adverts = {
        ("dev-motion-gap", sc_a.address): adv_a,
        ("dev-motion-gap", sc_b.address): adv_b,
        ("dev-motion-gap", sc_c.address): adv_c,
    }

    with patch("custom_components.bermuda.coordinator.monotonic_time_coarse", return_value=106.0):
        coordinator._refresh_trilateration_for_device(device)

    dx = float(device.trilat_x_m) - first_xy[0]
    dy = float(device.trilat_y_m) - first_xy[1]
    published_speed = ((dx * dx) + (dy * dy)) ** 0.5 / coordinator._TRILAT_MAX_FILTER_DT_S

    assert device.trilat_status == "ok"
    assert published_speed <= coordinator._TRILAT_MAX_POSITION_SPEED_MPS
    assert device.trilat_horizontal_speed_mps is not None
    assert device.trilat_horizontal_speed_mps <= coordinator._TRILAT_MAX_POSITION_SPEED_MPS
    assert device.trilat_x_m is not None and device.trilat_x_m > far_x


def test_trilat_holds_previous_z_through_same_floor_2d_gap():
    """A prior z solution should be held, but softly pulled toward the remaining anchor-height envelope."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-z-hold")

    sc_a = _make_scanner(coordinator, "zh-a", "f1", 0.0, 0.0, z_m=0.0)
    sc_b = _make_scanner(coordinator, "zh-b", "f1", 2.0, 0.0, z_m=0.0)
    sc_c = _make_scanner(coordinator, "zh-c", "f1", 0.0, 2.0, z_m=0.0)
    sc_d = _make_scanner(coordinator, "zh-d", "f1", 0.0, 0.0, z_m=2.0)

    with patch("custom_components.bermuda.coordinator.monotonic_time_coarse", return_value=200.0):
        dist = 3.0**0.5
        device.adverts = {
            ("dev-z-hold", sc_a.address): _make_advert(sc_a, 200.0, -70.0, dist),
            ("dev-z-hold", sc_b.address): _make_advert(sc_b, 200.0, -70.0, dist),
            ("dev-z-hold", sc_c.address): _make_advert(sc_c, 200.0, -70.0, dist),
            ("dev-z-hold", sc_d.address): _make_advert(sc_d, 200.0, -70.0, dist),
        }
        coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_z_m is not None
    first_z = device.trilat_z_m

    sc_d.anchor_z_m = None
    with patch("custom_components.bermuda.coordinator.monotonic_time_coarse", return_value=201.0):
        dist_2d = 2.0**0.5
        device.adverts = {
            ("dev-z-hold", sc_a.address): _make_advert(sc_a, 201.0, -70.0, dist_2d),
            ("dev-z-hold", sc_b.address): _make_advert(sc_b, 201.0, -70.0, dist_2d),
            ("dev-z-hold", sc_c.address): _make_advert(sc_c, 201.0, -70.0, dist_2d),
            ("dev-z-hold", sc_d.address): _make_advert(sc_d, 201.0, -70.0, dist_2d),
        }
        coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "ok"
    assert device.trilat_z_m is not None
    assert device.trilat_z_m < first_z
    assert device.trilat_z_m > 0.5
    assert abs(device.trilat_z_m - 0.9) < 0.05


def test_high_residual_yields_low_confidence_solution():
    """Geometrically inconsistent ranges should keep an estimate with low confidence."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-residual")

    # Anchors far apart but all claiming the device is only 1 m away — impossible geometry.
    sc_a = _make_scanner(coordinator, "res-a", "f1", 0.0, 0.0)
    sc_b = _make_scanner(coordinator, "res-b", "f1", 15.0, 0.0)
    sc_c = _make_scanner(coordinator, "res-c", "f1", 0.0, 15.0)

    fresh = time.monotonic()
    device.adverts = {
        ("dev-residual", sc_a.address): _make_advert(sc_a, fresh, -70.0, 1.0),
        ("dev-residual", sc_b.address): _make_advert(sc_b, fresh, -70.0, 1.0),
        ("dev-residual", sc_c.address): _make_advert(sc_c, fresh, -70.0, 1.0),
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "low_confidence"
    assert device.trilat_reason == "high_residual_low_confidence"
    assert device.trilat_x_m is not None
    assert device.trilat_y_m is not None


def test_ambiguous_floor_yields_low_confidence_after_dwell():
    """Sustained equal floor evidence should degrade confidence, not blank coordinates."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-ambig")

    # One scanner per floor with equal RSSI → perfectly tied evidence.
    sc_f1 = _make_scanner(coordinator, "amb-f1", "f1", 0.0, 0.0)
    sc_f2 = _make_scanner(coordinator, "amb-f2", "f2", 0.0, 0.0)

    fresh = time.monotonic()
    device.adverts = {
        ("dev-ambig", sc_f1.address): _make_advert(sc_f1, fresh, -70.0, 4.0),
        ("dev-ambig", sc_f2.address): _make_advert(sc_f2, fresh, -70.0, 4.0),
    }

    # First call: starts the ambiguous timer but does not yet emit Unknown.
    coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_ambiguous_since > 0, "ambiguous timer should be started"

    # Expire the timer artificially.
    state.floor_ambiguous_since = time.monotonic() - 100.0

    # Second call: dwell exceeded → ambiguous floor with low confidence.
    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "low_confidence"
    assert "low_confidence" in device.trilat_reason


def test_trilat_state_is_isolated_from_area_state():
    """Trilateration updates must not write to the area decision state."""
    coordinator = _make_coordinator()
    coordinator._area_decision_state = {}
    device = _DummyDevice("dev-iso")

    sc = _make_scanner(coordinator, "iso-a", "f1", 0.0, 0.0)
    fresh = time.monotonic()
    device.adverts = {("dev-iso", sc.address): _make_advert(sc, fresh, -70.0, 4.0)}

    coordinator._refresh_trilateration_for_device(device)

    assert len(coordinator._area_decision_state) == 0, "trilat must not touch area state"
    assert device.address in coordinator._trilat_decision_state


def test_trilat_solve_prior_predicts_same_floor_state():
    """Solve prior should extrapolate the last filtered state on the current floor."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-prior-state", mobility_type="stationary")
    state = coordinator._get_trilat_decision_state(device)
    state.floor_id = "f1"
    state.last_solution_xy = (4.0, 5.0)
    state.last_solution_z = 2.0
    state.velocity_x_mps = 1.0
    state.velocity_y_mps = -0.5
    state.velocity_z_mps = 0.25
    state.last_filter_stamp = 100.0
    state.last_residual_m = 0.6
    state.last_mean_sigma_m = 1.2
    state.last_status = "ok"

    prior = coordinator._build_trilat_solve_prior(
        state,
        nowstamp=102.0,
        mobility_type=device.get_mobility_type(),
        solver_dimension="3d",
        selected_floor_id="f1",
        mean_sigma_m=1.0,
        mean_anchor_range_delta_m=0.5,
    )

    assert prior is not None
    assert abs(prior.x_m - 6.0) < 0.01
    assert abs(prior.y_m - 4.0) < 0.01
    assert abs(prior.z_m - 2.5) < 0.01
    assert prior.sigma_x_m > 0.0
    assert prior.sigma_z_m > 0.0


def test_trilat_solve_prior_skips_cross_floor_state():
    """Solve prior should not be applied across floor changes."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-prior-cross-floor")
    state = coordinator._get_trilat_decision_state(device)
    state.floor_id = "f1"
    state.last_solution_xy = (1.0, 1.0)
    state.last_filter_stamp = 50.0

    prior = coordinator._build_trilat_solve_prior(
        state,
        nowstamp=52.0,
        mobility_type=device.get_mobility_type(),
        solver_dimension="2d",
        selected_floor_id="f2",
        mean_sigma_m=1.0,
        mean_anchor_range_delta_m=0.5,
    )

    assert prior is None
