"""Calibration-sample KDE room classifier for Bermuda."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RoomClassification:
    """Room classification result for one solved position."""

    area_id: str | None
    reason: str
    best_area_id: str | None = None
    best_score: float = 0.0
    second_score: float = 0.0
    topk_used: int = 0


@dataclass(frozen=True)
class _SampleKernel:
    """One persisted calibration sample represented as a soft kernel."""

    area_id: str
    floor_id: str | None
    x_m: float
    y_m: float
    z_m: float
    sigma_m: float


class BermudaRoomClassifier:
    """Classify trilat positions into rooms using calibration samples."""

    def __init__(self, calibration: BermudaCalibrationManager, area_registry: AreaRegistry) -> None:
        """Initialise classifier cache."""
        self._calibration = calibration
        self._area_registry = area_registry
        self._layouts: dict[str, list[_SampleKernel]] = {}

    async def async_rebuild(self) -> None:
        """Rebuild all room kernels from current calibration samples."""
        layouts: dict[str, list[_SampleKernel]] = defaultdict(list)
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
        self._layouts = dict(layouts)

    def has_trained_rooms(self, layout_hash: str, floor_id: str | None) -> bool:
        """Return whether trained rooms exist for a layout/floor pair."""
        return any(sample.floor_id == floor_id for sample in self._layouts.get(layout_hash, []))

    def classify(
        self,
        *,
        layout_hash: str,
        floor_id: str | None,
        x_m: float,
        y_m: float,
        z_m: float | None,
    ) -> RoomClassification:
        """Classify one solved position using per-sample Gaussian kernels."""
        if floor_id is None:
            return RoomClassification(area_id=None, reason="missing_floor")

        samples = [sample for sample in self._layouts.get(layout_hash, []) if sample.floor_id == floor_id]
        if not samples:
            return RoomClassification(area_id=None, reason="no_trained_rooms")

        position_z = 0.0 if z_m is None else z_m
        room_scores: dict[str, list[float]] = defaultdict(list)
        for sample in samples:
            dx = x_m - sample.x_m
            dy = y_m - sample.y_m
            dz = position_z - sample.z_m
            d2 = (dx * dx) + (dy * dy) + (ROOM_KERNEL_Z_WEIGHT * dz * dz)
            sample_score = math.exp(-0.5 * d2 / (sample.sigma_m * sample.sigma_m))
            room_scores[sample.area_id].append(sample_score)

        ranked_rooms: list[tuple[float, str, int]] = []
        for area_id, scores in room_scores.items():
            top_scores = sorted(scores, reverse=True)[:K_CAP]
            ranked_rooms.append((sum(top_scores) / len(top_scores), area_id, len(top_scores)))
        ranked_rooms.sort(key=lambda row: (row[0], row[1]), reverse=True)

        best_score, best_area_id, topk_used = ranked_rooms[0]
        second_score = ranked_rooms[1][0] if len(ranked_rooms) > 1 else 0.0

        if best_score < ROOM_SCORE_MIN:
            return RoomClassification(
                area_id=None,
                reason="weak_room_evidence",
                best_area_id=best_area_id,
                best_score=best_score,
                second_score=second_score,
                topk_used=topk_used,
            )
        if len(ranked_rooms) > 1 and (best_score / max(second_score, 1e-9)) < ROOM_SCORE_RATIO_MIN:
            return RoomClassification(
                area_id=None,
                reason="room_ambiguity",
                best_area_id=best_area_id,
                best_score=best_score,
                second_score=second_score,
                topk_used=topk_used,
            )
        return RoomClassification(
            area_id=best_area_id,
            reason="ok",
            best_area_id=best_area_id,
            best_score=best_score,
            second_score=second_score,
            topk_used=topk_used,
        )
