"""Tests for coordinator trilateration decision path."""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.ble_trilateration.const import (
    CONF_MAX_VELOCITY,
    DISTANCE_TIMEOUT,
)
from custom_components.ble_trilateration.coordinator import BermudaDataUpdateCoordinator
from custom_components.ble_trilateration.room_classifier import GlobalFingerprintResult, RoomClassification
from custom_components.ble_trilateration.trilat_bootstrap_store import TrilatBootstrapRecord


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
        self.trilat_x_raw_m = None
        self.trilat_y_raw_m = None
        self.trilat_z_raw_m = None
        self.trilat_x_m = None
        self.trilat_y_m = None
        self.trilat_z_m = None
        self.trilat_position_correction_x_m = 0.0
        self.trilat_position_correction_y_m = 0.0
        self.position_uncertainty_x_band_m = None
        self.position_uncertainty_y_band_m = None
        self.position_uncertainty_source = None
        self.trilat_residual_m = None
        self.trilat_confidence = 0.0
        self.trilat_confidence_level = "low"
        self.trilat_tracking_confidence = 0.0
        self.trilat_tracking_confidence_level = "low"
        self.trilat_geometry_quality = 0.0
        self.trilat_residual_consistency = 0.0
        self.trilat_geometry_gdop = None
        self.trilat_geometry_condition = None
        self.trilat_normalized_residual_rms = None
        self.trilat_horizontal_speed_mps = None
        self.trilat_vertical_speed_mps = None
        self.trilat_floor_evidence = {}
        self.trilat_floor_evidence_names = {}
        self.trilat_floor_diagnostics = {}
        self.trilat_cross_floor_anchor_count = 0
        self.trilat_cross_floor_anchor_diagnostics = []
        self.trilat_floor_switch_count = 0
        self.trilat_floor_switch_last_at = None
        self.trilat_floor_switch_last_from_floor_id = None
        self.trilat_floor_switch_last_to_floor_id = None
        self.trilat_floor_switch_last_from_name = None
        self.trilat_floor_switch_last_to_name = None
        self.trilat_floor_switch_reset_count = 0
        self.trilat_floor_switch_reset_last_at = None
        self.trilat_floor_switch_reset_last_from_floor_id = None
        self.trilat_floor_switch_reset_last_to_floor_id = None
        self.trilat_floor_switch_reset_last_from_name = None
        self.trilat_floor_switch_reset_last_to_name = None
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
        self.trilat_x_raw_m = None
        self.trilat_y_raw_m = None
        self.trilat_z_raw_m = None
        self.trilat_position_correction_x_m = 0.0
        self.trilat_position_correction_y_m = 0.0
        self.position_uncertainty_x_band_m = None
        self.position_uncertainty_y_band_m = None
        self.position_uncertainty_source = None
        self.trilat_x_m = None
        self.trilat_y_m = None
        self.trilat_z_m = None
        self.trilat_residual_m = None
        self.trilat_confidence = 0.0
        self.trilat_confidence_level = "low"
        self.trilat_tracking_confidence = 0.0
        self.trilat_tracking_confidence_level = "low"
        self.trilat_geometry_quality = 0.0
        self.trilat_residual_consistency = 0.0
        self.trilat_geometry_gdop = None
        self.trilat_geometry_condition = None
        self.trilat_normalized_residual_rms = None
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
    coordinator.options = {CONF_MAX_VELOCITY: 1.8}
    coordinator.devices = {}
    coordinator._scanners = set()
    coordinator._trilat_decision_state = {}
    coordinator.calibration = SimpleNamespace(
        current_anchor_layout_hash="layout-a",
        transition_support_diagnostics=lambda **_kwargs: {},
        trilat_position_adjustment=lambda **_kwargs: None,
        rebuild_trilat_position_model=lambda *_args, **_kwargs: None,
    )
    coordinator.fr = SimpleNamespace(async_get_floor=lambda floor_id: SimpleNamespace(name=f"Floor {floor_id}"))
    coordinator.ar = SimpleNamespace(async_get_area=lambda _area_id: None)
    coordinator.get_scanner_anchor_x = lambda scanner_addr: getattr(coordinator.devices.get(scanner_addr), "anchor_x_m", None)
    coordinator.get_scanner_anchor_y = lambda scanner_addr: getattr(coordinator.devices.get(scanner_addr), "anchor_y_m", None)
    coordinator.get_scanner_anchor_z = lambda scanner_addr: getattr(coordinator.devices.get(scanner_addr), "anchor_z_m", None)
    coordinator.trilat_cross_floor_penalty_db = lambda: 8.0
    coordinator.get_floor_z_m = lambda floor_id: None  # Phase 3: no Z config in unit tests
    coordinator.trilat_reachability_gate_enabled = lambda: False  # Phase 3: gate off in unit tests
    coordinator._transition_zone_store = SimpleNamespace(zones=[])
    coordinator.room_classifier = None
    coordinator._floor_config_store = SimpleNamespace(get=lambda _fid: None)
    coordinator._trilat_bootstrap_store = SimpleNamespace(
        get=lambda _addr: None,
        schedule_save=lambda *_args, **_kwargs: None,
    )
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

    scanner = _DummyScanner(
        address="scanner-a",
        floor_id="f1",
        anchor_x_m=0.0,
        anchor_y_m=0.0,
        name="Scanner A",
        is_scanner=False,
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


def test_trilat_position_adjustment_is_applied_to_published_coordinates():
    """Calibration-derived XY corrections should shift published trilat coordinates."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-c")
    sc_a, sc_b, sc_c = _right_triangle_anchors(coordinator, device.address, "f1")

    coordinator.calibration.trilat_position_adjustment = lambda **_kwargs: SimpleNamespace(
        correction_x_m=0.75,
        correction_y_m=-0.5,
        uncertainty_x_band_m=3.0,
        uncertainty_y_band_m=5.0,
        source="capture",
    )

    fresh_stamp = time.monotonic()
    device.adverts = {
        ("dev-c", sc_a.address): _make_advert(sc_a, fresh_stamp, -70.0, 5.0),
        ("dev-c", sc_b.address): _make_advert(sc_b, fresh_stamp, -70.0, 5.0),
        ("dev-c", sc_c.address): _make_advert(sc_c, fresh_stamp, -70.0, 5.0),
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_x_raw_m is not None
    assert device.trilat_y_raw_m is not None
    assert abs(float(device.trilat_x_m) - (float(device.trilat_x_raw_m) + 0.75)) < 0.01
    assert abs(float(device.trilat_y_m) - (float(device.trilat_y_raw_m) - 0.5)) < 0.01
    assert device.trilat_position_correction_x_m == 0.75
    assert device.trilat_position_correction_y_m == -0.5
    assert device.position_uncertainty_x_band_m == 3.0
    assert device.position_uncertainty_y_band_m == 5.0
    assert device.position_uncertainty_source == "capture"


def test_trilat_low_confidence_logs_anchor_status_counts():
    """Targeted insufficient-anchor logs should include anchor rejection counts."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-low-log")

    scanner = _DummyScanner(
        address="scanner-a",
        floor_id="f1",
        anchor_x_m=0.0,
        anchor_y_m=0.0,
        name="Scanner A",
        is_scanner=False,
    )
    coordinator.devices[scanner.address] = scanner
    coordinator._scanners.add(scanner)

    fresh_stamp = time.monotonic()
    advert = _make_advert(scanner, fresh_stamp, -72.0, 3.5)
    device.adverts = {("dev-low-log", scanner.address): advert}

    with (
        patch("custom_components.ble_trilateration.coordinator.debug_device_match", return_value=True),
        patch("custom_components.ble_trilateration.coordinator._LOGGER_TARGET_SPAM_LESS.debug") as log_debug,
    ):
        coordinator._refresh_trilateration_for_device(device)

    assert any(
        call.args[0] == "trilat_low_conf:dev-low-log:insufficient_anchors"
        and "status_counts=[%s]" in call.args[1]
        and call.args[-1] == "valid=1"
        for call in log_debug.call_args_list
    )


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


def test_floor_diagnostics_capture_evidence_and_cross_floor_candidates():
    """Diagnostics should expose floor evidence and valid cross-floor anchors."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-floor-diag")

    sc_a = _make_scanner(coordinator, "fd-a", "f1", 0.0, 0.0)
    sc_b = _make_scanner(coordinator, "fd-b", "f1", 6.0, 0.0)
    sc_c = _make_scanner(coordinator, "fd-c", "f1", 0.0, 8.0)
    sc_f2 = _make_scanner(coordinator, "fd-d", "f2", 5.0, 5.0)

    fresh = time.monotonic()
    device.adverts = {
        ("dev-floor-diag", sc_a.address): _make_advert(sc_a, fresh, -70.0, 5.0),
        ("dev-floor-diag", sc_b.address): _make_advert(sc_b, fresh, -70.0, 5.0),
        ("dev-floor-diag", sc_c.address): _make_advert(sc_c, fresh, -70.0, 5.0),
        ("dev-floor-diag", sc_f2.address): _make_advert(sc_f2, fresh, -69.0, 5.0),
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_floor_diagnostics["selected_floor_id"] == "f1"
    assert device.trilat_floor_diagnostics["best_floor_id"] == "f1"
    assert device.trilat_floor_evidence["f1"] > device.trilat_floor_evidence["f2"]
    assert device.trilat_cross_floor_anchor_count == 1
    status_entry = device.trilat_anchor_statuses["fd-d"]
    assert status_entry["status"] == "valid_other_floor"
    assert status_entry["affects_position"] is True
    assert status_entry["other_floor_sigma_m"] > 0.0
    assert any("other_floor_sigma=" in line for line in device.trilat_cross_floor_anchor_diagnostics)


def test_other_floor_anchors_always_participate_in_the_solve():
    """A valid other-floor anchor should always participate in the solve."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-phase2-soft")

    sc_a = _make_scanner(coordinator, "p2-a", "f1", 0.0, 0.0)
    sc_b = _make_scanner(coordinator, "p2-b", "f1", 6.0, 0.0)
    sc_c = _make_scanner(coordinator, "p2-c", "f2", 0.0, 8.0)

    fresh = time.monotonic()
    device.adverts = {
        ("dev-phase2-soft", sc_a.address): _make_advert(sc_a, fresh, -70.0, 5.0),
        ("dev-phase2-soft", sc_b.address): _make_advert(sc_b, fresh, -70.0, 5.0),
        ("dev-phase2-soft", sc_c.address): _make_advert(sc_c, fresh, -70.0, 5.0),
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "ok"
    assert device.trilat_anchor_count == 3
    status_entry = device.trilat_anchor_statuses["p2-c"]
    assert status_entry["status"] == "valid_other_floor"
    assert status_entry["affects_position"] is True
    assert any("valid_other_floor" in line for line in device.trilat_cross_floor_anchor_diagnostics)


def test_floor_challenger_pauses_when_fingerprint_supports_current_floor():
    """Strong current-floor fingerprints keep floor stable when RSSI noise challenges it.

    Phase 3: combined evidence (fp primary, RSSI secondary) favours the current floor,
    so the challenger never accumulates sufficient margin to form.
    """
    coordinator = _make_coordinator()
    coordinator.room_classifier = SimpleNamespace(
        fingerprint_global=lambda **_kwargs: GlobalFingerprintResult(
            area_id="guest_room",
            floor_id="f1",
            reason="ok",
            floor_confidence=0.82,
            room_confidence=0.68,
            best_score=0.62,
            second_score=0.21,
            floor_scores={"f1": 0.62, "f2": 0.14},
        )
    )
    device = _DummyDevice("dev-phase3-hold")

    sc_f1a = _make_scanner(coordinator, "p3h-a", "f1", 0.0, 0.0)
    sc_f1b = _make_scanner(coordinator, "p3h-b", "f1", 6.0, 0.0)
    sc_f1c = _make_scanner(coordinator, "p3h-c", "f1", 0.0, 8.0)
    sc_f2a = _make_scanner(coordinator, "p3h-d", "f2", 0.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "p3h-e", "f2", 6.0, 0.0)
    sc_f2c = _make_scanner(coordinator, "p3h-f", "f2", 0.0, 8.0)

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        device.adverts = {
            ("dev-phase3-hold", sc_f1a.address): _make_advert(sc_f1a, 100.0, -70.0, 5.0),
            ("dev-phase3-hold", sc_f1b.address): _make_advert(sc_f1b, 100.0, -70.0, 5.0),
            ("dev-phase3-hold", sc_f1c.address): _make_advert(sc_f1c, 100.0, -70.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=120.0):
        device.adverts = {
            ("dev-phase3-hold", sc_f1a.address): _make_advert(sc_f1a, 120.0, -82.0, 5.0),
            ("dev-phase3-hold", sc_f1b.address): _make_advert(sc_f1b, 120.0, -82.0, 5.0),
            ("dev-phase3-hold", sc_f1c.address): _make_advert(sc_f1c, 120.0, -82.0, 5.0),
            ("dev-phase3-hold", sc_f2a.address): _make_advert(sc_f2a, 120.0, -58.0, 5.0),
            ("dev-phase3-hold", sc_f2b.address): _make_advert(sc_f2b, 120.0, -58.0, 5.0),
            ("dev-phase3-hold", sc_f2c.address): _make_advert(sc_f2c, 120.0, -58.0, 5.0),
        }
        state.floor_challenger_id = "f2"
        state.floor_challenger_since = 101.0
        coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f1"
    assert state.floor_challenger_id is None
    assert device.trilat_floor_diagnostics["fingerprint_floor_id"] == "f1"
    assert device.trilat_floor_diagnostics["fingerprint_has_floor_signal"] is True


def test_floor_challenger_pauses_on_moderate_confidence_when_current_floor_score_is_still_best():
    """Moderate floor confidence should still pause when current-floor score clearly beats the challenger."""
    coordinator = _make_coordinator()
    coordinator.room_classifier = SimpleNamespace(
        fingerprint_global=lambda **_kwargs: GlobalFingerprintResult(
            area_id="guest_room",
            floor_id="f1",
            reason="ok",
            floor_confidence=0.62,
            room_confidence=0.48,
            best_score=0.41,
            second_score=0.35,
            floor_scores={"f1": 0.41, "f2": 0.24, "f3": 0.11},
        )
    )
    device = _DummyDevice("dev-phase3-moderate-hold")

    sc_f1a = _make_scanner(coordinator, "p3mh-a", "f1", 0.0, 0.0)
    sc_f1b = _make_scanner(coordinator, "p3mh-b", "f1", 6.0, 0.0)
    sc_f1c = _make_scanner(coordinator, "p3mh-c", "f1", 0.0, 8.0)
    sc_f2a = _make_scanner(coordinator, "p3mh-d", "f2", 0.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "p3mh-e", "f2", 6.0, 0.0)
    sc_f2c = _make_scanner(coordinator, "p3mh-f", "f2", 0.0, 8.0)

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        device.adverts = {
            ("dev-phase3-moderate-hold", sc_f1a.address): _make_advert(sc_f1a, 100.0, -70.0, 5.0),
            ("dev-phase3-moderate-hold", sc_f1b.address): _make_advert(sc_f1b, 100.0, -70.0, 5.0),
            ("dev-phase3-moderate-hold", sc_f1c.address): _make_advert(sc_f1c, 100.0, -70.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=120.0):
        device.adverts = {
            ("dev-phase3-moderate-hold", sc_f1a.address): _make_advert(sc_f1a, 120.0, -82.0, 5.0),
            ("dev-phase3-moderate-hold", sc_f1b.address): _make_advert(sc_f1b, 120.0, -82.0, 5.0),
            ("dev-phase3-moderate-hold", sc_f1c.address): _make_advert(sc_f1c, 120.0, -82.0, 5.0),
            ("dev-phase3-moderate-hold", sc_f2a.address): _make_advert(sc_f2a, 120.0, -58.0, 5.0),
            ("dev-phase3-moderate-hold", sc_f2b.address): _make_advert(sc_f2b, 120.0, -58.0, 5.0),
            ("dev-phase3-moderate-hold", sc_f2c.address): _make_advert(sc_f2c, 120.0, -58.0, 5.0),
        }
        state.floor_challenger_id = "f2"
        state.floor_challenger_since = 101.0
        coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f1"
    assert state.floor_challenger_id is None
    assert device.trilat_floor_diagnostics["fingerprint_floor_confidence"] == 0.62
    assert device.trilat_floor_diagnostics["fingerprint_current_floor_score"] == 0.41
    assert device.trilat_floor_diagnostics["fingerprint_has_floor_signal"] is True


def test_floor_challenger_does_not_switch_after_hold_ceiling_if_fingerprint_still_prefers_current_floor():
    """A challenger should still be vetoed at switch time when fingerprint keeps backing the current floor."""
    coordinator = _make_coordinator()
    coordinator.room_classifier = SimpleNamespace(
        fingerprint_global=lambda **_kwargs: GlobalFingerprintResult(
            area_id="guest_room",
            floor_id="f1",
            reason="ok",
            floor_confidence=0.64,
            room_confidence=0.52,
            best_score=0.43,
            second_score=0.29,
            floor_scores={"f1": 0.43, "f2": 0.26, "f3": 0.18},
        )
    )
    device = _DummyDevice("dev-phase3-veto")

    sc_f1a = _make_scanner(coordinator, "p3v-a", "f1", 0.0, 0.0)
    sc_f1b = _make_scanner(coordinator, "p3v-b", "f1", 6.0, 0.0)
    sc_f1c = _make_scanner(coordinator, "p3v-c", "f1", 0.0, 8.0)
    sc_f2a = _make_scanner(coordinator, "p3v-d", "f2", 0.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "p3v-e", "f2", 6.0, 0.0)
    sc_f2c = _make_scanner(coordinator, "p3v-f", "f2", 0.0, 8.0)

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        device.adverts = {
            ("dev-phase3-veto", sc_f1a.address): _make_advert(sc_f1a, 100.0, -70.0, 5.0),
            ("dev-phase3-veto", sc_f1b.address): _make_advert(sc_f1b, 100.0, -70.0, 5.0),
            ("dev-phase3-veto", sc_f1c.address): _make_advert(sc_f1c, 100.0, -70.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=126.0):
        device.adverts = {
            ("dev-phase3-veto", sc_f1a.address): _make_advert(sc_f1a, 126.0, -82.0, 5.0),
            ("dev-phase3-veto", sc_f1b.address): _make_advert(sc_f1b, 126.0, -82.0, 5.0),
            ("dev-phase3-veto", sc_f1c.address): _make_advert(sc_f1c, 126.0, -82.0, 5.0),
            ("dev-phase3-veto", sc_f2a.address): _make_advert(sc_f2a, 126.0, -58.0, 5.0),
            ("dev-phase3-veto", sc_f2b.address): _make_advert(sc_f2b, 126.0, -58.0, 5.0),
            ("dev-phase3-veto", sc_f2c.address): _make_advert(sc_f2c, 126.0, -58.0, 5.0),
        }
        state.floor_challenger_id = "f2"
        state.floor_challenger_since = 101.0
        state.challenger_fingerprint_hold_total_s = 16.0
        state.challenger_fingerprint_hold_expired = True
        coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f1"
    assert state.floor_challenger_id is None
    assert device.trilat_floor_diagnostics["fingerprint_has_floor_signal"] is True


def test_floor_challenger_switches_earlier_when_fingerprint_supports_challenger():
    """Strong challenger-floor fingerprints cause a switch at normal dwell.

    Phase 4: fp-dwell reduction is removed; combined evidence (fp primary) already
    boosts the challenger's score, so the switch happens at standard dwell.
    """
    coordinator = _make_coordinator()
    coordinator.room_classifier = SimpleNamespace(
        fingerprint_global=lambda **_kwargs: GlobalFingerprintResult(
            area_id="garage_front",
            floor_id="f2",
            reason="ok",
            floor_confidence=0.81,
            room_confidence=0.66,
            best_score=0.61,
            second_score=0.22,
            floor_scores={"f1": 0.15, "f2": 0.61},
        )
    )
    device = _DummyDevice("dev-phase3-switch")

    sc_f1a = _make_scanner(coordinator, "p3s-a", "f1", 0.0, 0.0)
    sc_f1b = _make_scanner(coordinator, "p3s-b", "f1", 6.0, 0.0)
    sc_f1c = _make_scanner(coordinator, "p3s-c", "f1", 0.0, 8.0)
    sc_f2a = _make_scanner(coordinator, "p3s-d", "f2", 0.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "p3s-e", "f2", 6.0, 0.0)
    sc_f2c = _make_scanner(coordinator, "p3s-f", "f2", 0.0, 8.0)

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        device.adverts = {
            ("dev-phase3-switch", sc_f1a.address): _make_advert(sc_f1a, 100.0, -70.0, 5.0),
            ("dev-phase3-switch", sc_f1b.address): _make_advert(sc_f1b, 100.0, -70.0, 5.0),
            ("dev-phase3-switch", sc_f1c.address): _make_advert(sc_f1c, 100.0, -70.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=109.0):
        device.adverts = {
            ("dev-phase3-switch", sc_f1a.address): _make_advert(sc_f1a, 109.0, -82.0, 5.0),
            ("dev-phase3-switch", sc_f1b.address): _make_advert(sc_f1b, 109.0, -82.0, 5.0),
            ("dev-phase3-switch", sc_f1c.address): _make_advert(sc_f1c, 109.0, -82.0, 5.0),
            ("dev-phase3-switch", sc_f2a.address): _make_advert(sc_f2a, 109.0, -58.0, 5.0),
            ("dev-phase3-switch", sc_f2b.address): _make_advert(sc_f2b, 109.0, -58.0, 5.0),
            ("dev-phase3-switch", sc_f2c.address): _make_advert(sc_f2c, 109.0, -58.0, 5.0),
        }
        state.floor_challenger_id = "f2"
        state.floor_challenger_since = 101.0
        coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f2"
    assert device.trilat_floor_diagnostics["fingerprint_floor_id"] == "f2"
    assert device.trilat_floor_diagnostics["effective_required_dwell_s"] == 8.0


def test_floor_challenger_switches_earlier_when_transition_supports_challenger():
    """Transition support is recorded in diagnostics but no longer reduces dwell (Phase 4).

    The switch still occurs once standard dwell expires.
    """
    coordinator = _make_coordinator()
    coordinator.calibration = SimpleNamespace(
        current_anchor_layout_hash="layout-a",
        transition_support_diagnostics=lambda **_kwargs: {"transition_support_01": 0.8},
    )
    device = _DummyDevice("dev-phase5-transition-switch")

    sc_f1a = _make_scanner(coordinator, "p5s-a", "f1", 0.0, 0.0)
    sc_f1b = _make_scanner(coordinator, "p5s-b", "f1", 6.0, 0.0)
    sc_f1c = _make_scanner(coordinator, "p5s-c", "f1", 0.0, 8.0)
    sc_f2a = _make_scanner(coordinator, "p5s-d", "f2", 0.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "p5s-e", "f2", 6.0, 0.0)
    sc_f2c = _make_scanner(coordinator, "p5s-f", "f2", 0.0, 8.0)

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        device.adverts = {
            ("dev-phase5-transition-switch", sc_f1a.address): _make_advert(sc_f1a, 100.0, -70.0, 5.0),
            ("dev-phase5-transition-switch", sc_f1b.address): _make_advert(sc_f1b, 100.0, -70.0, 5.0),
            ("dev-phase5-transition-switch", sc_f1c.address): _make_advert(sc_f1c, 100.0, -70.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=109.0):
        device.adverts = {
            ("dev-phase5-transition-switch", sc_f1a.address): _make_advert(sc_f1a, 109.0, -82.0, 5.0),
            ("dev-phase5-transition-switch", sc_f1b.address): _make_advert(sc_f1b, 109.0, -82.0, 5.0),
            ("dev-phase5-transition-switch", sc_f1c.address): _make_advert(sc_f1c, 109.0, -82.0, 5.0),
            ("dev-phase5-transition-switch", sc_f2a.address): _make_advert(sc_f2a, 109.0, -58.0, 5.0),
            ("dev-phase5-transition-switch", sc_f2b.address): _make_advert(sc_f2b, 109.0, -58.0, 5.0),
            ("dev-phase5-transition-switch", sc_f2c.address): _make_advert(sc_f2c, 109.0, -58.0, 5.0),
        }
        state.floor_challenger_id = "f2"
        state.floor_challenger_since = 101.0
        coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f2"
    assert device.trilat_floor_diagnostics["transition_support_01"] == 0.8
    assert device.trilat_floor_diagnostics["effective_required_dwell_s"] == 8.0


def test_restart_bootstrap_holds_restored_floor_until_fingerprint_is_ready():
    """A warm-started floor should survive restart bootstrap while the classifier is still cold."""
    coordinator = _make_coordinator()
    coordinator._trilat_bootstrap_store = SimpleNamespace(
        get=lambda _addr: TrilatBootstrapRecord(
            saved_at="2026-03-15T03:00:00+00:00",
            floor_id="f1",
            area_id="guest_room",
            x_m=9.0,
            y_m=7.0,
            z_m=3.3,
            layout_hash="layout-a",
            floor_confidence=0.9,
            geometry_quality_01=0.6,
        ),
        schedule_save=lambda *_args, **_kwargs: None,
    )
    device = _DummyDevice("dev-bootstrap")

    sc_f1a = _make_scanner(coordinator, "boot-a", "f1", 0.0, 0.0, 3.0)
    sc_f1b = _make_scanner(coordinator, "boot-b", "f1", 6.0, 0.0, 3.0)
    sc_f1c = _make_scanner(coordinator, "boot-c", "f1", 0.0, 8.0, 3.0)
    sc_f2a = _make_scanner(coordinator, "boot-d", "f2", 0.0, 0.0, 2.0)
    sc_f2b = _make_scanner(coordinator, "boot-e", "f2", 6.0, 0.0, 2.0)
    sc_f2c = _make_scanner(coordinator, "boot-f", "f2", 0.0, 8.0, 2.0)

    with (
        patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0),
        patch("custom_components.ble_trilateration.coordinator.now", return_value=datetime.fromisoformat("2026-03-15T03:00:20+00:00")),
    ):
        device.adverts = {
            ("dev-bootstrap", sc_f1a.address): _make_advert(sc_f1a, 100.0, -82.0, 5.0),
            ("dev-bootstrap", sc_f1b.address): _make_advert(sc_f1b, 100.0, -82.0, 5.0),
            ("dev-bootstrap", sc_f1c.address): _make_advert(sc_f1c, 100.0, -82.0, 5.0),
            ("dev-bootstrap", sc_f2a.address): _make_advert(sc_f2a, 100.0, -58.0, 5.0),
            ("dev-bootstrap", sc_f2b.address): _make_advert(sc_f2b, 100.0, -58.0, 5.0),
            ("dev-bootstrap", sc_f2c.address): _make_advert(sc_f2c, 100.0, -58.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"
    assert device.trilat_floor_diagnostics["best_floor_id"] == "f2"
    assert device.trilat_floor_diagnostics["selected_floor_id"] == "f1"
    assert device.trilat_floor_diagnostics["bootstrap_hold_active"] is True


def test_restart_bootstrap_restores_floor_even_when_layout_hash_differs():
    """A layout mismatch should suppress stale geometry, not discard the floor bootstrap."""
    coordinator = _make_coordinator()
    coordinator._trilat_bootstrap_store = SimpleNamespace(
        get=lambda _addr: TrilatBootstrapRecord(
            saved_at="2026-03-15T03:00:00+00:00",
            floor_id="f1",
            area_id="guest_room",
            x_m=9.0,
            y_m=7.0,
            z_m=3.3,
            layout_hash="layout-b",
            floor_confidence=0.9,
            geometry_quality_01=0.6,
        ),
        schedule_save=lambda *_args, **_kwargs: None,
    )
    device = _DummyDevice("dev-bootstrap-layout-mismatch")
    with patch(
        "custom_components.ble_trilateration.coordinator.now",
        return_value=datetime.fromisoformat("2026-03-15T03:00:20+00:00"),
    ):
        state = coordinator._get_trilat_decision_state(device)

    assert state.floor_id == "f1"
    assert state.bootstrap_restored_at > 0.0
    assert state.last_solution_xy is None
    assert state.last_solution_z is None
    assert state.last_good_position is None
    assert device.area_last_seen_id is None


def test_trilat_bootstrap_save_requires_fingerprint_floor_agreement():
    """Do not overwrite the restart bootstrap prior with a solve whose floor disagrees with fingerprint."""
    coordinator = _make_coordinator()
    saved_records = []
    coordinator._trilat_bootstrap_store = SimpleNamespace(
        get=lambda _addr: None,
        schedule_save=lambda address, record: saved_records.append((address, record)),
    )
    device = _DummyDevice("dev-bootstrap-save")
    device.trilat_status = "ok"
    device.trilat_floor_id = "f2"
    device.trilat_x_m = 1.5
    device.trilat_y_m = 6.9
    device.trilat_z_m = 2.0
    device.trilat_geometry_quality = 4.0
    device.area_last_seen_id = "guest_room"
    device.trilat_floor_diagnostics = {
        "fingerprint_floor_id": "f1",
        "fingerprint_floor_confidence": 0.72,
    }
    state = coordinator._get_trilat_decision_state(device)

    coordinator._schedule_trilat_bootstrap_save(device, state, layout_hash="layout-a")

    assert saved_records == []


def test_floor_challenger_does_not_reduce_dwell_on_weak_transition_support():
    """Transition support is diagnostic-only and never reduces dwell (Phase 4)."""
    coordinator = _make_coordinator()
    coordinator.calibration = SimpleNamespace(
        current_anchor_layout_hash="layout-a",
        transition_support_diagnostics=lambda **_kwargs: {"transition_support_01": 0.5},
    )
    device = _DummyDevice("dev-phase5-transition-weak")

    sc_f1a = _make_scanner(coordinator, "p5w-a", "f1", 0.0, 0.0)
    sc_f1b = _make_scanner(coordinator, "p5w-b", "f1", 6.0, 0.0)
    sc_f1c = _make_scanner(coordinator, "p5w-c", "f1", 0.0, 8.0)
    sc_f2a = _make_scanner(coordinator, "p5w-d", "f2", 0.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "p5w-e", "f2", 6.0, 0.0)
    sc_f2c = _make_scanner(coordinator, "p5w-f", "f2", 0.0, 8.0)

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        device.adverts = {
            ("dev-phase5-transition-weak", sc_f1a.address): _make_advert(sc_f1a, 100.0, -70.0, 5.0),
            ("dev-phase5-transition-weak", sc_f1b.address): _make_advert(sc_f1b, 100.0, -70.0, 5.0),
            ("dev-phase5-transition-weak", sc_f1c.address): _make_advert(sc_f1c, 100.0, -70.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=107.0):
        device.adverts = {
            ("dev-phase5-transition-weak", sc_f1a.address): _make_advert(sc_f1a, 107.0, -82.0, 5.0),
            ("dev-phase5-transition-weak", sc_f1b.address): _make_advert(sc_f1b, 107.0, -82.0, 5.0),
            ("dev-phase5-transition-weak", sc_f1c.address): _make_advert(sc_f1c, 107.0, -82.0, 5.0),
            ("dev-phase5-transition-weak", sc_f2a.address): _make_advert(sc_f2a, 107.0, -58.0, 5.0),
            ("dev-phase5-transition-weak", sc_f2b.address): _make_advert(sc_f2b, 107.0, -58.0, 5.0),
            ("dev-phase5-transition-weak", sc_f2c.address): _make_advert(sc_f2c, 107.0, -58.0, 5.0),
        }
        state.floor_challenger_id = "f2"
        state.floor_challenger_since = 101.0
        coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f1"
    assert device.trilat_floor_diagnostics["transition_support_01"] == 0.5
    assert device.trilat_floor_diagnostics["effective_required_dwell_s"] == 8.0


def test_floor_challenger_switches_despite_lacking_transition_route_when_gate_disabled():
    """Without the reachability gate, a challenger switches once dwell expires even without transition support.

    Phase 4: the transition_switch_veto mechanism is removed. The topology gate (disabled here)
    is the proper defense. With no gate and no veto, evidence competition and dwell govern the switch.
    """
    coordinator = _make_coordinator()
    coordinator.room_classifier = SimpleNamespace(
        fingerprint_global=lambda **_kwargs: GlobalFingerprintResult(
            area_id="guest_room",
            floor_id="f1",
            reason="ok",
            floor_confidence=0.60,
            room_confidence=0.48,
            best_score=0.40,
            second_score=0.39,
            floor_scores={"f1": 0.40, "f2": 0.39},
        )
    )

    def _transition_diag(**kwargs):
        room_area_id = kwargs.get("room_area_id")
        challenger_floor_id = kwargs.get("challenger_floor_id")
        if room_area_id is None and challenger_floor_id is None:
            return {
                "transition_layout_sample_count": 1,
                "transition_best_within_radius": False,
                "transition_best_floor_ids": ["f2"],
            }
        return {
            "transition_layout_sample_count": 1,
            "transition_support_01": 0.0,
            "transition_best_within_radius": False,
            "transition_best_floor_ids": ["f2"],
        }

    coordinator.calibration = SimpleNamespace(
        current_anchor_layout_hash="layout-a",
        transition_support_diagnostics=_transition_diag,
    )
    device = _DummyDevice("dev-transition-veto")
    device.area_id = "guest_room"
    device.area_last_seen_id = "guest_room"

    sc_f1a = _make_scanner(coordinator, "tv-a", "f1", 0.0, 0.0)
    sc_f1b = _make_scanner(coordinator, "tv-b", "f1", 6.0, 0.0)
    sc_f1c = _make_scanner(coordinator, "tv-c", "f1", 0.0, 8.0)
    sc_f2a = _make_scanner(coordinator, "tv-d", "f2", 0.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "tv-e", "f2", 6.0, 0.0)
    sc_f2c = _make_scanner(coordinator, "tv-f", "f2", 0.0, 8.0)

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        device.adverts = {
            ("dev-transition-veto", sc_f1a.address): _make_advert(sc_f1a, 100.0, -70.0, 5.0),
            ("dev-transition-veto", sc_f1b.address): _make_advert(sc_f1b, 100.0, -70.0, 5.0),
            ("dev-transition-veto", sc_f1c.address): _make_advert(sc_f1c, 100.0, -70.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=126.0):
        device.adverts = {
            ("dev-transition-veto", sc_f1a.address): _make_advert(sc_f1a, 126.0, -82.0, 5.0),
            ("dev-transition-veto", sc_f1b.address): _make_advert(sc_f1b, 126.0, -82.0, 5.0),
            ("dev-transition-veto", sc_f1c.address): _make_advert(sc_f1c, 126.0, -82.0, 5.0),
            ("dev-transition-veto", sc_f2a.address): _make_advert(sc_f2a, 126.0, -58.0, 5.0),
            ("dev-transition-veto", sc_f2b.address): _make_advert(sc_f2b, 126.0, -58.0, 5.0),
            ("dev-transition-veto", sc_f2c.address): _make_advert(sc_f2c, 126.0, -58.0, 5.0),
        }
        state.floor_challenger_id = "f2"
        state.floor_challenger_since = 101.0
        coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f2"
    assert device.trilat_floor_diagnostics["transition_support_01"] == 0.0


def test_floor_challenger_can_use_recent_transition_memory_when_room_context_lags():
    """A recent nearby transition sample should authorize a switch even when room context does not yet match."""
    coordinator = _make_coordinator()
    coordinator.room_classifier = SimpleNamespace(
        fingerprint_global=lambda **_kwargs: GlobalFingerprintResult(
            area_id="guest_room",
            floor_id="f1",
            reason="ok",
            floor_confidence=0.60,
            room_confidence=0.48,
            best_score=0.40,
            second_score=0.39,
            floor_scores={"f1": 0.40, "f2": 0.39},
        )
    )

    def _transition_diag(**kwargs):
        room_area_id = kwargs.get("room_area_id")
        challenger_floor_id = kwargs.get("challenger_floor_id")
        if room_area_id is None and challenger_floor_id is None:
            return {
                "transition_layout_sample_count": 1,
                "transition_best_name": "stairwell",
                "transition_best_room_area_id": "entrance_hall",
                "transition_best_floor_ids": ["f2"],
                "transition_best_within_radius": True,
            }
        return {
            "transition_layout_sample_count": 1,
            "transition_support_01": 0.0,
            "transition_best_name": "stairwell",
            "transition_best_room_area_id": "entrance_hall",
            "transition_best_floor_ids": ["f2"],
            "transition_best_within_radius": False,
        }

    coordinator.calibration = SimpleNamespace(
        current_anchor_layout_hash="layout-a",
        transition_support_diagnostics=_transition_diag,
    )
    device = _DummyDevice("dev-transition-memory")
    device.area_id = "guest_room"
    device.area_last_seen_id = "guest_room"
    device.trilat_geometry_quality = 4.0

    sc_f1a = _make_scanner(coordinator, "tm-a", "f1", 0.0, 0.0)
    sc_f1b = _make_scanner(coordinator, "tm-b", "f1", 6.0, 0.0)
    sc_f1c = _make_scanner(coordinator, "tm-c", "f1", 0.0, 8.0)
    sc_f2a = _make_scanner(coordinator, "tm-d", "f2", 0.0, 0.0)
    sc_f2b = _make_scanner(coordinator, "tm-e", "f2", 6.0, 0.0)
    sc_f2c = _make_scanner(coordinator, "tm-f", "f2", 0.0, 8.0)

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        device.adverts = {
            ("dev-transition-memory", sc_f1a.address): _make_advert(sc_f1a, 100.0, -70.0, 5.0),
            ("dev-transition-memory", sc_f1b.address): _make_advert(sc_f1b, 100.0, -70.0, 5.0),
            ("dev-transition-memory", sc_f1c.address): _make_advert(sc_f1c, 100.0, -70.0, 5.0),
        }
        coordinator._refresh_trilateration_for_device(device)

    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"
    assert state.recent_transition_name == "stairwell"
    assert state.recent_transition_floor_ids == ("f2",)

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=126.0):
        device.adverts = {
            ("dev-transition-memory", sc_f1a.address): _make_advert(sc_f1a, 126.0, -82.0, 5.0),
            ("dev-transition-memory", sc_f1b.address): _make_advert(sc_f1b, 126.0, -82.0, 5.0),
            ("dev-transition-memory", sc_f1c.address): _make_advert(sc_f1c, 126.0, -82.0, 5.0),
            ("dev-transition-memory", sc_f2a.address): _make_advert(sc_f2a, 126.0, -58.0, 5.0),
            ("dev-transition-memory", sc_f2b.address): _make_advert(sc_f2b, 126.0, -58.0, 5.0),
            ("dev-transition-memory", sc_f2c.address): _make_advert(sc_f2c, 126.0, -58.0, 5.0),
        }
        state.floor_challenger_id = "f2"
        state.floor_challenger_since = 101.0
        coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f2"
    assert device.trilat_floor_diagnostics["transition_support_01"] == 1.0
    assert device.trilat_floor_diagnostics["transition_recent_support_01"] == 1.0


def test_phase2_keeps_mean_sigma_and_z_bounds_same_floor_only():
    """Other-floor anchors should be excluded from confidence sigma and z bounds."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-phase2-bounds")

    sc_a = _make_scanner(coordinator, "p2b-a", "f1", 0.0, 0.0, z_m=0.0)
    sc_b = _make_scanner(coordinator, "p2b-b", "f1", 6.0, 0.0, z_m=0.0)
    sc_c = _make_scanner(coordinator, "p2b-c", "f1", 0.0, 8.0, z_m=0.0)
    sc_d = _make_scanner(coordinator, "p2b-d", "f2", 6.0, 8.0, z_m=10.0)

    fresh = time.monotonic()
    adv_a = _make_advert(sc_a, fresh, -70.0, 5.0)
    adv_b = _make_advert(sc_b, fresh, -70.0, 5.0)
    adv_c = _make_advert(sc_c, fresh, -70.0, 5.0)
    adv_d = _make_advert(sc_d, fresh, -70.0, 9.0)
    adv_d.rssi_distance_sigma_m = 20.0
    device.adverts = {
        ("dev-phase2-bounds", sc_a.address): adv_a,
        ("dev-phase2-bounds", sc_b.address): adv_b,
        ("dev-phase2-bounds", sc_c.address): adv_c,
        ("dev-phase2-bounds", sc_d.address): adv_d,
    }

    with patch.object(coordinator, "_apply_trilat_motion_filter", wraps=coordinator._apply_trilat_motion_filter) as motion_filter:
        coordinator._refresh_trilateration_for_device(device)

    call_kwargs = motion_filter.call_args.kwargs
    assert abs(call_kwargs["mean_sigma_m"] - 0.8) < 1e-6
    assert call_kwargs["anchor_z_bounds"] == (0.0, 0.0)
    assert device.trilat_anchor_count == 4
    assert abs(device.trilat_z_m) < 0.3


def test_phase2_cross_floor_xy_inclusion_preserves_same_floor_z():
    """Other-floor anchors should not drag z away from a solvable same-floor 3D result."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-phase2-z")

    sc_a = _make_scanner(coordinator, "p2z-a", "f1", 0.0, 0.0, z_m=0.0)
    sc_b = _make_scanner(coordinator, "p2z-b", "f1", 6.0, 0.0, z_m=0.0)
    sc_c = _make_scanner(coordinator, "p2z-c", "f1", 0.0, 8.0, z_m=0.0)
    sc_d = _make_scanner(coordinator, "p2z-d", "f1", 0.0, 0.0, z_m=2.0)
    sc_e = _make_scanner(coordinator, "p2z-e", "f2", 6.0, 8.0, z_m=10.0)

    fresh = time.monotonic()
    dist_same_floor = 26.0**0.5
    adv_a = _make_advert(sc_a, fresh, -60.0, dist_same_floor)
    adv_b = _make_advert(sc_b, fresh, -60.0, dist_same_floor)
    adv_c = _make_advert(sc_c, fresh, -60.0, dist_same_floor)
    adv_d = _make_advert(sc_d, fresh, -60.0, dist_same_floor)
    adv_e = _make_advert(sc_e, fresh, -60.0, 106.0**0.5)
    device.adverts = {
        ("dev-phase2-z", sc_a.address): adv_a,
        ("dev-phase2-z", sc_b.address): adv_b,
        ("dev-phase2-z", sc_c.address): adv_c,
        ("dev-phase2-z", sc_d.address): adv_d,
        ("dev-phase2-z", sc_e.address): adv_e,
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "ok"
    assert device.trilat_floor_id == "f1"
    assert device.trilat_anchor_count == 5
    assert device.trilat_z_m is not None
    assert abs(device.trilat_z_m - 1.0) < 0.75


def test_phase2_clears_anchor_ewma_when_floor_role_changes():
    """Changing an anchor from same-floor to other-floor should reset its EWMA range."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-phase2-ewma")

    sc_a = _make_scanner(coordinator, "p2e-a", "f1", 0.0, 0.0, z_m=0.0)
    sc_b = _make_scanner(coordinator, "p2e-b", "f1", 6.0, 0.0, z_m=0.0)
    sc_c = _make_scanner(coordinator, "p2e-c", "f1", 0.0, 8.0, z_m=0.0)
    sc_d = _make_scanner(coordinator, "p2e-d", "f2", 0.0, 0.0)
    sc_e = _make_scanner(coordinator, "p2e-e", "f2", 6.0, 0.0)
    sc_f = _make_scanner(coordinator, "p2e-f", "f2", 0.0, 8.0)

    fresh = time.monotonic()
    adv_a = _make_advert(sc_a, fresh, -60.0, 5.0)
    adv_b = _make_advert(sc_b, fresh, -60.0, 5.0)
    adv_c = _make_advert(sc_c, fresh, -60.0, 5.0)
    adv_d = _make_advert(sc_d, fresh, -60.0, 5.0)
    adv_e = _make_advert(sc_e, fresh, -60.0, 5.0)
    adv_f = _make_advert(sc_f, fresh, -60.0, 5.0)

    device.adverts = {
        ("dev-phase2-ewma", sc_a.address): adv_a,
        ("dev-phase2-ewma", sc_b.address): adv_b,
        ("dev-phase2-ewma", sc_c.address): adv_c,
    }
    coordinator._refresh_trilateration_for_device(device)
    assert adv_a.trilat_range_ewma_m == 5.0

    adv_a.rssi_distance_raw = 7.0
    adv_a.rssi_distance = 7.0
    device.adverts = {
        ("dev-phase2-ewma", sc_a.address): adv_a,
        ("dev-phase2-ewma", sc_d.address): adv_d,
        ("dev-phase2-ewma", sc_e.address): adv_e,
        ("dev-phase2-ewma", sc_f.address): adv_f,
    }
    state = coordinator._get_trilat_decision_state(device)
    state.floor_challenger_id = "f2"
    state.floor_challenger_since = time.monotonic() - 100.0

    coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f2"
    assert adv_a.trilat_range_ewma_m == 7.0


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
    assert any(": valid_other_floor" in line for line in device.trilat_anchor_diagnostics)
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
            fingerprint_best_area_id="living_room",
            fingerprint_best_score=0.73,
            fingerprint_second_score=0.40,
            fingerprint_confidence=0.33,
            fingerprint_coverage=1.0,
            fingerprint_rankings=(("living_room", 0.73, 1.0, 3), ("kitchen", 0.40, 0.75, 2)),
            sample_count=3,
        ),
    )

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", side_effect=[100.0, 101.0, 103.0]):
        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"
        assert "hold=room_switch_dwell" in device.diag_area_switch

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "living_room"


def test_area_switch_requires_extra_dwell_for_weak_transition():
    """Room switches with weak learned transition support should hold longer."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-room-transition")
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
            fingerprint_best_area_id="living_room",
            fingerprint_best_score=0.73,
            fingerprint_second_score=0.40,
            fingerprint_confidence=0.33,
            fingerprint_coverage=1.0,
            sample_count=3,
        ),
        transition_strength=lambda **_kwargs: 0.2,
    )

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", side_effect=[100.0, 102.0, 104.0, 104.6]):
        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"
        assert "transition=0.20" in device.diag_area_switch

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "living_room"


def test_area_switch_holds_when_geometry_is_weak_and_fingerprint_is_not_decisive():
    """Weak geometry should keep the stable room when fingerprint does not clearly back the challenger."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-room-guard")
    device.trilat_status = "ok"
    device.trilat_x_m = 10.0
    device.trilat_y_m = 2.0
    device.trilat_z_m = 3.0
    device.trilat_floor_id = "f1"
    device.trilat_floor_name = "Floor f1"
    device.trilat_geometry_quality = 2.0
    device.area_id = "kitchen"
    device.area_name = "kitchen"
    device.area_last_seen_id = "kitchen"
    coordinator.room_classifier = SimpleNamespace(
        has_trained_rooms=lambda _layout_hash, _floor_id: True,
        classify=lambda **_kwargs: RoomClassification(
            area_id="living_room",
            reason="ok",
            best_area_id="living_room",
            best_score=0.52,
            second_score=0.46,
            topk_used=2,
            geometry_score=0.08,
            fingerprint_score=0.55,
            fingerprint_best_area_id="living_room",
            fingerprint_best_score=0.55,
            fingerprint_second_score=0.53,
            fingerprint_confidence=0.02,
            fingerprint_coverage=0.80,
            sample_count=3,
        ),
        room_reference_point=lambda *_args, **_kwargs: (0.0, 0.0, 0.0),
    )

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        coordinator._refresh_area_from_trilat(device, "layout-a")

    state = coordinator._get_trilat_decision_state(device)
    assert device.area_id == "kitchen"
    assert state.room_challenger_id is None
    assert "hold=weak_geometry_guardrail" in device.diag_area_switch


def test_area_switch_allows_decisive_fingerprint_challenger_when_geometry_is_weak():
    """Weak geometry should still allow a challenger to accumulate dwell when fingerprint is decisive."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-room-guard-pass")
    device.trilat_status = "ok"
    device.trilat_x_m = 10.0
    device.trilat_y_m = 2.0
    device.trilat_z_m = 3.0
    device.trilat_floor_id = "f1"
    device.trilat_floor_name = "Floor f1"
    device.trilat_geometry_quality = 2.0
    device.area_id = "kitchen"
    device.area_name = "kitchen"
    device.area_last_seen_id = "kitchen"
    coordinator.room_classifier = SimpleNamespace(
        has_trained_rooms=lambda _layout_hash, _floor_id: True,
        classify=lambda **_kwargs: RoomClassification(
            area_id="living_room",
            reason="ok",
            best_area_id="living_room",
            best_score=0.70,
            second_score=0.10,
            topk_used=2,
            geometry_score=0.08,
            fingerprint_score=0.72,
            fingerprint_best_area_id="living_room",
            fingerprint_best_score=0.72,
            fingerprint_second_score=0.45,
            fingerprint_confidence=0.27,
            fingerprint_coverage=1.0,
            sample_count=3,
        ),
        room_reference_point=lambda *_args, **_kwargs: (0.0, 0.0, 0.0),
    )

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        coordinator._refresh_area_from_trilat(device, "layout-a")

    state = coordinator._get_trilat_decision_state(device)
    assert device.area_id == "kitchen"
    assert state.room_challenger_id == "living_room"
    assert "hold=weak_geometry_guardrail" not in device.diag_area_switch
    assert "hold=room_switch_dwell" in device.diag_area_switch


def test_area_switch_requires_extra_dwell_for_sparse_room_challenger():
    """Sparse challenger rooms should need longer dwell before switching."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-room-sparse")
    device.trilat_status = "ok"
    device.trilat_x_m = 10.0
    device.trilat_y_m = 2.0
    device.trilat_z_m = 3.0
    device.trilat_floor_id = "f1"
    device.trilat_floor_name = "Floor f1"
    device.trilat_geometry_quality = 5.0
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
            fingerprint_best_area_id="living_room",
            fingerprint_best_score=0.73,
            fingerprint_second_score=0.48,
            fingerprint_confidence=0.25,
            fingerprint_coverage=1.0,
            sample_count=1,
        ),
        room_reference_point=lambda *_args, **_kwargs: (0.0, 0.0, 0.0),
    )

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", side_effect=[100.0, 101.5, 103.1]):
        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"
        assert "hold=room_switch_dwell(3.0s)" in device.diag_area_switch

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "living_room"


def test_area_switch_resets_sparse_challenger_when_margin_is_too_small():
    """Sparse challenger rooms should be held before dwell accumulation if the score margin is too small."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-room-sparse-margin")
    device.trilat_status = "ok"
    device.trilat_x_m = 10.0
    device.trilat_y_m = 2.0
    device.trilat_z_m = 3.0
    device.trilat_floor_id = "f1"
    device.trilat_floor_name = "Floor f1"
    device.trilat_geometry_quality = 5.0
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
            second_score=0.55,
            topk_used=3,
            geometry_score=0.41,
            fingerprint_score=0.73,
            fingerprint_best_area_id="living_room",
            fingerprint_best_score=0.73,
            fingerprint_second_score=0.48,
            fingerprint_confidence=0.25,
            fingerprint_coverage=1.0,
            sample_count=1,
        ),
        room_reference_point=lambda *_args, **_kwargs: (0.0, 0.0, 0.0),
    )

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
        coordinator._refresh_area_from_trilat(device, "layout-a")

    state = coordinator._get_trilat_decision_state(device)
    assert device.area_id == "kitchen"
    assert state.room_challenger_id is None
    assert "hold=min_sample_margin(0.10)" in device.diag_area_switch


def test_room_live_covariance_xy_reflects_weak_axis_geometry():
    """Covariance helper should report larger variance on the weakly observed axis."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-room-cov")
    device.trilat_x_m = 0.0
    device.trilat_y_m = 0.0

    sc_north = _make_scanner(coordinator, "scanner-north", "f1", 0.0, 5.0)
    sc_south = _make_scanner(coordinator, "scanner-south", "f1", 0.0, -5.0)
    adverts = [
        _make_advert(sc_north, time.monotonic(), -70.0, 5.0),
        _make_advert(sc_south, time.monotonic(), -70.0, 5.0),
    ]

    covariance = coordinator._room_live_covariance_xy(device, adverts)

    assert covariance is not None
    cov_xx, cov_xy, cov_yy = covariance
    assert cov_xx > cov_yy
    assert abs(cov_xy) < 1e-3


def test_area_switch_weak_axis_alignment_increases_dwell_end_to_end():
    """Weak-axis aligned room switches should get the extra dwell multiplier in the live path."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-room-weak-axis")
    device.trilat_status = "ok"
    device.trilat_x_m = 0.0
    device.trilat_y_m = 0.0
    device.trilat_z_m = 3.0
    device.trilat_floor_id = "f1"
    device.trilat_floor_name = "Floor f1"
    device.trilat_geometry_quality = 5.0
    device.area_id = "kitchen"
    device.area_name = "kitchen"
    device.area_last_seen_id = "kitchen"

    sc_north = _make_scanner(coordinator, "scanner-north", "f1", 0.0, 5.0)
    sc_south = _make_scanner(coordinator, "scanner-south", "f1", 0.0, -5.0)
    nowstamp = time.monotonic()
    device.adverts = {
        ("dev-room-weak-axis", sc_north.address): _make_advert(sc_north, nowstamp, -70.0, 5.0),
        ("dev-room-weak-axis", sc_south.address): _make_advert(sc_south, nowstamp, -70.0, 5.0),
    }

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
            fingerprint_best_area_id="living_room",
            fingerprint_best_score=0.73,
            fingerprint_second_score=0.48,
            fingerprint_confidence=0.25,
            fingerprint_coverage=1.0,
            sample_count=3,
        ),
        room_reference_point=lambda _layout_hash, _floor_id, area_id: {
            "kitchen": (0.0, 0.0, 0.0),
            "living_room": (2.0, 0.0, 0.0),
        }.get(area_id),
    )

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", side_effect=[100.0, 102.0, 102.3]):
        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"
        assert "weak_axis_aligned=yes" in device.diag_area_switch
        assert "hold=room_switch_dwell(2.2s)" in device.diag_area_switch

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "kitchen"

        coordinator._refresh_area_from_trilat(device, "layout-a")
        assert device.area_id == "living_room"


def test_area_switch_emits_target_room_diag_logging():
    """Targeted debug devices should log the room-classifier decision summary."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-room-log")
    device.name = "Phil's iPhone"
    device.prefname = "Phil's iPhone"
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
            fingerprint_best_area_id="living_room",
            fingerprint_best_score=0.73,
            fingerprint_second_score=0.40,
            fingerprint_confidence=0.33,
            fingerprint_coverage=1.0,
            fingerprint_rankings=(("living_room", 0.73, 1.0, 3), ("kitchen", 0.40, 0.75, 2)),
            sample_count=3,
        ),
    )

    with (
        patch("custom_components.ble_trilateration.coordinator.debug_device_match", return_value=True),
        patch("custom_components.ble_trilateration.coordinator._LOGGER_TARGET_SPAM_LESS.debug") as log_debug,
        patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0),
    ):
        coordinator._refresh_area_from_trilat(device, "layout-a")

    log_debug.assert_called_once()
    args = log_debug.call_args.args
    assert args[0] == "trilat_room_diag:dev-room-log"
    assert "Trilat room diag:" in args[1]
    assert args[2] == "Phil's iPhone"
    assert args[3] == "f1"
    assert args[4] == "kitchen"
    assert args[5] == "living_room"
    assert args[6] == "living_room"
    assert args[7] == "kitchen"
    assert "hold=room_switch_dwell" in args[8]
    assert "geom=0.41" in args[8]
    assert "fp=0.73" in args[8]
    assert "fp_rooms=living_room:0.73/1.00/3,kitchen:0.40/0.75/2" in args[8]


def test_trilat_floor_switch_preserves_state_and_ewma():
    """Switching floors should preserve continuity state and existing EWMA values."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-ewma")

    sc_a = _make_scanner(coordinator, "ew-a", "f1", 0.0, 0.0, z_m=0.0)
    sc_b = _make_scanner(coordinator, "ew-b", "f1", 6.0, 0.0, z_m=0.0)
    sc_c = _make_scanner(coordinator, "ew-c", "f1", 0.0, 8.0, z_m=0.0)
    sc_d = _make_scanner(coordinator, "ew-d", "f1", 0.0, 0.0, z_m=2.0)
    sc_e1 = _make_scanner(coordinator, "ew-e1", "f2", 0.0, 0.0)
    sc_e2 = _make_scanner(coordinator, "ew-e2", "f2", 6.0, 0.0)
    sc_e3 = _make_scanner(coordinator, "ew-e3", "f2", 0.0, 8.0)

    fresh = time.monotonic()
    dist_3d = 26.0**0.5
    adv_a = _make_advert(sc_a, fresh, -60.0, dist_3d)
    adv_b = _make_advert(sc_b, fresh, -60.0, dist_3d)
    adv_c = _make_advert(sc_c, fresh, -60.0, dist_3d)
    adv_d = _make_advert(sc_d, fresh, -60.0, dist_3d)
    adv_e1 = _make_advert(sc_e1, fresh, -60.0, 5.0)
    adv_e2 = _make_advert(sc_e2, fresh, -60.0, 5.0)
    adv_e3 = _make_advert(sc_e3, fresh, -60.0, 5.0)

    # First call: solve a stable 3D point on f1.
    device.adverts = {
        ("dev-ewma", sc_a.address): adv_a,
        ("dev-ewma", sc_b.address): adv_b,
        ("dev-ewma", sc_c.address): adv_c,
        ("dev-ewma", sc_d.address): adv_d,
    }
    coordinator._refresh_trilateration_for_device(device)
    state = coordinator._get_trilat_decision_state(device)
    assert state.floor_id == "f1"
    assert adv_a.trilat_range_ewma_m is not None
    assert device.trilat_z_m is not None
    prior_z = device.trilat_z_m

    # Expose the f2 scanners and force the challenger dwell to be already expired.
    device.adverts = {
        ("dev-ewma", sc_a.address): adv_a,
        ("dev-ewma", sc_e1.address): adv_e1,
        ("dev-ewma", sc_e2.address): adv_e2,
        ("dev-ewma", sc_e3.address): adv_e3,
    }
    state.floor_challenger_id = "f2"
    state.floor_challenger_since = time.monotonic() - 100.0

    coordinator._refresh_trilateration_for_device(device)

    assert state.floor_id == "f2", "floor should have switched to f2"
    assert adv_a.trilat_range_ewma_m is not None, "EWMA should be preserved across floor switches"
    assert adv_e1.trilat_range_ewma_m == adv_e1.rssi_distance_raw
    assert adv_e2.trilat_range_ewma_m == adv_e2.rssi_distance_raw
    assert adv_e3.trilat_range_ewma_m == adv_e3.rssi_distance_raw
    assert device.trilat_floor_switch_count == 1
    assert device.trilat_floor_switch_last_from_floor_id == "f1"
    assert device.trilat_floor_switch_last_to_floor_id == "f2"
    assert device.trilat_floor_switch_reset_count == 0
    assert device.trilat_floor_diagnostics["reason"] == "floor_switch_preserved_state"
    assert state.last_solution_xy is not None
    assert device.trilat_z_m is not None
    assert abs(device.trilat_z_m - prior_z) < 1.0


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

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
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

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=101.0):
        coordinator._refresh_trilateration_for_device(device)

    dx = float(device.trilat_x_m) - float(first_xy[0])
    dy = float(device.trilat_y_m) - float(first_xy[1])
    published_speed = ((dx * dx) + (dy * dy)) ** 0.5 / 1.0

    assert device.trilat_status == "ok"
    assert published_speed <= coordinator.trilat_max_horizontal_speed_mps()
    assert device.trilat_x_m is not None and device.trilat_x_m < far_x
    assert device.trilat_y_m is not None and device.trilat_y_m < far_y
    assert device.trilat_horizontal_speed_mps is not None
    assert device.trilat_horizontal_speed_mps <= coordinator.trilat_max_horizontal_speed_mps()


def test_trilat_motion_filter_caps_unphysical_xy_jump_after_long_gap():
    """Long update gaps must still respect the motion cap instead of publishing the raw solve."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-motion-gap")
    sc_a, sc_b, sc_c = _right_triangle_anchors(coordinator, "dev-motion-gap", "f1")

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=100.0):
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

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=106.0):
        coordinator._refresh_trilateration_for_device(device)

    dx = float(device.trilat_x_m) - first_xy[0]
    dy = float(device.trilat_y_m) - first_xy[1]
    published_speed = ((dx * dx) + (dy * dy)) ** 0.5 / coordinator._TRILAT_MAX_FILTER_DT_S

    assert device.trilat_status == "ok"
    assert published_speed <= coordinator.trilat_max_horizontal_speed_mps()
    assert device.trilat_horizontal_speed_mps is not None
    assert device.trilat_horizontal_speed_mps <= coordinator.trilat_max_horizontal_speed_mps()
    assert device.trilat_x_m is not None and device.trilat_x_m > far_x


def test_trilat_motion_filter_caps_vertical_speed():
    """Vertical motion must be much more constrained than horizontal motion."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-motion-z")
    state = coordinator._get_trilat_decision_state(device)
    state.last_solution_xy = (0.0, 0.0)
    state.last_solution_z = 2.0
    state.last_filter_stamp = 100.0
    state.last_status = "ok"

    filtered_xy, filtered_z = coordinator._apply_trilat_motion_filter(
        state,
        nowstamp=101.0,
        mobility_type=device.get_mobility_type(),
        measurement_xy=(0.0, 0.0),
        measurement_z=4.0,
        anchor_z_bounds=(0.0, 4.0),
        residual_m=0.1,
        mean_sigma_m=0.1,
    )

    assert filtered_xy == (0.0, 0.0)
    assert filtered_z is not None and filtered_z < 2.5
    assert state.velocity_z_mps <= coordinator.trilat_max_vertical_speed_mps()


def test_trilat_holds_previous_z_through_same_floor_2d_gap():
    """A prior z solution should be held, but softly pulled toward the remaining anchor-height envelope."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-z-hold")

    sc_a = _make_scanner(coordinator, "zh-a", "f1", 0.0, 0.0, z_m=0.0)
    sc_b = _make_scanner(coordinator, "zh-b", "f1", 2.0, 0.0, z_m=0.0)
    sc_c = _make_scanner(coordinator, "zh-c", "f1", 0.0, 2.0, z_m=0.0)
    sc_d = _make_scanner(coordinator, "zh-d", "f1", 0.0, 0.0, z_m=2.0)

    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=200.0):
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
    with patch("custom_components.ble_trilateration.coordinator.monotonic_time_coarse", return_value=201.0):
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
    assert device.trilat_geometry_quality >= 0.0
    assert device.trilat_residual_consistency >= 0.0


def test_successful_solve_populates_quality_metrics():
    """Successful solves should populate geometry and residual quality diagnostics."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-quality")

    sc_a = _make_scanner(coordinator, "q-a", "f1", 0.0, 0.0, z_m=0.0)
    sc_b = _make_scanner(coordinator, "q-b", "f1", 6.0, 0.0, z_m=0.0)
    sc_c = _make_scanner(coordinator, "q-c", "f1", 0.0, 8.0, z_m=0.0)

    fresh = time.monotonic()
    device.adverts = {
        ("dev-quality", sc_a.address): _make_advert(sc_a, fresh, -70.0, 5.0),
        ("dev-quality", sc_b.address): _make_advert(sc_b, fresh, -70.0, 5.0),
        ("dev-quality", sc_c.address): _make_advert(sc_c, fresh, -70.0, 5.0),
    }

    coordinator._refresh_trilateration_for_device(device)

    assert device.trilat_status == "ok"
    assert device.trilat_geometry_quality > 0.0
    assert device.trilat_residual_consistency > 0.0
    assert device.trilat_geometry_gdop is not None
    assert device.trilat_geometry_condition is not None


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


def test_trilat_solve_prior_is_weakened_after_floor_switch():
    """Recent floor changes should inflate the carry-over prior uncertainty."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-prior-floor-switch", mobility_type="stationary")
    state = coordinator._get_trilat_decision_state(device)
    state.floor_id = "f2"
    state.last_solution_xy = (4.0, 5.0)
    state.last_solution_z = 2.0
    state.velocity_x_mps = 0.5
    state.velocity_y_mps = -0.25
    state.velocity_z_mps = 0.1
    state.last_filter_stamp = 100.0
    state.last_residual_m = 0.4
    state.last_mean_sigma_m = 1.0
    state.last_status = "ok"

    baseline = coordinator._build_trilat_solve_prior(
        state,
        nowstamp=102.0,
        mobility_type=device.get_mobility_type(),
        solver_dimension="3d",
        selected_floor_id="f2",
        mean_sigma_m=1.0,
        mean_anchor_range_delta_m=0.5,
    )

    state.last_floor_change_at = 101.5
    state.last_floor_change_from_id = "f1"
    switched = coordinator._build_trilat_solve_prior(
        state,
        nowstamp=102.0,
        mobility_type=device.get_mobility_type(),
        solver_dimension="3d",
        selected_floor_id="f2",
        mean_sigma_m=1.0,
        mean_anchor_range_delta_m=0.5,
    )

    assert baseline is not None
    assert switched is not None
    assert switched.sigma_x_m > baseline.sigma_x_m
    assert switched.sigma_z_m > baseline.sigma_z_m
