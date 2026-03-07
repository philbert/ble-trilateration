"""Calibration-sample room classifier for Bermuda."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import DEFAULT_ROOM_RADIUS_M

if TYPE_CHECKING:
    from homeassistant.helpers.area_registry import AreaRegistry

    from .calibration import BermudaCalibrationManager

_LOGGER = logging.getLogger(__name__)


MIN_ROOM_SAMPLE_COUNT = 1
ROOM_MARGIN_M = 0.5


@dataclass(frozen=True)
class RoomClassification:
    """Room classification result for one solved position."""

    area_id: str | None
    reason: str


@dataclass(frozen=True)
class _RoomPrototype:
    """Trained room envelope for one layout."""

    area_id: str
    floor_id: str | None
    centroid_x_m: float
    centroid_y_m: float
    centroid_z_m: float
    radius_m: float
    sample_count: int


class BermudaRoomClassifier:
    """Classify trilat positions into rooms using calibration samples."""

    def __init__(self, calibration: BermudaCalibrationManager, area_registry: AreaRegistry) -> None:
        """Initialise classifier cache."""
        self._calibration = calibration
        self._area_registry = area_registry
        self._layouts: dict[str, list[_RoomPrototype]] = {}

    async def async_rebuild(self) -> None:
        """Rebuild all room prototypes from current calibration samples."""
        grouped: dict[tuple[str, str], list[tuple[float, float, float, float]]] = {}
        for sample in self._calibration.samples():
            if sample.get("quality", {}).get("status") == "rejected":
                continue
            layout_hash = str(sample.get("anchor_layout_hash") or "")
            area_id = str(sample.get("room_area_id") or "")
            position = sample.get("position") or {}
            x_m = position.get("x_m")
            y_m = position.get("y_m")
            z_m = position.get("z_m")
            room_radius_m = float(sample.get("room_radius_m", DEFAULT_ROOM_RADIUS_M))
            if not layout_hash or not area_id or x_m is None or y_m is None or z_m is None:
                continue
            grouped.setdefault((layout_hash, area_id), []).append(
                (float(x_m), float(y_m), float(z_m), room_radius_m)
            )

        layouts: dict[str, list[_RoomPrototype]] = {}
        for (layout_hash, area_id), positions in grouped.items():
            if len(positions) < MIN_ROOM_SAMPLE_COUNT:
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
            centroid_x_m = sum(pos[0] for pos in positions) / len(positions)
            centroid_y_m = sum(pos[1] for pos in positions) / len(positions)
            centroid_z_m = sum(pos[2] for pos in positions) / len(positions)
            radius_m = 0.0
            for pos_x, pos_y, pos_z, declared_radius_m in positions:
                radius_m = max(
                    radius_m,
                    math.sqrt(
                        ((pos_x - centroid_x_m) ** 2)
                        + ((pos_y - centroid_y_m) ** 2)
                        + ((pos_z - centroid_z_m) ** 2)
                    )
                    + declared_radius_m,
                )
            layouts.setdefault(layout_hash, []).append(
                _RoomPrototype(
                    area_id=area_id,
                    floor_id=area.floor_id,
                    centroid_x_m=centroid_x_m,
                    centroid_y_m=centroid_y_m,
                    centroid_z_m=centroid_z_m,
                    radius_m=radius_m,
                    sample_count=len(positions),
                )
            )
        self._layouts = layouts

    def has_trained_rooms(self, layout_hash: str, floor_id: str | None) -> bool:
        """Return whether trained rooms exist for a layout/floor pair."""
        return any(room.floor_id == floor_id for room in self._layouts.get(layout_hash, []))

    def classify(
        self,
        *,
        layout_hash: str,
        floor_id: str | None,
        x_m: float,
        y_m: float,
        z_m: float | None,
    ) -> RoomClassification:
        """Classify one solved position."""
        if floor_id is None:
            return RoomClassification(area_id=None, reason="missing_floor")

        rooms = [room for room in self._layouts.get(layout_hash, []) if room.floor_id == floor_id]
        if not rooms:
            return RoomClassification(area_id=None, reason="no_trained_rooms")

        position_z = 0.0 if z_m is None else z_m
        containing: list[tuple[float, _RoomPrototype]] = []
        for room in rooms:
            centroid_distance = math.sqrt(
                ((x_m - room.centroid_x_m) ** 2)
                + ((y_m - room.centroid_y_m) ** 2)
                + ((position_z - room.centroid_z_m) ** 2)
            )
            if centroid_distance <= room.radius_m:
                containing.append((centroid_distance, room))

        if not containing:
            return RoomClassification(area_id=None, reason="outside_trained_support")

        containing.sort(key=lambda item: item[0])
        best_distance, best_room = containing[0]
        if len(containing) > 1:
            second_distance = containing[1][0]
            if (second_distance - best_distance) < ROOM_MARGIN_M:
                return RoomClassification(area_id=None, reason="margin_not_met")
        return RoomClassification(area_id=best_room.area_id, reason="ok")
