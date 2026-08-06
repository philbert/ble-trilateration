"""
Decision frames and the ground-truth label contract.

Two separate things live here, and keeping them separate is the point.

A **decision frame** is what the system saw and concluded on one cycle: the
layout epoch, each source's scores and quality, the reliabilities in force, the
committed state before and after, and the versions of everything involved. It
makes an adaptive decision reconstructible after the fact, which fixed weights
never needed and adaptive weights cannot do without.

A **label** is independent evidence of where a device actually was. Frames are
predictions; labels are truth. Conflating them would let the system train its own
reliability on its own output, which is the failure mode this whole architecture
was built to remove - so `LabelStore` refuses any label whose provenance is the
system itself.

Labels are also grouped into **episodes**. A stationary device emitting a frame
every second for an hour has not produced 3600 independent confirmations; it has
produced one, observed 3600 times. Counting cycles as samples would make a single
unmoved bin look like overwhelming statistical evidence.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

DECISION_FRAME_VERSION = 1


class LabelProvenance(str, Enum):
    """
    Where a ground-truth label came from.

    Only sources a human explicitly asserted are admissible. Registry assignments
    are a suggestion until confirmed, because a user may well have set the area
    from what this integration reported - which would make the label a copy of
    the prediction.
    """

    CALIBRATION_CAPTURE = "calibration_capture"
    DESIGNATED_REFERENCE = "designated_reference"
    MANUAL_WINDOW = "manual_window"
    CONFIRMED_REGISTRY = "confirmed_registry"


# Provenances that would let the system label its own output. Never admissible.
FORBIDDEN_PROVENANCE = frozenset(
    {
        "source_consensus",
        "committed_floor",
        "prediction_stability",
        "integration_output",
        "unconfirmed_registry",
    }
)


class InvalidLabelProvenanceError(ValueError):
    """Raised when a label's provenance is the system's own conclusions."""


@dataclass(frozen=True)
class GroundTruthLabel:
    """One independently asserted observation of where a device was."""

    device_address: str
    floor_id: str | None
    area_id: str | None
    provenance: LabelProvenance
    # Episode key: repeated frames from one unmoved device share one key, so they
    # weigh as a single observation however long the device sits still.
    episode_id: str
    layout_epoch: str
    valid_from: str
    valid_until: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        """Reject any provenance that traces back to the integration's own output."""
        if str(self.provenance) in FORBIDDEN_PROVENANCE:
            msg = f"label provenance {self.provenance!r} is derived from system output, not independent truth"
            raise InvalidLabelProvenanceError(msg)

    def covers(self, timestamp: str) -> bool:
        """True when this label asserts truth at the given ISO timestamp."""
        if timestamp < self.valid_from:
            return False
        return not (self.valid_until is not None and timestamp > self.valid_until)


@dataclass(frozen=True)
class DecisionFrame:
    """Everything needed to reconstruct one floor or room decision."""

    device_address: str
    recorded_at: str
    layout_epoch: str
    decision_kind: str  # "floor" or "room"
    # Per-source diagnostics, already JSON-safe (EvidenceResult.as_diagnostics()).
    source_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    fused_scores: dict[str, float] = field(default_factory=dict)
    fused_choice: str | None = None
    fused_confidence: float = 0.0
    published_choice: str | None = None
    committed_before: str | None = None
    committed_after: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    algorithm_versions: dict[str, int] = field(default_factory=dict)
    frame_version: int = DECISION_FRAME_VERSION

    def as_diagnostics(self) -> dict[str, Any]:
        """Return the frame as a plain dict for diagnostics dumps and replay."""
        return {
            "device_address": self.device_address,
            "recorded_at": self.recorded_at,
            "layout_epoch": self.layout_epoch,
            "decision_kind": self.decision_kind,
            "source_results": self.source_results,
            "fused_scores": {key: round(value, 6) for key, value in self.fused_scores.items()},
            "fused_choice": self.fused_choice,
            "fused_confidence": round(self.fused_confidence, 4),
            "published_choice": self.published_choice,
            "committed_before": self.committed_before,
            "committed_after": self.committed_after,
            "overrides": self.overrides,
            "algorithm_versions": self.algorithm_versions,
            "frame_version": self.frame_version,
        }


class DecisionFrameRecorder:
    """
    Rolling buffer of recent decision frames, per device.

    Bounded on purpose: this is a diagnostic window for answering "why did it
    decide that", not a history store. Frames are cheap to produce and expensive
    to keep, so the buffer holds the recent past and nothing more.
    """

    def __init__(self, max_frames_per_device: int = 32) -> None:
        """Create an empty recorder."""
        self._max = max(1, int(max_frames_per_device))
        self._frames: dict[str, list[DecisionFrame]] = defaultdict(list)

    def record(self, frame: DecisionFrame) -> None:
        """Store one frame, evicting the oldest for that device when full."""
        frames = self._frames[frame.device_address]
        frames.append(frame)
        if len(frames) > self._max:
            del frames[: len(frames) - self._max]

    def frames_for(self, device_address: str) -> list[DecisionFrame]:
        """Return recent frames for one device, oldest first."""
        return list(self._frames.get(device_address, ()))

    def latest(self, device_address: str) -> DecisionFrame | None:
        """Return the most recent frame for one device."""
        frames = self._frames.get(device_address)
        return frames[-1] if frames else None

    def clear(self, device_address: str | None = None) -> None:
        """Drop frames for one device, or all of them."""
        if device_address is None:
            self._frames.clear()
        else:
            self._frames.pop(device_address, None)

    def as_diagnostics(self, limit_per_device: int = 3) -> dict[str, list[dict[str, Any]]]:
        """Return the tail of each device's frames for the diagnostics dump."""
        return {
            address: [frame.as_diagnostics() for frame in frames[-limit_per_device:]]
            for address, frames in self._frames.items()
            if frames
        }


class LabelStore:
    """Independently asserted ground truth, grouped into episodes."""

    def __init__(self) -> None:
        """Create an empty label store."""
        self._labels: list[GroundTruthLabel] = []

    def add(self, label: GroundTruthLabel) -> None:
        """Record one label. Provenance is validated by GroundTruthLabel itself."""
        self._labels.append(label)

    def labels_for(self, device_address: str, timestamp: str) -> list[GroundTruthLabel]:
        """Return every label asserting truth for one device at one time."""
        return [label for label in self._labels if label.device_address == device_address and label.covers(timestamp)]

    def episode_count(self, *, layout_epoch: str | None = None) -> int:
        """
        Return the number of independent episodes, not the number of labels.

        This is the number that governs whether empirical calibration has enough
        evidence to mean anything.
        """
        episodes = {
            label.episode_id for label in self._labels if layout_epoch is None or label.layout_epoch == layout_epoch
        }
        return len(episodes)

    def as_diagnostics(self) -> dict[str, Any]:
        """Return a JSON-safe summary of what truth is available."""
        by_provenance: dict[str, int] = defaultdict(int)
        for label in self._labels:
            by_provenance[label.provenance.value] += 1
        return {
            "label_count": len(self._labels),
            "episode_count": self.episode_count(),
            "by_provenance": dict(by_provenance),
        }
