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
        self._areas = {
            "living_room": SimpleNamespace(id="living_room", floor_id="ground", name="Living Room"),
            "bedroom": SimpleNamespace(id="bedroom", floor_id="ground", name="Bedroom"),
            "office": SimpleNamespace(id="office", floor_id="upper", name="Office"),
        }

    def async_get_area(self, area_id):
        return self._areas.get(area_id)


@pytest.mark.asyncio
async def test_single_sample_uses_default_sample_radius() -> None:
    """Single samples should classify within the default 1 m support radius."""
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
    assert result.topk_used == 1


@pytest.mark.asyncio
async def test_single_sample_honours_declared_sample_radius() -> None:
    """Single samples should use the persisted per-sample support radius."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 10.0, "y_m": 5.0, "z_m": 3.0},
                    "sample_radius_m": 1.8,
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


@pytest.mark.asyncio
async def test_legacy_room_radius_field_is_still_read() -> None:
    """Stored samples using the old room_radius_m field should still work."""
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


@pytest.mark.asyncio
async def test_sample_count_bias_is_normalized() -> None:
    """Many far samples should not beat one very close sample on count alone."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 4.0, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 4.5, "y_m": 0.2, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 5.0, "y_m": -0.2, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "bedroom",
                    "position": {"x_m": 0.1, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    result = classifier.classify(layout_hash="layout-a", floor_id="ground", x_m=0.0, y_m=0.0, z_m=0.0)
    assert result.area_id == "bedroom"
    assert result.reason == "ok"


@pytest.mark.asyncio
async def test_ambiguous_rooms_return_unknown() -> None:
    """Near-equal room evidence should return Unknown with ambiguity reason."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": -0.5, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "bedroom",
                    "position": {"x_m": 0.5, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    result = classifier.classify(layout_hash="layout-a", floor_id="ground", x_m=0.0, y_m=0.0, z_m=0.0)
    assert result.area_id is None
    assert result.reason == "room_ambiguity"
    assert result.best_area_id is not None


@pytest.mark.asyncio
async def test_weak_room_evidence_returns_unknown() -> None:
    """Very weak room evidence should return Unknown instead of guessing."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                }
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    result = classifier.classify(layout_hash="layout-a", floor_id="ground", x_m=3.0, y_m=0.0, z_m=0.0)
    assert result.area_id is None
    assert result.reason == "weak_room_evidence"


@pytest.mark.asyncio
async def test_rooms_on_other_floors_are_ignored() -> None:
    """Classifier should only use rooms on the selected floor."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "office",
                    "position": {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                }
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    result = classifier.classify(layout_hash="layout-a", floor_id="ground", x_m=0.0, y_m=0.0, z_m=0.0)
    assert result.area_id is None
    assert result.reason == "no_trained_rooms"
