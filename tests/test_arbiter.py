"""Tests for the reliability-weighted evidence arbiter."""

from __future__ import annotations

import pytest

from custom_components.ble_trilateration.arbiter import EvidenceArbiter
from custom_components.ble_trilateration.evidence import EvidenceResult, EvidenceSource, ReliabilityOverrides


def _rssi(scores, reliability=0.7) -> EvidenceResult:
    return EvidenceResult(source=EvidenceSource.RSSI, candidate_scores=scores, reliability=reliability)


def _fingerprint(scores, reliability=0.5) -> EvidenceResult:
    return EvidenceResult(source=EvidenceSource.FINGERPRINT, candidate_scores=scores, reliability=reliability)


def _geometry(scores, reliability=0.0) -> EvidenceResult:
    return EvidenceResult(source=EvidenceSource.GEOMETRY, candidate_scores=scores, reliability=reliability)


def test_reliable_source_outweighs_an_unreliable_one():
    """Weight is preference times reliability - nothing else orders the sources."""
    decision = EvidenceArbiter().fuse(
        [
            _rssi({"street_level": 12.0, "ground_floor": 9.0}, reliability=0.8),
            _fingerprint({"street_level": 0.30, "ground_floor": 0.70}, reliability=0.1),
        ]
    )

    assert decision.choice == "street_level"


def test_zero_reliability_source_contributes_nothing():
    """Geometry that cannot see height must not tilt the result at all."""
    with_geometry = EvidenceArbiter().fuse(
        [
            _rssi({"street_level": 12.0, "ground_floor": 9.0}),
            _geometry({"ground_floor": 1.0, "street_level": 0.0}, reliability=0.0),
        ]
    )
    without_geometry = EvidenceArbiter().fuse([_rssi({"street_level": 12.0, "ground_floor": 9.0})])

    assert with_geometry.scores == pytest.approx(without_geometry.scores)
    assert "geometry" not in with_geometry.contributing_sources


def test_unavailable_source_is_recorded_but_unused():
    """Absence is explicit, and visible in the weights for diagnosis."""
    decision = EvidenceArbiter().fuse(
        [
            _rssi({"street_level": 12.0, "ground_floor": 9.0}),
            EvidenceResult.unavailable(EvidenceSource.FINGERPRINT, "no_trained_rooms"),
        ]
    )

    assert decision.effective_weights["fingerprint"] == pytest.approx(0.0)
    assert decision.contributing_sources == ("rssi",)


def test_no_reliable_evidence_yields_no_choice_with_a_reason():
    """When nothing can be trusted, say so rather than inventing a winner."""
    decision = EvidenceArbiter().fuse([_geometry({"ground_floor": 1.0}, reliability=0.0)])

    assert decision.choice is None
    assert decision.reason == "no_reliable_evidence"


def test_a_lone_weak_source_still_produces_a_best_guess():
    """A guess carries information a user can act on; Unknown does not."""
    decision = EvidenceArbiter().fuse([_rssi({"street_level": 12.0, "ground_floor": 11.0}, reliability=0.15)])

    assert decision.choice == "street_level"
    assert 0.0 < decision.confidence < 0.2


def test_confidence_is_separate_from_the_winning_score():
    """Same winner, different trustworthiness, depending on support and margin."""
    strong = EvidenceArbiter().fuse([_rssi({"street_level": 30.0, "ground_floor": 5.0}, reliability=0.9)])
    weak = EvidenceArbiter().fuse([_rssi({"street_level": 12.0, "ground_floor": 11.0}, reliability=0.2)])

    assert strong.choice == weak.choice == "street_level"
    assert strong.confidence > weak.confidence


def test_disagreement_lowers_confidence_without_vetoing():
    """Sources that contradict each other reduce trust, they do not block a result."""
    agreeing = EvidenceArbiter().fuse(
        [
            _rssi({"street_level": 12.0, "ground_floor": 6.0}, reliability=0.6),
            _fingerprint({"street_level": 0.8, "ground_floor": 0.4}, reliability=0.6),
        ]
    )
    conflicting = EvidenceArbiter().fuse(
        [
            _rssi({"street_level": 12.0, "ground_floor": 6.0}, reliability=0.6),
            _fingerprint({"street_level": 0.4, "ground_floor": 0.8}, reliability=0.6),
        ]
    )

    assert conflicting.choice is not None
    assert conflicting.disagreement > agreeing.disagreement
    assert conflicting.confidence < agreeing.confidence


def test_correlated_sources_are_pooled_not_multiplied():
    """Three agreeing sources must not compound one shared error into certainty."""
    scores = {"street_level": 0.7, "ground_floor": 0.3}
    decision = EvidenceArbiter().fuse(
        [
            _rssi(scores, reliability=1.0),
            _fingerprint(scores, reliability=1.0),
            _geometry(scores, reliability=1.0),
        ]
    )

    # Linear pooling preserves the shared distribution; a product would spike it.
    assert decision.scores["street_level"] == pytest.approx(0.7)
    assert decision.confidence < 1.0


def test_preferences_can_disable_a_source_for_diagnosis():
    """Manual weights are an advanced control, expressed through the same path."""
    arbiter = EvidenceArbiter(preferences={EvidenceSource.FINGERPRINT: 0.0})
    decision = arbiter.fuse(
        [
            _rssi({"street_level": 12.0, "ground_floor": 9.0}, reliability=0.5),
            _fingerprint({"street_level": 0.1, "ground_floor": 0.9}, reliability=1.0),
        ]
    )

    assert decision.contributing_sources == ("rssi",)
    assert decision.choice == "street_level"


def test_frozen_reliability_makes_a_decision_reproducible():
    """Pinning the weights is what turns 'why did it decide that' into a question with an answer."""
    overrides = ReliabilityOverrides(reliability={EvidenceSource.RSSI: 0.0, EvidenceSource.FINGERPRINT: 1.0})
    arbiter = EvidenceArbiter(overrides=overrides)

    decision = arbiter.fuse(
        [
            _rssi({"street_level": 12.0, "ground_floor": 9.0}, reliability=0.9),
            _fingerprint({"street_level": 0.1, "ground_floor": 0.9}, reliability=0.1),
        ]
    )

    assert decision.choice == "ground_floor"
    assert decision.effective_weights["rssi"] == pytest.approx(0.0)
