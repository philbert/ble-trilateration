"""Tests for mobility-aware area resolution in coordinator logic."""

from __future__ import annotations

import time
from types import SimpleNamespace

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
        self.mobility_type = mobility_type
        self.adverts = {}
        self.area_advert = None
        self.area_name = None
        self.area_is_unknown = False
        self.diag_area_switch = None
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
    """Weak and close contenders should emit Unknown instead of phantom room picks."""
    coordinator = _make_coordinator()
    device = _DummyDevice("dev-b", mobility_type="stationary")

    weak_a = _make_advert("scanner_a", "Garage", -96.0, 8.0)
    weak_b = _make_advert("scanner_b", "Roadside", -96.4, 8.2)
    device.area_advert = weak_a
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
