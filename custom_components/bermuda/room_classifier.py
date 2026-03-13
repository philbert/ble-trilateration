"""Calibration-sample hybrid room classifier for Bermuda."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .const import DEFAULT_SAMPLE_RADIUS_M

if TYPE_CHECKING:
    from homeassistant.helpers.area_registry import AreaRegistry

    from .calibration import BermudaCalibrationManager

_LOGGER = logging.getLogger(__name__)

ROOM_KERNEL_Z_WEIGHT = 0.15
ROOM_SCORE_MIN = 0.15
ROOM_SCORE_RATIO_MIN = 1.25
K_CAP = 3
FINGERPRINT_WEIGHT = 0.65
FINGERPRINT_K_CAP = 5
FINGERPRINT_SIGMA_DB = 7.0
FINGERPRINT_MISSING_PENALTY_DB = 9.0
FINGERPRINT_EXTRA_SCANNER_PENALTY_DB = 4.5
FINGERPRINT_MIN_COMMON_SCANNERS = 2
TRANSITION_GAP_SIGMA_M = 1.5


@dataclass(frozen=True)
class RoomClassification:
    """Room classification result for one solved position."""

    area_id: str | None
    reason: str
    best_area_id: str | None = None
    best_score: float = 0.0
    second_score: float = 0.0
    topk_used: int = 0
    geometry_score: float = 0.0
    fingerprint_score: float = 0.0


@dataclass(frozen=True)
class GlobalFingerprintResult:
    """Cross-floor fingerprint-only classification result."""

    area_id: str | None
    floor_id: str | None
    reason: str
    floor_confidence: float = 0.0
    room_confidence: float = 0.0
    best_score: float = 0.0
    second_score: float = 0.0
    floor_scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class _SampleKernel:
    """One persisted calibration sample represented as a soft kernel."""

    area_id: str
    floor_id: str | None
    x_m: float
    y_m: float
    z_m: float
    sigma_m: float


@dataclass(frozen=True)
class _SampleFingerprint:
    """One persisted calibration sample represented in RSSI-space."""

    area_id: str
    floor_id: str | None
    rssi_by_scanner: dict[str, float]


class BermudaRoomClassifier:
    """Classify trilat positions into rooms using calibration samples."""

    def __init__(self, calibration: BermudaCalibrationManager, area_registry: AreaRegistry) -> None:
        """Initialise classifier cache."""
        self._calibration = calibration
        self._area_registry = area_registry
        self._layouts: dict[str, list[_SampleKernel]] = {}
        self._fingerprints: dict[str, list[_SampleFingerprint]] = {}
        self._transition_strengths: dict[tuple[str, str | None, str, str], float] = {}

    async def async_rebuild(self) -> None:
        """Rebuild all room kernels from current calibration samples."""
        layouts: dict[str, list[_SampleKernel]] = defaultdict(list)
        fingerprints: dict[str, list[_SampleFingerprint]] = defaultdict(list)
        for sample in self._calibration.samples():
            if sample.get("quality", {}).get("status") == "rejected":
                continue
            layout_hash = str(sample.get("anchor_layout_hash") or "")
            area_id = str(sample.get("room_area_id") or "")
            position = sample.get("position") or {}
            x_m = position.get("x_m")
            y_m = position.get("y_m")
            z_m = position.get("z_m")
            sigma_m = float(
                sample.get(
                    "sample_radius_m",
                    sample.get("room_radius_m", DEFAULT_SAMPLE_RADIUS_M),
                )
            )
            if not layout_hash or not area_id or x_m is None or y_m is None or z_m is None:
                continue
            area = self._area_registry.async_get_area(area_id)
            if area is None:
                _LOGGER.warning(
                    "Room classifier: area %s referenced by calibration samples no longer exists; "
                    "those samples will not contribute to room classification until the area is restored "
                    "or the samples are deleted",
                    area_id,
                )
                continue
            layouts[layout_hash].append(
                _SampleKernel(
                    area_id=area_id,
                    floor_id=area.floor_id,
                    x_m=float(x_m),
                    y_m=float(y_m),
                    z_m=float(z_m),
                    sigma_m=max(float(sigma_m), 0.1),
                )
            )
            fingerprint_rssi: dict[str, float] = {}
            for scanner_address, anchor in (sample.get("anchors") or {}).items():
                rssi_median = anchor.get("rssi_median")
                if rssi_median is None:
                    continue
                fingerprint_rssi[str(scanner_address).lower()] = float(rssi_median)
            if fingerprint_rssi:
                fingerprints[layout_hash].append(
                    _SampleFingerprint(
                        area_id=area_id,
                        floor_id=area.floor_id,
                        rssi_by_scanner=fingerprint_rssi,
                    )
                )
        self._layouts = dict(layouts)
        self._fingerprints = dict(fingerprints)
        self._transition_strengths = self._build_transition_strengths(layouts)

    def transition_strength(
        self,
        *,
        layout_hash: str,
        floor_id: str | None,
        from_area_id: str | None,
        to_area_id: str | None,
    ) -> float:
        """Return soft transition plausibility between two rooms on a layout/floor."""
        if not layout_hash or floor_id is None or not from_area_id or not to_area_id:
            return 1.0
        if from_area_id == to_area_id:
            return 1.0
        return self._transition_strengths.get((layout_hash, floor_id, from_area_id, to_area_id), 0.0)

    def has_trained_rooms(self, layout_hash: str, floor_id: str | None) -> bool:
        """Return whether trained rooms exist for a layout/floor pair."""
        return any(sample.floor_id == floor_id for sample in self._layouts.get(layout_hash, [])) or any(
            sample.floor_id == floor_id for sample in self._fingerprints.get(layout_hash, [])
        )

    def classify(
        self,
        *,
        layout_hash: str,
        floor_id: str | None,
        x_m: float,
        y_m: float,
        z_m: float | None,
        live_rssi_by_scanner: dict[str, float] | None = None,
    ) -> RoomClassification:
        """Classify one solved position using geometry and optional RSSI fingerprints."""
        if floor_id is None:
            return RoomClassification(area_id=None, reason="missing_floor")

        samples = [sample for sample in self._layouts.get(layout_hash, []) if sample.floor_id == floor_id]
        fingerprints = [sample for sample in self._fingerprints.get(layout_hash, []) if sample.floor_id == floor_id]
        if not samples and not fingerprints:
            return RoomClassification(area_id=None, reason="no_trained_rooms")

        geometry_scores, geometry_topk = self._geometry_room_scores(samples, x_m=x_m, y_m=y_m, z_m=z_m)
        fingerprint_scores, fingerprint_topk = self._fingerprint_room_scores(
            fingerprints,
            live_rssi_by_scanner or {},
        )

        if geometry_scores:
            room_scores = {
                area_id: (FINGERPRINT_WEIGHT * fingerprint_scores.get(area_id, 0.0))
                + ((1.0 - FINGERPRINT_WEIGHT) * geometry_scores.get(area_id, 0.0))
                if fingerprint_scores and live_rssi_by_scanner
                else geometry_scores.get(area_id, 0.0)
                for area_id in (set(geometry_scores) | set(fingerprint_scores))
            }
            topk_by_area = {
                area_id: max(geometry_topk.get(area_id, 0), fingerprint_topk.get(area_id, 0))
                for area_id in room_scores
            }
        else:
            room_scores = fingerprint_scores
            topk_by_area = fingerprint_topk

        if not room_scores:
            return RoomClassification(area_id=None, reason="weak_room_evidence")

        ranked_rooms = sorted(room_scores.items(), key=lambda row: (row[1], row[0]), reverse=True)
        best_area_id, best_score = ranked_rooms[0]
        second_score = ranked_rooms[1][1] if len(ranked_rooms) > 1 else 0.0
        topk_used = topk_by_area.get(best_area_id, 0)
        geometry_score = geometry_scores.get(best_area_id, 0.0)
        fingerprint_score = fingerprint_scores.get(best_area_id, 0.0)

        if best_score < ROOM_SCORE_MIN:
            return RoomClassification(
                area_id=None,
                reason="weak_room_evidence",
                best_area_id=best_area_id,
                best_score=best_score,
                second_score=second_score,
                topk_used=topk_used,
                geometry_score=geometry_score,
                fingerprint_score=fingerprint_score,
            )
        if len(ranked_rooms) > 1 and (best_score / max(second_score, 1e-9)) < ROOM_SCORE_RATIO_MIN:
            return RoomClassification(
                area_id=None,
                reason="room_ambiguity",
                best_area_id=best_area_id,
                best_score=best_score,
                second_score=second_score,
                topk_used=topk_used,
                geometry_score=geometry_score,
                fingerprint_score=fingerprint_score,
            )
        return RoomClassification(
            area_id=best_area_id,
            reason="ok",
            best_area_id=best_area_id,
            best_score=best_score,
            second_score=second_score,
            topk_used=topk_used,
            geometry_score=geometry_score,
            fingerprint_score=fingerprint_score,
        )

    def fingerprint_global(
        self,
        *,
        layout_hash: str,
        live_rssi_by_scanner: dict[str, float] | None = None,
    ) -> GlobalFingerprintResult:
        """Score fingerprints across all floors for split-level arbitration."""
        fingerprints = self._fingerprints.get(layout_hash, [])
        if not fingerprints:
            return GlobalFingerprintResult(area_id=None, floor_id=None, reason="no_trained_rooms")

        fingerprint_scores, _fingerprint_topk = self._fingerprint_room_scores(
            fingerprints,
            live_rssi_by_scanner or {},
        )
        if not fingerprint_scores:
            return GlobalFingerprintResult(area_id=None, floor_id=None, reason="weak_room_evidence")

        area_floor_ids: dict[str, str | None] = {}
        for sample in fingerprints:
            area_floor_ids.setdefault(sample.area_id, sample.floor_id)

        ranked_rooms = sorted(fingerprint_scores.items(), key=lambda row: (row[1], row[0]), reverse=True)
        best_area_id, best_score = ranked_rooms[0]
        second_score = ranked_rooms[1][1] if len(ranked_rooms) > 1 else 0.0

        floor_scores: dict[str, float] = {}
        for area_id, room_score in fingerprint_scores.items():
            floor_id = area_floor_ids.get(area_id)
            if floor_id is None:
                continue
            floor_scores[floor_id] = max(floor_scores.get(floor_id, 0.0), room_score)

        if not floor_scores:
            return GlobalFingerprintResult(area_id=None, floor_id=None, reason="missing_floor")

        ranked_floors = sorted(floor_scores.items(), key=lambda row: (row[1], row[0]), reverse=True)
        best_floor_id, best_floor_score = ranked_floors[0]
        total_floor_score = sum(floor_scores.values())
        total_room_score = sum(fingerprint_scores.values())
        floor_confidence = best_floor_score / total_floor_score if total_floor_score > 0.0 else 0.0
        room_confidence = best_score / total_room_score if total_room_score > 0.0 else 0.0

        result_area_id: str | None = best_area_id
        reason = "ok"
        if best_score < ROOM_SCORE_MIN:
            result_area_id = None
            reason = "weak_room_evidence"
        elif len(ranked_rooms) > 1 and (best_score / max(second_score, 1e-9)) < ROOM_SCORE_RATIO_MIN:
            result_area_id = None
            reason = "room_ambiguity"

        return GlobalFingerprintResult(
            area_id=result_area_id,
            floor_id=best_floor_id,
            reason=reason,
            floor_confidence=floor_confidence,
            room_confidence=room_confidence,
            best_score=best_score,
            second_score=second_score,
            floor_scores=floor_scores,
        )

    def _geometry_room_scores(
        self,
        samples: list[_SampleKernel],
        *,
        x_m: float,
        y_m: float,
        z_m: float | None,
    ) -> tuple[dict[str, float], dict[str, int]]:
        """Return per-room geometry scores from the current solved point."""
        position_z = 0.0 if z_m is None else z_m
        room_scores: dict[str, list[float]] = defaultdict(list)
        for sample in samples:
            dx = x_m - sample.x_m
            dy = y_m - sample.y_m
            dz = position_z - sample.z_m
            d2 = (dx * dx) + (dy * dy) + (ROOM_KERNEL_Z_WEIGHT * dz * dz)
            sample_score = math.exp(-0.5 * d2 / (sample.sigma_m * sample.sigma_m))
            room_scores[sample.area_id].append(sample_score)

        scored_rooms: dict[str, float] = {}
        topk_by_area: dict[str, int] = {}
        for area_id, scores in room_scores.items():
            top_scores = sorted(scores, reverse=True)[:K_CAP]
            scored_rooms[area_id] = sum(top_scores) / len(top_scores)
            topk_by_area[area_id] = len(top_scores)
        return scored_rooms, topk_by_area

    def _fingerprint_room_scores(
        self,
        samples: list[_SampleFingerprint],
        live_rssi_by_scanner: dict[str, float],
    ) -> tuple[dict[str, float], dict[str, int]]:
        """Return per-room RSSI-space fingerprint scores."""
        if not live_rssi_by_scanner:
            return {}, {}

        live = {str(scanner_address).lower(): float(rssi) for scanner_address, rssi in live_rssi_by_scanner.items()}
        room_scores: dict[str, list[float]] = defaultdict(list)

        for sample in samples:
            common_scanners = sorted(set(sample.rssi_by_scanner) & set(live))
            if len(common_scanners) < min(FINGERPRINT_MIN_COMMON_SCANNERS, len(live)):
                continue

            total_sq = 0.0
            for scanner_address in common_scanners:
                delta = live[scanner_address] - sample.rssi_by_scanner[scanner_address]
                total_sq += delta * delta

            missing_sample_scanners = len(set(sample.rssi_by_scanner) - set(live))
            extra_live_scanners = len(set(live) - set(sample.rssi_by_scanner))
            total_sq += missing_sample_scanners * (FINGERPRINT_MISSING_PENALTY_DB**2)
            total_sq += extra_live_scanners * (FINGERPRINT_EXTRA_SCANNER_PENALTY_DB**2)
            compared_count = len(common_scanners) + missing_sample_scanners + extra_live_scanners
            mean_sq = total_sq / max(compared_count, 1)
            sample_score = math.exp(-0.5 * mean_sq / (FINGERPRINT_SIGMA_DB**2))
            room_scores[sample.area_id].append(sample_score)

        scored_rooms: dict[str, float] = {}
        topk_by_area: dict[str, int] = {}
        for area_id, scores in room_scores.items():
            top_scores = sorted(scores, reverse=True)[:FINGERPRINT_K_CAP]
            scored_rooms[area_id] = sum(top_scores) / len(top_scores)
            topk_by_area[area_id] = len(top_scores)
        return scored_rooms, topk_by_area

    def _build_transition_strengths(
        self,
        layouts: dict[str, list[_SampleKernel]],
    ) -> dict[tuple[str, str | None, str, str], float]:
        """Infer soft room-transition strengths from sample-cloud overlap/gap."""
        strengths: dict[tuple[str, str | None, str, str], float] = {}
        for layout_hash, samples in layouts.items():
            by_floor_area: dict[tuple[str | None, str], list[_SampleKernel]] = defaultdict(list)
            for sample in samples:
                by_floor_area[(sample.floor_id, sample.area_id)].append(sample)

            floor_ids = {floor_id for floor_id, _area_id in by_floor_area}
            for floor_id in floor_ids:
                areas = sorted(area_id for _floor, area_id in by_floor_area if _floor == floor_id)
                for from_area_id in areas:
                    from_samples = by_floor_area[(floor_id, from_area_id)]
                    for to_area_id in areas:
                        if from_area_id == to_area_id:
                            continue
                        to_samples = by_floor_area[(floor_id, to_area_id)]
                        strength = self._pairwise_transition_strength(from_samples, to_samples)
                        strengths[(layout_hash, floor_id, from_area_id, to_area_id)] = strength
        return strengths

    @staticmethod
    def _pairwise_transition_strength(
        from_samples: list[_SampleKernel],
        to_samples: list[_SampleKernel],
    ) -> float:
        """Return soft plausibility that two sampled rooms can transition locally."""
        best_strength = 0.0
        for sample_a in from_samples:
            for sample_b in to_samples:
                dx = sample_a.x_m - sample_b.x_m
                dy = sample_a.y_m - sample_b.y_m
                dz = sample_a.z_m - sample_b.z_m
                center_distance = math.sqrt((dx * dx) + (dy * dy) + (ROOM_KERNEL_Z_WEIGHT * dz * dz))
                support_gap = max(0.0, center_distance - (sample_a.sigma_m + sample_b.sigma_m))
                strength = math.exp(-0.5 * ((support_gap / TRANSITION_GAP_SIGMA_M) ** 2))
                if strength > best_strength:
                    best_strength = strength
        return max(0.0, min(1.0, best_strength))
