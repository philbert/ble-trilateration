"""Tests for Bermuda's sample-derived ranging model."""

from __future__ import annotations

import math

import pytest

from custom_components.bermuda.ranging_model import BermudaRangingModel


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


def _make_samples_from_formula(
    *,
    scanner: str,
    distances_m: list[float],
    intercept_dbm: float,
    slope_db_per_log10_m: float,
    prefix: str,
) -> list[dict]:
    samples: list[dict] = []
    for index, distance_m in enumerate(distances_m, start=1):
        rssi_dbm = intercept_dbm + (slope_db_per_log10_m * math.log10(distance_m))
        samples.append(_make_sample(f"{prefix}-{index}", distance_m, rssi_dbm, scanner=scanner))
    return samples


@pytest.mark.asyncio
async def test_ranging_model_fits_simple_layout():
    """A fitted model should reproduce matching sample geometry."""
    # Consistent log-distance samples with intercept ~= -50 dBm and path-loss exponent ~= 2.
    samples = [
        _make_sample("s1", 1.0, -50.0),
        _make_sample("s2", 2.0, -56.0),
        _make_sample("s3", 3.0, -59.5),
        _make_sample("s4", 4.0, -62.0),
        _make_sample("s5", 6.0, -65.5),
    ]
    model = BermudaRangingModel(_FakeCalibration(samples))
    await model.async_rebuild()

    estimate = model.estimate_range(
        layout_hash="layout-1",
        scanner_address="scanner-a",
        device_id="device-one",
        filtered_rssi=-62.0,
        live_rssi_dispersion=0.5,
    )

    assert estimate is not None
    assert estimate.range_m == pytest.approx(4.0, rel=0.15)
    assert estimate.sigma_m > 0.0


@pytest.mark.asyncio
async def test_ranging_model_refuses_tiny_training_sets():
    """Fewer than the minimum training rows should not produce a fitted layout."""
    samples = [
        _make_sample("s1", 1.0, -50.0),
        _make_sample("s2", 2.0, -56.0),
        _make_sample("s3", 3.0, -59.5),
        _make_sample("s4", 4.0, -62.0),
    ]
    model = BermudaRangingModel(_FakeCalibration(samples))
    await model.async_rebuild()

    assert model.estimate_range(
        layout_hash="layout-1",
        scanner_address="scanner-a",
        device_id="device-one",
        filtered_rssi=-62.0,
    ) is None


@pytest.mark.asyncio
async def test_sigma_grows_with_range():
    """For a fixed RSSI uncertainty, longer ranges should produce larger sigma_m."""
    samples = [
        _make_sample("s1", 1.0, -50.0),
        _make_sample("s2", 2.0, -56.0),
        _make_sample("s3", 3.0, -59.5),
        _make_sample("s4", 4.0, -62.0),
        _make_sample("s5", 6.0, -65.5),
    ]
    model = BermudaRangingModel(_FakeCalibration(samples))
    await model.async_rebuild()

    near = model.estimate_range(
        layout_hash="layout-1",
        scanner_address="scanner-a",
        device_id="device-one",
        filtered_rssi=-56.0,
    )
    far = model.estimate_range(
        layout_hash="layout-1",
        scanner_address="scanner-a",
        device_id="device-one",
        filtered_rssi=-65.5,
    )

    assert near is not None
    assert far is not None
    assert far.range_m > near.range_m
    assert far.sigma_m > near.sigma_m


@pytest.mark.asyncio
async def test_sigma_rewards_more_packets_and_penalizes_sync_health():
    """Live support should tighten sigma while timestamp problems widen it."""
    samples = [
        _make_sample("s1", 1.0, -50.0),
        _make_sample("s2", 2.0, -56.0),
        _make_sample("s3", 3.0, -59.5),
        _make_sample("s4", 4.0, -62.0),
        _make_sample("s5", 6.0, -65.5),
    ]
    model = BermudaRangingModel(_FakeCalibration(samples))
    await model.async_rebuild()

    sparse = model.estimate_range(
        layout_hash="layout-1",
        scanner_address="scanner-a",
        device_id="device-one",
        filtered_rssi=-62.0,
        live_rssi_dispersion=1.2,
        live_packet_count=1,
    )
    dense = model.estimate_range(
        layout_hash="layout-1",
        scanner_address="scanner-a",
        device_id="device-one",
        filtered_rssi=-62.0,
        live_rssi_dispersion=0.3,
        live_packet_count=10,
    )
    drifting = model.estimate_range(
        layout_hash="layout-1",
        scanner_address="scanner-a",
        device_id="device-one",
        filtered_rssi=-62.0,
        live_rssi_dispersion=0.3,
        live_packet_count=10,
        timestamp_health_penalty=0.75,
    )

    assert sparse is not None
    assert dense is not None
    assert drifting is not None
    assert dense.sigma_m < sparse.sigma_m
    assert drifting.sigma_m > dense.sigma_m


@pytest.mark.asyncio
async def test_scanner_specific_slope_is_used_only_with_enough_support():
    """Scanner-specific slope should activate only for well-supported scanners."""
    scanner_a_samples = _make_samples_from_formula(
        scanner="scanner-a",
        distances_m=[0.8, 1.0, 1.2, 1.5, 1.8, 2.1, 2.5, 2.9, 3.3, 3.7, 4.1, 4.5, 4.9, 5.3, 5.7, 6.1],
        intercept_dbm=-50.0,
        slope_db_per_log10_m=-30.0,
        prefix="a",
    )
    scanner_b_samples = _make_samples_from_formula(
        scanner="scanner-b",
        distances_m=[1.0, 2.0, 3.0, 4.0, 5.0],
        intercept_dbm=-50.0,
        slope_db_per_log10_m=-20.0,
        prefix="b",
    )
    model = BermudaRangingModel(_FakeCalibration(scanner_a_samples + scanner_b_samples))
    await model.async_rebuild()

    summary = model.describe_layout("layout-1")
    assert summary["scanner_slope_count"] == 1

    estimate_a = model.estimate_range(
        layout_hash="layout-1",
        scanner_address="scanner-a",
        device_id="device-one",
        filtered_rssi=-68.06179973983887,  # ~= -50 - 30*log10(4.0)
    )
    estimate_b = model.estimate_range(
        layout_hash="layout-1",
        scanner_address="scanner-b",
        device_id="device-one",
        filtered_rssi=-62.04119982655925,  # ~= -50 - 20*log10(4.0)
    )

    assert estimate_a is not None
    assert estimate_b is not None
    assert estimate_a.range_m == pytest.approx(4.0, rel=0.08)
    # Scanner-b should still be usable, but without a dedicated slope fit.
    assert estimate_b.range_m == pytest.approx(4.0, rel=0.35)
