"""Tests for mobility-aware area resolution in coordinator logic."""

from __future__ import annotations

import time
from types import SimpleNamespace

from custom_components.bermuda.const import AREA_NAME_UNKNOWN
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator


def _make_advert(scanner: str, area: str, rssi_filtered: float, distance: float):
    nowstamp = time.monotonic()
    return SimpleNamespace(
        stamp=nowstamp,
        scanner_address=scanner,
        name=scanner,
        area_id=f"{area.lower()}_id",
        area_name=area,
        rssi_distance=distance,
        rssi_filtered=rssi_filtered,
        rssi=rssi_filtered,
        conf_rssi_offset=0.0,
        rssi_dispersion=1.0,
        scanner_device=SimpleNamespace(last_seen=nowstamp),
    )


class _DummyDevice:
    def __init__(self, address: str, mobility_type: str = "moving"):
        self.address = address
        self.name = address
        self.prefname = address
        self.mobility_type = mobility_type
        self.adverts = {}
        self.area_advert = None
        self.area_name = None
        self.area_last_seen = None
        self.area_last_seen_id = None
        self.area_is_unknown = False
        self.diag_area_switch = None
        self.name_by_user = None
        self.name_devreg = None
        self.name_bt_local_name = None
        self.name_bt_serviceinfo = None
        self.applied: list[tuple[object | None, bool]] = []

    def get_mobility_type(self):
        return self.mobility_type

    def apply_scanner_selection(self, advert, force_unknown: bool = False):
        self.applied.append((advert, force_unknown))
        self.area_advert = advert
        self.area_is_unknown = force_unknown
        if force_unknown:
            self.area_name = "Unknown"
        elif advert is not None:
            self.area_name = advert.area_name
            self.area_last_seen = advert.area_name
            self.area_last_seen_id = advert.area_id
        else:
            self.area_name = None


def _make_coordinator():
    coordinator = object.__new__(BermudaDataUpdateCoordinator)
    coordinator._area_decision_state = {}
    coordinator.get_scanner_max_radius = lambda _scanner: 20.0
    return coordinator


def test_slow_lane_prevents_quick_oscillation():
    """Small score margins should not immediately flip area selection."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-a", mobility_type="moving")

    incumbent = _make_advert("scanner_a", "Garage", -70.0, 3.0)
    challenger = _make_advert("scanner_b", "Roadside", -68.0, 3.2)
    device.area_advert = incumbent
    device.adverts = {("dev-a", "scanner_a"): incumbent, ("dev-a", "scanner_b"): challenger}

    coordinator._refresh_area_by_min_distance(device)
    coordinator._refresh_area_by_min_distance(device)

    # Challenger is better, but not enough for fast-lane and not long enough for slow-lane.
    assert device.applied[-1][0] is incumbent
    assert device.applied[-1][1] is False


def test_unknown_when_weak_and_ambiguous():
    """Weak and close contenders with no prior area should emit Unknown, not a phantom room."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-b", mobility_type="stationary")

    weak_a = _make_advert("scanner_a", "Garage", -96.0, 8.0)
    weak_b = _make_advert("scanner_b", "Roadside", -96.4, 8.2)
    # No area_advert set: device has no prior area, so there is nothing to hold.
    device.adverts = {("dev-b", "scanner_a"): weak_a, ("dev-b", "scanner_b"): weak_b}

    coordinator._refresh_area_by_min_distance(device)

    assert device.applied[-1][0] is None
    assert device.applied[-1][1] is True
    assert device.diag_area_switch is not None
    assert "UNKNOWN" in device.diag_area_switch


def test_unknown_entry_is_delayed_while_incumbent_exists():
    """Short weak periods should hold the incumbent area before moving to Unknown."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-c", mobility_type="moving")

    incumbent = _make_advert("scanner_a", "Garage", -82.0, 4.0)
    weak = _make_advert("scanner_b", "Laundry", -101.0, 9.0)
    device.area_advert = incumbent
    device.area_name = "Garage"
    device.adverts = {("dev-c", "scanner_a"): weak}

    coordinator._refresh_area_by_min_distance(device)

    # First weak detection should keep incumbent instead of flapping to Unknown immediately.
    assert device.applied[-1][0] is incumbent
    assert device.applied[-1][1] is False


def test_no_valid_contender_holds_stale_incumbent_during_grace():
    """No-contender gaps should hold prior area for the unknown grace window."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-gap", mobility_type="moving")

    incumbent = _make_advert("scanner_a", "Garage", -80.0, 3.0)
    device.area_advert = incumbent
    device.area_name = "Garage"
    device.area_last_seen = "Garage"
    device.area_last_seen_id = incumbent.area_id
    device.adverts = {}

    coordinator._refresh_area_by_min_distance(device)

    assert device.applied[-1][0] is incumbent
    assert device.applied[-1][1] is False


def test_unknown_exit_does_not_require_ratio_for_same_area_scanners():
    """Unknown should clear when top contenders are from the same area."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-same-area", mobility_type="stationary")

    top = _make_advert("scanner_g1", "Garage", -88.5, 4.4)
    second = _make_advert("scanner_g2", "Garage", -89.2, 4.6)
    device.area_is_unknown = True
    device.area_name = "Unknown"
    device.area_last_seen = "Garage"
    device.area_last_seen_id = top.area_id
    device.adverts = {
        ("dev-same-area", "scanner_g1"): top,
        ("dev-same-area", "scanner_g2"): second,
    }

    coordinator._refresh_area_by_min_distance(device)

    assert device.applied[-1][0] is top
    assert device.applied[-1][1] is False
    assert device.area_name == "Garage"


def test_unknown_cycles_record_unknown_in_dominant_history():
    """Unknown periods should not accumulate majority-vote credit for any scanner."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-d", mobility_type="stationary")

    # Two ambiguous adverts — scores very close, ratio well below ambiguity_ratio (1.2).
    advert_a = _make_advert("scanner_a", "Kitchen", -96.0, 8.0)
    advert_b = _make_advert("scanner_b", "Hallway", -96.1, 8.1)
    device.adverts = {("dev-d", "scanner_a"): advert_a, ("dev-d", "scanner_b"): advert_b}

    # Run once to initialise AreaDecisionState for this device.
    coordinator._refresh_area_by_min_distance(device)

    # Force ambiguous_since into the past so the hold timer is already expired.
    state = coordinator._get_area_decision_state(device)
    state.ambiguous_since = time.monotonic() - 100.0

    # Run again — this time the ambiguity gate should fire and resolve to Unknown.
    coordinator._refresh_area_by_min_distance(device)

    assert device.area_is_unknown, "device should be in Unknown state"

    # The history entry recorded for the Unknown cycle must be AREA_NAME_UNKNOWN,
    # not either scanner's address — Unknown periods must not inflate scanner votes.
    assert state.dominant_history[-1] == AREA_NAME_UNKNOWN, (
        f"last history entry should be AREA_NAME_UNKNOWN, got {state.dominant_history[-1]!r}"
    )
    recent = list(state.dominant_history)
    unknown_count = recent.count(AREA_NAME_UNKNOWN)
    a_count = recent.count("scanner_a")
    b_count = recent.count("scanner_b")
    assert unknown_count > 0
    assert a_count + b_count < unknown_count, (
        "scanner votes should not outnumber Unknown entries after a sustained ambiguous period"
    )
