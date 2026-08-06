"""
Two-layer reliability for evidence sources.

Layer one is **instantaneous information quality**: how much the current
observation could possibly tell us, computed every cycle from the observation
itself. It needs no labels, so it works from the first minute of a fresh install
and reacts immediately when an anchor is added, moved, or lost.

Layer two is **empirical correctness**: how often this source has actually been
right, measured against independently labelled episodes. It refines layer one
where evidence exists and is silent everywhere else.

The split matters because a single-home installation may accumulate only a
handful of independent episodes - possibly ever. So `provisional` is treated as
the normal permanent state and must be good on its own, with empirical
calibration an enhancement some installations earn rather than a phase everyone
passes through.

Every input used here is drawn from the `ReliabilityInput` allow-list. Nothing
derived from the system's own predictions is permitted, so a source cannot
become confident merely by having been consistent.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .evidence import CalibrationStatus, EvidenceSource, ReliabilityInput

# Below this many independent episodes, measured accuracy is noise. Four devices
# observed once cannot distinguish a good source from a lucky one.
MIN_EPISODES_FOR_CALIBRATION = 12
# How far measured accuracy is allowed to pull reliability away from the
# physics-derived estimate once it has earned the right to speak at all.
EMPIRICAL_BLEND_CEILING = 0.6


@dataclass(frozen=True)
class ReliabilityAssessment:
    """One source's reliability, and the reasoning behind it."""

    value: float
    status: CalibrationStatus
    reason: str
    inputs: tuple[ReliabilityInput, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    def as_diagnostics(self) -> dict[str, Any]:
        """Return a JSON-safe view."""
        return {
            "value": round(self.value, 4),
            "status": self.status.value,
            "reason": self.reason,
            "inputs": [str(item) for item in self.inputs],
            "detail": self.detail,
        }


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def rssi_information_quality(
    *,
    margin: float,
    effective_anchor_count: int,
    floor_coverage_balance: float,
    timestamp_health: float,
) -> ReliabilityAssessment:
    """
    Score how much this RSSI observation could tell us.

    Margin dominates: RSSI floor evidence is a comparison, so a narrow gap means
    little regardless of how many anchors produced it. Coverage balance guards
    the structural trap that sank candidate geometry - if one floor owns most of
    the anchors, a win on that floor is partly an artefact of the layout.
    """
    anchor_term = _clamp01(effective_anchor_count / 8.0)
    value = _clamp01(
        (0.45 * _clamp01(margin * 3.0))
        + (0.25 * anchor_term)
        + (0.20 * _clamp01(floor_coverage_balance))
        + (0.10 * _clamp01(timestamp_health))
    )
    return ReliabilityAssessment(
        value=value,
        status=CalibrationStatus.PROVISIONAL,
        reason="rssi_information_quality",
        inputs=(
            ReliabilityInput.EVIDENCE_MARGIN,
            ReliabilityInput.EFFECTIVE_OBSERVATION_COUNT,
            ReliabilityInput.COVERAGE_BALANCE,
            ReliabilityInput.TIMESTAMP_HEALTH,
        ),
        detail={
            "margin": round(margin, 4),
            "effective_anchor_count": effective_anchor_count,
            "floor_coverage_balance": round(floor_coverage_balance, 4),
            "timestamp_health": round(timestamp_health, 4),
        },
    )


def fingerprint_information_quality(
    *,
    matched_features: int,
    live_trained_fraction: float,
    trained_match_fraction: float,
    match_separation: float,
) -> ReliabilityAssessment:
    """
    Score how much of the live picture the trained features actually cover.

    A newly added, untrained anchor does not make the trained subset wrong; it
    makes it partial. That shows up here as reduced reliability rather than as a
    distorted score, so fingerprints demote themselves as a layout grows and
    re-earn weight on retraining without anyone flipping a mode.
    """
    if matched_features < 2:
        return ReliabilityAssessment(
            value=0.0,
            status=CalibrationStatus.PROVISIONAL,
            reason="insufficient_matched_features",
            inputs=(ReliabilityInput.FEATURE_OVERLAP,),
            detail={"matched_features": matched_features},
        )
    value = _clamp01(
        (0.40 * _clamp01(live_trained_fraction))
        + (0.30 * _clamp01(trained_match_fraction))
        + (0.20 * _clamp01(match_separation * 4.0))
        + (0.10 * _clamp01(matched_features / 6.0))
    )
    return ReliabilityAssessment(
        value=value,
        status=CalibrationStatus.PROVISIONAL,
        reason="fingerprint_information_quality",
        inputs=(
            ReliabilityInput.FEATURE_OVERLAP,
            ReliabilityInput.EVIDENCE_MARGIN,
            ReliabilityInput.EFFECTIVE_OBSERVATION_COUNT,
        ),
        detail={
            "matched_features": matched_features,
            "live_trained_fraction": round(live_trained_fraction, 4),
            "trained_match_fraction": round(trained_match_fraction, 4),
            "match_separation": round(match_separation, 4),
        },
    )


def geometry_information_quality(
    *,
    z_informative: bool,
    z_sigma_m: float | None,
    floor_separation_m: float,
    anchor_height_diversity_m: float,
    candidate_margin: float,
) -> ReliabilityAssessment:
    """
    Score whether geometry can see height at all, before scoring how well.

    The gate is deliberately hard: if the vertical profile has no minimum, or its
    width exceeds the gap between floors, geometry cannot separate them and any
    floor vote it casts would be reporting something other than height. Zero here
    is the honest value, and it is expected to rise as anchor coverage improves.
    """
    if not z_informative or z_sigma_m is None:
        return ReliabilityAssessment(
            value=0.0,
            status=CalibrationStatus.PROVISIONAL,
            reason="z_unobservable",
            inputs=(ReliabilityInput.GEOMETRIC_CONDITIONING,),
            detail={"z_informative": z_informative, "z_sigma_m": z_sigma_m},
        )
    if floor_separation_m > 0 and z_sigma_m >= floor_separation_m:
        return ReliabilityAssessment(
            value=0.0,
            status=CalibrationStatus.PROVISIONAL,
            reason="z_sigma_exceeds_floor_separation",
            inputs=(ReliabilityInput.GEOMETRIC_CONDITIONING,),
            detail={"z_sigma_m": round(z_sigma_m, 3), "floor_separation_m": round(floor_separation_m, 3)},
        )
    sharpness = _clamp01(1.0 - (z_sigma_m / floor_separation_m)) if floor_separation_m > 0 else 0.0
    value = _clamp01(
        (0.50 * sharpness)
        + (0.30 * _clamp01(candidate_margin * 3.0))
        + (0.20 * _clamp01(anchor_height_diversity_m / 4.0))
    )
    return ReliabilityAssessment(
        value=value,
        status=CalibrationStatus.PROVISIONAL,
        reason="geometry_information_quality",
        inputs=(
            ReliabilityInput.GEOMETRIC_CONDITIONING,
            ReliabilityInput.ANCHOR_DIVERSITY,
            ReliabilityInput.EVIDENCE_MARGIN,
        ),
        detail={
            "z_sigma_m": round(z_sigma_m, 3),
            "floor_separation_m": round(floor_separation_m, 3),
            "anchor_height_diversity_m": round(anchor_height_diversity_m, 3),
            "candidate_margin": round(candidate_margin, 4),
        },
    )


class EmpiricalCalibrator:
    """
    Measured per-source accuracy, grouped by independent episode.

    Outcomes are recorded per episode, not per cycle, so a device that sits still
    for an hour contributes one data point. Until enough distinct episodes exist
    the calibrator declines to speak, and reliability stays provisional.
    """

    def __init__(self, min_episodes: int = MIN_EPISODES_FOR_CALIBRATION) -> None:
        """Create an empty calibrator."""
        self._min_episodes = max(1, int(min_episodes))
        # source -> episode_id -> (correct, total) for that episode
        self._episodes: dict[EvidenceSource, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))

    def record_outcome(self, source: EvidenceSource, episode_id: str, *, correct: bool) -> None:
        """Record one source's prediction against one labelled episode."""
        tally = self._episodes[source][episode_id]
        tally[0] += 1 if correct else 0
        tally[1] += 1

    def episode_count(self, source: EvidenceSource) -> int:
        """Return how many distinct episodes this source has been judged against."""
        return len(self._episodes.get(source, {}))

    def measured_accuracy(self, source: EvidenceSource) -> float | None:
        """
        Return accuracy as the mean of per-episode accuracies, or None.

        Averaging per-episode rather than per-observation keeps a long stationary
        episode from outweighing many short ones.
        """
        episodes = self._episodes.get(source)
        if not episodes or len(episodes) < self._min_episodes:
            return None
        per_episode = [correct / total for correct, total in episodes.values() if total > 0]
        if not per_episode:
            return None
        return sum(per_episode) / len(per_episode)

    def apply(self, source: EvidenceSource, provisional: ReliabilityAssessment) -> ReliabilityAssessment:
        """
        Blend measured accuracy into a provisional assessment, where it has earned it.

        The blend is capped so a small labelled sample cannot overwhelm the
        physics-derived estimate, and the status only becomes `calibrated` when
        real episodes backed it.
        """
        accuracy = self.measured_accuracy(source)
        if accuracy is None:
            return ReliabilityAssessment(
                value=provisional.value,
                status=CalibrationStatus.PROVISIONAL,
                reason=f"{provisional.reason}+insufficient_episodes",
                inputs=provisional.inputs,
                detail={
                    **provisional.detail,
                    "episode_count": self.episode_count(source),
                    "episodes_required": self._min_episodes,
                },
            )
        blended = ((1.0 - EMPIRICAL_BLEND_CEILING) * provisional.value) + (EMPIRICAL_BLEND_CEILING * accuracy)
        return ReliabilityAssessment(
            value=_clamp01(blended),
            status=CalibrationStatus.CALIBRATED,
            reason=f"{provisional.reason}+measured",
            inputs=(*provisional.inputs, ReliabilityInput.LABELLED_ACCURACY),
            detail={
                **provisional.detail,
                "measured_accuracy": round(accuracy, 4),
                "episode_count": self.episode_count(source),
            },
        )

    def as_diagnostics(self) -> dict[str, Any]:
        """Return a JSON-safe summary of what has been measured so far."""
        return {
            source.value: {
                "episode_count": self.episode_count(source),
                "measured_accuracy": self.measured_accuracy(source),
                "episodes_required": self._min_episodes,
            }
            for source in self._episodes
        }
