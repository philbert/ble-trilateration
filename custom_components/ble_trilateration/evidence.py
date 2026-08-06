"""
Common evidence-result contract for floor and room inference.

Every evidence source - RSSI topology, fingerprint matching, candidate geometry -
answers the same question in the same shape, so the arbiter can weigh them
without knowing how any of them works.

The contract exists because the sources are not interchangeable in quality and
that quality changes constantly. Anchors get added, moved, and removed; trained
features go stale; geometry improves as coverage improves. A fixed weighting can
only ever be right for one layout, so each source reports how much its own answer
should count *right now*, and the arbiter fuses on that.

Two rules are load-bearing and are enforced rather than documented:

1. Reliability must never be derived from the source's own past predictions, from
   the committed floor, or from the previous fused output. A source that scores
   itself on how consistently it has been saying the same thing manufactures
   confidence from repetition - the exact circular-evidence failure this
   architecture exists to remove. Persistence may govern switching dwell; it may
   not govern reliability. See `ReliabilityInput`.

2. Reliability computed without labelled outcomes is `provisional`. It is an
   informed guess from physics and coverage, not a measured hit rate, and it must
   describe itself that way so a miscalibrated source cannot masquerade as a
   measured one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

# Bumped when the meaning of a score or quality metric changes, so recorded
# decision frames stay interpretable after the algorithm moves on.
EVIDENCE_CONTRACT_VERSION = 1


class EvidenceSource(str, Enum):
    """Sources that can contribute candidate scores."""

    RSSI = "rssi"
    FINGERPRINT = "fingerprint"
    GEOMETRY = "geometry"


class CalibrationStatus(str, Enum):
    """Whether a reliability value has been checked against labelled outcomes."""

    PROVISIONAL = "provisional"
    CALIBRATED = "calibrated"


class ReliabilityInput(str, Enum):
    """
    Quantities a source is permitted to derive reliability from.

    Deliberately an allow-list. The banned quantities all share one property:
    they are downstream of the system's own conclusions, so using them lets a
    source confirm itself. Anything not listed here needs to be argued for and
    added explicitly.
    """

    # Information content of the current observation
    EVIDENCE_MARGIN = "evidence_margin"
    EFFECTIVE_OBSERVATION_COUNT = "effective_observation_count"
    OBSERVATION_DISPERSION = "observation_dispersion"
    # Structural quality of the inputs
    FEATURE_OVERLAP = "feature_overlap"
    GEOMETRIC_CONDITIONING = "geometric_conditioning"
    ANCHOR_DIVERSITY = "anchor_diversity"
    COVERAGE_BALANCE = "coverage_balance"
    # Freshness and health of the inputs themselves (not of the outputs)
    INPUT_FRESHNESS = "input_freshness"
    TIMESTAMP_HEALTH = "timestamp_health"
    # Measured agreement with independently labelled truth
    LABELLED_ACCURACY = "labelled_accuracy"


BANNED_RELIABILITY_INPUTS = frozenset(
    {
        "prediction_persistence",
        "committed_floor_agreement",
        "previous_output_agreement",
        "source_consensus",
    }
)


class UnsafeReliabilityInputError(ValueError):
    """Raised when a source tries to justify reliability with its own output."""


@dataclass(frozen=True)
class EvidenceResult:
    """One source's answer for one decision."""

    source: EvidenceSource
    candidate_scores: dict[str, float] = field(default_factory=dict)
    available: bool = True
    # Why the source could not answer, or why its reliability is what it is.
    reason: str = "ok"
    quality_metrics: dict[str, Any] = field(default_factory=dict)
    reliability: float = 0.0
    reliability_inputs: tuple[ReliabilityInput, ...] = ()
    calibration_status: CalibrationStatus = CalibrationStatus.PROVISIONAL
    contract_version: int = EVIDENCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        """Reject reliability that was justified by the system's own conclusions."""
        for supplied in self.reliability_inputs:
            if str(supplied) in BANNED_RELIABILITY_INPUTS:
                msg = (
                    f"{self.source.value} reliability may not be derived from {supplied!r}: "
                    "self-referential evidence is what this contract exists to prevent"
                )
                raise UnsafeReliabilityInputError(msg)

    @classmethod
    def unavailable(cls, source: EvidenceSource, reason: str) -> EvidenceResult:
        """A source that had nothing to say contributes nothing, and says why."""
        return cls(source=source, available=False, reason=reason, reliability=0.0)

    @property
    def best_candidate(self) -> str | None:
        """Return the highest-scoring candidate, or None when there is nothing to rank."""
        if not self.candidate_scores:
            return None
        return max(self.candidate_scores.items(), key=lambda row: (row[1], row[0]))[0]

    @property
    def margin(self) -> float:
        """Return the gap between the best and second candidate, normalised by the total."""
        total = sum(self.candidate_scores.values())
        if total <= 0.0:
            # Either nothing to rank, or every candidate scored zero. A lone
            # zero-scored candidate is not an unopposed winner, it is silence.
            return 0.0
        if len(self.candidate_scores) < 2:
            return 1.0
        ranked = sorted(self.candidate_scores.values(), reverse=True)
        return (ranked[0] - ranked[1]) / total

    def normalised_scores(self) -> dict[str, float]:
        """Return candidate scores as a distribution summing to 1."""
        total = sum(self.candidate_scores.values())
        if total <= 0.0:
            return dict.fromkeys(self.candidate_scores, 0.0)
        return {candidate: score / total for candidate, score in self.candidate_scores.items()}

    def with_reliability(self, reliability: float, *, reason: str | None = None) -> EvidenceResult:
        """Return a copy carrying an overridden reliability, for replay and debugging."""
        return replace(
            self,
            reliability=max(0.0, min(1.0, float(reliability))),
            reason=reason if reason is not None else self.reason,
        )

    def as_diagnostics(self) -> dict[str, Any]:
        """Return a JSON-safe view for decision frames and diagnostics dumps."""
        return {
            "source": self.source.value,
            "available": self.available,
            "reason": self.reason,
            "reliability": round(self.reliability, 4),
            "reliability_inputs": [str(item) for item in self.reliability_inputs],
            "calibration_status": self.calibration_status.value,
            "contract_version": self.contract_version,
            "best_candidate": self.best_candidate,
            "margin": round(self.margin, 4),
            "candidate_scores": {key: round(value, 6) for key, value in self.candidate_scores.items()},
            "quality_metrics": self.quality_metrics,
        }


@dataclass
class ReliabilityOverrides:
    """
    Pinned reliabilities and availability, for reproducing a decision.

    An adaptive arbiter re-weights every cycle, so a wrong answer is hard to
    reconstruct after the fact - the weights that produced it are gone. Freezing
    them turns "why did it decide that" into a question with an answer. This is a
    diagnostic control, never an operating mode: normal operation leaves it empty.
    """

    reliability: dict[EvidenceSource, float] = field(default_factory=dict)
    unavailable: set[EvidenceSource] = field(default_factory=set)

    @property
    def active(self) -> bool:
        """True when any override is in force."""
        return bool(self.reliability or self.unavailable)

    def apply(self, result: EvidenceResult) -> EvidenceResult:
        """Return the result as the overrides say it should be seen."""
        if result.source in self.unavailable:
            return EvidenceResult.unavailable(result.source, "override_unavailable")
        pinned = self.reliability.get(result.source)
        if pinned is None:
            return result
        return result.with_reliability(pinned, reason=f"{result.reason}+override_reliability")

    def as_diagnostics(self) -> dict[str, Any]:
        """Return a JSON-safe view of the overrides in force."""
        return {
            "active": self.active,
            "reliability": {source.value: value for source, value in self.reliability.items()},
            "unavailable": sorted(source.value for source in self.unavailable),
        }
