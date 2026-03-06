"""Persistent storage for Bermuda calibration samples."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1


class BermudaCalibrationStore:
    """Persist calibration samples outside config entry options."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise store wrapper."""
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.calibration_samples.{entry_id}")
        self._data: dict[str, Any] = {"samples": []}
        self._loaded = False

    async def async_load(self) -> None:
        """Load calibration data from storage."""
        if self._loaded:
            return
        loaded = await self._store.async_load()
        if isinstance(loaded, dict) and isinstance(loaded.get("samples"), list):
            self._data = loaded
        self._loaded = True

    async def async_ensure_loaded(self) -> None:
        """Load storage on first use."""
        await self.async_load()

    @property
    def samples(self) -> list[dict[str, Any]]:
        """Return a defensive copy of stored samples."""
        return deepcopy(self._data["samples"])

    @property
    def sample_count(self) -> int:
        """Return total stored sample count."""
        return len(self._data["samples"])

    async def async_add_sample(self, sample: dict[str, Any]) -> None:
        """Persist a single calibration sample."""
        await self.async_ensure_loaded()
        self._data["samples"].append(deepcopy(sample))
        await self._store.async_save(self._data)

    async def async_delete_sample(self, sample_id: str) -> bool:
        """Delete one sample by id."""
        await self.async_ensure_loaded()
        original_len = len(self._data["samples"])
        self._data["samples"] = [sample for sample in self._data["samples"] if sample.get("id") != sample_id]
        changed = len(self._data["samples"]) != original_len
        if changed:
            await self._store.async_save(self._data)
        return changed

    async def async_clear_all(self) -> int:
        """Delete all stored samples."""
        await self.async_ensure_loaded()
        removed = len(self._data["samples"])
        self._data["samples"] = []
        await self._store.async_save(self._data)
        return removed

    async def async_clear_device(self, device_id: str) -> int:
        """Delete all samples for a device registry id."""
        await self.async_ensure_loaded()
        kept = [sample for sample in self._data["samples"] if sample.get("device_id") != device_id]
        removed = len(self._data["samples"]) - len(kept)
        if removed:
            self._data["samples"] = kept
            await self._store.async_save(self._data)
        return removed

    async def async_clear_anchor_layout(self, anchor_layout_hash: str) -> int:
        """Delete all samples tied to an anchor layout hash."""
        await self.async_ensure_loaded()
        kept = [sample for sample in self._data["samples"] if sample.get("anchor_layout_hash") != anchor_layout_hash]
        removed = len(self._data["samples"]) - len(kept)
        if removed:
            self._data["samples"] = kept
            await self._store.async_save(self._data)
        return removed
