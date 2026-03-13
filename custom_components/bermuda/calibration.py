"""Calibration sample recording and management helpers."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import math
import re
import statistics
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
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
    DISTANCE_TIMEOUT,
    DOMAIN_PRIVATE_BLE_DEVICE,
)
from .trilateration import AnchorMeasurement, solve_quality_metrics_2d, solve_quality_metrics_3d

if TYPE_CHECKING:
    from .bermuda_advert import BermudaAdvert
    from .bermuda_device import BermudaDevice
    from .calibration_store import BermudaCalibrationStore
    from .coordinator import BermudaDataUpdateCoordinator


@dataclass
class _AnchorObservationAccumulator:
    """Aggregate observations for one anchor during a capture session."""

    scanner_address: str
    scanner_name: str
    anchor_position: dict[str, float | None]
    values: list[float] = field(default_factory=list)
    buckets: dict[int, list[float]] = field(default_factory=dict)
    first_seen_at: str | None = None
    last_seen_at: str | None = None


@dataclass
class _CalibrationSession:
    """Active calibration capture session."""

    session_id: str
    started_at: str
    started_monotonic: float
    duration_s: int
    device_id: str
    device_name: str
    device_address: str
    room_area_id: str
    room_name: str
    position: dict[str, float]
    sample_radius_m: float
    notes: str | None
    anchors: dict[str, _AnchorObservationAccumulator] = field(default_factory=dict)


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
        self._sessions: dict[str, _CalibrationSession] = {}
        self._session_tasks: dict[str, asyncio.Task[None]] = {}
        self._change_callbacks: list = []

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

    def get_summary(self) -> dict[str, Any]:
        """Return a small in-memory summary for config flow."""
        samples = self.samples()
        by_room: dict[str, int] = {}
        by_device: dict[str, int] = {}
        by_quality: dict[str, int] = {}
        current_layout_hash = self.current_anchor_layout_hash
        current_layout_count = 0
        for sample in samples:
            room_name = str(sample.get("room_name") or sample.get("room_area_id") or "Unknown")
            by_room[room_name] = by_room.get(room_name, 0) + 1
            device_name = str(sample.get("device_name") or sample.get("device_id") or "Unknown")
            by_device[device_name] = by_device.get(device_name, 0) + 1
            quality_level = self._sample_quality_level(sample)
            by_quality[quality_level] = by_quality.get(quality_level, 0) + 1
            if sample.get("anchor_layout_hash") == current_layout_hash:
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

    def get_transition_summary(self) -> dict[str, Any]:
        """Return a small in-memory summary for stored transition samples."""
        samples = self.transition_samples()
        by_room: dict[str, int] = {}
        by_name: dict[str, int] = {}
        by_layout: dict[str, int] = {}
        for sample in samples:
            room_name = str(sample.get("room_name") or sample.get("room_area_id") or "Unknown")
            by_room[room_name] = by_room.get(room_name, 0) + 1
            transition_name = str(sample.get("transition_name") or "Unknown")
            by_name[transition_name] = by_name.get(transition_name, 0) + 1
            layout_hash = str(sample.get("anchor_layout_hash") or "unknown")
            by_layout[layout_hash] = by_layout.get(layout_hash, 0) + 1
        return {
            "transition_sample_count": len(samples),
            "by_room": by_room,
            "by_name": by_name,
            "by_layout": by_layout,
            "current_layout_hash": self.current_anchor_layout_hash,
        }

    def current_anchor_geometry(self) -> dict[str, dict[str, float | None]]:
        """Return current configured anchor geometry by scanner address."""
        geometry: dict[str, dict[str, float | None]] = {}
        for scanner_address in sorted(self._coordinator.scanner_list):
            scanner = self._coordinator.devices.get(scanner_address)
            if scanner is None:
                continue
            anchor_x = getattr(scanner, "anchor_x_m", None)
            anchor_y = getattr(scanner, "anchor_y_m", None)
            if anchor_x is None or anchor_y is None:
                continue
            geometry[str(scanner_address).lower()] = {
                "x_m": float(anchor_x),
                "y_m": float(anchor_y),
                "z_m": float(getattr(scanner, "anchor_z_m", 0.0) or 0.0),
            }
        return geometry

    def get_layout_mismatch_summary(self) -> dict[str, Any] | None:
        """Describe a sample/layout mismatch that requires user confirmation."""
        samples = self.samples()
        if not samples:
            return None

        current_geometry = self.current_anchor_geometry()
        if not current_geometry:
            return None

        current_layout_hash = self.current_anchor_layout_hash
        if current_layout_hash in self.acknowledged_layout_hashes:
            return None

        current_layout_samples = [
            sample for sample in samples if sample.get("anchor_layout_hash") == current_layout_hash
        ]
        if current_layout_samples:
            return None

        by_layout: dict[str, int] = {}
        for sample in samples:
            layout_hash = str(sample.get("anchor_layout_hash") or "unknown")
            by_layout[layout_hash] = by_layout.get(layout_hash, 0) + 1
        dominant_layout_hash, dominant_layout_count = max(by_layout.items(), key=lambda row: row[1])
        representative_sample = next(
            (sample for sample in samples if str(sample.get("anchor_layout_hash") or "unknown") == dominant_layout_hash),
            samples[0],
        )

        changed_anchor_lines: list[str] = []
        for scanner_address, anchor in sorted((representative_sample.get("anchors") or {}).items()):
            current_anchor = current_geometry.get(str(scanner_address).lower())
            sample_position = anchor.get("anchor_position") or {}
            sample_x = sample_position.get("x_m")
            sample_y = sample_position.get("y_m")
            sample_z = sample_position.get("z_m")
            if current_anchor is None or sample_x is None or sample_y is None or sample_z is None:
                continue
            delta_m = math.sqrt(
                ((float(current_anchor["x_m"]) - float(sample_x)) ** 2)
                + ((float(current_anchor["y_m"]) - float(sample_y)) ** 2)
                + ((float(current_anchor["z_m"]) - float(sample_z)) ** 2)
            )
            if delta_m < 0.01:
                continue
            changed_anchor_lines.append(
                f"- {anchor.get('scanner_name') or scanner_address}: moved {delta_m:.2f} m"
            )

        if not changed_anchor_lines:
            changed_anchor_lines.append("- No direct coordinate delta detected; layout fingerprint changed.")

        return {
            "sample_count": len(samples),
            "current_layout_hash": current_layout_hash,
            "dominant_layout_hash": dominant_layout_hash,
            "dominant_layout_count": dominant_layout_count,
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
        """Store or merge a Bermuda-native transition sample."""
        await self._store.async_ensure_loaded()
        if sample_radius_m <= 0:
            raise HomeAssistantError("Transition sample radius must be greater than 0 metres.")
        if capture_duration_s < 1:
            raise HomeAssistantError("Transition capture duration must be at least 1 second.")

        device = self._resolve_device_from_registry_id(device_id)
        if device is None:
            raise HomeAssistantError("Selected device is not currently available in Bermuda.")

        area = self._coordinator.ar.async_get_area(room_area_id)
        if area is None:
            raise HomeAssistantError("Selected room area does not exist.")
        if area.floor_id is None:
            raise HomeAssistantError("Transition samples require the room area to belong to a floor.")

        cleaned_name = str(transition_name).strip()
        if not cleaned_name:
            raise HomeAssistantError("transition_name must not be empty.")

        cleaned_floor_ids = self._normalize_transition_floor_ids(
            transition_floor_ids=transition_floor_ids,
            room_floor_id=area.floor_id,
        )
        if not cleaned_floor_ids:
            raise HomeAssistantError("transition_floor_ids must include at least one floor other than the room floor.")

        created_at = now().isoformat()
        layout_hash = self.current_anchor_layout_hash
        transition_key = self._derive_transition_key(
            room_area_id=room_area_id,
            transition_name=cleaned_name,
            anchor_layout_hash=layout_hash,
        )
        transition_samples = self.transition_samples()
        existing = next((sample for sample in transition_samples if sample.get("transition_key") == transition_key), None)
        merged = existing is not None

        if existing is None:
            stored = {
                "transition_key": transition_key,
                "created_at": created_at,
                "updated_at": created_at,
                "device_id": device_id,
                "device_name": device.name,
                "device_address": device.address,
                "room_area_id": room_area_id,
                "room_name": area.name,
                "room_floor_id": area.floor_id,
                "transition_name": cleaned_name,
                "position": {"x_m": float(x_m), "y_m": float(y_m), "z_m": float(z_m)},
                "sample_radius_m": float(sample_radius_m),
                "transition_floor_ids": cleaned_floor_ids,
                "anchor_layout_hash": layout_hash,
                "capture_count": 1,
                "last_capture_duration_s": int(capture_duration_s),
                "total_capture_duration_s": int(capture_duration_s),
            }
            transition_samples.append(stored)
        else:
            capture_count = max(int(existing.get("capture_count", 1)), 1)
            new_capture_count = capture_count + 1
            position = existing.get("position") or {}
            existing["position"] = {
                "x_m": round(((float(position.get("x_m", x_m)) * capture_count) + float(x_m)) / new_capture_count, 6),
                "y_m": round(((float(position.get("y_m", y_m)) * capture_count) + float(y_m)) / new_capture_count, 6),
                "z_m": round(((float(position.get("z_m", z_m)) * capture_count) + float(z_m)) / new_capture_count, 6),
            }
            existing["updated_at"] = created_at
            existing["device_id"] = device_id
            existing["device_name"] = device.name
            existing["device_address"] = device.address
            existing["room_name"] = area.name
            existing["room_floor_id"] = area.floor_id
            existing["sample_radius_m"] = max(float(existing.get("sample_radius_m", 0.0)), float(sample_radius_m))
            existing["transition_floor_ids"] = sorted(
                {*(existing.get("transition_floor_ids") or []), *cleaned_floor_ids}
            )
            existing["capture_count"] = new_capture_count
            existing["last_capture_duration_s"] = int(capture_duration_s)
            existing["total_capture_duration_s"] = int(existing.get("total_capture_duration_s", 0)) + int(
                capture_duration_s
            )
            stored = existing

        await self._store.async_replace_transition_samples(transition_samples)

        return {
            "created_at": stored["created_at"],
            "updated_at": stored["updated_at"],
            "merged": merged,
            "device_id": device_id,
            "room_area_id": room_area_id,
            "room_name": area.name,
            "room_floor_id": area.floor_id,
            "transition_name": cleaned_name,
            "x_m": float(stored["position"]["x_m"]),
            "y_m": float(stored["position"]["y_m"]),
            "z_m": float(stored["position"]["z_m"]),
            "sample_radius_m": float(stored["sample_radius_m"]),
            "capture_duration_s": int(capture_duration_s),
            "capture_count": int(stored["capture_count"]),
            "transition_floor_ids": list(stored["transition_floor_ids"]),
            "anchor_layout_hash": layout_hash,
        }

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
            raise HomeAssistantError("Calibration duration must be at least 1 second.")
        if sample_radius_m <= 0:
            raise HomeAssistantError("Calibration sample radius must be greater than 0 metres.")

        device = self._resolve_device_from_registry_id(device_id)
        if device is None:
            raise HomeAssistantError("Selected device is not currently available in Bermuda.")

        if any(session.device_id == device_id for session in self._sessions.values()):
            raise HomeAssistantError("A calibration session is already running for that device.")

        area = self._coordinator.ar.async_get_area(room_area_id)
        if area is None:
            raise HomeAssistantError("Selected room area does not exist.")

        started_dt = now()
        session = _CalibrationSession(
            session_id=f"cal_{uuid4().hex[:12]}",
            started_at=started_dt.isoformat(),
            started_monotonic=monotonic_time_coarse(),
            duration_s=duration_s,
            device_id=device_id,
            device_name=device.name,
            device_address=device.address,
            room_area_id=room_area_id,
            room_name=area.name,
            position={"x_m": float(x_m), "y_m": float(y_m), "z_m": float(z_m)},
            sample_radius_m=float(sample_radius_m),
            notes=notes,
        )
        self._sessions[session.session_id] = session
        self._update_session_notification(
            session,
            status="started",
            expected_complete_at=(started_dt + timedelta(seconds=duration_s)).isoformat(),
        )
        task = asyncio.create_task(self._async_wait_and_finalize(session.session_id))
        self._session_tasks[session.session_id] = task
        task.add_done_callback(lambda _task, session_id=session.session_id: self._session_tasks.pop(session_id, None))
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
            "expected_complete_at": (started_dt + timedelta(seconds=duration_s)).isoformat(),
        }

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
        """Adopt the current anchor geometry for all stored samples."""
        await self._store.async_ensure_loaded()
        samples = self.samples()
        if not samples:
            return 0

        current_geometry = self.current_anchor_geometry()
        current_layout_hash = self.current_anchor_layout_hash
        updated = 0

        for sample in samples:
            sample_changed = False
            anchors = sample.get("anchors") or {}
            for scanner_address, anchor in anchors.items():
                current_anchor = current_geometry.get(str(scanner_address).lower())
                if current_anchor is None:
                    continue
                anchor_position = anchor.get("anchor_position") or {}
                replacement = {
                    "x_m": current_anchor["x_m"],
                    "y_m": current_anchor["y_m"],
                    "z_m": current_anchor["z_m"],
                }
                if anchor_position != replacement:
                    anchor["anchor_position"] = replacement
                    sample_changed = True
            if sample.get("anchor_layout_hash") != current_layout_hash:
                sample["anchor_layout_hash"] = current_layout_hash
                sample_changed = True
            if sample_changed:
                updated += 1

        if updated == 0:
            return 0

        await self._store.async_replace_samples(samples)
        await self._store.async_forget_layout_hash(current_layout_hash)
        await self._async_notify_changed()
        return updated

    def _record_observation(
        self,
        session: _CalibrationSession,
        advert: BermudaAdvert,
        observed_at: str,
        nowstamp: float,
    ) -> None:
        """Add one snapshot observation for an active session."""
        offset_s = min(max(int(nowstamp - session.started_monotonic), 0), max(session.duration_s - 1, 0))
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
        accumulator.buckets.setdefault(offset_s, []).append(value)
        if accumulator.first_seen_at is None:
            accumulator.first_seen_at = observed_at
        accumulator.last_seen_at = observed_at

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
            self._emit_completion_event(
                session=session,
                sample_id=None,
                quality_status="failed",
                quality_reason=failure_reason,
            )
            return

        sample = self._build_sample(session)
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

    def _build_sample(self, session: _CalibrationSession) -> dict[str, Any]:
        """Build the persisted sample payload for a completed session."""
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
                "buckets_1s": [
                    {
                        "offset_s": offset_s,
                        "rssi": round(statistics.median(values), 3),
                    }
                    for offset_s, values in sorted(accumulator.buckets.items())
                ],
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
            (0.35 * anchor_score)
            + (0.25 * packet_score)
            + (0.20 * stability_score)
            + (0.20 * geometry_quality_01),
            3,
        )
        quality_level = self._quality_level_from_metrics(
            quality_status=quality_status,
            quality_score_01=quality_score_01,
            eligible_anchor_count=eligible_anchor_count,
        )

        return {
            "id": f"sample_{uuid4().hex[:12]}",
            "created_at": created_at,
            "started_at": session.started_at,
            "duration_s": session.duration_s,
            "device_id": session.device_id,
            "device_name": session.device_name,
            "device_address": session.device_address,
            "room_area_id": session.room_area_id,
            "room_name": session.room_name,
            "position": deepcopy(session.position),
            "sample_radius_m": session.sample_radius_m,
            "anchor_layout_hash": self.current_anchor_layout_hash,
            "notes": session.notes,
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
        layout_samples = [sample for sample in all_samples if sample.get("anchor_layout_hash") == layout_hash]
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
            transition_floor_ids = [str(floor_id) for floor_id in (sample.get("transition_floor_ids") or []) if floor_id]
            room_context_match = room_area_id == sample_room_area_id if room_area_id is not None else False
            supports_challenger = (
                challenger_floor_id in transition_floor_ids if challenger_floor_id is not None else False
            )
            if room_context_match:
                diagnostics["transition_matching_room_count"] = int(diagnostics["transition_matching_room_count"]) + 1
            if supports_challenger:
                diagnostics["transition_supporting_floor_count"] = int(
                    diagnostics["transition_supporting_floor_count"]
                ) + 1

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

            if (
                support_01 > best_support_01
                or (
                    math.isclose(support_01, best_support_01)
                    and distance_m is not None
                    and (best_distance_m is None or distance_m < best_distance_m)
                )
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

    @staticmethod
    def _derive_transition_key(*, room_area_id: str, transition_name: str, anchor_layout_hash: str) -> str:
        """Return the internal stable key for a transition point."""
        normalized_name = re.sub(r"[^a-z0-9]+", "_", transition_name.strip().lower()).strip("_")
        raw_key = f"{room_area_id}\x1f{normalized_name}\x1f{anchor_layout_hash}"
        return f"transition_{hashlib.sha256(raw_key.encode('utf-8')).hexdigest()[:16]}"

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
                raise HomeAssistantError(f"Transition floor '{candidate}' does not exist.")
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

    def _emit_completion_event(
        self,
        *,
        session: _CalibrationSession,
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
        session: _CalibrationSession,
        *,
        status: str,
        expected_complete_at: str | None = None,
        sample_id: str | None = None,
        quality_reason: str | None = None,
    ) -> None:
        """Create or update the persistent notification for one calibration session."""
        title = "Bermuda calibration sample"
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
            notification_id=f"bermuda_calibration_{session.session_id}",
        )
