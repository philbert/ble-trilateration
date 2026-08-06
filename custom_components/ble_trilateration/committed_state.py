"""
Published estimate versus committed floor.

These are deliberately two different things, and separating them is the
structural fix for the circular-evidence failure that motivated this work.

The **published estimate** is always the current best guess, emitted with its
confidence and reason. It is never suppressed: a wrong guess carries information
a user can trace back to a layout problem, while Unknown is opaque. This is the
outward-facing answer.

The **committed floor** is an internal conditioning state - it is what tightens
vertical priors, applies cross-floor anchor weighting, and provides continuity.
Because it feeds back into how the next solve is computed, letting a weak guess
set it is exactly how a bad assignment used to seal itself in: the commitment
shaped the geometry, the geometry confirmed the commitment, and no amount of
contrary evidence could dislodge it.

So the commitment requires sustained, sufficiently reliable, independent
evidence to acquire or switch, and it has a real release path. Releasing does
not publish Unknown; it only means the geometry stops being conditioned on a
floor it can no longer justify, which is precisely when that conditioning is
most harmful.

The rule that keeps this honest: anything computed while conditioned on the
committed floor must never become evidence about the floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

COMMITTED_STATE_VERSION = 1

# Sustained support required before a floor may condition the solve.
DEFAULT_ACQUIRE_CONFIDENCE = 0.45
DEFAULT_ACQUIRE_DWELL_S = 20.0
# Support below which an existing commitment stops being justifiable. Lower than
# the acquire threshold so the commitment does not chatter at the boundary.
DEFAULT_RELEASE_CONFIDENCE = 0.20
DEFAULT_RELEASE_DWELL_S = 45.0


@dataclass
class CommittedFloorState:
    """Tracks the internally committed floor and the evidence sustaining it."""

    floor_id: str | None = None
    committed_at: float | None = None
    # When the currently-leading candidate first started leading, and which it is.
    candidate_id: str | None = None
    candidate_since: float | None = None
    # When support for the existing commitment first dropped below release level.
    weak_since: float | None = None
    last_reason: str = "no_commitment"
    history: list[dict[str, Any]] = field(default_factory=list)

    def as_diagnostics(self) -> dict[str, Any]:
        """Return a JSON-safe view for decision frames."""
        return {
            "committed_floor_id": self.floor_id,
            "committed_at": self.committed_at,
            "candidate_id": self.candidate_id,
            "candidate_since": self.candidate_since,
            "weak_since": self.weak_since,
            "reason": self.last_reason,
            "version": COMMITTED_STATE_VERSION,
            "recent_transitions": self.history[-5:],
        }


class CommittedFloorPolicy:
    """
    Decides when evidence may change the internally committed floor.

    Transition plausibility (reachability, dwell) may lengthen the dwell required
    for a switch, but it can never permanently veto sustained strong evidence.
    A veto is how a stationary device that started on the wrong floor becomes
    trapped there for good.
    """

    def __init__(
        self,
        *,
        acquire_confidence: float = DEFAULT_ACQUIRE_CONFIDENCE,
        acquire_dwell_s: float = DEFAULT_ACQUIRE_DWELL_S,
        release_confidence: float = DEFAULT_RELEASE_CONFIDENCE,
        release_dwell_s: float = DEFAULT_RELEASE_DWELL_S,
    ) -> None:
        """Create a policy with the given acquire/release thresholds."""
        self.acquire_confidence = acquire_confidence
        self.acquire_dwell_s = acquire_dwell_s
        self.release_confidence = release_confidence
        self.release_dwell_s = release_dwell_s

    def update(
        self,
        state: CommittedFloorState,
        *,
        candidate_floor_id: str | None,
        confidence: float,
        nowstamp: float,
        transition_dwell_multiplier: float = 1.0,
    ) -> CommittedFloorState:
        """
        Advance the committed state by one decision.

        `transition_dwell_multiplier` is how implausible the move looks - a device
        that has not passed a transition zone simply has to sustain its evidence
        for longer, rather than being blocked outright.
        """
        # Track how long the current leader has been leading.
        if candidate_floor_id != state.candidate_id:
            state.candidate_id = candidate_floor_id
            state.candidate_since = nowstamp

        # Does the existing commitment still have support?
        released_now = False
        if state.floor_id is not None:
            supported = candidate_floor_id == state.floor_id and confidence >= self.release_confidence
            if supported:
                state.weak_since = None
            elif state.weak_since is None:
                state.weak_since = nowstamp
            elif (nowstamp - state.weak_since) >= self.release_dwell_s:
                self._release(state, nowstamp)
                released_now = True

        if candidate_floor_id is None or confidence < self.acquire_confidence:
            # A release this cycle is the more specific explanation; do not
            # overwrite it with the generic "nothing strong enough to commit".
            if state.floor_id is None and not released_now:
                state.last_reason = "insufficient_support_to_commit"
            return state

        required_dwell = self.acquire_dwell_s * max(1.0, transition_dwell_multiplier)
        # Explicit None check: candidate_since of 0.0 is a real timestamp, and
        # `or` would silently treat it as "unset" and reset the dwell to zero.
        candidate_since = state.candidate_since if state.candidate_since is not None else nowstamp
        held_for = nowstamp - candidate_since
        if held_for < required_dwell:
            if state.floor_id != candidate_floor_id:
                state.last_reason = "awaiting_dwell"
            return state

        if state.floor_id != candidate_floor_id:
            self._commit(state, candidate_floor_id, nowstamp)
        return state

    def _commit(self, state: CommittedFloorState, floor_id: str, nowstamp: float) -> None:
        """Record a new commitment and the transition that produced it."""
        state.history.append(
            {
                "from": state.floor_id,
                "to": floor_id,
                "at": nowstamp,
                "event": "commit",
            }
        )
        state.floor_id = floor_id
        state.committed_at = nowstamp
        state.weak_since = None
        state.last_reason = "committed"

    def _release(self, state: CommittedFloorState, nowstamp: float) -> None:
        """
        Drop the commitment when its support has gone, with no replacement needed.

        Releasing without a strong challenger is the path that did not previously
        exist. Without it, a commitment only ever ended by being beaten - and it
        was conditioning the very evidence that would have had to beat it.
        """
        state.history.append(
            {
                "from": state.floor_id,
                "to": None,
                "at": nowstamp,
                "event": "release",
            }
        )
        state.floor_id = None
        state.committed_at = None
        state.weak_since = None
        state.last_reason = "released_unsupported"


def geometry_conditioning(state: CommittedFloorState) -> dict[str, Any]:
    """
    Return how geometry should be conditioned given the committed state.

    With no commitment, geometry runs floor-neutral: equal measurement treatment
    for every anchor and no vertical prior. That is a worse position estimate in
    the short term and the only honest one - it is the state in which geometry
    can produce evidence about the floor rather than about the assumption.
    """
    if state.floor_id is None:
        return {
            "floor_neutral": True,
            "apply_cross_floor_weighting": False,
            "apply_floor_z_prior": False,
            "conditioned_on_floor_id": None,
        }
    return {
        "floor_neutral": False,
        "apply_cross_floor_weighting": True,
        "apply_floor_z_prior": True,
        "conditioned_on_floor_id": state.floor_id,
    }
