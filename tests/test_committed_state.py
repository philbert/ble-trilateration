"""Tests for the published-estimate / committed-floor split."""

from __future__ import annotations

from custom_components.ble_trilateration.committed_state import (
    CommittedFloorPolicy,
    CommittedFloorState,
    geometry_conditioning,
)


def _policy(**kwargs) -> CommittedFloorPolicy:
    base = {
        "acquire_confidence": 0.45,
        "acquire_dwell_s": 20.0,
        "release_confidence": 0.20,
        "release_dwell_s": 45.0,
    }
    base.update(kwargs)
    return CommittedFloorPolicy(**base)


def test_weak_evidence_never_commits():
    """A low-confidence guess is publishable, but must not condition the solve."""
    policy = _policy()
    state = CommittedFloorState()

    for tick in range(0, 200, 10):
        state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.2, nowstamp=float(tick))

    assert state.floor_id is None
    assert state.last_reason == "insufficient_support_to_commit"


def test_sustained_strong_evidence_acquires_the_commitment():
    """Commitment is earned by holding up over time, not by one good cycle."""
    policy = _policy()
    state = CommittedFloorState()

    state = policy.update(state, candidate_floor_id="street_level", confidence=0.8, nowstamp=0.0)
    assert state.floor_id is None  # dwell not yet served

    state = policy.update(state, candidate_floor_id="street_level", confidence=0.8, nowstamp=25.0)
    assert state.floor_id == "street_level"
    assert state.last_reason == "committed"


def test_implausible_transitions_lengthen_dwell_but_do_not_veto():
    """The trap was a permanent veto; the fix is a higher bar that can still be cleared."""
    policy = _policy()
    state = CommittedFloorState()

    state = policy.update(
        state, candidate_floor_id="street_level", confidence=0.9, nowstamp=0.0, transition_dwell_multiplier=4.0
    )
    state = policy.update(
        state, candidate_floor_id="street_level", confidence=0.9, nowstamp=30.0, transition_dwell_multiplier=4.0
    )
    assert state.floor_id is None  # 30s < 20s * 4

    state = policy.update(
        state, candidate_floor_id="street_level", confidence=0.9, nowstamp=90.0, transition_dwell_multiplier=4.0
    )
    assert state.floor_id == "street_level"


def test_commitment_releases_when_support_disappears_with_no_challenger():
    """The path that did not exist: losing support is enough, beating it is not required."""
    policy = _policy()
    state = CommittedFloorState()
    state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.8, nowstamp=0.0)
    state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.8, nowstamp=25.0)
    assert state.floor_id == "ground_floor"

    # Evidence decays to nothing conclusive - no rival floor wins outright.
    state = policy.update(state, candidate_floor_id=None, confidence=0.0, nowstamp=30.0)
    state = policy.update(state, candidate_floor_id=None, confidence=0.0, nowstamp=100.0)

    assert state.floor_id is None
    assert state.last_reason == "released_unsupported"


def test_release_hysteresis_prevents_chatter():
    """A brief dip must not drop a commitment that immediately recovers."""
    policy = _policy()
    state = CommittedFloorState()
    state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.8, nowstamp=0.0)
    state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.8, nowstamp=25.0)

    state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.05, nowstamp=30.0)
    state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.8, nowstamp=40.0)

    assert state.floor_id == "ground_floor"
    assert state.weak_since is None


def test_switching_commitment_requires_the_full_dwell():
    """Moving the conditioning state is as consequential as setting it initially."""
    policy = _policy()
    state = CommittedFloorState()
    state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.9, nowstamp=0.0)
    state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.9, nowstamp=25.0)

    state = policy.update(state, candidate_floor_id="street_level", confidence=0.9, nowstamp=30.0)
    assert state.floor_id == "ground_floor"
    assert state.last_reason == "awaiting_dwell"

    state = policy.update(state, candidate_floor_id="street_level", confidence=0.9, nowstamp=55.0)
    assert state.floor_id == "street_level"


def test_uncommitted_geometry_runs_floor_neutral():
    """With nothing committed, geometry must not be conditioned on any floor."""
    conditioning = geometry_conditioning(CommittedFloorState())

    assert conditioning["floor_neutral"] is True
    assert conditioning["apply_floor_z_prior"] is False
    assert conditioning["apply_cross_floor_weighting"] is False


def test_committed_geometry_applies_conditioning():
    """Once a floor is earned, continuity and cross-floor weighting are legitimate."""
    conditioning = geometry_conditioning(CommittedFloorState(floor_id="street_level"))

    assert conditioning["floor_neutral"] is False
    assert conditioning["conditioned_on_floor_id"] == "street_level"


def test_transitions_are_recorded_for_diagnosis():
    """Commit and release events must be reconstructible after the fact."""
    policy = _policy()
    state = CommittedFloorState()
    state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.9, nowstamp=0.0)
    state = policy.update(state, candidate_floor_id="ground_floor", confidence=0.9, nowstamp=25.0)
    state = policy.update(state, candidate_floor_id=None, confidence=0.0, nowstamp=30.0)
    state = policy.update(state, candidate_floor_id=None, confidence=0.0, nowstamp=100.0)

    events = [entry["event"] for entry in state.as_diagnostics()["recent_transitions"]]
    assert events == ["commit", "release"]
