"""Persistent storage for last trusted trilat state used during restart bootstrap."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORAGE_VERSION = 1
STORAGE_KEY = "bermuda/trilat_bootstrap"
SAVE_DELAY_S = 1.0


@dataclass
class TrilatBootstrapRecord:
    """Last trusted trilat state for one tracked device."""

    saved_at: str
    floor_id: str
    area_id: str | None
    x_m: float
    y_m: float
    z_m: float | None
    layout_hash: str
    floor_confidence: float
    geometry_quality_01: float


class BermudaTrilatBootstrapStore:
    """Persist warm-start trilat bootstrap hints outside entity restore state."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._records: dict[str, TrilatBootstrapRecord] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Load persisted bootstrap data once."""
        if self._loaded:
            return
        loaded = await self._store.async_load()
        if isinstance(loaded, dict):
            devices_raw = loaded.get("devices", {})
            if isinstance(devices_raw, dict):
                for address, raw in devices_raw.items():
                    if not isinstance(raw, dict):
                        continue
                    try:
                        self._records[str(address).lower()] = TrilatBootstrapRecord(
                            saved_at=str(raw["saved_at"]),
                            floor_id=str(raw["floor_id"]),
                            area_id=str(raw["area_id"]) if raw.get("area_id") else None,
                            x_m=float(raw["x_m"]),
                            y_m=float(raw["y_m"]),
                            z_m=float(raw["z_m"]) if raw.get("z_m") is not None else None,
                            layout_hash=str(raw.get("layout_hash") or ""),
                            floor_confidence=float(raw.get("floor_confidence") or 0.0),
                            geometry_quality_01=float(raw.get("geometry_quality_01") or 0.0),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
        self._loaded = True

    def get(self, address: str) -> TrilatBootstrapRecord | None:
        """Return the persisted record for an address, if loaded."""
        return self._records.get(address.lower())

    def schedule_save(self, address: str, record: TrilatBootstrapRecord) -> None:
        """Queue a save for the given device bootstrap state."""
        self._records[address.lower()] = record
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY_S)

    async def async_save(self) -> None:
        """Persist current state immediately."""
        await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "devices": {
                address: asdict(record)
                for address, record in self._records.items()
            }
        }
