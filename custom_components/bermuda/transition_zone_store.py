"""Persistent storage for Bermuda transition zones."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORAGE_KEY = "bermuda/transition_zones"
STORAGE_VERSION = 1


@dataclass
class TransitionZoneCapture:
    x_m: float
    y_m: float
    z_m: float
    sigma_m: float  # Gaussian kernel width — same as sample_radius_m, NOT a hard disc radius


@dataclass
class TransitionZone:
    zone_id: str
    name: str
    captures: list[TransitionZoneCapture]
    floor_pairs: list[tuple[str, str]]   # (from_floor_id, to_floor_id), both directions stored
    anchor_layout_hash: str
    created_at: str

    def score(self, x_m: float, y_m: float, z_m: float) -> float:
        """Max Gaussian kernel score across all captures. No hard boundary."""
        best = 0.0
        for cap in self.captures:
            dx = x_m - cap.x_m
            dy = y_m - cap.y_m
            dz = z_m - cap.z_m
            d2 = (dx*dx) + (dy*dy) + (dz*dz)
            s = math.exp(-0.5 * d2 / max(cap.sigma_m * cap.sigma_m, 1e-6))
            if s > best:
                best = s
        return best

    def covers_pair(self, from_floor_id: str, to_floor_id: str) -> bool:
        return (from_floor_id, to_floor_id) in self.floor_pairs


class BermudaTransitionZoneStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._zones: dict[str, TransitionZone] = {}

    @property
    def zones(self) -> list[TransitionZone]:
        return list(self._zones.values())

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            for z in data.get("zones", []):
                zone = TransitionZone(
                    zone_id=z["zone_id"],
                    name=z["name"],
                    captures=[TransitionZoneCapture(**c) for c in z["captures"]],
                    floor_pairs=[tuple(p) for p in z["floor_pairs"]],
                    anchor_layout_hash=z["anchor_layout_hash"],
                    created_at=z["created_at"],
                )
                self._zones[zone.zone_id] = zone

    async def async_save_zone(self, zone: TransitionZone) -> None:
        self._zones[zone.zone_id] = zone
        await self._persist()

    async def async_delete_zone(self, zone_id: str) -> bool:
        if zone_id in self._zones:
            del self._zones[zone_id]
            await self._persist()
            return True
        return False

    async def _persist(self) -> None:
        data = {"zones": [
            {
                "zone_id": z.zone_id,
                "name": z.name,
                "captures": [{"x_m": c.x_m, "y_m": c.y_m, "z_m": c.z_m, "sigma_m": c.sigma_m} for c in z.captures],
                "floor_pairs": [list(p) for p in z.floor_pairs],
                "anchor_layout_hash": z.anchor_layout_hash,
                "created_at": z.created_at,
            }
            for z in self._zones.values()
        ]}
        await self._store.async_save(data)
