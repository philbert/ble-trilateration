"""Tests for decision frames and the ground-truth label contract."""

from __future__ import annotations

import pytest

from custom_components.ble_trilateration.decision_record import (
    DecisionFrame,
    DecisionFrameRecorder,
    GroundTruthLabel,
    InvalidLabelProvenanceError,
    LabelProvenance,
    LabelStore,
)


def _frame(address="dev-a", recorded_at="2026-08-06T12:00:00+00:00", **kwargs) -> DecisionFrame:
    base = {
        "device_address": address,
        "recorded_at": recorded_at,
        "layout_epoch": "epoch-1",
        "decision_kind": "floor",
        "fused_scores": {"street_level": 0.6, "ground_floor": 0.4},
        "fused_choice": "street_level",
        "fused_confidence": 0.55,
        "published_choice": "street_level",
        "committed_before": "ground_floor",
        "committed_after": "ground_floor",
    }
    base.update(kwargs)
    return DecisionFrame(**base)


def _label(**kwargs) -> GroundTruthLabel:
    base = {
        "device_address": "dev-a",
        "floor_id": "street_level",
        "area_id": "garage_front",
        "provenance": LabelProvenance.DESIGNATED_REFERENCE,
        "episode_id": "bin-parked-1",
        "layout_epoch": "epoch-1",
        "valid_from": "2026-08-06T00:00:00+00:00",
    }
    base.update(kwargs)
    return GroundTruthLabel(**base)


def test_frame_records_both_published_and_committed_state():
    """The split is the whole point: a published guess need not move the commit."""
    frame = _frame()
    dumped = frame.as_diagnostics()

    assert dumped["published_choice"] == "street_level"
    assert dumped["committed_before"] == "ground_floor"
    assert dumped["committed_after"] == "ground_floor"


def test_recorder_keeps_a_bounded_window_per_device():
    """A diagnostic window, not a history store."""
    recorder = DecisionFrameRecorder(max_frames_per_device=3)
    for index in range(6):
        recorder.record(_frame(recorded_at=f"2026-08-06T12:00:0{index}+00:00"))

    frames = recorder.frames_for("dev-a")
    assert len(frames) == 3
    assert frames[0].recorded_at.endswith("03+00:00")
    assert recorder.latest("dev-a").recorded_at.endswith("05+00:00")


def test_recorder_separates_devices():
    """One device's history must not evict another's."""
    recorder = DecisionFrameRecorder(max_frames_per_device=2)
    recorder.record(_frame(address="dev-a"))
    recorder.record(_frame(address="dev-b"))

    assert len(recorder.frames_for("dev-a")) == 1
    assert len(recorder.frames_for("dev-b")) == 1


@pytest.mark.parametrize(
    "forbidden",
    ["source_consensus", "committed_floor", "prediction_stability", "integration_output", "unconfirmed_registry"],
)
def test_labels_derived_from_system_output_are_rejected(forbidden):
    """Truth may not be manufactured from the prediction being evaluated."""
    with pytest.raises(InvalidLabelProvenanceError):
        _label(provenance=forbidden)


@pytest.mark.parametrize(
    "provenance",
    [
        LabelProvenance.CALIBRATION_CAPTURE,
        LabelProvenance.DESIGNATED_REFERENCE,
        LabelProvenance.MANUAL_WINDOW,
        LabelProvenance.CONFIRMED_REGISTRY,
    ],
)
def test_independently_asserted_provenances_are_accepted(provenance):
    """Every admissible provenance traces back to an explicit human assertion."""
    assert _label(provenance=provenance).provenance is provenance


def test_label_validity_window_is_honoured():
    """A designated reference is only truth while it is actually parked there."""
    label = _label(valid_from="2026-08-06T10:00:00+00:00", valid_until="2026-08-06T14:00:00+00:00")

    assert label.covers("2026-08-06T12:00:00+00:00") is True
    assert label.covers("2026-08-06T09:00:00+00:00") is False
    assert label.covers("2026-08-06T15:00:00+00:00") is False


def test_repeated_cycles_of_one_parked_device_count_as_one_episode():
    """3600 frames from an unmoved bin are one observation, not 3600."""
    store = LabelStore()
    for index in range(50):
        store.add(_label(valid_from=f"2026-08-06T12:{index:02d}:00+00:00", episode_id="bin-parked-1"))

    assert store.episode_count() == 1


def test_distinct_episodes_are_counted_separately():
    """Independent placements are what actually accumulate statistical evidence."""
    store = LabelStore()
    store.add(_label(episode_id="bin-parked-1"))
    store.add(_label(episode_id="bin-parked-2", device_address="dev-b"))
    store.add(_label(episode_id="phone-walk-1", device_address="dev-c"))

    assert store.episode_count() == 3


def test_episode_count_can_be_scoped_to_a_layout_epoch():
    """Evidence gathered under a different anchor layout is not evidence for this one."""
    store = LabelStore()
    store.add(_label(episode_id="e1", layout_epoch="epoch-1"))
    store.add(_label(episode_id="e2", layout_epoch="epoch-2"))

    assert store.episode_count(layout_epoch="epoch-1") == 1
    assert store.episode_count() == 2


def test_labels_are_matched_by_device_and_time():
    """Lookup must not leak one device's truth into another's evaluation."""
    store = LabelStore()
    store.add(_label(device_address="dev-a"))
    store.add(_label(device_address="dev-b", floor_id="ground_floor"))

    matched = store.labels_for("dev-b", "2026-08-06T12:00:00+00:00")
    assert len(matched) == 1
    assert matched[0].floor_id == "ground_floor"
