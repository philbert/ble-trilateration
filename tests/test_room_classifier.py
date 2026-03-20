"""Tests for Bermuda room classifier."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.ble_trilateration.room_classifier import BermudaRoomClassifier


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


@pytest.mark.asyncio
async def test_fingerprint_score_breaks_geometry_tie() -> None:
    """RSSI fingerprints should distinguish rooms when geometry is ambiguous."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": -0.5, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {"rssi_median": -52.0},
                        "scanner_b": {"rssi_median": -77.0},
                    },
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "bedroom",
                    "position": {"x_m": 0.5, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {"rssi_median": -77.0},
                        "scanner_b": {"rssi_median": -52.0},
                    },
                },
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    result = classifier.classify(
        layout_hash="layout-a",
        floor_id="ground",
        x_m=0.0,
        y_m=0.0,
        z_m=0.0,
        live_rssi_by_scanner={"scanner_a": -53.0, "scanner_b": -75.0},
    )
    assert result.area_id == "living_room"
    assert result.reason == "ok"
    assert result.fingerprint_score > result.geometry_score
    assert result.fingerprint_best_area_id == "living_room"
    assert result.fingerprint_confidence > 0.0
    assert result.fingerprint_coverage == pytest.approx(1.0)
    assert result.sample_count == 1


@pytest.mark.asyncio
async def test_classifier_exposes_room_reference_point_and_sample_count() -> None:
    """Classifier should expose per-room sample count and centroid-like reference point."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 2.0, "y_m": 4.0, "z_m": 6.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    assert classifier.room_sample_count("layout-a", "ground", "living_room") == 2
    assert classifier.room_reference_point("layout-a", "ground", "living_room") == pytest.approx((1.0, 2.0, 3.0))


@pytest.mark.asyncio
async def test_missing_weak_scanner_does_not_overwhelm_strong_fingerprint_match() -> None:
    """A missing flaky scanner should not dominate an otherwise strong room fingerprint match."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": -0.5, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {
                            "rssi_median": -50.0,
                            "packet_count": 5,
                            "rssi_mad": 0.5,
                            "rssi_min": -51.0,
                            "rssi_max": -49.0,
                        },
                        "scanner_b": {
                            "rssi_median": -85.0,
                            "packet_count": 1,
                            "rssi_mad": 8.0,
                            "rssi_min": -92.0,
                            "rssi_max": -80.0,
                        },
                    },
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "bedroom",
                    "position": {"x_m": 0.5, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {
                            "rssi_median": -55.0,
                            "packet_count": 5,
                            "rssi_mad": 0.5,
                            "rssi_min": -56.0,
                            "rssi_max": -54.0,
                        },
                    },
                },
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    room_scores, _, _ = classifier._fingerprint_room_scores(
        classifier._fingerprints["layout-a"],
        {"scanner_a": -50.0},
    )

    assert room_scores["living_room"] > room_scores["bedroom"]


@pytest.mark.asyncio
async def test_noisy_low_count_scanners_are_treated_as_softer_fingerprint_evidence() -> None:
    """Mismatches on noisy low-count scanners should hurt less than mismatches on stable scanners."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": -0.5, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {
                            "rssi_median": -50.0,
                            "packet_count": 5,
                            "rssi_mad": 0.5,
                            "rssi_min": -51.0,
                            "rssi_max": -49.0,
                        },
                        "scanner_b": {
                            "rssi_median": -70.0,
                            "packet_count": 5,
                            "rssi_mad": 0.5,
                            "rssi_min": -71.0,
                            "rssi_max": -69.0,
                        },
                    },
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "bedroom",
                    "position": {"x_m": 0.5, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {
                            "rssi_median": -50.0,
                            "packet_count": 5,
                            "rssi_mad": 0.5,
                            "rssi_min": -51.0,
                            "rssi_max": -49.0,
                        },
                        "scanner_b": {
                            "rssi_median": -70.0,
                            "packet_count": 1,
                            "rssi_mad": 8.0,
                            "rssi_min": -78.0,
                            "rssi_max": -62.0,
                        },
                    },
                },
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    result = classifier.classify(
        layout_hash="layout-a",
        floor_id="ground",
        x_m=0.0,
        y_m=0.0,
        z_m=0.0,
        live_rssi_by_scanner={"scanner_a": -50.0, "scanner_b": -76.0},
    )

    assert result.area_id == "bedroom"
    assert result.reason == "ok"
    assert result.fingerprint_best_area_id == "bedroom"


@pytest.mark.asyncio
async def test_fingerprint_global_can_pick_a_room_on_another_floor() -> None:
    """Cross-floor fingerprint scoring should return the strongest floor-aware room candidate."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {"rssi_median": -51.0},
                        "scanner_b": {"rssi_median": -77.0},
                    },
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "office",
                    "position": {"x_m": 0.0, "y_m": 0.0, "z_m": 3.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {"rssi_median": -76.0},
                        "scanner_b": {"rssi_median": -52.0},
                    },
                },
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    result = classifier.fingerprint_global(
        layout_hash="layout-a",
        live_rssi_by_scanner={"scanner_a": -75.0, "scanner_b": -53.0},
    )

    assert result.reason == "ok"
    assert result.area_id == "office"
    assert result.floor_id == "upper"
    assert result.floor_confidence > 0.7


@pytest.mark.asyncio
async def test_fingerprint_global_floor_confidence_is_based_on_best_room_per_floor() -> None:
    """Floor confidence should not be inflated just because one floor has more rooms."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {"rssi_median": -52.0},
                        "scanner_b": {"rssi_median": -80.0},
                    },
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "bedroom",
                    "position": {"x_m": 1.0, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {"rssi_median": -63.0},
                        "scanner_b": {"rssi_median": -72.0},
                    },
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "office",
                    "position": {"x_m": 0.0, "y_m": 0.0, "z_m": 3.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                    "anchors": {
                        "scanner_a": {"rssi_median": -65.0},
                        "scanner_b": {"rssi_median": -53.0},
                    },
                },
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    result = classifier.fingerprint_global(
        layout_hash="layout-a",
        live_rssi_by_scanner={"scanner_a": -53.0, "scanner_b": -79.0},
    )

    room_scores, _, _ = classifier._fingerprint_room_scores(
        classifier._fingerprints["layout-a"],
        {"scanner_a": -53.0, "scanner_b": -79.0},
    )
    naive_floor_confidence = (
        room_scores["living_room"] + room_scores["bedroom"]
    ) / (
        room_scores["living_room"] + room_scores["bedroom"] + room_scores["office"]
    )

    assert result.floor_id == "ground"
    expected_ground = result.floor_scores["ground"]
    expected_upper = result.floor_scores["upper"]
    assert result.floor_confidence == pytest.approx(expected_ground / (expected_ground + expected_upper))
    assert result.floor_confidence < naive_floor_confidence


@pytest.mark.asyncio
async def test_transition_strength_prefers_nearby_rooms() -> None:
    """Transition strength should be higher for nearby sample clouds than distant ones."""
    classifier = BermudaRoomClassifier(
        _FakeCalibration(
            [
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "living_room",
                    "position": {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "bedroom",
                    "position": {"x_m": 1.8, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
                {
                    "anchor_layout_hash": "layout-a",
                    "room_area_id": "bedroom",
                    "position": {"x_m": 8.0, "y_m": 0.0, "z_m": 0.0},
                    "sample_radius_m": 1.0,
                    "quality": {"status": "accepted"},
                },
            ]
        ),
        _FakeAreaRegistry(),
    )

    await classifier.async_rebuild()

    near_strength = classifier.transition_strength(
        layout_hash="layout-a",
        floor_id="ground",
        from_area_id="living_room",
        to_area_id="bedroom",
    )

    assert 0.0 <= near_strength <= 1.0
    assert near_strength > 0.8
