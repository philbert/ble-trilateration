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
FINGERPRINT_SIGMA_DB_MIN = 4.0
FINGERPRINT_SIGMA_DB_MAX = 12.0
FINGERPRINT_MISSING_PENALTY_DB = 9.0
FINGERPRINT_MISSING_FEATURE_FLOOR_DB = 6.0
FINGERPRINT_EXTRA_SCANNER_PENALTY_DB = 4.5
FINGERPRINT_MIN_COMMON_SCANNERS = 2
FINGERPRINT_PACKET_COUNT_REFERENCE = 3.0
FINGERPRINT_RELIABILITY_FLOOR = 0.25
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
    fingerprint_best_area_id: str | None = None
    fingerprint_best_score: float = 0.0
    fingerprint_second_score: float = 0.0
    fingerprint_confidence: float = 0.0
    fingerprint_coverage: float = 0.0
    sample_count: int = 0


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
    features_by_scanner: dict[str, "_FingerprintFeature"]


@dataclass(frozen=True)
class _FingerprintFeature:
    """Compact per-scanner fingerprint statistics persisted with one sample."""

    rssi_median: float
    rssi_mad: float
    packet_count: int
    rssi_span: float


class BermudaRoomClassifier:
    """Classify trilat positions into rooms using calibration samples."""

    def __init__(self, calibration: BermudaCalibrationManager, area_registry: AreaRegistry) -> None:
        """Initialise classifier cache."""
        self._calibration = calibration
        self._area_registry = area_registry
        self._layouts: dict[str, list[_SampleKernel]] = {}
        self._fingerprints: dict[str, list[_SampleFingerprint]] = {}
        self._transition_strengths: dict[tuple[str, str | None, str, str], float] = {}
        self._room_sample_counts: dict[str, dict[tuple[str | None, str], int]] = {}
        self._room_reference_points: dict[str, dict[tuple[str | None, str], tuple[float, float, float]]] = {}

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
            fingerprint_features: dict[str, _FingerprintFeature] = {}
            for scanner_address, anchor in (sample.get("anchors") or {}).items():
                rssi_median = anchor.get("rssi_median")
                if rssi_median is None:
                    continue
                scanner_key = str(scanner_address).lower()
                rssi_mad = max(float(anchor.get("rssi_mad") or 0.0), 0.0)
                packet_count = max(int(anchor.get("packet_count") or 1), 1)
                rssi_min = anchor.get("rssi_min")
                rssi_max = anchor.get("rssi_max")
                if rssi_min is not None and rssi_max is not None:
                    rssi_span = max(float(rssi_max) - float(rssi_min), 0.0)
                else:
                    rssi_span = 0.0
                fingerprint_rssi[scanner_key] = float(rssi_median)
                fingerprint_features[scanner_key] = _FingerprintFeature(
                    rssi_median=float(rssi_median),
                    rssi_mad=rssi_mad,
                    packet_count=packet_count,
                    rssi_span=rssi_span,
                )
            if fingerprint_rssi:
                fingerprints[layout_hash].append(
                    _SampleFingerprint(
                        area_id=area_id,
                        floor_id=area.floor_id,
                        rssi_by_scanner=fingerprint_rssi,
                        features_by_scanner=fingerprint_features,
                    )
                )
        self._layouts = dict(layouts)
        self._fingerprints = dict(fingerprints)
        self._transition_strengths = self._build_transition_strengths(layouts)
        room_sample_counts: dict[str, dict[tuple[str | None, str], int]] = {}
        room_reference_points: dict[str, dict[tuple[str | None, str], tuple[float, float, float]]] = {}
        for layout_hash, kernels in layouts.items():
            counts: dict[tuple[str | None, str], int] = defaultdict(int)
            sums: dict[tuple[str | None, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
            for kernel in kernels:
                key = (kernel.floor_id, kernel.area_id)
                counts[key] += 1
                sums[key][0] += kernel.x_m
                sums[key][1] += kernel.y_m
                sums[key][2] += kernel.z_m
            room_sample_counts[layout_hash] = dict(counts)
            room_reference_points[layout_hash] = {
                key: (totals[0] / count, totals[1] / count, totals[2] / count)
                for key, totals in sums.items()
                if (count := counts.get(key, 0)) > 0
            }
        self._room_sample_counts = room_sample_counts
        self._room_reference_points = room_reference_points

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

    def room_sample_count(self, layout_hash: str, floor_id: str | None, area_id: str | None) -> int:
        """Return accepted calibration sample count for one room on a layout/floor."""
        if not layout_hash or area_id is None:
            return 0
        return int(self._room_sample_counts.get(layout_hash, {}).get((floor_id, area_id), 0))

    def room_reference_point(
        self,
        layout_hash: str,
        floor_id: str | None,
        area_id: str | None,
    ) -> tuple[float, float, float] | None:
        """Return centroid-like reference point for one room on a layout/floor."""
        if not layout_hash or area_id is None:
            return None
        return self._room_reference_points.get(layout_hash, {}).get((floor_id, area_id))

    def floor_xy_envelope(
        self,
        layout_hash: str,
        floor_id: str | None,
    ) -> tuple[float, float, float, float] | None:
        """Return (x_min, x_max, y_min, y_max) bounding box for a floor's calibration samples.

        Each sample is expanded by its sigma (capture radius). Returns None if no samples
        are available for this floor and layout hash.
        """
        if floor_id is None:
            return None
        kernels = [k for k in self._layouts.get(layout_hash, []) if k.floor_id == floor_id]
        if not kernels:
            return None
        x_min = min(k.x_m - k.sigma_m for k in kernels)
        x_max = max(k.x_m + k.sigma_m for k in kernels)
        y_min = min(k.y_m - k.sigma_m for k in kernels)
        y_max = max(k.y_m + k.sigma_m for k in kernels)
        return (x_min, x_max, y_min, y_max)

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
        fingerprint_scores, fingerprint_topk, fingerprint_coverage = self._fingerprint_room_scores(
            fingerprints,
            live_rssi_by_scanner or {},
        )
        ranked_fingerprints = sorted(fingerprint_scores.items(), key=lambda row: (row[1], row[0]), reverse=True)
        fingerprint_best_area_id = ranked_fingerprints[0][0] if ranked_fingerprints else None
        fingerprint_best_score = ranked_fingerprints[0][1] if ranked_fingerprints else 0.0
        fingerprint_second_score = ranked_fingerprints[1][1] if len(ranked_fingerprints) > 1 else 0.0
        fingerprint_confidence = (
            1.0
            if len(ranked_fingerprints) == 1 and fingerprint_best_score > 0.0
            else max(0.0, fingerprint_best_score - fingerprint_second_score)
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
        sample_count = self.room_sample_count(layout_hash, floor_id, best_area_id)
        candidate_fingerprint_coverage = fingerprint_coverage.get(best_area_id, 0.0)

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
                fingerprint_best_area_id=fingerprint_best_area_id,
                fingerprint_best_score=fingerprint_best_score,
                fingerprint_second_score=fingerprint_second_score,
                fingerprint_confidence=fingerprint_confidence,
                fingerprint_coverage=candidate_fingerprint_coverage,
                sample_count=sample_count,
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
                fingerprint_best_area_id=fingerprint_best_area_id,
                fingerprint_best_score=fingerprint_best_score,
                fingerprint_second_score=fingerprint_second_score,
                fingerprint_confidence=fingerprint_confidence,
                fingerprint_coverage=candidate_fingerprint_coverage,
                sample_count=sample_count,
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
            fingerprint_best_area_id=fingerprint_best_area_id,
            fingerprint_best_score=fingerprint_best_score,
            fingerprint_second_score=fingerprint_second_score,
            fingerprint_confidence=fingerprint_confidence,
            fingerprint_coverage=candidate_fingerprint_coverage,
            sample_count=sample_count,
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

        fingerprint_scores, _fingerprint_topk, _fingerprint_coverage = self._fingerprint_room_scores(
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
    ) -> tuple[dict[str, float], dict[str, int], dict[str, float]]:
        """Return per-room RSSI-space fingerprint scores."""
        if not live_rssi_by_scanner:
            return {}, {}, {}

        live = {str(scanner_address).lower(): float(rssi) for scanner_address, rssi in live_rssi_by_scanner.items()}
        room_scores: dict[str, list[float]] = defaultdict(list)
        total_samples_by_area: dict[str, int] = defaultdict(int)
        covered_samples_by_area: dict[str, int] = defaultdict(int)

        for sample in samples:
            total_samples_by_area[sample.area_id] += 1
            sample_scanners = set(sample.features_by_scanner)
            common_scanners = sorted(sample_scanners & set(live))
            if common_scanners:
                covered_samples_by_area[sample.area_id] += 1
            if len(common_scanners) < min(FINGERPRINT_MIN_COMMON_SCANNERS, len(live)):
                continue

            total_weighted_sq = 0.0
            total_weight = 0.0
            for scanner_address in common_scanners:
                feature = sample.features_by_scanner[scanner_address]
                sigma_db = self._fingerprint_sigma_db(feature)
                weight = self._fingerprint_reliability_weight(feature)
                delta = live[scanner_address] - feature.rssi_median
                total_weighted_sq += weight * ((delta / sigma_db) ** 2)
                total_weight += weight

            for scanner_address in sorted(sample_scanners - set(live)):
                feature = sample.features_by_scanner[scanner_address]
                sigma_db = self._fingerprint_sigma_db(feature)
                weight = self._fingerprint_reliability_weight(feature)
                penalty_db = min(
                    FINGERPRINT_MISSING_PENALTY_DB,
                    max(
                        FINGERPRINT_MISSING_FEATURE_FLOOR_DB,
                        feature.rssi_mad,
                        feature.rssi_span / 4.0,
                    ),
                )
                total_weighted_sq += weight * ((penalty_db / sigma_db) ** 2)
                total_weight += weight

            extra_live_scanners = len(set(live) - sample_scanners)
            if extra_live_scanners:
                extra_penalty = (FINGERPRINT_EXTRA_SCANNER_PENALTY_DB / FINGERPRINT_SIGMA_DB) ** 2
                total_weighted_sq += extra_live_scanners * extra_penalty
                total_weight += float(extra_live_scanners)

            mean_sq = total_weighted_sq / max(total_weight, 1e-6)
            sample_score = math.exp(-0.5 * mean_sq)
            room_scores[sample.area_id].append(sample_score)

        scored_rooms: dict[str, float] = {}
        topk_by_area: dict[str, int] = {}
        coverage_by_area: dict[str, float] = {
            area_id: (covered_samples_by_area.get(area_id, 0) / count)
            for area_id, count in total_samples_by_area.items()
            if count > 0
        }
        for area_id, scores in room_scores.items():
            top_scores = sorted(scores, reverse=True)[:FINGERPRINT_K_CAP]
            scored_rooms[area_id] = sum(top_scores) / len(top_scores)
            topk_by_area[area_id] = len(top_scores)
        return scored_rooms, topk_by_area, coverage_by_area

    def _fingerprint_sigma_db(self, feature: _FingerprintFeature) -> float:
        """Return expected RSSI spread for one scanner feature."""
        spread_db = max(feature.rssi_mad, feature.rssi_span / 4.0)
        return max(
            FINGERPRINT_SIGMA_DB_MIN,
            min(FINGERPRINT_SIGMA_DB_MAX, (FINGERPRINT_SIGMA_DB * 0.5) + spread_db),
        )

    def _fingerprint_reliability_weight(self, feature: _FingerprintFeature) -> float:
        """Return scanner reliability weight from calibration stability and visibility."""
        spread_db = max(feature.rssi_mad, feature.rssi_span / 4.0)
        packet_weight = min(1.0, float(feature.packet_count) / FINGERPRINT_PACKET_COUNT_REFERENCE)
        stability_weight = max(FINGERPRINT_RELIABILITY_FLOOR, min(1.0, 1.0 - (spread_db / 10.0)))
        return max(FINGERPRINT_RELIABILITY_FLOOR, packet_weight * stability_weight)

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
