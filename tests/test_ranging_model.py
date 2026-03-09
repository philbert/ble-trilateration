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
