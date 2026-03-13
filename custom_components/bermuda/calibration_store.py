"""Persistent storage for Bermuda calibration samples."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORAGE_VERSION = 1
STORAGE_KEY = "bermuda/calibration_samples"


class BermudaCalibrationStore:
    """Persist calibration samples outside config entry options."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise store wrapper."""
        del entry_id
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] = {
            "samples": [],
            "transition_samples": [],
            "acknowledged_layout_hashes": [],
        }
        self._loaded = False

    async def async_load(self) -> None:
        """Load calibration data from storage."""
        if self._loaded:
            return
        loaded = await self._store.async_load()
        if isinstance(loaded, dict) and isinstance(loaded.get("samples"), list):
            self._data = {
                "samples": loaded.get("samples", []),
                "transition_samples": loaded.get("transition_samples", []),
                "acknowledged_layout_hashes": loaded.get("acknowledged_layout_hashes", []),
            }
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

    @property
    def transition_samples(self) -> list[dict[str, Any]]:
        """Return a defensive copy of stored transition samples."""
        return deepcopy(self._data.get("transition_samples", []))

    @property
    def transition_sample_count(self) -> int:
        """Return total stored transition-sample count."""
        return len(self._data.get("transition_samples", []))

    @property
    def acknowledged_layout_hashes(self) -> list[str]:
        """Return acknowledged layout hashes."""
        raw_hashes = self._data.get("acknowledged_layout_hashes", [])
        if not isinstance(raw_hashes, list):
            return []
        return [str(layout_hash) for layout_hash in raw_hashes if str(layout_hash)]

    async def async_add_sample(self, sample: dict[str, Any]) -> None:
        """Persist a single calibration sample."""
        await self.async_ensure_loaded()
        self._data["samples"].append(deepcopy(sample))
        await self._store.async_save(self._data)

    async def async_replace_samples(self, samples: list[dict[str, Any]]) -> None:
        """Replace all stored samples."""
        await self.async_ensure_loaded()
        self._data["samples"] = deepcopy(samples)
        await self._store.async_save(self._data)

    async def async_replace_transition_samples(self, transition_samples: list[dict[str, Any]]) -> None:
        """Replace all stored transition samples."""
        await self.async_ensure_loaded()
        self._data["transition_samples"] = deepcopy(transition_samples)
        await self._store.async_save(self._data)

    async def async_delete_transition_sample(self, transition_key: str) -> bool:
        """Delete one transition sample by internal key."""
        await self.async_ensure_loaded()
        original_len = len(self._data.get("transition_samples", []))
        self._data["transition_samples"] = [
            sample
            for sample in self._data.get("transition_samples", [])
            if sample.get("transition_key") != transition_key
        ]
        changed = len(self._data["transition_samples"]) != original_len
        if changed:
            await self._store.async_save(self._data)
        return changed

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

    async def async_clear_room(self, room_area_id: str) -> int:
        """Delete all samples for one room area id."""
        await self.async_ensure_loaded()
        kept = [sample for sample in self._data["samples"] if sample.get("room_area_id") != room_area_id]
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

    async def async_acknowledge_layout_hash(self, anchor_layout_hash: str) -> None:
        """Remember that a layout mismatch for this hash was intentionally acknowledged."""
        await self.async_ensure_loaded()
        if anchor_layout_hash in self.acknowledged_layout_hashes:
            return
        self._data["acknowledged_layout_hashes"] = [
            *self.acknowledged_layout_hashes,
            anchor_layout_hash,
        ]
        await self._store.async_save(self._data)

    async def async_forget_layout_hash(self, anchor_layout_hash: str) -> None:
        """Remove one acknowledged layout hash."""
        await self.async_ensure_loaded()
        kept = [
            layout_hash
            for layout_hash in self.acknowledged_layout_hashes
            if layout_hash != anchor_layout_hash
        ]
        if len(kept) == len(self.acknowledged_layout_hashes):
            return
        self._data["acknowledged_layout_hashes"] = kept
        await self._store.async_save(self._data)
