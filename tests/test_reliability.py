"""Tests for two-layer evidence reliability."""

from __future__ import annotations

import pytest

from custom_components.ble_trilateration.evidence import CalibrationStatus, EvidenceSource, ReliabilityInput
from custom_components.ble_trilateration.reliability import (
    EmpiricalCalibrator,
    fingerprint_information_quality,
    geometry_information_quality,
    rssi_information_quality,
)


def test_rssi_reliability_rises_with_margin():
    """A wider separation between floors is more informative, all else equal."""
    weak = rssi_information_quality(
        margin=0.02, effective_anchor_count=8, floor_coverage_balance=0.9, timestamp_health=1.0
    )
    strong = rssi_information_quality(
        margin=0.30, effective_anchor_count=8, floor_coverage_balance=0.9, timestamp_health=1.0
    )

    assert strong.value > weak.value


def test_rssi_reliability_penalises_lopsided_floor_coverage():
    """A win on the floor that owns most anchors is partly a layout artefact."""
    balanced = rssi_information_quality(
        margin=0.2, effective_anchor_count=8, floor_coverage_balance=1.0, timestamp_health=1.0
    )
    lopsided = rssi_information_quality(
        margin=0.2, effective_anchor_count=8, floor_coverage_balance=0.1, timestamp_health=1.0
    )

    assert balanced.value > lopsided.value


def test_fingerprint_reliability_falls_as_untrained_anchors_are_added():
    """Adding anchors makes the trained subset partial, not wrong."""
    fully_trained = fingerprint_information_quality(
        matched_features=8, live_trained_fraction=1.0, trained_match_fraction=1.0, match_separation=0.1
    )
    half_trained = fingerprint_information_quality(
        matched_features=8, live_trained_fraction=0.45, trained_match_fraction=1.0, match_separation=0.1
    )

    assert fully_trained.value > half_trained.value
    assert half_trained.value > 0.0


def test_fingerprint_reliability_is_zero_without_enough_matched_features():
    """Below two comparable features there is nothing to compare."""
    result = fingerprint_information_quality(
        matched_features=1, live_trained_fraction=1.0, trained_match_fraction=1.0, match_separation=0.5
    )

    assert result.value == 0.0
    assert result.reason == "insufficient_matched_features"


def test_geometry_reliability_is_zero_when_height_is_unobservable():
    """No vertical minimum means no vertical vote, whatever the scores look like."""
    result = geometry_information_quality(
        z_informative=False,
        z_sigma_m=None,
        floor_separation_m=1.3,
        anchor_height_diversity_m=5.0,
        candidate_margin=0.4,
    )

    assert result.value == 0.0
    assert result.reason == "z_unobservable"


def test_geometry_reliability_is_zero_when_z_sigma_exceeds_floor_separation():
    """Resolving 1.3 m apart floors needs better than 1.3 m of vertical precision."""
    result = geometry_information_quality(
        z_informative=True,
        z_sigma_m=2.0,
        floor_separation_m=1.3,
        anchor_height_diversity_m=5.0,
        candidate_margin=0.4,
    )

    assert result.value == 0.0
    assert result.reason == "z_sigma_exceeds_floor_separation"


def test_geometry_reliability_rises_as_vertical_precision_sharpens():
    """The path to geometry earning floor authority is measurable, not argued."""
    coarse = geometry_information_quality(
        z_informative=True,
        z_sigma_m=1.1,
        floor_separation_m=1.3,
        anchor_height_diversity_m=5.0,
        candidate_margin=0.4,
    )
    sharp = geometry_information_quality(
        z_informative=True,
        z_sigma_m=0.3,
        floor_separation_m=1.3,
        anchor_height_diversity_m=5.0,
        candidate_margin=0.4,
    )

    assert sharp.value > coarse.value


def test_all_quality_inputs_are_from_the_allow_list():
    """Reliability must never be justified by the system's own conclusions."""
    assessments = [
        rssi_information_quality(
            margin=0.2, effective_anchor_count=8, floor_coverage_balance=0.9, timestamp_health=1.0
        ),
        fingerprint_information_quality(
            matched_features=8, live_trained_fraction=0.8, trained_match_fraction=0.9, match_separation=0.1
        ),
        geometry_information_quality(
            z_informative=True,
            z_sigma_m=0.4,
            floor_separation_m=1.3,
            anchor_height_diversity_m=5.0,
            candidate_margin=0.3,
        ),
    ]
    for assessment in assessments:
        for supplied in assessment.inputs:
            assert isinstance(supplied, ReliabilityInput)


def test_uncalibrated_sources_stay_provisional():
    """Provisional is the normal permanent state for a small installation."""
    calibrator = EmpiricalCalibrator(min_episodes=12)
    provisional = rssi_information_quality(
        margin=0.2, effective_anchor_count=8, floor_coverage_balance=0.9, timestamp_health=1.0
    )

    applied = calibrator.apply(EvidenceSource.RSSI, provisional)

    assert applied.status is CalibrationStatus.PROVISIONAL
    assert applied.value == pytest.approx(provisional.value)
    assert "insufficient_episodes" in applied.reason


def test_one_long_episode_does_not_unlock_calibration():
    """3600 cycles from one parked bin is one episode, not a labelled dataset."""
    calibrator = EmpiricalCalibrator(min_episodes=12)
    for _ in range(3600):
        calibrator.record_outcome(EvidenceSource.RSSI, "bin-parked-1", correct=True)

    assert calibrator.episode_count(EvidenceSource.RSSI) == 1
    assert calibrator.measured_accuracy(EvidenceSource.RSSI) is None


def test_calibration_unlocks_once_enough_distinct_episodes_exist():
    """Independent placements are what accumulate evidence."""
    calibrator = EmpiricalCalibrator(min_episodes=4)
    for index in range(4):
        calibrator.record_outcome(EvidenceSource.RSSI, f"episode-{index}", correct=True)

    provisional = rssi_information_quality(
        margin=0.05, effective_anchor_count=4, floor_coverage_balance=0.5, timestamp_health=1.0
    )
    applied = calibrator.apply(EvidenceSource.RSSI, provisional)

    assert applied.status is CalibrationStatus.CALIBRATED
    assert applied.value > provisional.value
    assert ReliabilityInput.LABELLED_ACCURACY in applied.inputs


def test_per_episode_averaging_stops_one_long_episode_dominating():
    """A long correct episode must not drown out several short wrong ones."""
    calibrator = EmpiricalCalibrator(min_episodes=3)
    for _ in range(500):
        calibrator.record_outcome(EvidenceSource.GEOMETRY, "long-correct", correct=True)
    calibrator.record_outcome(EvidenceSource.GEOMETRY, "short-wrong-1", correct=False)
    calibrator.record_outcome(EvidenceSource.GEOMETRY, "short-wrong-2", correct=False)

    # Per-observation this would be ~0.996; per-episode it is 1/3.
    assert calibrator.measured_accuracy(EvidenceSource.GEOMETRY) == pytest.approx(1 / 3)


def test_measured_accuracy_cannot_fully_override_information_quality():
    """A small labelled sample refines the estimate; it does not replace it."""
    calibrator = EmpiricalCalibrator(min_episodes=2)
    for index in range(2):
        calibrator.record_outcome(EvidenceSource.FINGERPRINT, f"episode-{index}", correct=True)

    zero_quality = fingerprint_information_quality(
        matched_features=2, live_trained_fraction=0.0, trained_match_fraction=0.0, match_separation=0.0
    )
    applied = calibrator.apply(EvidenceSource.FINGERPRINT, zero_quality)

    assert applied.value < 1.0
