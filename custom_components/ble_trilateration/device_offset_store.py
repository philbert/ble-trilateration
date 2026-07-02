"""
Persistent storage for per-device RSSI offset calibration.

Calibration samples form a single-device "map" of the house: every stored
RSSI median is on the reference device's TX/antenna scale. Other devices
broadcast at different effective power, which shifts their live RSSI by a
roughly constant amount across all anchors. This store holds that per-device
scalar so live readings can be mapped back onto the reference scale before
ranging and fingerprint matching.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1
STORAGE_SUBDIR = "ble_trilateration"
STORAGE_KEY = f"{STORAGE_SUBDIR}/device_offsets"


class BermudaDeviceOffsetStore:
    """Persist per-device RSSI offsets keyed by HA device registry id."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise store wrapper."""
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] = {"devices": {}}
        self._loaded = False

    async def async_load(self) -> None:
        """Load stored offset data."""
        if self._loaded:
            return
        loaded = await self._store.async_load()
        if isinstance(loaded, dict) and isinstance(loaded.get("devices"), dict):
            self._data = loaded
        self._loaded = True

    async def async_ensure_loaded(self) -> None:
        """Load storage on first use."""
        await self.async_load()

    @property
    def has_offsets(self) -> bool:
        """Return whether any device offset is stored (fast path for hot loops)."""
        return self._loaded and bool(self._data["devices"])

    def get_offset_db(self, device_id: str | None) -> float | None:
        """
        Return the stored offset for a device registry id, when known.

        Positive means the device reads hotter than the calibration reference
        device; subtract it from live RSSI to land on the reference scale.
        """
        if not self._loaded or device_id is None:
            return None
        record = self._data["devices"].get(device_id)
        if not isinstance(record, dict):
            return None
        try:
            return float(record["offset_db"])
        except (KeyError, TypeError, ValueError):
            return None

    def get_record(self, device_id: str | None) -> dict[str, Any] | None:
        """Return the full stored record for a device registry id."""
        if not self._loaded or device_id is None:
            return None
        record = self._data["devices"].get(device_id)
        return deepcopy(record) if isinstance(record, dict) else None

    async def async_save_offset(self, device_id: str, record: dict[str, Any]) -> None:
        """Persist one device offset record."""
        await self.async_ensure_loaded()
        self._data["devices"][device_id] = deepcopy(record)
        await self._store.async_save(self._data)

    async def async_remove_offset(self, device_id: str) -> bool:
        """Remove a stored offset. Returns True when a record existed."""
        await self.async_ensure_loaded()
        removed = self._data["devices"].pop(device_id, None) is not None
        if removed:
            await self._store.async_save(self._data)
        return removed

    @property
    def devices(self) -> dict[str, Any]:
        """Return a defensive copy of all stored offset records."""
        return deepcopy(self._data["devices"])
