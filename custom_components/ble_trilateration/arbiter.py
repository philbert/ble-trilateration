"""
Reliability-weighted evidence arbiter for floor and room decisions.

Fusion is a **reliability-weighted arithmetic pool**, not a product of
likelihoods. RSSI, fingerprint matching and geometry are all derived from the
same radio observations, so treating them as independent and multiplying would
compound one shared error into false certainty. Linear pooling is the standard
robust choice for correlated experts: a confident source can lead, but it cannot
manufacture agreement it does not have.

There are no permanent source priorities and no vetoes. A weak source
contributes a small amount; an unavailable source contributes nothing. That is
the whole ordering mechanism, and it means the same code serves an installation
with pristine fingerprints and one that has just had six anchors moved.

Confidence is computed separately from the winning score, because they answer
different questions. The winner answers "which candidate leads"; confidence
answers "how much should anyone rely on that", and it accounts for how much total
reliable support existed, how far ahead the winner was, and whether the sources
disagreed. A single weak source therefore still yields a published best guess -
carrying honest low confidence rather than being suppressed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .evidence import EvidenceResult, EvidenceSource, ReliabilityOverrides

ARBITER_VERSION = 1


@dataclass(frozen=True)
class FusedDecision:
    """The arbiter's answer for one decision."""

    choice: str | None
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)
    # Reliability actually applied per source after overrides and availability.
    effective_weights: dict[str, float] = field(default_factory=dict)
    contributing_sources: tuple[str, ...] = ()
    disagreement: float = 0.0
    reason: str = "ok"
    arbiter_version: int = ARBITER_VERSION

    @property
    def margin(self) -> float:
        """Return the normalised gap between the winner and the runner-up."""
        if len(self.scores) < 2:
            return 1.0 if self.scores else 0.0
        ranked = sorted(self.scores.values(), reverse=True)
        total = sum(self.scores.values())
        if total <= 0.0:
            return 0.0
        return (ranked[0] - ranked[1]) / total

    def as_diagnostics(self) -> dict[str, Any]:
        """Return a JSON-safe view for decision frames."""
        return {
            "choice": self.choice,
            "confidence": round(self.confidence, 4),
            "margin": round(self.margin, 4),
            "scores": {key: round(value, 6) for key, value in self.scores.items()},
            "effective_weights": {key: round(value, 4) for key, value in self.effective_weights.items()},
            "contributing_sources": list(self.contributing_sources),
            "disagreement": round(self.disagreement, 4),
            "reason": self.reason,
            "arbiter_version": self.arbiter_version,
        }


def _disagreement(results: list[EvidenceResult]) -> float:
    """
    Return how much the contributing sources disagree about the winner.

    Zero when every source that had an opinion picked the same candidate; one
    when no two agree. Used only to temper confidence - never to veto, and never
    fed back into any source's reliability.
    """
    choices = [result.best_candidate for result in results if result.best_candidate is not None]
    if len(choices) < 2:
        return 0.0
    top = max(choices.count(choice) for choice in set(choices))
    return 1.0 - ((top - 1) / (len(choices) - 1))


class EvidenceArbiter:
    """Fuses evidence results into one decision, with configurable preferences."""

    def __init__(
        self,
        *,
        preferences: dict[EvidenceSource, float] | None = None,
        overrides: ReliabilityOverrides | None = None,
    ) -> None:
        """
        Create an arbiter.

        `preferences` are the advanced manual controls: normally every source is
        fully enabled at 1.0, and turning one down is a diagnostic or
        experimental act rather than a lifecycle mode.
        """
        self.preferences = preferences or {}
        self.overrides = overrides or ReliabilityOverrides()

    def preference_for(self, source: EvidenceSource) -> float:
        """Return the configured preference for one source, defaulting to fully enabled."""
        return max(0.0, min(1.0, float(self.preferences.get(source, 1.0))))

    def fuse(self, results: list[EvidenceResult]) -> FusedDecision:
        """Combine source results into a single weighted decision."""
        applied = [self.overrides.apply(result) for result in results]
        contributing: list[EvidenceResult] = []
        effective_weights: dict[str, float] = {}
        for result in applied:
            weight = self.preference_for(result.source) * result.reliability
            effective_weights[result.source.value] = weight
            if result.available and weight > 0.0 and result.candidate_scores:
                contributing.append(result)

        if not contributing:
            return FusedDecision(
                choice=None,
                confidence=0.0,
                effective_weights=effective_weights,
                reason="no_reliable_evidence",
            )

        pooled: dict[str, float] = {}
        total_weight = 0.0
        for result in contributing:
            weight = effective_weights[result.source.value]
            total_weight += weight
            for candidate, score in result.normalised_scores().items():
                pooled[candidate] = pooled.get(candidate, 0.0) + (weight * score)

        if total_weight <= 0.0:
            return FusedDecision(
                choice=None,
                confidence=0.0,
                effective_weights=effective_weights,
                reason="no_reliable_evidence",
            )
        scores = {candidate: value / total_weight for candidate, value in pooled.items()}
        choice = max(scores.items(), key=lambda row: (row[1], row[0]))[0]

        disagreement = _disagreement(contributing)
        decision = FusedDecision(
            choice=choice,
            confidence=0.0,
            scores=scores,
            effective_weights=effective_weights,
            contributing_sources=tuple(result.source.value for result in contributing),
            disagreement=disagreement,
            reason="ok",
        )
        # Support saturates: one fully reliable source is enough to be confident,
        # several weak ones should not add up to certainty they do not have.
        support = min(1.0, total_weight)
        confidence = max(0.0, min(1.0, support * decision.margin * (1.0 - (0.5 * disagreement))))
        return FusedDecision(
            choice=decision.choice,
            confidence=confidence,
            scores=scores,
            effective_weights=effective_weights,
            contributing_sources=decision.contributing_sources,
            disagreement=disagreement,
            reason="ok",
        )
