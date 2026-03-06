"""Calibration sample recording and management helpers."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
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
    DISTANCE_TIMEOUT,
    DOMAIN_PRIVATE_BLE_DEVICE,
)

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

    def samples(self) -> list[dict[str, Any]]:
        """Return stored samples."""
        return self._store.samples

    def get_summary(self) -> dict[str, Any]:
        """Return a small in-memory summary for config flow."""
        samples = self.samples()
        by_room: dict[str, int] = {}
        by_device: dict[str, int] = {}
        current_layout_hash = self.current_anchor_layout_hash
        current_layout_count = 0
        for sample in samples:
            room_name = str(sample.get("room_name") or sample.get("room_area_id") or "Unknown")
            by_room[room_name] = by_room.get(room_name, 0) + 1
            device_name = str(sample.get("device_name") or sample.get("device_id") or "Unknown")
            by_device[device_name] = by_device.get(device_name, 0) + 1
            if sample.get("anchor_layout_hash") == current_layout_hash:
                current_layout_count += 1
        recent = sorted(samples, key=lambda sample: sample.get("created_at", ""), reverse=True)[:5]
        return {
            "sample_count": len(samples),
            "by_room": by_room,
            "by_device": by_device,
            "current_layout_hash": current_layout_hash,
            "current_layout_count": current_layout_count,
            "recent": recent,
            "warn_threshold": CALIBRATION_SAMPLE_WARN_THRESHOLD,
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

    async def async_delete_sample(self, sample_id: str) -> bool:
        """Delete one persisted sample."""
        return await self._store.async_delete_sample(sample_id)

    async def async_clear_all(self) -> int:
        """Delete all persisted samples."""
        return await self._store.async_clear_all()

    async def async_clear_device(self, device_id: str) -> int:
        """Delete all samples for one device."""
        return await self._store.async_clear_device(device_id)

    async def async_clear_current_anchor_layout(self) -> int:
        """Delete samples that match the current anchor layout."""
        return await self._store.async_clear_anchor_layout(self.current_anchor_layout_hash)

    async def async_start_session(
        self,
        *,
        device_id: str,
        room_area_id: str,
        x_m: float,
        y_m: float,
        z_m: float,
        duration_s: int = 60,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Validate and start a calibration sample capture."""
        await self._store.async_ensure_loaded()
        if duration_s < 1:
            raise HomeAssistantError("Calibration duration must be at least 1 second.")

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
            notes=notes,
        )
        self._sessions[session.session_id] = session
        task = asyncio.create_task(self._async_wait_and_finalize(session.session_id))
        self._session_tasks[session.session_id] = task
        task.add_done_callback(lambda _task, session_id=session.session_id: self._session_tasks.pop(session_id, None))
        return {
            "session_id": session.session_id,
            "started_at": session.started_at,
            "device_id": device_id,
            "room_area_id": room_area_id,
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
        anchors: list[tuple[str, bool, float | None, float | None, float | None]] = []
        for scanner_address in sorted(self._coordinator.scanner_list):
            scanner = self._coordinator.devices.get(scanner_address)
            if scanner is None:
                continue
            anchors.append(
                (
                    scanner_address,
                    bool(getattr(scanner, "anchor_enabled", False)),
                    getattr(scanner, "anchor_x_m", None),
                    getattr(scanner, "anchor_y_m", None),
                    getattr(scanner, "anchor_z_m", None),
                )
            )
        encoded = json.dumps(anchors, separators=(",", ":"), sort_keys=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

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
        for scanner_address, accumulator in sorted(session.anchors.items()):
            if not accumulator.values:
                continue
            eligible_anchor_count += 1
            anchors[scanner_address] = {
                "scanner_name": accumulator.scanner_name,
                "anchor_position": deepcopy(accumulator.anchor_position),
                "packet_count": len(accumulator.values),
                "rssi_median": round(statistics.median(accumulator.values), 3),
                "rssi_mean": round(statistics.fmean(accumulator.values), 3),
                "rssi_mad": round(self._median_abs_deviation(accumulator.values), 3),
                "rssi_min": min(accumulator.values),
                "rssi_max": max(accumulator.values),
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
            "anchor_layout_hash": self.current_anchor_layout_hash,
            "notes": session.notes,
            "anchors": anchors,
            "quality": {
                "status": quality_status,
                "eligible_anchor_count": eligible_anchor_count,
                "reason": quality_reason,
            },
        }

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

    @staticmethod
    def _median_abs_deviation(values: list[float]) -> float:
        """Return a simple median absolute deviation for captured values."""
        if not values:
            return 0.0
        median = statistics.median(values)
        return float(statistics.median(abs(value - median) for value in values))

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
        title = "Bermuda calibration sample complete"
        message = (
            f"Device: {session.device_name}\n"
            f"Room: {session.room_name}\n"
            f"Status: {quality_status}\n"
            f"Sample ID: {sample_id or 'not_saved'}"
        )
        if quality_reason is not None:
            message += f"\nReason: {quality_reason}"
        persistent_notification.async_create(
            self.hass,
            message,
            title=title,
            notification_id=f"bermuda_calibration_{session.session_id}",
        )
