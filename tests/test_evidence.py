"""Tests for the common evidence-result contract."""

from __future__ import annotations

import pytest

from custom_components.ble_trilateration.evidence import (
    CalibrationStatus,
    EvidenceResult,
    EvidenceSource,
    ReliabilityInput,
    ReliabilityOverrides,
    UnsafeReliabilityInputError,
)


def _result(**kwargs) -> EvidenceResult:
    base = {
        "source": EvidenceSource.RSSI,
        "candidate_scores": {"ground_floor": 9.0, "street_level": 12.0},
        "reliability": 0.6,
    }
    base.update(kwargs)
    return EvidenceResult(**base)


def test_best_candidate_and_margin():
    """Ranking and margin come from the scores, not from any stored state."""
    result = _result()
    assert result.best_candidate == "street_level"
    assert result.margin == pytest.approx(3.0 / 21.0)


def test_margin_is_zero_for_a_single_worthless_candidate():
    """A lone zero-scored candidate carries no discriminating information."""
    assert _result(candidate_scores={"ground_floor": 0.0}).margin == pytest.approx(0.0)


def test_normalised_scores_sum_to_one():
    """Sources report on their own scales; the arbiter needs a distribution."""
    normalised = _result().normalised_scores()
    assert sum(normalised.values()) == pytest.approx(1.0)


def test_unavailable_source_contributes_nothing():
    """Absence must be explicit and carry zero weight, not a default score."""
    result = EvidenceResult.unavailable(EvidenceSource.FINGERPRINT, "no_trained_rooms")
    assert result.available is False
    assert result.reliability == 0.0
    assert result.best_candidate is None
    assert result.reason == "no_trained_rooms"


@pytest.mark.parametrize(
    "banned",
    ["prediction_persistence", "committed_floor_agreement", "previous_output_agreement", "source_consensus"],
)
def test_self_referential_reliability_is_rejected(banned):
    """A source may not justify its reliability with the system's own conclusions."""
    with pytest.raises(UnsafeReliabilityInputError):
        _result(reliability_inputs=(banned,))


def test_permitted_reliability_inputs_are_accepted():
    """The allow-listed observation-quality inputs remain usable."""
    result = _result(
        reliability_inputs=(ReliabilityInput.EVIDENCE_MARGIN, ReliabilityInput.TIMESTAMP_HEALTH),
    )
    assert len(result.reliability_inputs) == 2


def test_uncalibrated_reliability_declares_itself_provisional():
    """Reliability that has never met a label must not look measured."""
    assert _result().calibration_status is CalibrationStatus.PROVISIONAL


def test_overrides_pin_reliability_for_replay():
    """Freezing reliability is what makes an adaptive decision reproducible."""
    overrides = ReliabilityOverrides(reliability={EvidenceSource.RSSI: 0.1})
    pinned = overrides.apply(_result())
    assert pinned.reliability == pytest.approx(0.1)
    assert "override_reliability" in pinned.reason
    assert overrides.active is True


def test_overrides_can_remove_a_source_entirely():
    """Disabling a source is a diagnostic control, expressed through the same contract."""
    overrides = ReliabilityOverrides(unavailable={EvidenceSource.RSSI})
    removed = overrides.apply(_result())
    assert removed.available is False
    assert removed.reliability == 0.0


def test_no_overrides_leaves_results_untouched():
    """Normal operation must be the empty-override path."""
    overrides = ReliabilityOverrides()
    original = _result()
    assert overrides.active is False
    assert overrides.apply(original) is original
