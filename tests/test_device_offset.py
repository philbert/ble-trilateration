"""Tests for per-device RSSI offset calibration."""

from __future__ import annotations

import math

import pytest

from custom_components.ble_trilateration.calibration import (
    DEVICE_OFFSET_MIN_ANCHORS,
    DEVICE_OFFSET_MIN_PACKETS,
    compute_device_offset,
)
from custom_components.ble_trilateration.device_offset_store import BermudaDeviceOffsetStore
from custom_components.ble_trilateration.ranging_model import BermudaRangingModel


class _FakeCalibration:
    def __init__(self, samples):
        self._samples = samples

    def samples(self):
        return self._samples


def _make_sample(sample_id: str, distance_m: float, rssi_dbm: float, scanner: str = "scanner-a"):
    return {
        "id": sample_id,
        "device_id": "device-one",
        "anchor_layout_hash": "layout-1",
        "position": {"x_m": distance_m, "y_m": 0.0, "z_m": 0.0},
        "anchors": {
            scanner: {
                "anchor_position": {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0},
                "rssi_median": rssi_dbm,
            }
        },
        "quality": {"status": "accepted"},
    }


def _log_distance_samples(intercept_dbm: float, slope: float) -> list[dict]:
    distances = [1.0, 2.0, 3.0, 4.0, 6.0, 8.0]
    return [_make_sample(f"s{i}", d, intercept_dbm + slope * math.log10(d)) for i, d in enumerate(distances, start=1)]


@pytest.mark.asyncio
async def test_predict_rssi_inverts_estimate_range():
    """predict_rssi must be the exact inverse of estimate_range's distance mapping."""
    model = BermudaRangingModel(_FakeCalibration(_log_distance_samples(-50.0, -20.0)))
    await model.async_rebuild()

    for distance_m in (0.5, 1.0, 2.5, 5.0, 9.0):
        predicted = model.predict_rssi(
            layout_hash="layout-1",
            scanner_address="scanner-a",
            device_id="device-one",
            distance_m=distance_m,
        )
        assert predicted is not None
        estimate = model.estimate_range(
            layout_hash="layout-1",
            scanner_address="scanner-a",
            device_id="device-one",
            filtered_rssi=predicted,
            live_rssi_dispersion=0.0,
            live_packet_count=5,
        )
        assert estimate is not None
        assert estimate.range_m == pytest.approx(max(distance_m, 0.1), rel=0.02)


@pytest.mark.asyncio
async def test_predict_rssi_unknown_layout_returns_none():
    """No fitted layout means no prediction."""
    model = BermudaRangingModel(_FakeCalibration([]))
    await model.async_rebuild()
    assert (
        model.predict_rssi(
            layout_hash="nope",
            scanner_address="scanner-a",
            device_id=None,
            distance_m=2.0,
        )
        is None
    )


def _observations(shift_db: float, count: int = 6, packets: int = 10):
    observations = []
    for index in range(count):
        distance_m = 1.5 + index
        expected = -50.0 - 20.0 * math.log10(distance_m)
        observations.append(
            {
                "scanner_address": f"scanner-{index}",
                "scanner_name": f"Scanner {index}",
                "rssi_median": expected + shift_db,
                "distance_m": distance_m,
                "packet_count": packets,
            }
        )
    return observations


def _predict(scanner_address: str, distance_m: float) -> float:
    return -50.0 - 20.0 * math.log10(distance_m)


def test_compute_device_offset_recovers_constant_shift():
    """A uniform TX shift across anchors must come back as the offset."""
    result = compute_device_offset(_observations(-8.0), _predict)
    assert result["status"] == "ok"
    assert result["offset_db"] == pytest.approx(-8.0, abs=0.01)
    assert result["spread_db"] == pytest.approx(0.0, abs=0.01)
    assert result["anchor_count"] == 6


def test_compute_device_offset_uses_median_against_outliers():
    """One wildly attenuated anchor must not drag the offset."""
    observations = _observations(5.0)
    observations[0]["rssi_median"] -= 25.0  # blocked line-of-sight to one anchor
    result = compute_device_offset(observations, _predict)
    assert result["status"] == "ok"
    assert result["offset_db"] == pytest.approx(5.0, abs=0.5)


def test_compute_device_offset_rejects_insufficient_anchors():
    """Fewer usable anchors than the minimum must reject the capture."""
    result = compute_device_offset(_observations(-8.0, count=DEVICE_OFFSET_MIN_ANCHORS - 1), _predict)
    assert result["status"] == "rejected"
    assert "insufficient_anchors" in result["reason"]


def test_compute_device_offset_skips_thin_anchors():
    """Anchors below the packet minimum contribute nothing."""
    observations = _observations(-8.0, count=DEVICE_OFFSET_MIN_ANCHORS)
    observations[0]["packet_count"] = DEVICE_OFFSET_MIN_PACKETS - 1
    result = compute_device_offset(observations, _predict)
    assert result["status"] == "rejected"
    assert "insufficient_anchors" in result["reason"]


def test_compute_device_offset_rejects_implausible_shift():
    """An offset beyond the plausibility bound is rejected, not stored."""
    result = compute_device_offset(_observations(-40.0), _predict)
    assert result["status"] == "rejected"
    assert "implausible_offset" in result["reason"]


def test_compute_device_offset_skips_unpredictable_scanners():
    """Scanners the model cannot predict are excluded from the estimate."""

    def predict_some(scanner_address: str, distance_m: float) -> float | None:
        if scanner_address == "scanner-0":
            return None
        return _predict(scanner_address, distance_m)

    result = compute_device_offset(_observations(3.0), predict_some)
    assert result["status"] == "ok"
    assert result["anchor_count"] == 5
    assert result["offset_db"] == pytest.approx(3.0, abs=0.01)


@pytest.mark.asyncio
async def test_device_offset_store_roundtrip(hass):
    """Offsets persist, read back, and can be removed."""
    store = BermudaDeviceOffsetStore(hass)
    await store.async_load()
    assert store.get_offset_db("device-one") is None

    await store.async_save_offset("device-one", {"offset_db": -7.5, "spread_db": 1.2})
    assert store.get_offset_db("device-one") == pytest.approx(-7.5)
    record = store.get_record("device-one")
    assert record is not None
    assert record["spread_db"] == pytest.approx(1.2)

    assert await store.async_remove_offset("device-one") is True
    assert store.get_offset_db("device-one") is None
    assert await store.async_remove_offset("device-one") is False
