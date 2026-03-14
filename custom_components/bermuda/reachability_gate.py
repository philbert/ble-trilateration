"""Reachability gate for topology-gated floor inference."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .transition_zone_store import TransitionZone


@dataclass(frozen=True)
class ReachabilityDecision:
    allowed: bool
    reason: str
    matching_zone_count: int
    best_zone_score: float
    motion_budget_m: float
    nearest_zone_distance_m: float | None


ENTRY_SCORE_THRESHOLD = 0.45


class ReachabilityGate:
    """Evaluates whether a challenger floor is reachable. Stateless — all state passed in."""

    def evaluate(
        self,
        *,
        from_floor_id: str | None,
        to_floor_id: str,
        floor_confidence: float,
        floor_confidence_threshold: float,
        reference_position: tuple[float, float, float] | None,
        motion_budget_m: float,
        zones: list[TransitionZone],
        zone_traversal_history: dict[str, tuple[float, float]],
        nowstamp: float,
        traversal_recency_s: float,
        layout_hash: str,
    ) -> ReachabilityDecision:
        # 1. Bypass: no source floor
        if from_floor_id is None:
            return ReachabilityDecision(allowed=True, reason="bypass_no_floor", matching_zone_count=0, best_zone_score=0.0, motion_budget_m=motion_budget_m, nearest_zone_distance_m=None)
        # 2. Bypass: low floor confidence
        if floor_confidence < floor_confidence_threshold:
            return ReachabilityDecision(allowed=True, reason="bypass_low_confidence", matching_zone_count=0, best_zone_score=0.0, motion_budget_m=motion_budget_m, nearest_zone_distance_m=None)
        # 3. Bypass: no reference position
        if reference_position is None:
            return ReachabilityDecision(allowed=True, reason="bypass_no_reference", matching_zone_count=0, best_zone_score=0.0, motion_budget_m=motion_budget_m, nearest_zone_distance_m=None)
        # 4. Find matching zones for this (from, to) pair and layout
        matching = [z for z in zones if z.anchor_layout_hash == layout_hash and z.covers_pair(from_floor_id, to_floor_id)]
        # 5. Bypass: no zones configured for this pair
        if not matching:
            return ReachabilityDecision(allowed=True, reason="bypass_no_coverage", matching_zone_count=0, best_zone_score=0.0, motion_budget_m=motion_budget_m, nearest_zone_distance_m=None)
        # 6. Check for recent compatible traversal (entry + exit, not just proximity)
        for zone in matching:
            entry_at, exit_at = zone_traversal_history.get(zone.zone_id, (0.0, 0.0))
            if exit_at > 0.0 and (nowstamp - exit_at) <= traversal_recency_s:
                return ReachabilityDecision(allowed=True, reason="allowed_traversal", matching_zone_count=len(matching), best_zone_score=1.0, motion_budget_m=motion_budget_m, nearest_zone_distance_m=0.0)
        # 7. Budget distance test
        ref_x, ref_y, ref_z = reference_position
        best_score = 0.0
        nearest_dist: float | None = None
        for zone in matching:
            s = zone.score(ref_x, ref_y, ref_z)
            if s > best_score:
                best_score = s
            for cap in zone.captures:
                dx = ref_x - cap.x_m
                dy = ref_y - cap.y_m
                dz = ref_z - cap.z_m
                d = math.sqrt(dx*dx + dy*dy + dz*dz)
                if nearest_dist is None or d < nearest_dist:
                    nearest_dist = d
        # Allow if score already high (device is near zone from reference position)
        if best_score >= ENTRY_SCORE_THRESHOLD:
            return ReachabilityDecision(allowed=True, reason="allowed_budget", matching_zone_count=len(matching), best_zone_score=best_score, motion_budget_m=motion_budget_m, nearest_zone_distance_m=nearest_dist)
        # Allow if motion budget reaches nearest zone
        if nearest_dist is not None and motion_budget_m >= nearest_dist:
            return ReachabilityDecision(allowed=True, reason="allowed_budget", matching_zone_count=len(matching), best_zone_score=best_score, motion_budget_m=motion_budget_m, nearest_zone_distance_m=nearest_dist)
        return ReachabilityDecision(allowed=False, reason="blocked_budget", matching_zone_count=len(matching), best_zone_score=best_score, motion_budget_m=motion_budget_m, nearest_zone_distance_m=nearest_dist)
