"""Calibration sample recording and management helpers."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import math
import statistics
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components import persistent_notification
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.util.dt import now

from .const import (
    CALIBRATION_EVENT_SAMPLE_CAPTURED,
    CALIBRATION_QUALITY_ACCEPTED,
    CALIBRATION_QUALITY_POOR,
    CALIBRATION_QUALITY_REJECTED,
    CALIBRATION_SAMPLE_WARN_THRESHOLD,
    DEFAULT_SAMPLE_RADIUS_M,
    DEVICE_OFFSET_EVENT_CAPTURED,
    DISTANCE_TIMEOUT,
    DOMAIN_PRIVATE_BLE_DEVICE,
)
from .trilateration import (
    AnchorMeasurement,
    solve_2d_soft_l1,
    solve_3d_soft_l1,
    solve_quality_metrics_2d,
    solve_quality_metrics_3d,
)
from .util import mac_norm

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .bermuda_advert import BermudaAdvert
    from .bermuda_device import BermudaDevice
    from .calibration_store import BermudaCalibrationStore
    from .coordinator import BermudaDataUpdateCoordinator
    from .ranging_model import BermudaRangingModel


# Device-offset capture: minimum anchors that must yield a usable delta,
# minimum packets per anchor before its median is trusted, and a plausibility
# bound on the resulting offset (beyond this, something other than TX power
# difference is going on and the capture is rejected).
DEVICE_OFFSET_MIN_ANCHORS = 4
DEVICE_OFFSET_MIN_PACKETS = 3
DEVICE_OFFSET_MAX_ABS_DB = 30.0


def compute_device_offset(
    observations: list[dict[str, Any]],
    predict_rssi,
) -> dict[str, Any]:
    """
    Compute a device's global RSSI offset against the calibrated reference scale.

    observations: one entry per anchor with scanner_address, scanner_name,
    rssi_median, distance_m, and packet_count. predict_rssi(scanner_address,
    distance_m) returns the expected RSSI on the reference scale, or None.

    Returns a result dict with status "ok" or "rejected" (plus reason). The
    offset sign convention: positive means the device reads hotter than the
    reference device, so live readings should have the offset subtracted.
    """
    deltas: list[float] = []
    anchor_rows: list[dict[str, Any]] = []
    for observation in observations:
        if int(observation.get("packet_count") or 0) < DEVICE_OFFSET_MIN_PACKETS:
            continue
        expected = predict_rssi(observation["scanner_address"], float(observation["distance_m"]))
        if expected is None:
            continue
        delta = float(observation["rssi_median"]) - float(expected)
        deltas.append(delta)
        anchor_rows.append(
            {
                "scanner_address": observation["scanner_address"],
                "scanner_name": observation.get("scanner_name") or observation["scanner_address"],
                "distance_m": round(float(observation["distance_m"]), 3),
                "rssi_median": round(float(observation["rssi_median"]), 3),
                "expected_rssi": round(float(expected), 3),
                "delta_db": round(delta, 3),
                "packet_count": int(observation.get("packet_count") or 0),
            }
        )

    if len(deltas) < DEVICE_OFFSET_MIN_ANCHORS:
        return {
            "status": "rejected",
            "reason": f"insufficient_anchors ({len(deltas)}/{DEVICE_OFFSET_MIN_ANCHORS})",
            "anchor_count": len(deltas),
            "anchors": anchor_rows,
        }

    offset_db = float(statistics.median(deltas))
    if abs(offset_db) > DEVICE_OFFSET_MAX_ABS_DB:
        return {
            "status": "rejected",
            "reason": f"implausible_offset ({offset_db:+.1f} dB)",
            "offset_db": round(offset_db, 3),
            "anchor_count": len(deltas),
            "anchors": anchor_rows,
        }

    spread_db = float(statistics.median(abs(delta - offset_db) for delta in deltas)) * 1.4826
    return {
        "status": "ok",
        "reason": None,
        "offset_db": round(offset_db, 3),
        "spread_db": round(spread_db, 3),
        "anchor_count": len(deltas),
        "anchors": anchor_rows,
    }


@dataclass
class _AnchorObservationAccumulator:
    """Aggregate observations for one anchor during a capture session."""

    scanner_address: str
    scanner_name: str
    anchor_position: dict[str, float | None]
    values: list[float] = field(default_factory=list)
    first_seen_at: str | None = None
    last_seen_at: str | None = None


@dataclass
class _CaptureSession:
    """Active calibration or transition capture session."""

    session_type: str
    session_id: str
    started_at: str
    started_monotonic: float
    duration_s: int
    device_id: str
    device_name: str
    device_address: str
    room_area_id: str
    room_name: str
    room_floor_id: str | None
    position: dict[str, float]
    sample_radius_m: float
    notes: str | None = None
    transition_name: str | None = None
    transition_floor_ids: list[str] = field(default_factory=list)
    anchors: dict[str, _AnchorObservationAccumulator] = field(default_factory=dict)
    trilat_x_values: list[float] = field(default_factory=list)
    trilat_y_values: list[float] = field(default_factory=list)
    trilat_z_values: list[float] = field(default_factory=list)
    trilat_residual_values: list[float] = field(default_factory=list)
    trilat_geometry_quality_values: list[float] = field(default_factory=list)
    trilat_tracking_confidence_values: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class _TrilatCorrectionSample:
    """One calibration-derived local XY correction reference."""

    layout_hash: str
    floor_id: str | None
    room_area_id: str
    x_m: float
    y_m: float
    z_m: float
    sample_radius_m: float
    bias_x_m: float
    bias_y_m: float
    half_width_x_m: float
    half_width_y_m: float
    reference_residual_m: float | None
    quality_weight: float
    source: str


@dataclass(frozen=True)
class TrilatPositionAdjustment:
    """Local XY correction and band estimate for a live trilat point."""

    correction_x_m: float
    correction_y_m: float
    uncertainty_x_band_m: float | None
    uncertainty_y_band_m: float | None
    source: str
    sample_count: int
    reference_residual_m: float | None = None


class BermudaCalibrationManager:
    """Own sample recording, storage, and summary logic."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: BermudaDataUpdateCoordinator,
        store: BermudaCalibrationStore,
    ) -> None:
        """Initialise the calibration manager."""
        self.hass = hass
        self._coordinator = coordinator
        self._store = store
        self._sessions: dict[str, _CaptureSession] = {}
        self._session_tasks: dict[str, asyncio.Task[None]] = {}
        self._change_callbacks: list = []
        self._trilat_correction_samples: dict[str, list[_TrilatCorrectionSample]] = {}
        # alias (normalized) -> canonical physical scanner identity. The canonical
        # identity is the scanner-anchor storage key, which survives BLE/Wi-Fi MAC
        # alias flips. Rebuilt via refresh_layout_identity() when scanners or
        # geometry change.
        self._scanner_identity_map: dict[str, str] = {}
        self._equivalent_layout_hashes_cache: set[str] | None = None

    async def async_initialize(self) -> None:
        """Load persistent calibration data."""
        await self._store.async_ensure_loaded()

    async def async_shutdown(self) -> None:
        """Cancel active recording tasks and emit failure completion events."""
        tasks = list(self._session_tasks.values())
        self._session_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        session_ids = list(self._sessions)
        for session_id in session_ids:
            await self._async_finalize_session(session_id, failure_reason="integration_unloaded")

    @property
    def sample_count(self) -> int:
        """Return total sample count."""
        return self._store.sample_count

    @property
    def current_anchor_layout_hash(self) -> str:
        """Return the current anchor layout hash."""
        return self.compute_anchor_layout_hash()

    @property
    def acknowledged_layout_hashes(self) -> set[str]:
        """Return acknowledged current-layout hashes."""
        return set(self._store.acknowledged_layout_hashes)

    def samples(self) -> list[dict[str, Any]]:
        """Return stored samples."""
        return self._store.samples

    def transition_samples(self) -> list[dict[str, Any]]:
        """Return stored transition samples."""
        return self._store.transition_samples

    def refresh_layout_identity(self) -> None:
        """Rebuild the canonical scanner identity map and invalidate layout caches."""
        alias_to_canonical: dict[str, str] = {}
        # Stored anchor records first: the storage key is the long-lived canonical
        # identity for a physical scanner, regardless of which alias is live now.
        for storage_key, payload in self._coordinator.scanner_anchor_store.scanners.items():
            canonical = mac_norm(str(storage_key))
            alias_to_canonical[canonical] = canonical
            for alias in payload.get("aliases", []) or []:
                if alias:
                    alias_to_canonical[mac_norm(str(alias))] = canonical
        # Live scanners without a stored record yet: group their own aliases under
        # one identity so a later BLE/Wi-Fi flip still resolves consistently.
        for scanner_address in sorted(self._coordinator.scanner_list):
            scanner = self._coordinator.devices.get(scanner_address)
            if scanner is None:
                continue
            aliases = {
                mac_norm(str(alias))
                for alias in (
                    scanner.address,
                    getattr(scanner, "address_ble_mac", None),
                    getattr(scanner, "address_wifi_mac", None),
                    getattr(scanner, "unique_id", None),
                )
                if alias
            }
            known = sorted({alias_to_canonical[alias] for alias in aliases if alias in alias_to_canonical})
            canonical = (
                known[0] if known else mac_norm(str(getattr(scanner, "address_ble_mac", None) or scanner.address))
            )
            for alias in aliases:
                alias_to_canonical.setdefault(alias, canonical)
        self._scanner_identity_map = alias_to_canonical
        self._equivalent_layout_hashes_cache = None

    def canonical_scanner_key(self, scanner_address: str) -> str:
        """Return the canonical physical identity for any scanner alias."""
        if not self._scanner_identity_map:
            self.refresh_layout_identity()
        key = mac_norm(str(scanner_address))
        return self._scanner_identity_map.get(key, key)

    def misconfigured_anchor_scanners(self) -> list[str]:
        """
        Return scanners whose anchor coordinates are not fully configured.

        Captures snapshot every visible scanner's anchor position into the stored
        sample, so any scanner with a missing x, y, or z would write unprovable
        geometry. Capture sessions refuse to start while this list is non-empty.
        """
        problems: list[str] = []
        for scanner_address in sorted(self._coordinator.scanner_list):
            scanner = self._coordinator.devices.get(scanner_address)
            if scanner is None:
                continue
            missing = [
                axis
                for axis, value in (
                    ("x", getattr(scanner, "anchor_x_m", None)),
                    ("y", getattr(scanner, "anchor_y_m", None)),
                    ("z", getattr(scanner, "anchor_z_m", None)),
                )
                if value is None
            ]
            if missing:
                name = str(getattr(scanner, "name", None) or scanner_address)
                problems.append(f"{name} (missing {'/'.join(missing)})")
        return problems

    def _raise_if_anchors_misconfigured(self) -> None:
        """Refuse to start a capture while any scanner anchor is misconfigured."""
        misconfigured = self.misconfigured_anchor_scanners()
        if misconfigured:
            raise HomeAssistantError(
                "Cannot start capture: anchor coordinates are not fully configured for: "
                + "; ".join(misconfigured)
                + ". Configure or remove these scanners before recording samples."
            )

    def incomplete_current_anchor_scanners(self) -> list[str]:
        """Return names of configured anchor scanners with partial x/y/z coordinates."""
        incomplete: list[str] = []
        for scanner_address in sorted(self._coordinator.scanner_list):
            scanner = self._coordinator.devices.get(scanner_address)
            if scanner is None:
                continue
            anchor_x = getattr(scanner, "anchor_x_m", None)
            anchor_y = getattr(scanner, "anchor_y_m", None)
            anchor_z = getattr(scanner, "anchor_z_m", None)
            coords = (anchor_x, anchor_y, anchor_z)
            if any(value is None for value in coords) and any(value is not None for value in coords):
                incomplete.append(str(getattr(scanner, "name", None) or scanner_address))
        return incomplete

    def equivalent_layout_hashes(self) -> set[str]:
        """
        Return stored layout hashes proven geometry-equivalent to the current layout.

        Always contains the current layout hash. A stored hash is equivalent when
        every sample carrying it resolves cleanly onto the current anchor geometry,
        so a hash mismatch alone never invalidates matching samples.
        """
        if self._equivalent_layout_hashes_cache is not None:
            return set(self._equivalent_layout_hashes_cache)
        current_layout_hash = self.current_anchor_layout_hash
        equivalent = {current_layout_hash} if current_layout_hash else set()
        current_anchor_index = self._current_anchor_identity_index()
        if current_anchor_index:
            matched: set[str] = set()
            mismatched: set[str] = set()
            for sample in [*self.samples(), *self.transition_samples()]:
                stored_hash = str(sample.get("anchor_layout_hash") or "")
                if not stored_hash or stored_hash == current_layout_hash:
                    continue
                if self._sample_matches_current_geometry(sample, current_anchor_index):
                    matched.add(stored_hash)
                else:
                    mismatched.add(stored_hash)
            equivalent.update(matched - mismatched)
        self._equivalent_layout_hashes_cache = set(equivalent)
        return equivalent

    @staticmethod
    def _anchor_delta_m(
        current_anchor: dict[str, float | None],
        sample_position: dict[str, Any],
    ) -> float | None:
        """Return Euclidean delta between current and stored anchor positions."""
        sample_x = sample_position.get("x_m")
        sample_y = sample_position.get("y_m")
        sample_z = sample_position.get("z_m")
        if sample_x is None or sample_y is None or sample_z is None:
            return None
        current_x = current_anchor.get("x_m")
        current_y = current_anchor.get("y_m")
        current_z = current_anchor.get("z_m")
        if current_x is None or current_y is None or current_z is None:
            return None
        return math.sqrt(
            ((float(current_x) - float(sample_x)) ** 2)
            + ((float(current_y) - float(sample_y)) ** 2)
            + ((float(current_z) - float(sample_z)) ** 2)
        )

    @staticmethod
    def _anchor_delta_xy_m(
        current_anchor: dict[str, float | None],
        sample_position: dict[str, Any],
    ) -> float | None:
        """Return XY-plane delta between current and stored anchor positions."""
        sample_x = sample_position.get("x_m")
        sample_y = sample_position.get("y_m")
        current_x = current_anchor.get("x_m")
        current_y = current_anchor.get("y_m")
        if sample_x is None or sample_y is None or current_x is None or current_y is None:
            return None
        return math.hypot(float(current_x) - float(sample_x), float(current_y) - float(sample_y))

    def _sample_matches_current_geometry(
        self,
        sample: dict[str, Any],
        current_anchor_index: dict[str, tuple[dict[str, float | None], str | None]],
    ) -> bool:
        """Return true when a stored sample resolves cleanly to the current anchor geometry."""
        anchors = sample.get("anchors") or {}
        if not anchors:
            return False

        matched_anchor_count = 0
        for scanner_address, anchor in anchors.items():
            resolved = current_anchor_index.get(mac_norm(str(scanner_address)))
            if resolved is None:
                return False

            current_anchor, _current_name = resolved
            sample_position = anchor.get("anchor_position") or {}
            delta_m = self._anchor_delta_m(current_anchor, sample_position)
            if delta_m is None or delta_m >= 0.01:
                return False
            matched_anchor_count += 1

        return matched_anchor_count > 0

    # Sample classifications against the current anchor layout.
    SAMPLE_CLASS_CURRENT = "current"
    SAMPLE_CLASS_HASH_ONLY = "hash_only_alias_churn"
    SAMPLE_CLASS_COORDINATE_CORRECTION = "coordinate_correction"
    SAMPLE_CLASS_PHYSICAL_CHANGE = "physical_layout_changed"
    SAMPLE_CLASS_CORRUPTED = "corrupted_stored_geometry"
    SAMPLE_CLASS_REPAIRABLE_NULL_Z = "repairable_null_z"

    def classify_sample_against_current(
        self,
        sample: dict[str, Any],
        current_anchor_index: dict[str, tuple[dict[str, float | None], str | None]],
        *,
        current_layout_hash: str | None = None,
    ) -> str:
        """
        Classify one stored sample against the current anchor layout.

        - current: geometry and stored hash both match.
        - hash_only_alias_churn: geometry matches, only the stored hash is stale.
        - coordinate_correction: same scanners, but stored coordinates differ.
        - physical_layout_changed: sample references scanners not in the current set.
        - corrupted_stored_geometry: stored anchor positions have null components
          that cannot be proven against current anchors.
        - repairable_null_z: stored z is null, but x/y prove the anchor identity and
          the current anchor has a known z — safe to backfill.
        """
        if current_layout_hash is None:
            current_layout_hash = self.current_anchor_layout_hash
        anchors = sample.get("anchors") or {}
        if not anchors:
            return self.SAMPLE_CLASS_CORRUPTED

        unknown_count = 0
        corrupted_count = 0
        repairable_z_count = 0
        moved_count = 0
        matched_count = 0
        for scanner_address, anchor in anchors.items():
            resolved = current_anchor_index.get(mac_norm(str(scanner_address)))
            if resolved is None:
                unknown_count += 1
                continue
            current_anchor, _name = resolved
            sample_position = anchor.get("anchor_position") or {}
            sample_x = sample_position.get("x_m")
            sample_y = sample_position.get("y_m")
            sample_z = sample_position.get("z_m")
            if sample_x is None or sample_y is None:
                corrupted_count += 1
                continue
            if sample_z is None:
                delta_xy = self._anchor_delta_xy_m(current_anchor, sample_position)
                if delta_xy is not None and delta_xy < 0.01 and current_anchor.get("z_m") is not None:
                    repairable_z_count += 1
                else:
                    corrupted_count += 1
                continue
            delta_m = self._anchor_delta_m(current_anchor, sample_position)
            if delta_m is None:
                # Current anchor is missing a coordinate; cannot prove equivalence.
                corrupted_count += 1
            elif delta_m < 0.01:
                matched_count += 1
            else:
                moved_count += 1

        if unknown_count:
            return self.SAMPLE_CLASS_PHYSICAL_CHANGE
        if corrupted_count:
            return self.SAMPLE_CLASS_CORRUPTED
        if moved_count:
            return self.SAMPLE_CLASS_COORDINATE_CORRECTION
        if repairable_z_count:
            return self.SAMPLE_CLASS_REPAIRABLE_NULL_Z
        if matched_count == 0:
            return self.SAMPLE_CLASS_CORRUPTED
        stored_hash = str(sample.get("anchor_layout_hash") or "")
        if stored_hash and current_layout_hash and stored_hash != current_layout_hash:
            return self.SAMPLE_CLASS_HASH_ONLY
        return self.SAMPLE_CLASS_CURRENT

    def classify_stored_samples(self) -> dict[str, Any]:
        """Classify all stored samples and bootstrap records for diagnostics and repair."""
        current_anchor_index = self._current_anchor_identity_index()
        current_layout_hash = self.current_anchor_layout_hash
        equivalent_hashes = self.equivalent_layout_hashes()
        incomplete_scanners = self.incomplete_current_anchor_scanners()

        def _bucket(samples: list[dict[str, Any]]) -> dict[str, int]:
            counts: dict[str, int] = {}
            for sample in samples:
                classification = self.classify_sample_against_current(
                    sample,
                    current_anchor_index,
                    current_layout_hash=current_layout_hash,
                )
                counts[classification] = counts.get(classification, 0) + 1
            return counts

        result: dict[str, Any] = {
            "current_layout_hash": current_layout_hash,
            "equivalent_layout_hashes": sorted(equivalent_hashes),
            "current_geometry_complete": not incomplete_scanners,
            "incomplete_anchor_scanners": incomplete_scanners,
            "samples": _bucket(self.samples()),
            "transition_samples": _bucket(self.transition_samples()),
        }

        bootstrap_store = getattr(self._coordinator, "trilat_bootstrap_store", None)
        if bootstrap_store is not None:
            records = getattr(bootstrap_store, "records", {})
            bootstrap: dict[str, int] = {"current": 0, "stale_layout": 0}
            for record in records.values():
                record_hash = getattr(record, "layout_hash", "")
                if not record_hash or record_hash in equivalent_hashes:
                    bootstrap["current"] += 1
                else:
                    bootstrap["stale_layout"] += 1
            result["bootstrap_records"] = bootstrap
        return result

    def get_summary(self) -> dict[str, Any]:
        """Return a small in-memory summary for config flow."""
        samples = self.samples()
        by_room: dict[str, int] = {}
        by_device: dict[str, int] = {}
        by_quality: dict[str, int] = {}
        current_layout_hash = self.current_anchor_layout_hash
        current_anchor_index = self._current_anchor_identity_index()
        current_layout_count = 0
        for sample in samples:
            room_name = str(sample.get("room_name") or sample.get("room_area_id") or "Unknown")
            by_room[room_name] = by_room.get(room_name, 0) + 1
            device_name = str(sample.get("device_name") or sample.get("device_id") or "Unknown")
            by_device[device_name] = by_device.get(device_name, 0) + 1
            quality_level = self._sample_quality_level(sample)
            by_quality[quality_level] = by_quality.get(quality_level, 0) + 1
            if self._sample_matches_current_geometry(sample, current_anchor_index):
                current_layout_count += 1
        recent = sorted(samples, key=lambda sample: sample.get("created_at", ""), reverse=True)[:5]
        return {
            "sample_count": len(samples),
            "by_room": by_room,
            "by_device": by_device,
            "by_quality": by_quality,
            "current_layout_hash": current_layout_hash,
            "current_layout_count": current_layout_count,
            "recent": recent,
            "warn_threshold": CALIBRATION_SAMPLE_WARN_THRESHOLD,
        }

    def runtime_layout_hash_for_sample(
        self,
        sample: dict[str, Any],
        *,
        current_anchor_index: dict[str, tuple[dict[str, float | None], str | None]] | None = None,
        current_layout_hash: str | None = None,
    ) -> str:
        """
        Return the effective runtime layout hash for one sample.

        Samples that already match the current anchor geometry should participate
        in the current runtime model/classifier even if their stored hash is
        older. This keeps runtime behavior aligned with repair/mismatch logic.
        """
        stored_layout_hash = str(sample.get("anchor_layout_hash") or "")
        if current_layout_hash is None:
            current_layout_hash = self.current_anchor_layout_hash
        if current_anchor_index is None:
            current_anchor_index = self._current_anchor_identity_index()
        if (
            stored_layout_hash
            and current_layout_hash
            and current_anchor_index
            and self._sample_matches_current_geometry(sample, current_anchor_index)
        ):
            return current_layout_hash
        return stored_layout_hash

    def get_transition_summary(self) -> dict[str, Any]:
        """Return a small in-memory summary for stored transition samples."""
        samples = self.transition_samples()
        by_room: dict[str, int] = {}
        by_name: dict[str, int] = {}
        by_floor: dict[str, int] = {}
        by_layout: dict[str, int] = {}
        current_layout_hash = self.current_anchor_layout_hash
        equivalent_hashes = self.equivalent_layout_hashes()
        current_layout_count = 0
        for sample in samples:
            room_name = str(sample.get("room_name") or sample.get("room_area_id") or "Unknown")
            by_room[room_name] = by_room.get(room_name, 0) + 1
            transition_name = str(sample.get("transition_name") or "Unknown")
            by_name[transition_name] = by_name.get(transition_name, 0) + 1
            for floor_id in sample.get("transition_floor_ids") or []:
                floor = self._coordinator.fr.async_get_floor(str(floor_id))
                floor_name = floor.name if floor is not None else str(floor_id)
                by_floor[floor_name] = by_floor.get(floor_name, 0) + 1
            layout_hash = str(sample.get("anchor_layout_hash") or "unknown")
            by_layout[layout_hash] = by_layout.get(layout_hash, 0) + 1
            if layout_hash in equivalent_hashes:
                current_layout_count += 1
        recent = sorted(
            samples,
            key=lambda sample: str(sample.get("updated_at") or sample.get("created_at") or ""),
            reverse=True,
        )[:5]
        return {
            "transition_sample_count": len(samples),
            "by_room": by_room,
            "by_name": by_name,
            "by_floor": by_floor,
            "by_layout": by_layout,
            "current_layout_hash": current_layout_hash,
            "current_layout_count": current_layout_count,
            "recent": recent,
        }

    def current_anchor_geometry(self) -> dict[str, dict[str, float | None]]:
        """
        Return the configured anchor geometry by canonical scanner identity.

        Keys are canonical physical scanner identities so the derived layout hash
        is stable across BLE/Wi-Fi MAC alias flips. An unconfigured Z stays None;
        it must never be silently coerced to 0.0.

        The geometry is seeded from the anchor store, so a temporarily offline
        scanner (proxy reboot, backend init failure) keeps its place in the
        layout. Deriving the layout from live scanners only meant one missing
        proxy shifted the layout hash and orphaned every calibration sample that
        referenced it. Live scanner values overlay stored ones so in-progress
        coordinate edits take effect immediately.
        """
        geometry: dict[str, dict[str, float | None]] = {}
        anchor_store = getattr(self._coordinator, "scanner_anchor_store", None)
        stored_scanners = anchor_store.scanners if anchor_store is not None else {}
        for storage_key, payload in stored_scanners.items():
            coords = payload.get("coordinates") or {}
            anchor_x = coords.get("anchor_x_m")
            anchor_y = coords.get("anchor_y_m")
            if anchor_x is None or anchor_y is None:
                continue
            anchor_z = coords.get("anchor_z_m")
            geometry[self.canonical_scanner_key(storage_key)] = {
                "x_m": float(anchor_x),
                "y_m": float(anchor_y),
                "z_m": float(anchor_z) if anchor_z is not None else None,
            }
        for scanner_address in sorted(self._coordinator.scanner_list):
            scanner = self._coordinator.devices.get(scanner_address)
            if scanner is None:
                continue
            anchor_x = getattr(scanner, "anchor_x_m", None)
            anchor_y = getattr(scanner, "anchor_y_m", None)
            if anchor_x is None or anchor_y is None:
                continue
            anchor_z = getattr(scanner, "anchor_z_m", None)
            geometry[self.canonical_scanner_key(scanner_address)] = {
                "x_m": float(anchor_x),
                "y_m": float(anchor_y),
                "z_m": float(anchor_z) if anchor_z is not None else None,
            }
        return geometry

    def _current_anchor_identity_index(
        self,
    ) -> dict[str, tuple[dict[str, float | None], str | None]]:
        """
        Return the configured anchor geometry keyed by every known scanner identity alias.

        Seeded from the anchor store so samples referencing a temporarily
        offline scanner still resolve against its configured coordinates
        instead of being misclassified as a physical layout change. Live
        scanner values overlay stored ones.
        """
        index: dict[str, tuple[dict[str, float | None], str | None]] = {}
        stored_scanners = self._coordinator.scanner_anchor_store.scanners

        for storage_key, payload in stored_scanners.items():
            coords = payload.get("coordinates") or {}
            anchor_x = coords.get("anchor_x_m")
            anchor_y = coords.get("anchor_y_m")
            if anchor_x is None or anchor_y is None:
                continue
            anchor_z = coords.get("anchor_z_m")
            stored_anchor: dict[str, float | None] = {
                "x_m": float(anchor_x),
                "y_m": float(anchor_y),
                "z_m": float(anchor_z) if anchor_z is not None else None,
            }
            record_aliases = {mac_norm(str(storage_key))}
            record_aliases.update(mac_norm(str(alias)) for alias in payload.get("aliases", []) if alias)
            for alias in record_aliases:
                index[alias] = (stored_anchor, payload.get("name"))

        for scanner_address in sorted(self._coordinator.scanner_list):
            scanner = self._coordinator.devices.get(scanner_address)
            if scanner is None:
                continue

            anchor_x = getattr(scanner, "anchor_x_m", None)
            anchor_y = getattr(scanner, "anchor_y_m", None)
            if anchor_x is None or anchor_y is None:
                continue

            anchor_z = getattr(scanner, "anchor_z_m", None)
            current_anchor = {
                "x_m": float(anchor_x),
                "y_m": float(anchor_y),
                "z_m": float(anchor_z) if anchor_z is not None else None,
            }
            aliases = {
                mac_norm(alias)
                for alias in (
                    scanner.address,
                    scanner.address_ble_mac,
                    scanner.address_wifi_mac,
                    scanner.unique_id,
                )
                if alias
            }
            for storage_key, payload in stored_scanners.items():
                record_aliases = {mac_norm(storage_key)}
                record_aliases.update(mac_norm(alias) for alias in payload.get("aliases", []) if alias)
                if aliases & record_aliases:
                    aliases.update(record_aliases)
                    break
            for alias in aliases:
                index[alias] = (current_anchor, scanner.name)

        return index

    def _layout_changed_anchor_lines(
        self,
        layout_samples: list[dict[str, Any]],
        current_anchor_index: dict[str, tuple[dict[str, float | None], str | None]],
    ) -> list[str]:
        """Return human-readable anchor deltas for one saved layout."""
        changed_anchors: dict[str, float] = {}
        missing_anchors: set[str] = set()

        for sample in layout_samples:
            for scanner_address, anchor in (sample.get("anchors") or {}).items():
                label = str(anchor.get("scanner_name") or scanner_address)
                resolved = current_anchor_index.get(mac_norm(str(scanner_address)))
                if resolved is None:
                    missing_anchors.add(label)
                    continue

                current_anchor, current_name = resolved
                sample_position = anchor.get("anchor_position") or {}
                delta_m = self._anchor_delta_m(current_anchor, sample_position)
                if delta_m is None:
                    continue
                if delta_m < 0.01:
                    continue

                label = str(anchor.get("scanner_name") or current_name or scanner_address)
                changed_anchors[label] = max(changed_anchors.get(label, 0.0), delta_m)

        changed_lines = [f"- {label}: moved {delta_m:.2f} m" for label, delta_m in sorted(changed_anchors.items())]
        missing_lines = [f"- {label}: no longer present in current anchor set" for label in sorted(missing_anchors)]
        return [*changed_lines, *missing_lines]

    def get_layout_mismatch_summary(self) -> dict[str, Any] | None:
        """
        Describe a coordinate-correction mismatch that is eligible for repair.

        The repair is only offered when the stored samples provably reference the
        same physical scanners as the current layout and only the configured
        coordinates differ. Hash-only alias churn, unknown scanners, corrupted
        stored geometry, and incomplete current geometry never raise the repair.
        """
        samples = self.samples()
        if not samples:
            return None

        current_anchor_index = self._current_anchor_identity_index()
        if not current_anchor_index:
            return None

        current_layout_hash = self.current_anchor_layout_hash
        if current_layout_hash in self.acknowledged_layout_hashes:
            return None

        if self.incomplete_current_anchor_scanners():
            # Current geometry cannot be proven complete; never offer to rewrite
            # stored samples against it.
            return None

        matched_samples: list[dict[str, Any]] = []
        correction_samples: list[dict[str, Any]] = []
        blocker_classes: dict[str, int] = {}
        for sample in samples:
            classification = self.classify_sample_against_current(
                sample,
                current_anchor_index,
                current_layout_hash=current_layout_hash,
            )
            if classification in {self.SAMPLE_CLASS_CURRENT, self.SAMPLE_CLASS_HASH_ONLY}:
                matched_samples.append(sample)
            elif classification == self.SAMPLE_CLASS_COORDINATE_CORRECTION:
                correction_samples.append(sample)
            else:
                blocker_classes[classification] = blocker_classes.get(classification, 0) + 1

        # Transition samples are mutated by the repair too, so unresolvable
        # scanners there block the offer just as hard.
        for sample in self.transition_samples():
            classification = self.classify_sample_against_current(
                sample,
                current_anchor_index,
                current_layout_hash=current_layout_hash,
            )
            if classification == self.SAMPLE_CLASS_PHYSICAL_CHANGE:
                blocker_classes[classification] = blocker_classes.get(classification, 0) + 1

        if not correction_samples or blocker_classes:
            return None

        by_layout: dict[str, list[dict[str, Any]]] = {}
        for sample in correction_samples:
            layout_hash = str(sample.get("anchor_layout_hash") or "unknown")
            by_layout.setdefault(layout_hash, []).append(sample)

        dominant_layout_hash, layout_samples = max(by_layout.items(), key=lambda row: len(row[1]))
        changed_anchor_lines = self._layout_changed_anchor_lines(layout_samples, current_anchor_index)
        if not changed_anchor_lines:
            changed_anchor_lines = [
                "- Some saved sample anchors no longer resolve cleanly against the current anchor set"
            ]

        return {
            "sample_count": len(correction_samples),
            "total_sample_count": len(samples),
            "current_layout_count": len(matched_samples),
            "mismatched_sample_count": len(correction_samples),
            "mismatched_layout_count": len(by_layout),
            "current_layout_hash": current_layout_hash,
            "dominant_layout_hash": dominant_layout_hash,
            "dominant_layout_count": len(layout_samples),
            "changed_anchor_lines": "\n".join(changed_anchor_lines[:8]),
        }

    def get_device_samples(self) -> dict[str, dict[str, str]]:
        """Return a map of device ids present in storage."""
        device_map: dict[str, dict[str, str]] = {}
        for sample in self.samples():
            device_id = sample.get("device_id")
            if not device_id or device_id in device_map:
                continue
            device_map[device_id] = {
                "name": str(sample.get("device_name") or device_id),
                "address": str(sample.get("device_address") or ""),
            }
        return device_map

    def get_room_samples(self) -> dict[str, dict[str, str | int]]:
        """Return a map of room area ids present in storage."""
        room_map: dict[str, dict[str, str | int]] = {}
        for sample in self.samples():
            room_area_id = sample.get("room_area_id")
            if not room_area_id:
                continue
            details = room_map.setdefault(
                room_area_id,
                {
                    "name": str(sample.get("room_name") or room_area_id),
                    "count": 0,
                },
            )
            details["count"] = int(details["count"]) + 1
        return room_map

    def rebuild_trilat_position_model(self, ranging_model: BermudaRangingModel) -> None:
        """Rebuild cached local XY correction samples from stored calibration captures."""
        grouped: dict[str, list[_TrilatCorrectionSample]] = {}
        for sample in self.samples():
            if sample.get("quality", {}).get("status") == CALIBRATION_QUALITY_REJECTED:
                continue
            built = self._build_trilat_correction_sample(sample, ranging_model)
            if built is None:
                continue
            grouped.setdefault(built.layout_hash, []).append(built)
        self._trilat_correction_samples = grouped

    def trilat_position_adjustment(
        self,
        *,
        layout_hash: str,
        floor_id: str | None,
        x_m: float,
        y_m: float,
        residual_m: float | None,
    ) -> TrilatPositionAdjustment | None:
        """Return weighted local XY correction and empirical uncertainty bands."""
        candidates = [
            sample for sample in self._trilat_correction_samples.get(layout_hash, []) if sample.floor_id == floor_id
        ]
        if not candidates:
            return None

        total_weight = 0.0
        weighted_bias_x = 0.0
        weighted_bias_y = 0.0
        weighted_half_width_x = 0.0
        weighted_half_width_y = 0.0
        weighted_residual_ref = 0.0
        residual_ref_weight = 0.0
        capture_count = 0
        bootstrap_count = 0
        used_count = 0
        for sample in candidates:
            distance_xy = math.hypot(x_m - sample.x_m, y_m - sample.y_m)
            support_sigma_m = max(sample.sample_radius_m * 1.75, 0.75)
            if distance_xy > (support_sigma_m * 3.0):
                continue
            weight = sample.quality_weight * math.exp(-0.5 * ((distance_xy / support_sigma_m) ** 2))
            if weight <= 1e-6:
                continue
            total_weight += weight
            weighted_bias_x += weight * sample.bias_x_m
            weighted_bias_y += weight * sample.bias_y_m
            weighted_half_width_x += weight * sample.half_width_x_m
            weighted_half_width_y += weight * sample.half_width_y_m
            if sample.reference_residual_m is not None:
                weighted_residual_ref += weight * sample.reference_residual_m
                residual_ref_weight += weight
            if sample.source == "capture":
                capture_count += 1
            else:
                bootstrap_count += 1
            used_count += 1

        if total_weight <= 0.0 or used_count <= 0:
            return None

        correction_x_m = weighted_bias_x / total_weight
        correction_y_m = weighted_bias_y / total_weight
        half_width_x_m = weighted_half_width_x / total_weight
        half_width_y_m = weighted_half_width_y / total_weight
        reference_residual_m = weighted_residual_ref / residual_ref_weight if residual_ref_weight > 0.0 else None
        residual_factor = 1.0
        if residual_m is not None and reference_residual_m is not None and reference_residual_m > 0.0:
            residual_factor = max(1.0, float(residual_m) / reference_residual_m)

        if capture_count and bootstrap_count:
            source = "mixed"
        elif capture_count:
            source = "capture"
        else:
            source = "bootstrap"

        return TrilatPositionAdjustment(
            correction_x_m=correction_x_m,
            correction_y_m=correction_y_m,
            uncertainty_x_band_m=max(0.2, 2.0 * half_width_x_m * residual_factor),
            uncertainty_y_band_m=max(0.2, 2.0 * half_width_y_m * residual_factor),
            source=source,
            sample_count=used_count,
            reference_residual_m=reference_residual_m,
        )

    @staticmethod
    def _sample_quality_level(sample: dict[str, Any]) -> str:
        """Return the persisted quality level or derive a fallback."""
        quality = sample.get("quality") or {}
        if isinstance(quality, dict):
            if level := quality.get("level"):
                return str(level)
            status = str(quality.get("status") or "")
            if status == CALIBRATION_QUALITY_REJECTED:
                return "rejected"
            if status == CALIBRATION_QUALITY_POOR:
                return "low"
        return "medium"

    def register_change_callback(self, callback) -> None:
        """Register a callback fired after stored samples change."""
        self._change_callbacks.append(callback)

    async def async_delete_sample(self, sample_id: str) -> bool:
        """Delete one persisted sample."""
        deleted = await self._store.async_delete_sample(sample_id)
        if deleted:
            await self._async_notify_changed()
        return deleted

    async def async_clear_all(self) -> int:
        """Delete all persisted samples."""
        removed = await self._store.async_clear_all()
        if removed:
            await self._async_notify_changed()
        return removed

    async def async_clear_device(self, device_id: str) -> int:
        """Delete all samples for one device."""
        removed = await self._store.async_clear_device(device_id)
        if removed:
            await self._async_notify_changed()
        return removed

    async def async_clear_room(self, room_area_id: str) -> int:
        """Delete all samples for one room."""
        removed = await self._store.async_clear_room(room_area_id)
        if removed:
            await self._async_notify_changed()
        return removed

    async def async_clear_current_anchor_layout(self) -> int:
        """Delete samples that match the current anchor layout."""
        removed = await self._store.async_clear_anchor_layout(self.current_anchor_layout_hash)
        if removed:
            await self._async_notify_changed()
        return removed

    async def async_delete_transition_sample(self, sample_id: str) -> bool:
        """Delete one persisted transition sample."""
        return await self._store.async_delete_transition_sample(sample_id)

    async def async_record_transition_sample(
        self,
        *,
        device_id: str,
        room_area_id: str,
        transition_name: str,
        x_m: float,
        y_m: float,
        z_m: float,
        sample_radius_m: float = DEFAULT_SAMPLE_RADIUS_M,
        capture_duration_s: int = 60,
        transition_floor_ids: list[str],
    ) -> dict[str, Any]:
        """Backward-compatible wrapper for transition-session capture."""
        return await self.async_start_transition_session(
            device_id=device_id,
            room_area_id=room_area_id,
            transition_name=transition_name,
            x_m=x_m,
            y_m=y_m,
            z_m=z_m,
            sample_radius_m=sample_radius_m,
            capture_duration_s=capture_duration_s,
            transition_floor_ids=transition_floor_ids,
        )

    async def async_start_session(
        self,
        *,
        device_id: str,
        room_area_id: str,
        x_m: float,
        y_m: float,
        z_m: float,
        sample_radius_m: float = DEFAULT_SAMPLE_RADIUS_M,
        duration_s: int = 60,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Validate and start a calibration sample capture."""
        await self._store.async_ensure_loaded()
        if duration_s < 1:
            msg = "Calibration duration must be at least 1 second."
            raise HomeAssistantError(msg)
        if sample_radius_m <= 0:
            msg = "Calibration sample radius must be greater than 0 metres."
            raise HomeAssistantError(msg)
        self._raise_if_anchors_misconfigured()

        device = self._resolve_device_from_registry_id(device_id)
        if device is None:
            msg = "Selected device is not currently available in Bermuda."
            raise HomeAssistantError(msg)

        if any(session.device_id == device_id for session in self._sessions.values()):
            msg = "A capture session is already running for that device."
            raise HomeAssistantError(msg)

        area = self._coordinator.ar.async_get_area(room_area_id)
        if area is None:
            msg = "Selected room area does not exist."
            raise HomeAssistantError(msg)

        started_dt = now()
        session = _CaptureSession(
            session_type="calibration",
            session_id=f"cal_{uuid4().hex[:12]}",
            started_at=started_dt.isoformat(),
            started_monotonic=monotonic_time_coarse(),
            duration_s=duration_s,
            device_id=device_id,
            device_name=device.name,
            device_address=device.address,
            room_area_id=room_area_id,
            room_name=area.name,
            room_floor_id=area.floor_id,
            position={"x_m": float(x_m), "y_m": float(y_m), "z_m": float(z_m)},
            sample_radius_m=float(sample_radius_m),
            notes=notes,
        )
        expected_complete_at = self._register_session(session)
        return {
            "session_id": session.session_id,
            "started_at": session.started_at,
            "device_id": device_id,
            "room_area_id": room_area_id,
            "x_m": float(x_m),
            "y_m": float(y_m),
            "z_m": float(z_m),
            "sample_radius_m": float(sample_radius_m),
            "duration_s": duration_s,
            "expected_complete_at": expected_complete_at,
        }

    async def async_start_transition_session(
        self,
        *,
        device_id: str,
        room_area_id: str,
        transition_name: str,
        x_m: float,
        y_m: float,
        z_m: float,
        sample_radius_m: float = DEFAULT_SAMPLE_RADIUS_M,
        capture_duration_s: int = 60,
        transition_floor_ids: list[str],
    ) -> dict[str, Any]:
        """Validate and start a transition sample capture session."""
        await self._store.async_ensure_loaded()
        if sample_radius_m <= 0:
            msg = "Transition sample radius must be greater than 0 metres."
            raise HomeAssistantError(msg)
        if capture_duration_s < 1:
            msg = "Transition capture duration must be at least 1 second."
            raise HomeAssistantError(msg)
        self._raise_if_anchors_misconfigured()

        device = self._resolve_device_from_registry_id(device_id)
        if device is None:
            msg = "Selected device is not currently available in Bermuda."
            raise HomeAssistantError(msg)

        if any(session.device_id == device_id for session in self._sessions.values()):
            msg = "A capture session is already running for that device."
            raise HomeAssistantError(msg)

        area = self._coordinator.ar.async_get_area(room_area_id)
        if area is None:
            msg = "Selected room area does not exist."
            raise HomeAssistantError(msg)
        if area.floor_id is None:
            msg = "Transition samples require the room area to belong to a floor."
            raise HomeAssistantError(msg)

        cleaned_name = str(transition_name).strip()
        if not cleaned_name:
            msg = "transition_name must not be empty."
            raise HomeAssistantError(msg)

        cleaned_floor_ids = self._normalize_transition_floor_ids(
            transition_floor_ids=transition_floor_ids,
            room_floor_id=area.floor_id,
        )
        if not cleaned_floor_ids:
            msg = "transition_floor_ids must include at least one floor other than the room floor."
            raise HomeAssistantError(msg)

        started_dt = now()
        session = _CaptureSession(
            session_type="transition",
            session_id=f"transition_{uuid4().hex[:12]}",
            started_at=started_dt.isoformat(),
            started_monotonic=monotonic_time_coarse(),
            duration_s=capture_duration_s,
            device_id=device_id,
            device_name=device.name,
            device_address=device.address,
            room_area_id=room_area_id,
            room_name=area.name,
            room_floor_id=area.floor_id,
            position={"x_m": float(x_m), "y_m": float(y_m), "z_m": float(z_m)},
            sample_radius_m=float(sample_radius_m),
            transition_name=cleaned_name,
            transition_floor_ids=cleaned_floor_ids,
        )
        expected_complete_at = self._register_session(session)
        return {
            "session_id": session.session_id,
            "started_at": session.started_at,
            "device_id": device_id,
            "room_area_id": room_area_id,
            "room_name": area.name,
            "room_floor_id": area.floor_id,
            "transition_name": cleaned_name,
            "x_m": float(x_m),
            "y_m": float(y_m),
            "z_m": float(z_m),
            "sample_radius_m": float(sample_radius_m),
            "capture_duration_s": capture_duration_s,
            "transition_floor_ids": cleaned_floor_ids,
            "expected_complete_at": expected_complete_at,
        }

    async def async_start_device_offset_session(
        self,
        *,
        device_id: str,
        x_m: float,
        y_m: float,
        z_m: float,
        duration_s: int = 60,
    ) -> dict[str, Any]:
        """
        Validate and start a per-device RSSI offset capture.

        The device is held still at a known reference position; observed
        per-anchor RSSI medians are compared against the fitted ranging model's
        expectations to derive one global offset for the device. Calibration
        samples themselves are untouched — the map stays single-device.
        """
        await self._store.async_ensure_loaded()
        if duration_s < 1:
            msg = "Device offset capture duration must be at least 1 second."
            raise HomeAssistantError(msg)
        self._raise_if_anchors_misconfigured()

        ranging_model = getattr(self._coordinator, "ranging_model", None)
        current_layout_hash = self.current_anchor_layout_hash
        if ranging_model is None or not ranging_model.has_model(current_layout_hash):
            msg = (
                "Device offset calibration needs a fitted ranging model for the current anchor "
                "layout. Record calibration samples first."
            )
            raise HomeAssistantError(msg)

        device = self._resolve_device_from_registry_id(device_id)
        if device is None:
            msg = "Selected device is not currently available in Bermuda."
            raise HomeAssistantError(msg)

        if any(session.device_id == device_id for session in self._sessions.values()):
            msg = "A capture session is already running for that device."
            raise HomeAssistantError(msg)

        started_dt = now()
        session = _CaptureSession(
            session_type="device_offset",
            session_id=f"offset_{uuid4().hex[:12]}",
            started_at=started_dt.isoformat(),
            started_monotonic=monotonic_time_coarse(),
            duration_s=duration_s,
            device_id=device_id,
            device_name=device.name,
            device_address=device.address,
            room_area_id="",
            room_name="(offset reference point)",
            room_floor_id=None,
            position={"x_m": float(x_m), "y_m": float(y_m), "z_m": float(z_m)},
            sample_radius_m=DEFAULT_SAMPLE_RADIUS_M,
        )
        expected_complete_at = self._register_session(session)
        return {
            "session_id": session.session_id,
            "started_at": session.started_at,
            "device_id": device_id,
            "device_name": device.name,
            "x_m": float(x_m),
            "y_m": float(y_m),
            "z_m": float(z_m),
            "duration_s": duration_s,
            "expected_complete_at": expected_complete_at,
        }

    def _register_session(self, session: _CaptureSession) -> str:
        """Track and schedule one active capture session."""
        expected_complete_at = (now() + timedelta(seconds=session.duration_s)).isoformat()
        self._sessions[session.session_id] = session
        if session.session_type == "transition":
            self._update_transition_session_notification(
                session,
                status="started",
                expected_complete_at=expected_complete_at,
            )
        elif session.session_type == "device_offset":
            self._update_device_offset_notification(
                session,
                status="started",
                expected_complete_at=expected_complete_at,
            )
        else:
            self._update_session_notification(
                session,
                status="started",
                expected_complete_at=expected_complete_at,
            )
        task = asyncio.create_task(self._async_wait_and_finalize(session.session_id))
        self._session_tasks[session.session_id] = task
        task.add_done_callback(lambda _task, session_id=session.session_id: self._session_tasks.pop(session_id, None))
        return expected_complete_at

    async def _async_wait_and_finalize(self, session_id: str) -> None:
        """Sleep until the capture window ends, then finalize."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        try:
            await asyncio.sleep(session.duration_s)
        except asyncio.CancelledError:
            return
        await self._async_finalize_session(session_id)

    def capture_update(self) -> None:
        """Snapshot current coordinator device state into active calibration sessions."""
        if not self._sessions:
            return
        nowstamp = monotonic_time_coarse()
        observed_at = now().isoformat()
        for session in self._sessions.values():
            device = self._coordinator.devices.get(session.device_address)
            if device is None:
                continue
            self._record_trilat_observation(session, device)
            for scanner_address in sorted(self._coordinator.scanner_list):
                advert = device.get_scanner(scanner_address)
                if advert is None or not self._advert_is_usable(advert, nowstamp):
                    continue
                self._record_observation(session, advert, observed_at, nowstamp)

    def compute_anchor_layout_hash(self) -> str:
        """Return a deterministic hash for the current anchor layout."""
        anchors: list[tuple[str, float, float, float | None]] = []
        for scanner_address, anchor in sorted(self.current_anchor_geometry().items()):
            anchors.append(
                (
                    scanner_address,
                    float(anchor["x_m"]),
                    float(anchor["y_m"]),
                    None if anchor["z_m"] is None else float(anchor["z_m"]),
                )
            )
        encoded = json.dumps(anchors, separators=(",", ":"), sort_keys=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def async_acknowledge_current_layout_mismatch(self) -> None:
        """Acknowledge that the current layout mismatch is intentional."""
        await self._store.async_acknowledge_layout_hash(self.current_anchor_layout_hash)

    async def async_update_samples_to_current_geometry(self) -> int:
        """
        Adopt the current anchor geometry for all stored samples.

        Mutates ordinary samples, transition samples, and dependent transition-zone
        layout hashes consistently. Refuses to run when the current geometry is
        incomplete or any stored sample references a scanner that cannot be proven
        to exist in the current anchor set. Never writes null coordinates and only
        touches anchor positions and layout hashes — RSSI statistics, packet
        counts, timestamps, and calibration positions are preserved.
        """
        await self._store.async_ensure_loaded()
        samples = self.samples()
        transition_samples = self.transition_samples()
        if not samples and not transition_samples:
            return 0

        current_anchor_index = self._current_anchor_identity_index()
        if not current_anchor_index:
            msg = "Cannot update stored sample geometry: no current anchor geometry is configured."
            raise HomeAssistantError(msg)
        incomplete_scanners = self.incomplete_current_anchor_scanners()
        if incomplete_scanners:
            raise HomeAssistantError(
                "Cannot update stored sample geometry: current anchors are missing coordinates: "
                + ", ".join(incomplete_scanners)
            )

        unknown_scanners: set[str] = set()
        for sample in [*samples, *transition_samples]:
            for scanner_address, anchor in (sample.get("anchors") or {}).items():
                if current_anchor_index.get(mac_norm(str(scanner_address))) is None:
                    unknown_scanners.add(str(anchor.get("scanner_name") or scanner_address))
        if unknown_scanners:
            raise HomeAssistantError(
                "Cannot update stored sample geometry: stored samples reference scanners "
                "missing from the current anchor set: " + ", ".join(sorted(unknown_scanners))
            )

        current_layout_hash = self.current_anchor_layout_hash
        previous_hashes: set[str] = set()

        def _apply(sample: dict[str, Any]) -> bool:
            sample_changed = False
            for scanner_address, anchor in (sample.get("anchors") or {}).items():
                current_anchor, _name = current_anchor_index[mac_norm(str(scanner_address))]
                replacement = {
                    "x_m": current_anchor["x_m"],
                    "y_m": current_anchor["y_m"],
                    "z_m": current_anchor["z_m"],
                }
                if any(value is None for value in replacement.values()):
                    msg = (
                        "Cannot update stored sample geometry: refusing to write a null coordinate "
                        f"for scanner {scanner_address}."
                    )
                    raise HomeAssistantError(msg)
                if (anchor.get("anchor_position") or {}) != replacement:
                    anchor["anchor_position"] = replacement
                    sample_changed = True
            stored_hash = str(sample.get("anchor_layout_hash") or "")
            if stored_hash != current_layout_hash:
                if stored_hash:
                    previous_hashes.add(stored_hash)
                sample["anchor_layout_hash"] = current_layout_hash
                sample_changed = True
            return sample_changed

        updated = sum(1 for sample in samples if _apply(sample))
        transition_updated = sum(1 for sample in transition_samples if _apply(sample))

        if updated == 0 and transition_updated == 0:
            return 0

        if updated:
            await self._store.async_replace_samples(samples)
        if transition_updated:
            await self._store.async_replace_transition_samples(transition_samples)
        await self._async_remap_transition_zone_hashes(previous_hashes, current_layout_hash)
        await self._store.async_forget_layout_hash(current_layout_hash)
        self._equivalent_layout_hashes_cache = None
        await self._async_notify_changed()
        return updated + transition_updated

    async def _async_remap_transition_zone_hashes(
        self,
        previous_hashes: set[str],
        current_layout_hash: str,
    ) -> None:
        """Re-key transition zones built from samples whose layout hash was repaired."""
        if not previous_hashes:
            return
        zone_store = getattr(self._coordinator, "transition_zone_store", None)
        if zone_store is None:
            return
        for zone in zone_store.zones:
            if zone.anchor_layout_hash in previous_hashes:
                zone.anchor_layout_hash = current_layout_hash
                await zone_store.async_save_zone(zone)

    async def async_repair_transition_sample_null_z(self) -> dict[str, int]:
        """
        Backfill code-corrupted null anchor Z values in stored transition samples.

        Only fills Z when the stored X/Y exactly match the current anchor for the
        same proven scanner identity and that anchor has a known Z — i.e. the null
        is provably storage corruption, not a layout change. Samples that cannot
        be proven are left untouched and reported as corrupted.
        """
        await self._store.async_ensure_loaded()
        transition_samples = self.transition_samples()
        if not transition_samples:
            return {"repaired": 0, "corrupted": 0, "unchanged": 0}

        current_anchor_index = self._current_anchor_identity_index()
        current_layout_hash = self.current_anchor_layout_hash
        repaired = 0
        corrupted = 0
        unchanged = 0
        changed_any = False
        previous_hashes: set[str] = set()

        for sample in transition_samples:
            classification = self.classify_sample_against_current(
                sample,
                current_anchor_index,
                current_layout_hash=current_layout_hash,
            )
            if classification != self.SAMPLE_CLASS_REPAIRABLE_NULL_Z:
                if classification == self.SAMPLE_CLASS_CORRUPTED:
                    corrupted += 1
                else:
                    unchanged += 1
                continue
            for scanner_address, anchor in (sample.get("anchors") or {}).items():
                resolved = current_anchor_index.get(mac_norm(str(scanner_address)))
                if resolved is None:
                    continue
                current_anchor, _name = resolved
                anchor_position = anchor.get("anchor_position") or {}
                if anchor_position.get("z_m") is None and current_anchor.get("z_m") is not None:
                    anchor_position["z_m"] = current_anchor["z_m"]
                    anchor["anchor_position"] = anchor_position
            stored_hash = str(sample.get("anchor_layout_hash") or "")
            if stored_hash and stored_hash != current_layout_hash:
                previous_hashes.add(stored_hash)
            sample["anchor_layout_hash"] = current_layout_hash
            repaired += 1
            changed_any = True

        if changed_any:
            await self._store.async_replace_transition_samples(transition_samples)
            # Zones may already have been migrated from these samples under their
            # old hash (e.g. when the first repair attempt ran before scanners
            # were discovered); re-key them so they stay active.
            await self._async_remap_transition_zone_hashes(previous_hashes, current_layout_hash)
            self._equivalent_layout_hashes_cache = None
            await self._async_notify_changed()
        return {"repaired": repaired, "corrupted": corrupted, "unchanged": unchanged}

    def _record_observation(
        self,
        session: _CaptureSession,
        advert: BermudaAdvert,
        observed_at: str,
        nowstamp: float,
    ) -> None:
        """Add one snapshot observation for an active session."""
        scanner = self._coordinator.devices.get(advert.scanner_address)
        accumulator = session.anchors.setdefault(
            advert.scanner_address,
            _AnchorObservationAccumulator(
                scanner_address=advert.scanner_address,
                scanner_name=scanner.name if scanner is not None else advert.scanner_address,
                anchor_position={
                    "x_m": self._coordinator.get_scanner_anchor_x(advert.scanner_address),
                    "y_m": self._coordinator.get_scanner_anchor_y(advert.scanner_address),
                    "z_m": self._coordinator.get_scanner_anchor_z(advert.scanner_address),
                },
            ),
        )
        value = float(advert.rssi)
        accumulator.values.append(value)
        if accumulator.first_seen_at is None:
            accumulator.first_seen_at = observed_at
        accumulator.last_seen_at = observed_at

    @staticmethod
    def _record_trilat_observation(session: _CaptureSession, device: BermudaDevice) -> None:
        """Capture the current raw filtered trilat solution for calibration summaries."""
        x_val = getattr(device, "trilat_x_raw_m", None)
        y_val = getattr(device, "trilat_y_raw_m", None)
        z_val = getattr(device, "trilat_z_raw_m", None)
        if x_val is None or y_val is None:
            x_val = getattr(device, "trilat_x_m", None)
            y_val = getattr(device, "trilat_y_m", None)
            z_val = getattr(device, "trilat_z_m", None)
        if x_val is None or y_val is None:
            return
        session.trilat_x_values.append(float(x_val))
        session.trilat_y_values.append(float(y_val))
        if z_val is not None:
            session.trilat_z_values.append(float(z_val))
        residual_m = getattr(device, "trilat_residual_m", None)
        if residual_m is not None:
            session.trilat_residual_values.append(float(residual_m))
        geometry_quality = getattr(device, "trilat_geometry_quality", None)
        if geometry_quality is not None:
            session.trilat_geometry_quality_values.append(float(geometry_quality))
        tracking_confidence = getattr(device, "trilat_tracking_confidence", None)
        if tracking_confidence is not None:
            session.trilat_tracking_confidence_values.append(float(tracking_confidence))

    @staticmethod
    def _advert_is_usable(advert: BermudaAdvert, nowstamp: float) -> bool:
        """Check whether an advert is fresh enough for calibration capture."""
        if advert.rssi is None or advert.stamp is None:
            return False
        return advert.stamp >= nowstamp - DISTANCE_TIMEOUT

    async def _async_finalize_session(self, session_id: str, failure_reason: str | None = None) -> None:
        """Finalize a session, persist if usable, and fire completion event."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return

        if failure_reason is not None:
            if session.session_type == "transition":
                self._update_transition_session_notification(
                    session,
                    status="failed",
                    quality_reason=failure_reason,
                )
            elif session.session_type == "device_offset":
                self._emit_device_offset_event(
                    session=session,
                    result={"status": "failed", "reason": failure_reason},
                )
            else:
                self._emit_completion_event(
                    session=session,
                    sample_id=None,
                    quality_status="failed",
                    quality_reason=failure_reason,
                )
            return

        if session.session_type == "device_offset":
            await self._async_finalize_device_offset_session(session)
            return

        if session.session_type == "transition":
            sample = self._build_transition_sample(session)
            quality = sample["quality"]
            sample_id: str | None = None
            if quality["status"] in {CALIBRATION_QUALITY_ACCEPTED, CALIBRATION_QUALITY_POOR}:
                transition_samples = self.transition_samples()
                transition_samples.append(sample)
                await self._store.async_replace_transition_samples(transition_samples)
                sample_id = sample["id"]
                await self._async_notify_changed()
            self._update_transition_session_notification(
                session,
                status=quality["status"],
                sample_id=sample_id,
                quality_reason=quality["reason"],
            )
            return

        sample = self._build_calibration_sample(session)
        quality = sample["quality"]
        sample_id: str | None = None
        if quality["status"] in {CALIBRATION_QUALITY_ACCEPTED, CALIBRATION_QUALITY_POOR}:
            await self._store.async_add_sample(sample)
            sample_id = sample["id"]
            await self._async_notify_changed()
        self._emit_completion_event(
            session=session,
            sample_id=sample_id,
            quality_status=quality["status"],
            quality_reason=quality["reason"],
        )

    @staticmethod
    def _p95_abs_error(values: list[float], target: float) -> float:
        """Return a simple 95th-percentile absolute error estimate."""
        if not values:
            return 0.0
        errors = sorted(abs(value - target) for value in values)
        if len(errors) == 1:
            return float(errors[0])
        index = max(0, min(len(errors) - 1, math.ceil(0.95 * len(errors)) - 1))
        return float(errors[index])

    @staticmethod
    def _series_stddev(values: list[float]) -> float:
        """Return population stddev for a capture series."""
        if len(values) < 2:
            return 0.0
        return float(statistics.pstdev(values))

    def _build_trilat_capture_summary(self, session: _CaptureSession) -> dict[str, Any] | None:
        """Summarise the raw filtered trilat path observed during a capture."""
        observed_count = min(len(session.trilat_x_values), len(session.trilat_y_values))
        if observed_count <= 0:
            return None

        target_x = float(session.position["x_m"])
        target_y = float(session.position["y_m"])
        target_z = float(session.position["z_m"])
        x_values = session.trilat_x_values[:observed_count]
        y_values = session.trilat_y_values[:observed_count]
        z_values = session.trilat_z_values

        x_rmse = math.sqrt(sum((value - target_x) ** 2 for value in x_values) / observed_count)
        y_rmse = math.sqrt(sum((value - target_y) ** 2 for value in y_values) / observed_count)
        z_summary: dict[str, float | int | str | None] = {}
        if z_values:
            z_count = len(z_values)
            z_rmse = math.sqrt(sum((value - target_z) ** 2 for value in z_values) / z_count)
            z_summary = {
                "z_mean_m": round(float(statistics.fmean(z_values)), 4),
                "z_stddev_m": round(self._series_stddev(z_values), 4),
                "z_rmse_from_target_m": round(z_rmse, 4),
                "z_p95_abs_error_m": round(self._p95_abs_error(z_values, target_z), 4),
            }

        x_mean = float(statistics.fmean(x_values))
        y_mean = float(statistics.fmean(y_values))
        summary: dict[str, Any] = {
            "position_source": "raw_filtered",
            "observed_count": observed_count,
            "x_mean_m": round(x_mean, 4),
            "y_mean_m": round(y_mean, 4),
            "x_stddev_m": round(self._series_stddev(x_values), 4),
            "y_stddev_m": round(self._series_stddev(y_values), 4),
            "x_rmse_from_target_m": round(x_rmse, 4),
            "y_rmse_from_target_m": round(y_rmse, 4),
            "x_p95_abs_error_m": round(self._p95_abs_error(x_values, target_x), 4),
            "y_p95_abs_error_m": round(self._p95_abs_error(y_values, target_y), 4),
            # Post-correction spread: p95 from the observed mean, not from the declared
            # target.  After bias correction removes the systematic offset, the residual
            # uncertainty is characterised by spread around the mean, not from target.
            "x_p95_spread_m": round(self._p95_abs_error(x_values, x_mean), 4),
            "y_p95_spread_m": round(self._p95_abs_error(y_values, y_mean), 4),
            "residual_mean_m": (
                round(float(statistics.fmean(session.trilat_residual_values)), 4)
                if session.trilat_residual_values
                else None
            ),
            "geometry_quality_mean": (
                round(float(statistics.fmean(session.trilat_geometry_quality_values)), 4)
                if session.trilat_geometry_quality_values
                else None
            ),
            "tracking_confidence_mean": (
                round(float(statistics.fmean(session.trilat_tracking_confidence_values)), 4)
                if session.trilat_tracking_confidence_values
                else None
            ),
        }
        summary.update(z_summary)
        return summary

    def _build_capture_quality(self, session: _CaptureSession) -> dict[str, Any]:
        """Build shared anchor and quality payload for a completed capture session."""
        created_at = now().isoformat()
        anchors: dict[str, Any] = {}
        eligible_anchor_count = 0
        packet_counts: list[int] = []
        rssi_mads: list[float] = []
        rssi_spans: list[float] = []
        sample_x = float(session.position["x_m"])
        sample_y = float(session.position["y_m"])
        sample_z = float(session.position["z_m"])
        geometry_anchors_2d: list[AnchorMeasurement] = []
        geometry_anchors_3d: list[AnchorMeasurement] = []
        for scanner_address, accumulator in sorted(session.anchors.items()):
            if not accumulator.values:
                continue
            eligible_anchor_count += 1
            packet_count = len(accumulator.values)
            rssi_mad = round(self._median_abs_deviation(accumulator.values), 3)
            rssi_min = min(accumulator.values)
            rssi_max = max(accumulator.values)
            rssi_span = round(rssi_max - rssi_min, 3)
            packet_counts.append(packet_count)
            rssi_mads.append(rssi_mad)
            rssi_spans.append(rssi_span)

            anchor_position = deepcopy(accumulator.anchor_position)
            anchor_x = anchor_position.get("x_m")
            anchor_y = anchor_position.get("y_m")
            anchor_z = anchor_position.get("z_m")
            if anchor_x is not None and anchor_y is not None:
                distance_xy = math.hypot(sample_x - float(anchor_x), sample_y - float(anchor_y))
                geometry_anchors_2d.append(
                    AnchorMeasurement(
                        scanner_address=scanner_address,
                        x_m=float(anchor_x),
                        y_m=float(anchor_y),
                        range_m=distance_xy,
                        sigma_m=1.0,
                    )
                )
                if anchor_z is not None:
                    geometry_anchors_3d.append(
                        AnchorMeasurement(
                            scanner_address=scanner_address,
                            x_m=float(anchor_x),
                            y_m=float(anchor_y),
                            z_m=float(anchor_z),
                            range_m=math.sqrt(
                                ((sample_x - float(anchor_x)) ** 2)
                                + ((sample_y - float(anchor_y)) ** 2)
                                + ((sample_z - float(anchor_z)) ** 2)
                            ),
                            sigma_m=1.0,
                        )
                    )

            anchors[scanner_address] = {
                "scanner_name": accumulator.scanner_name,
                "anchor_position": anchor_position,
                "packet_count": packet_count,
                "rssi_median": round(statistics.median(accumulator.values), 3),
                "rssi_mean": round(statistics.fmean(accumulator.values), 3),
                "rssi_mad": rssi_mad,
                "rssi_min": rssi_min,
                "rssi_max": rssi_max,
                "first_seen_at": accumulator.first_seen_at,
                "last_seen_at": accumulator.last_seen_at,
            }

        quality_status = CALIBRATION_QUALITY_ACCEPTED
        quality_reason: str | None = None
        if eligible_anchor_count < 1:
            quality_status = CALIBRATION_QUALITY_REJECTED
            quality_reason = "no_visible_anchors"
        elif eligible_anchor_count < 3:
            quality_status = CALIBRATION_QUALITY_POOR
            quality_reason = "insufficient_anchors"

        geometry_quality_01 = 0.0
        geometry_gdop: float | None = None
        if len(geometry_anchors_3d) >= 4:
            geometry_metrics = solve_quality_metrics_3d(sample_x, sample_y, sample_z, geometry_anchors_3d)
            geometry_quality_01 = geometry_metrics.geometry_quality_01
            geometry_gdop = geometry_metrics.gdop
        elif len(geometry_anchors_2d) >= 3:
            geometry_metrics = solve_quality_metrics_2d(sample_x, sample_y, geometry_anchors_2d)
            geometry_quality_01 = geometry_metrics.geometry_quality_01
            geometry_gdop = geometry_metrics.gdop

        total_packet_count = sum(packet_counts)
        median_packet_count = float(statistics.median(packet_counts)) if packet_counts else 0.0
        median_rssi_mad_db = round(float(statistics.median(rssi_mads)), 3) if rssi_mads else 0.0
        median_rssi_span_db = round(float(statistics.median(rssi_spans)), 3) if rssi_spans else 0.0

        anchor_score = min(1.0, eligible_anchor_count / 5.0)
        packet_score = min(1.0, median_packet_count / 3.0) if median_packet_count > 0 else 0.0
        stability_score = max(0.0, min(1.0, 1.0 - (median_rssi_mad_db / 10.0)))
        quality_score_01 = round(
            (0.35 * anchor_score) + (0.25 * packet_score) + (0.20 * stability_score) + (0.20 * geometry_quality_01),
            3,
        )
        quality_level = self._quality_level_from_metrics(
            quality_status=quality_status,
            quality_score_01=quality_score_01,
            eligible_anchor_count=eligible_anchor_count,
        )

        return {
            "created_at": created_at,
            "anchors": anchors,
            "quality": {
                "status": quality_status,
                "level": quality_level,
                "score_01": quality_score_01,
                "eligible_anchor_count": eligible_anchor_count,
                "total_packet_count": total_packet_count,
                "median_packet_count": round(median_packet_count, 3),
                "median_rssi_mad_db": median_rssi_mad_db,
                "median_rssi_span_db": median_rssi_span_db,
                "geometry_quality_01": round(geometry_quality_01, 3),
                "geometry_gdop": round(float(geometry_gdop), 3) if geometry_gdop is not None else None,
                "reason": quality_reason,
            },
        }

    def _build_calibration_sample(self, session: _CaptureSession) -> dict[str, Any]:
        """Build the persisted calibration-sample payload for a completed session."""
        shared = self._build_capture_quality(session)
        trilat_capture = self._build_trilat_capture_summary(session)
        return {
            "id": f"sample_{uuid4().hex[:12]}",
            "created_at": shared["created_at"],
            "started_at": session.started_at,
            "duration_s": session.duration_s,
            "device_id": session.device_id,
            "device_name": session.device_name,
            "device_address": session.device_address,
            "room_area_id": session.room_area_id,
            "room_name": session.room_name,
            "room_floor_id": session.room_floor_id,
            "position": deepcopy(session.position),
            "sample_radius_m": session.sample_radius_m,
            "anchor_layout_hash": self.current_anchor_layout_hash,
            "notes": session.notes,
            "anchors": shared["anchors"],
            "quality": shared["quality"],
            "trilat_capture": trilat_capture,
        }

    def _build_transition_sample(self, session: _CaptureSession) -> dict[str, Any]:
        """Build the persisted transition-sample payload for a completed session."""
        shared = self._build_capture_quality(session)
        trilat_capture = self._build_trilat_capture_summary(session)
        return {
            "id": f"transition_sample_{uuid4().hex[:12]}",
            "created_at": shared["created_at"],
            "updated_at": shared["created_at"],
            "started_at": session.started_at,
            "capture_duration_s": session.duration_s,
            "device_id": session.device_id,
            "device_name": session.device_name,
            "device_address": session.device_address,
            "room_area_id": session.room_area_id,
            "room_name": session.room_name,
            "room_floor_id": session.room_floor_id,
            "transition_name": session.transition_name,
            "position": deepcopy(session.position),
            "sample_radius_m": session.sample_radius_m,
            "transition_floor_ids": list(session.transition_floor_ids),
            "anchor_layout_hash": self.current_anchor_layout_hash,
            "anchors": shared["anchors"],
            "quality": shared["quality"],
            "trilat_capture": trilat_capture,
        }

    @staticmethod
    def _sample_floor_id(
        sample: dict[str, Any],
        coordinator: BermudaDataUpdateCoordinator,
    ) -> str | None:
        """Resolve the floor id for one stored calibration sample."""
        room_floor_id = sample.get("room_floor_id")
        if room_floor_id:
            return str(room_floor_id)
        room_area_id = sample.get("room_area_id")
        if not room_area_id:
            return None
        area = coordinator.ar.async_get_area(str(room_area_id))
        return area.floor_id if area is not None else None

    @staticmethod
    def _solve_covariance_xy(
        anchors: list[AnchorMeasurement],
        *,
        x_m: float,
        y_m: float,
    ) -> tuple[float, float, float] | None:
        """Return a simple XY covariance estimate for a solved anchor set."""
        info_00 = 0.0
        info_01 = 0.0
        info_11 = 0.0
        contributing = 0
        for anchor in anchors:
            dx = float(x_m) - float(anchor.x_m)
            dy = float(y_m) - float(anchor.y_m)
            distance = max(math.hypot(dx, dy), 1e-6)
            sigma = max(float(anchor.sigma_m or 1.0), 0.05)
            grad_x = dx / distance / sigma
            grad_y = dy / distance / sigma
            info_00 += grad_x * grad_x
            info_01 += grad_x * grad_y
            info_11 += grad_y * grad_y
            contributing += 1

        if contributing < 2:
            return None

        info_00 += 1e-6
        info_11 += 1e-6
        det = (info_00 * info_11) - (info_01 * info_01)
        if det <= 1e-12 or not math.isfinite(det):
            return None
        cov_xx = info_11 / det
        cov_xy = -info_01 / det
        cov_yy = info_00 / det
        if not all(math.isfinite(value) for value in (cov_xx, cov_xy, cov_yy)):
            return None
        return max(cov_xx, 0.0), cov_xy, max(cov_yy, 0.0)

    def _build_trilat_correction_sample(
        self,
        sample: dict[str, Any],
        ranging_model: BermudaRangingModel,
    ) -> _TrilatCorrectionSample | None:
        """Build one local XY correction sample from persisted calibration data."""
        layout_hash = self.runtime_layout_hash_for_sample(sample)
        room_area_id = str(sample.get("room_area_id") or "")
        position = sample.get("position") or {}
        sample_x = position.get("x_m")
        sample_y = position.get("y_m")
        sample_z = position.get("z_m")
        if not layout_hash or not room_area_id or sample_x is None or sample_y is None or sample_z is None:
            return None

        sample_radius_m = max(float(sample.get("sample_radius_m") or DEFAULT_SAMPLE_RADIUS_M), 0.1)
        floor_id = self._sample_floor_id(sample, self._coordinator)
        trilat_capture = sample.get("trilat_capture") or {}
        observed_count = int(trilat_capture.get("observed_count") or 0)
        quality_score = max(0.0, min(1.0, float(sample.get("quality", {}).get("score_01") or 0.0)))

        if (
            observed_count > 0
            and trilat_capture.get("x_mean_m") is not None
            and trilat_capture.get("y_mean_m") is not None
        ):
            # Use post-correction spread (p95 from mean) as half_width — this represents
            # residual uncertainty after bias is removed.  Fall back to stddev for older
            # summaries that predate the x_p95_spread_m field.
            half_width_x_m = max(
                float(trilat_capture.get("x_p95_spread_m") or 0.0),
                float(trilat_capture.get("x_stddev_m") or 0.0),
                0.1,
            )
            half_width_y_m = max(
                float(trilat_capture.get("y_p95_spread_m") or 0.0),
                float(trilat_capture.get("y_stddev_m") or 0.0),
                0.1,
            )
            return _TrilatCorrectionSample(
                layout_hash=layout_hash,
                floor_id=floor_id,
                room_area_id=room_area_id,
                x_m=float(sample_x),
                y_m=float(sample_y),
                z_m=float(sample_z),
                sample_radius_m=sample_radius_m,
                bias_x_m=float(sample_x) - float(trilat_capture["x_mean_m"]),
                bias_y_m=float(sample_y) - float(trilat_capture["y_mean_m"]),
                half_width_x_m=half_width_x_m,
                half_width_y_m=half_width_y_m,
                reference_residual_m=(
                    float(trilat_capture["residual_mean_m"])
                    if trilat_capture.get("residual_mean_m") is not None
                    else None
                ),
                quality_weight=max(0.35, min(1.0, 0.5 + (min(observed_count, 10) * 0.05) + (0.15 * quality_score))),
                source="capture",
            )

        anchors: list[AnchorMeasurement] = []
        device_id = str(sample.get("device_id") or "")
        for scanner_address, anchor in (sample.get("anchors") or {}).items():
            anchor_position = anchor.get("anchor_position") or {}
            anchor_x = anchor_position.get("x_m")
            anchor_y = anchor_position.get("y_m")
            anchor_z = anchor_position.get("z_m")
            rssi_median = anchor.get("rssi_median")
            if anchor_x is None or anchor_y is None or rssi_median is None:
                continue
            range_estimate = ranging_model.estimate_range(
                layout_hash=layout_hash,
                scanner_address=str(scanner_address),
                device_id=device_id or None,
                filtered_rssi=float(rssi_median),
                live_rssi_dispersion=(float(anchor.get("rssi_mad")) if anchor.get("rssi_mad") is not None else None),
                live_packet_count=int(anchor.get("packet_count") or 1),
            )
            if range_estimate is None:
                continue
            anchors.append(
                AnchorMeasurement(
                    scanner_address=str(scanner_address),
                    x_m=float(anchor_x),
                    y_m=float(anchor_y),
                    z_m=(float(anchor_z) if anchor_z is not None else None),
                    range_m=range_estimate.range_m,
                    sigma_m=range_estimate.sigma_m,
                )
            )

        can_solve_3d = len(anchors) >= 4 and all(anchor.z_m is not None for anchor in anchors)
        if can_solve_3d:
            solve_result = solve_3d_soft_l1(
                anchors,
                initial_guess=(float(sample_x), float(sample_y), float(sample_z)),
            )
        elif len(anchors) >= 3:
            solve_result = solve_2d_soft_l1(
                anchors,
                initial_guess=(float(sample_x), float(sample_y)),
            )
        else:
            return None

        if (
            not solve_result.ok
            or solve_result.x_m is None
            or solve_result.y_m is None
            or (can_solve_3d and solve_result.z_m is None)
        ):
            return None

        covariance_xy = self._solve_covariance_xy(
            anchors,
            x_m=solve_result.x_m,
            y_m=solve_result.y_m,
        )
        sigma_x_m = math.sqrt(covariance_xy[0]) if covariance_xy is not None else 0.0
        sigma_y_m = math.sqrt(covariance_xy[2]) if covariance_xy is not None else 0.0
        bias_x_m = float(sample_x) - solve_result.x_m
        bias_y_m = float(sample_y) - solve_result.y_m
        min_half_width = max(0.25, sample_radius_m * 0.5)
        return _TrilatCorrectionSample(
            layout_hash=layout_hash,
            floor_id=floor_id,
            room_area_id=room_area_id,
            x_m=float(sample_x),
            y_m=float(sample_y),
            z_m=float(sample_z),
            sample_radius_m=sample_radius_m,
            bias_x_m=bias_x_m,
            bias_y_m=bias_y_m,
            half_width_x_m=max(abs(bias_x_m), sigma_x_m, min_half_width),
            half_width_y_m=max(abs(bias_y_m), sigma_y_m, min_half_width),
            reference_residual_m=solve_result.residual_rms_m,
            quality_weight=max(0.2, min(0.75, 0.25 + (0.45 * quality_score))),
            source="bootstrap",
        )

    @staticmethod
    def _quality_level_from_metrics(
        *,
        quality_status: str,
        quality_score_01: float,
        eligible_anchor_count: int,
    ) -> str:
        """Return a user-facing quality level from persisted sample metrics."""
        if quality_status == CALIBRATION_QUALITY_REJECTED:
            return "rejected"
        if quality_status == CALIBRATION_QUALITY_POOR or eligible_anchor_count < 3:
            return "low"
        if quality_score_01 >= 0.75:
            return "high"
        if quality_score_01 >= 0.45:
            return "medium"
        return "low"

    def _resolve_device_from_registry_id(self, registry_id: str) -> BermudaDevice | None:
        """Return the matching Bermuda device for a Home Assistant device registry id."""
        device = self._coordinator.dr.async_get(registry_id)
        if device is None:
            return None
        device_address = None
        for connection in device.connections:
            if connection[0] in {DOMAIN_PRIVATE_BLE_DEVICE, dr.CONNECTION_BLUETOOTH, "ibeacon"}:
                device_address = connection[1]
                break
        if device_address is None:
            return None
        return self._coordinator.devices.get(str(device_address).lower())

    def transition_support_diagnostics(
        self,
        *,
        layout_hash: str,
        x_m: float | None,
        y_m: float | None,
        z_m: float | None,
        room_area_id: str | None,
        challenger_floor_id: str | None,
        geometry_quality_01: float,
    ) -> dict[str, Any]:
        """Return transition-proximity diagnostics for the current solve."""
        all_samples = self.transition_samples()
        accepted_hashes = self.equivalent_layout_hashes() | {layout_hash}
        layout_samples = [
            sample for sample in all_samples if str(sample.get("anchor_layout_hash") or "") in accepted_hashes
        ]
        diagnostics: dict[str, Any] = {
            "transition_sample_count": len(all_samples),
            "transition_layout_sample_count": len(layout_samples),
            "transition_room_context_area_id": room_area_id,
            "transition_challenger_floor_id": challenger_floor_id,
            "transition_support_01": 0.0,
            "transition_matching_room_count": 0,
            "transition_supporting_floor_count": 0,
            "transition_nearby_match_count": 0,
            "transition_best_name": None,
            "transition_best_room_area_id": None,
            "transition_best_floor_ids": [],
            "transition_best_distance_m": None,
            "transition_best_distance_mode": None,
            "transition_best_within_radius": False,
            "transition_best_room_context_match": False,
            "transition_best_supports_challenger": False,
        }
        if not layout_samples:
            return diagnostics

        best_sample: dict[str, Any] | None = None
        best_distance_m: float | None = None
        best_distance_mode: str | None = None
        best_support_01 = 0.0
        room_quality_ok = geometry_quality_01 >= 0.30

        for sample in layout_samples:
            sample_room_area_id = str(sample.get("room_area_id") or "")
            transition_floor_ids = [
                str(floor_id) for floor_id in (sample.get("transition_floor_ids") or []) if floor_id
            ]
            room_context_match = room_area_id == sample_room_area_id if room_area_id is not None else False
            supports_challenger = (
                challenger_floor_id in transition_floor_ids if challenger_floor_id is not None else False
            )
            if room_context_match:
                diagnostics["transition_matching_room_count"] = int(diagnostics["transition_matching_room_count"]) + 1
            if supports_challenger:
                diagnostics["transition_supporting_floor_count"] = (
                    int(diagnostics["transition_supporting_floor_count"]) + 1
                )

            position = sample.get("position") or {}
            distance_m: float | None = None
            distance_mode: str | None = None
            if x_m is not None and y_m is not None:
                dx = float(x_m) - float(position.get("x_m", x_m))
                dy = float(y_m) - float(position.get("y_m", y_m))
                if z_m is not None and position.get("z_m") is not None:
                    dz = float(z_m) - float(position.get("z_m", z_m))
                    distance_m = math.sqrt((dx * dx) + (dy * dy) + (dz * dz))
                    distance_mode = "3d"
                else:
                    distance_m = math.hypot(dx, dy)
                    distance_mode = "xy"
            within_radius = distance_m is not None and distance_m <= float(sample.get("sample_radius_m", 0.0))
            if within_radius:
                diagnostics["transition_nearby_match_count"] = int(diagnostics["transition_nearby_match_count"]) + 1

            support_01 = 0.0
            if room_context_match and supports_challenger:
                if within_radius and room_quality_ok:
                    support_01 = 1.0
                elif not room_quality_ok:
                    support_01 = 0.5

            if support_01 > best_support_01 or (
                math.isclose(support_01, best_support_01)
                and distance_m is not None
                and (best_distance_m is None or distance_m < best_distance_m)
            ):
                best_support_01 = support_01
                best_sample = sample
                best_distance_m = distance_m
                best_distance_mode = distance_mode
                diagnostics["transition_support_01"] = support_01
                diagnostics["transition_best_within_radius"] = bool(within_radius)
                diagnostics["transition_best_room_context_match"] = room_context_match
                diagnostics["transition_best_supports_challenger"] = supports_challenger

        if best_sample is not None:
            diagnostics["transition_best_name"] = best_sample.get("transition_name")
            diagnostics["transition_best_room_area_id"] = best_sample.get("room_area_id")
            diagnostics["transition_best_floor_ids"] = list(best_sample.get("transition_floor_ids") or [])
            diagnostics["transition_best_distance_m"] = (
                round(float(best_distance_m), 3) if best_distance_m is not None else None
            )
            diagnostics["transition_best_distance_mode"] = best_distance_mode

        return diagnostics

    def _normalize_transition_floor_ids(
        self,
        *,
        transition_floor_ids: list[str],
        room_floor_id: str,
    ) -> list[str]:
        """Validate and normalize transition floor ids from service input."""
        cleaned: list[str] = []
        for floor_id in transition_floor_ids:
            candidate = str(floor_id).strip()
            if not candidate or candidate == room_floor_id or candidate in cleaned:
                continue
            if self._coordinator.fr.async_get_floor(candidate) is None:
                msg = f"Transition floor '{candidate}' does not exist."
                raise HomeAssistantError(msg)
            cleaned.append(candidate)
        return cleaned

    @staticmethod
    def _median_abs_deviation(values: list[float]) -> float:
        """Return a simple median absolute deviation for captured values."""
        if not values:
            return 0.0
        median = statistics.median(values)
        return float(statistics.median(abs(value - median) for value in values))

    async def _async_notify_changed(self) -> None:
        """Fire registered sample-change callbacks."""
        for callback in list(self._change_callbacks):
            result = callback()
            if inspect.isawaitable(result):
                await result

    async def _async_finalize_device_offset_session(self, session: _CaptureSession) -> None:
        """Compute and persist the device offset from a completed capture."""
        current_layout_hash = self.current_anchor_layout_hash
        ranging_model = getattr(self._coordinator, "ranging_model", None)
        if ranging_model is None or not ranging_model.has_model(current_layout_hash):
            self._emit_device_offset_event(
                session=session,
                result={"status": "failed", "reason": "no_ranging_model"},
            )
            return

        reference_x = float(session.position["x_m"])
        reference_y = float(session.position["y_m"])
        reference_z = float(session.position["z_m"])
        observations: list[dict[str, Any]] = []
        for scanner_address, accumulator in sorted(session.anchors.items()):
            if not accumulator.values:
                continue
            anchor_position = accumulator.anchor_position
            anchor_x = anchor_position.get("x_m")
            anchor_y = anchor_position.get("y_m")
            anchor_z = anchor_position.get("z_m")
            if anchor_x is None or anchor_y is None or anchor_z is None:
                continue
            distance_m = math.sqrt(
                ((reference_x - float(anchor_x)) ** 2)
                + ((reference_y - float(anchor_y)) ** 2)
                + ((reference_z - float(anchor_z)) ** 2)
            )
            observations.append(
                {
                    "scanner_address": scanner_address,
                    "scanner_name": accumulator.scanner_name,
                    "rssi_median": float(statistics.median(accumulator.values)),
                    "distance_m": distance_m,
                    "packet_count": len(accumulator.values),
                }
            )

        result = compute_device_offset(
            observations,
            lambda scanner_address, distance_m: ranging_model.predict_rssi(
                layout_hash=current_layout_hash,
                scanner_address=scanner_address,
                device_id=session.device_id,
                distance_m=distance_m,
            ),
        )

        if result["status"] == "ok":
            record = {
                "device_name": session.device_name,
                "device_address": session.device_address,
                "offset_db": result["offset_db"],
                "spread_db": result["spread_db"],
                "anchor_count": result["anchor_count"],
                "anchors": result["anchors"],
                "anchor_layout_hash": current_layout_hash,
                "reference_position": dict(session.position),
                "captured_at": now().isoformat(),
                "duration_s": session.duration_s,
            }
            offset_store = getattr(self._coordinator, "device_offset_store", None)
            if offset_store is not None:
                await offset_store.async_save_offset(session.device_id, record)
            else:
                result = {"status": "failed", "reason": "offset_store_unavailable"}

        self._emit_device_offset_event(session=session, result=result)

    def _emit_device_offset_event(
        self,
        *,
        session: _CaptureSession,
        result: dict[str, Any],
    ) -> None:
        """Emit the completion event and notification for a device offset capture."""
        self.hass.bus.async_fire(
            DEVICE_OFFSET_EVENT_CAPTURED,
            {
                "session_id": session.session_id,
                "device_id": session.device_id,
                "status": result.get("status"),
                "reason": result.get("reason"),
                "offset_db": result.get("offset_db"),
                "spread_db": result.get("spread_db"),
                "anchor_count": result.get("anchor_count"),
            },
        )
        self._update_device_offset_notification(
            session,
            status=str(result.get("status")),
            result=result,
        )

    def _update_device_offset_notification(
        self,
        session: _CaptureSession,
        *,
        status: str,
        expected_complete_at: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Create or update the persistent notification for one offset capture."""
        message = (
            f"Device: {session.device_name}\n"
            f"Reference position: x={session.position['x_m']:.3f}, "
            f"y={session.position['y_m']:.3f}, z={session.position['z_m']:.3f}\n"
            f"Capture duration: {session.duration_s} s\n"
            f"Status: {status}"
        )
        if expected_complete_at is not None:
            message += f"\nExpected complete at: {expected_complete_at}"
        if result is not None:
            if result.get("offset_db") is not None:
                message += (
                    f"\nOffset: {float(result['offset_db']):+.1f} dB "
                    f"(spread={float(result.get('spread_db') or 0.0):.1f} dB, "
                    f"anchors={int(result.get('anchor_count') or 0)})"
                )
            if result.get("reason"):
                message += f"\nReason: {result['reason']}"
        persistent_notification.async_create(
            self.hass,
            message,
            title="BLE Trilateration device RSSI offset",
            notification_id=f"ble_trilateration_device_offset_{session.session_id}",
        )

    def _emit_completion_event(
        self,
        *,
        session: _CaptureSession,
        sample_id: str | None,
        quality_status: str,
        quality_reason: str | None,
    ) -> None:
        """Emit the completion event for a calibration capture."""
        self.hass.bus.async_fire(
            CALIBRATION_EVENT_SAMPLE_CAPTURED,
            {
                "session_id": session.session_id,
                "sample_id": sample_id,
                "device_id": session.device_id,
                "room_area_id": session.room_area_id,
                "quality_status": quality_status,
                "quality_reason": quality_reason,
            },
        )
        self._update_session_notification(
            session,
            status=quality_status,
            sample_id=sample_id,
            quality_reason=quality_reason,
        )

    def _update_session_notification(
        self,
        session: _CaptureSession,
        *,
        status: str,
        expected_complete_at: str | None = None,
        sample_id: str | None = None,
        quality_reason: str | None = None,
    ) -> None:
        """Create or update the persistent notification for one calibration session."""
        title = "BLE Trilateration calibration sample"
        message = (
            f"Device: {session.device_name}\n"
            f"Room: {session.room_name}\n"
            f"Position: x={session.position['x_m']:.3f}, y={session.position['y_m']:.3f}, z={session.position['z_m']:.3f}\n"
            f"Status: {status}"
        )
        if expected_complete_at is not None:
            message += f"\nExpected complete at: {expected_complete_at}"
        if sample_id is not None:
            message += f"\nSample ID: {sample_id or 'not_saved'}"
        if sample_id is not None and status != "started":
            sample = next((stored for stored in self.samples() if stored.get("id") == sample_id), None)
            if sample is not None:
                quality = sample.get("quality") or {}
                message += (
                    f"\nQuality: {quality.get('level', 'unknown')} "
                    f"(status={quality.get('status', 'unknown')}, score={float(quality.get('score_01', 0.0)):.2f})"
                )
                message += (
                    f"\nQuality details: anchors={int(quality.get('eligible_anchor_count', 0))}, "
                    f"packets={int(quality.get('total_packet_count', 0))}, "
                    f"median_mad={float(quality.get('median_rssi_mad_db', 0.0)):.2f} dB, "
                    f"geometry={float(quality.get('geometry_quality_01', 0.0)):.2f}"
                )
        if session.notes:
            message += f"\nNotes: {session.notes}"
        if quality_reason is not None:
            message += f"\nReason: {quality_reason}"
        persistent_notification.async_create(
            self.hass,
            message,
            title=title,
            notification_id=f"ble_trilateration_calibration_{session.session_id}",
        )

    def _update_transition_session_notification(
        self,
        session: _CaptureSession,
        *,
        status: str,
        expected_complete_at: str | None = None,
        sample_id: str | None = None,
        quality_reason: str | None = None,
    ) -> None:
        """Create or update the persistent notification for one transition session."""
        room_floor_name = self._floor_name_for_id(session.room_floor_id)
        transition_floor_names = ", ".join(
            self._floor_name_for_id(floor_id) for floor_id in session.transition_floor_ids
        )
        message = (
            f"Device: {session.device_name}\n"
            f"Room: {session.room_name}\n"
            f"Room floor: {room_floor_name}\n"
            f"Transition: {session.transition_name or 'unknown'}\n"
            f"Position: x={session.position['x_m']:.3f}, y={session.position['y_m']:.3f}, z={session.position['z_m']:.3f}\n"
            f"Radius: {session.sample_radius_m:.3f} m\n"
            f"Capture duration: {session.duration_s} s\n"
            f"Transition floors: {transition_floor_names or 'none'}\n"
            f"Status: {status}"
        )
        if expected_complete_at is not None:
            message += f"\nExpected complete at: {expected_complete_at}"
        if sample_id is not None:
            message += f"\nSample ID: {sample_id or 'not_saved'}"
        if sample_id is not None and status != "started":
            sample = next((stored for stored in self.transition_samples() if stored.get("id") == sample_id), None)
            if sample is not None:
                quality = sample.get("quality") or {}
                message += (
                    f"\nQuality: {quality.get('level', 'unknown')} "
                    f"(status={quality.get('status', 'unknown')}, score={float(quality.get('score_01', 0.0)):.2f})"
                )
                message += (
                    f"\nQuality details: anchors={int(quality.get('eligible_anchor_count', 0))}, "
                    f"packets={int(quality.get('total_packet_count', 0))}, "
                    f"median_mad={float(quality.get('median_rssi_mad_db', 0.0)):.2f} dB, "
                    f"geometry={float(quality.get('geometry_quality_01', 0.0)):.2f}"
                )
        if quality_reason is not None:
            message += f"\nReason: {quality_reason}"
        persistent_notification.async_create(
            self.hass,
            message,
            title="BLE Trilateration transition sample",
            notification_id=f"ble_trilateration_transition_{session.session_id}",
        )

    def _floor_name_for_id(self, floor_id: str | None) -> str:
        """Return a human-friendly floor name for one floor id."""
        if not floor_id:
            return "unknown"
        floor = self._coordinator.fr.async_get_floor(floor_id)
        return floor.name if floor is not None else str(floor_id)

    async def async_migrate_transition_samples_to_zones(self, transition_zone_store) -> int:
        """Migrate existing transition samples to TransitionZone objects. Non-destructive."""
        import uuid
        from collections import defaultdict
        from datetime import datetime

        from .transition_zone_store import TransitionZone, TransitionZoneCapture

        if transition_zone_store.zones:
            return 0  # Already populated, skip

        groups: dict[tuple[str, str], list] = defaultdict(list)
        for sample in self.transition_samples():
            t_name = sample.get("transition_name")
            layout_hash = sample.get("anchor_layout_hash", "")
            if not t_name:
                continue
            groups[(t_name, layout_hash)].append(sample)

        count = 0
        for (t_name, layout_hash), samples in groups.items():
            captures = []
            all_floor_ids: set[str] = set()
            for s in samples:
                pos = s.get("position", {})
                x_m = pos.get("x_m")
                y_m = pos.get("y_m")
                z_m = pos.get("z_m")
                sigma_m = float(s.get("sample_radius_m", 1.0))
                if x_m is None or y_m is None or z_m is None:
                    continue
                captures.append(TransitionZoneCapture(x_m=float(x_m), y_m=float(y_m), z_m=float(z_m), sigma_m=sigma_m))
                for fid in s.get("transition_floor_ids", []):
                    all_floor_ids.add(fid)
                # The transition connects the capture room's floor with each
                # declared transition floor; without it a single-target capture
                # (e.g. ground_floor -> [street_level]) yields no pairs at all.
                room_floor_id = self._sample_floor_id(s, self._coordinator)
                if room_floor_id:
                    all_floor_ids.add(str(room_floor_id))

            if not captures:
                continue

            # Build bidirectional floor pairs
            floor_ids = sorted(all_floor_ids)
            floor_pairs = []
            for i, a in enumerate(floor_ids):
                for b in floor_ids[i + 1 :]:
                    floor_pairs.append((a, b))
                    floor_pairs.append((b, a))

            zone = TransitionZone(
                zone_id=uuid.uuid4().hex,
                name=t_name,
                captures=captures,
                floor_pairs=floor_pairs,
                anchor_layout_hash=layout_hash,
                created_at=datetime.now(UTC).isoformat(),
            )
            await transition_zone_store.async_save_zone(zone)
            count += 1

        return count
