"""Tests for Bermuda room classifier."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.bermuda.room_classifier import BermudaRoomClassifier


class _FakeCalibration:
    def __init__(self, samples):
        self._samples = samples

    def samples(self):
        return self._samples


class _FakeAreaRegistry:
    def __init__(self):
        self._areas = {"living_room": SimpleNamespace(id="living_room", floor_id="ground", name="Living Room")}

    def async_get_area(self, area_id):
        return self._areas.get(area_id)


@pytest.mark.asyncio
async def test_single_sample_uses_default_room_radius() -> None:
    """Single samples should classify within the default 1 m radius."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 10.0, "y_m": 5.0, "z_m": 3.0},
                    "quality": {"status": "accepted"},
                }
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    result = classifier.classify(layout_hash="layout-a", floor_id="ground", x_m=10.7, y_m=5.0, z_m=3.0)
    assert result.area_id == "living_room"
    assert result.reason == "ok"


@pytest.mark.asyncio
async def test_single_sample_honours_declared_room_radius() -> None:
    """Single samples should use the persisted per-sample room radius."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 10.0, "y_m": 5.0, "z_m": 3.0},
                    "room_radius_m": 1.8,
                    "quality": {"status": "accepted"},
                }
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    result = classifier.classify(layout_hash="layout-a", floor_id="ground", x_m=11.6, y_m=5.0, z_m=3.0)
    assert result.area_id == "living_room"
    assert result.reason == "ok"
