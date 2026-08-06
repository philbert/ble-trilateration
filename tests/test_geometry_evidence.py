"""Tests for floor-neutral geometry evidence and z observability."""

from __future__ import annotations

import math

import pytest

from custom_components.ble_trilateration.geometry_evidence import (
    evaluate_floor_neutral_geometry,
    floor_scores_from_z_profile,
    measure_z_observability,
)
from custom_components.ble_trilateration.trilateration import AnchorMeasurement


def _anchors_around(x_m, y_m, z_m, *, sigma_m=0.5, layout=None):
    """Anchors with ranges consistent with a true position, at a common sigma."""
    layout = layout or [
        (0.0, 0.0, 1.0),
        (10.0, 0.0, 1.5),
        (0.0, 10.0, 3.5),
        (10.0, 10.0, 6.5),
        (5.0, 5.0, 7.0),
    ]
    anchors = []
    for index, (ax, ay, az) in enumerate(layout):
        true_range = math.sqrt(((x_m - ax) ** 2) + ((y_m - ay) ** 2) + ((z_m - az) ** 2))
        anchors.append(
            AnchorMeasurement(
                scanner_address=f"anchor-{index}",
                x_m=ax,
                y_m=ay,
                range_m=true_range,
                z_m=az,
                sigma_m=sigma_m,
            )
        )
    return anchors


def test_precise_ranges_make_height_observable():
    """With tight ranges and vertical anchor spread, the profile finds the true z."""
    result = measure_z_observability(_anchors_around(5.0, 5.0, 2.0, sigma_m=0.3))

    assert result.informative is True
    assert result.best_z_m == pytest.approx(2.0, abs=0.6)
    assert result.sigma_z_m is not None


def test_noisy_ranges_leave_height_unobservable():
    """Large sigma flattens the profile: the honest answer is 'we cannot tell'."""
    result = measure_z_observability(_anchors_around(5.0, 5.0, 2.0, sigma_m=25.0))

    assert result.informative is False
    assert result.reason == "flat_profile"


def test_too_few_anchors_is_reported_not_guessed():
    """Fewer than three usable anchors cannot constrain height at all."""
    anchors = _anchors_around(5.0, 5.0, 2.0)[:2]
    result = measure_z_observability(anchors)

    assert result.informative is False
    assert result.reason == "insufficient_anchors"


def test_anchors_without_z_are_excluded():
    """An anchor with no height cannot contribute vertical information."""
    anchors = _anchors_around(5.0, 5.0, 2.0)
    flattened = [
        AnchorMeasurement(
            scanner_address=anchor.scanner_address,
            x_m=anchor.x_m,
            y_m=anchor.y_m,
            range_m=anchor.range_m,
            z_m=None,
            sigma_m=anchor.sigma_m,
        )
        for anchor in anchors
    ]
    assert measure_z_observability(flattened).reason == "insufficient_anchors"


def test_floor_scores_follow_the_measured_profile():
    """The floor containing the profile minimum should score highest."""
    observability = measure_z_observability(_anchors_around(5.0, 5.0, 2.0, sigma_m=0.3))
    bands = {"street_level": (1.4, 2.6), "ground_floor": (2.7, 3.9)}

    scores = floor_scores_from_z_profile(observability, bands)

    assert scores["street_level"] > scores["ground_floor"]


def test_no_floor_scores_when_height_is_unobservable():
    """An uninformative profile must produce no vote, rather than a weak one."""
    observability = measure_z_observability(_anchors_around(5.0, 5.0, 2.0, sigma_m=25.0))
    scores = floor_scores_from_z_profile(observability, {"street_level": (1.4, 2.6)})

    assert scores == {}


def test_evaluation_is_identical_regardless_of_any_incumbent_floor():
    """The whole point: no candidate gets a different noise model or prior."""
    anchors = _anchors_around(5.0, 5.0, 2.0, sigma_m=0.3)
    bands = {"street_level": (1.4, 2.6), "ground_floor": (2.7, 3.9)}

    first = evaluate_floor_neutral_geometry(anchors, bands)
    second = evaluate_floor_neutral_geometry(list(reversed(anchors)), bands)

    assert first.floor_scores.keys() == second.floor_scores.keys()
    for floor_id, score in first.floor_scores.items():
        assert second.floor_scores[floor_id] == pytest.approx(score, rel=0.05)


def test_unobservable_height_reports_reason_rather_than_scoring():
    """Geometry that cannot see height says so, instead of voting anyway."""
    result = evaluate_floor_neutral_geometry(_anchors_around(5.0, 5.0, 2.0, sigma_m=25.0), {"a": (0.0, 1.0)})

    assert result.floor_scores == {}
    assert result.reason == "flat_profile"
