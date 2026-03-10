"""Persistent storage for Bermuda scanner anchor coordinates."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .util import mac_norm

if TYPE_CHECKING:
    from .bermuda_device import BermudaDevice

STORAGE_VERSION = 1
STORAGE_SUBDIR = "bermuda"
STORAGE_KEY = f"{STORAGE_SUBDIR}/scanner_anchors"


class BermudaScannerAnchorStore:
    """Persist scanner anchor coordinates outside entity restore state."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise store wrapper."""
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] = {"scanners": {}}
        self._loaded = False

    async def async_load(self) -> None:
        """Load stored anchor data."""
        if self._loaded:
            return
        loaded = await self._store.async_load()
        if isinstance(loaded, dict) and isinstance(loaded.get("scanners"), dict):
            self._data = loaded
        self._loaded = True

    async def async_ensure_loaded(self) -> None:
        """Load storage on first use."""
        await self.async_load()

    def _aliases_for_scanner(self, scanner: BermudaDevice) -> set[str]:
        """Return normalized identity aliases for a scanner."""
        aliases = {
            scanner.address,
            scanner.address_ble_mac,
            scanner.address_wifi_mac,
            scanner.unique_id,
        }
        return {mac_norm(alias) for alias in aliases if alias}

    def _find_storage_key(self, scanner: BermudaDevice) -> str | None:
        """Find an existing storage key matching the scanner."""
        aliases = self._aliases_for_scanner(scanner)
        for storage_key, payload in self._data["scanners"].items():
            record_aliases = {mac_norm(storage_key)}
            record_aliases.update(mac_norm(alias) for alias in payload.get("aliases", []) if alias)
            if aliases & record_aliases:
                return storage_key
        return None

    def _preferred_storage_key(self, scanner: BermudaDevice) -> str:
        """Return the preferred key for a scanner record."""
        return mac_norm(scanner.address_ble_mac or scanner.address)

    async def async_get_coordinates(self, scanner: BermudaDevice) -> dict[str, float] | None:
        """Return stored coordinates for a scanner, if present."""
        await self.async_ensure_loaded()
        return self.get_coordinates_if_loaded(scanner)

    def get_coordinates_if_loaded(self, scanner: BermudaDevice) -> dict[str, float] | None:
        """Return stored coordinates for a scanner when the store is already loaded."""
        if not self._loaded:
            return None
        if (storage_key := self._find_storage_key(scanner)) is None:
            return None
        payload = self._data["scanners"].get(storage_key, {})
        coords = payload.get("coordinates")
        if not isinstance(coords, dict):
            return None
        try:
            return {
                "anchor_x_m": float(coords["anchor_x_m"]),
                "anchor_y_m": float(coords["anchor_y_m"]),
                "anchor_z_m": float(coords["anchor_z_m"]),
            }
        except (KeyError, TypeError, ValueError):
            return None

    async def async_save_scanner(self, scanner: BermudaDevice) -> None:
        """Persist the current coordinates for a scanner."""
        await self.async_ensure_loaded()
        storage_key = self._find_storage_key(scanner) or self._preferred_storage_key(scanner)
        self._data["scanners"][storage_key] = {
            "name": scanner.name,
            "aliases": sorted(self._aliases_for_scanner(scanner)),
            "coordinates": {
                "anchor_x_m": scanner.anchor_x_m,
                "anchor_y_m": scanner.anchor_y_m,
                "anchor_z_m": scanner.anchor_z_m,
            },
        }
        await self._store.async_save(self._data)

    @property
    def scanners(self) -> dict[str, Any]:
        """Return a defensive copy of stored scanner anchor data."""
        return deepcopy(self._data["scanners"])
