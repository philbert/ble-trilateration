# Calibration Sample Capture And Management Plan

## 1. Scope

This document defines a non-breaking first phase for Bermuda calibration work:

- record labeled calibration samples
- persist and manage those samples
- expose sample capture through a Home Assistant action/service
- expose sample management through the Bermuda config flow

This phase does **not**:

- change existing area resolution
- change current trilateration results
- use calibration samples to determine device location
- remove legacy calibration/config options yet

The goal is to land the data-collection and management foundation first, without affecting current users.

## 2. User Model

The intended user workflow is simple:

1. Scanner anchors have known `x/y/z` coordinates.
2. The user moves a calibration device to a known point in the home.
3. The user declares:
   - room
   - `x`
   - `y`
   - `z`
4. The user triggers a Bermuda action to record a 60-second sample.
5. Bermuda stores the captured observations as a calibration sample.
6. The user later reviews, deletes, or organizes samples in the Bermuda config flow.

The user is not asked to:

- manually tune attenuation
- manually tune max distance
- manually calibrate individual scanners
- understand scanner-specific or device-specific correction terms

## 3. Separation Of Concerns

The system should be split into two parts.

### 3.1 Sample capture

Sample capture is the operational path. It should be implemented as a Bermuda action/service so it can be:

- run manually from Home Assistant
- called from a script or automation
- reused later by other UI surfaces

This is the only mechanism that actually records a calibration sample.

### 3.2 Sample management

Sample management is the administrative path. It should be implemented in the Bermuda config flow so users can:

- review saved samples
- inspect sample quality
- delete individual samples
- clear sample sets
- see whether samples belong to the current anchor layout

The config flow should manage samples, not perform the timed recording itself.

## 4. Why Not Use Config Flow For Recording

Config flow is usable for collecting a few values and stepping a user through a short process, but it is a poor fit for the actual sample recorder.

Reasons:

- a 60-second timed capture is better represented as an action than a configuration form
- the capture path should be reusable from scripts/automations
- repeated field collection of many samples is awkward inside `Settings -> Devices & Services -> Configure`
- recorded samples are operational data, not configuration

Config flow remains appropriate for management and review.

## 5. Phase 1 Entry Points

### 5.1 Action/service for recording

Add a new Bermuda service, tentatively:

- `bermuda.record_calibration_sample`

Required fields:

- `device_id`
- `room_area_id`
- `x_m`
- `y_m`
- `z_m`

Optional fields:

- `duration_s` default `60`
- `notes`

Expected behavior:

1. Validate inputs.
2. Start a capture session for the selected device.
3. Collect scanner observations for the requested duration.
4. Aggregate observations into a sample.
5. Evaluate sample quality.
6. Persist the sample.
7. Return structured result data.

The service should work even if the user invokes it manually from a script or Developer Tools.

### 5.2 Config flow for management

Add a new top-level Bermuda config flow menu entry:

- `Calibration Samples`

Suggested sub-steps:

- sample summary
- recent samples
- delete sample
- clear all samples for current anchor layout
- clear all samples for a device

Phase 1 config flow should not attempt to derive a calibration model or modify runtime location logic.

## 6. Sample Session Model

Each capture session is a temporary in-memory object owned by the coordinator or a dedicated calibration manager.

Responsibilities:

- track the target device
- store session metadata
- gather observations during the time window
- reject overlapping sessions for the same device
- finalize to a persistent sample record

Phase 1 should support one active session per target device. It is acceptable to support only one active global session initially if that simplifies implementation.

## 7. What To Capture

For each sample, Bermuda should store:

- sample identity and timestamps
- selected device
- declared room
- declared `x/y/z`
- the anchor-layout identity at capture time
- aggregated per-anchor observations
- sample quality result

Room is required because the eventual system resolves to a room name.

Coordinates are required because they are the physical truth used later for model fitting.

## 8. Recommended Stored Shape

Store sample records in Bermuda-owned persistent storage, not config entry options.

Recommended logical structure:

```json
{
  "id": "sample_20260306_192201_abcd",
  "created_at": "2026-03-06T19:22:01Z",
  "duration_s": 60,
  "device_id": "device_registry_id",
  "device_name": "Phil Phone",
  "room_area_id": "living_room",
  "room_name": "Living Room",
  "position": {
    "x_m": 4.2,
    "y_m": 1.8,
    "z_m": 1.1
  },
  "anchor_layout_hash": "7f2b2ef3...",
  "notes": "optional",
  "anchors": {
    "AA:BB:CC:DD:EE:FF": {
      "scanner_name": "Living room proxy",
      "packet_count": 84,
      "rssi_median": -71.5,
      "rssi_mean": -71.2,
      "rssi_mad": 2.1,
      "rssi_min": -77,
      "rssi_max": -67,
      "first_seen_at": "2026-03-06T19:22:03Z",
      "last_seen_at": "2026-03-06T19:23:00Z",
      "buckets_1s": [
        { "offset_s": 0, "count": 2, "rssi_median": -72.0 },
        { "offset_s": 1, "count": 1, "rssi_median": -71.0 }
      ]
    }
  },
  "quality": {
    "status": "accepted",
    "eligible_anchor_count": 4,
    "reason": null
  }
}
```

Notes:

- `room_area_id` is the canonical room key
- `room_name` is a display snapshot
- 1-second buckets are a useful compromise between raw packet storage and summary-only storage
- per-anchor summaries should be sufficient for phase 1 and still useful later

## 9. Storage And Persistence

Use `homeassistant.helpers.storage.Store`.

Phase 1 should add a dedicated calibration storage module, for example:

- `custom_components/bermuda/calibration_store.py`

Recommended stores:

- `calibration_samples`
- optionally later `calibration_model`

In this phase, only `calibration_samples` is needed.

Samples must not be stored in:

- config entry options
- entity state attributes
- Home Assistant recorder tables

Reason:

- samples are Bermuda-owned operational data
- sample history may grow
- the data should be easy to version and migrate independently

## 10. Anchor Layout Tracking

Each sample should be stamped with an `anchor_layout_hash`.

The hash should be derived from the current enabled anchors and their coordinates:

- scanner identity
- anchor enabled state
- `x`
- `y`
- `z`

This enables future handling of moved anchors without deleting history.

Phase 1 behavior:

- capture and store the hash
- show it in sample management
- do not yet use it to invalidate runtime behavior

This creates a clean foundation for later model invalidation.

## 11. Quality Checks

Phase 1 should include simple and conservative sample quality checks.

Suggested checks:

- minimum number of visible anchors
- minimum packets from at least one anchor
- session duration actually completed
- sample not empty

Each saved sample should be marked as either:

- `accepted`
- `poor_quality`
- `rejected`

Phase 1 may either:

- persist only accepted and poor-quality samples, or
- persist all attempted samples with status

Preferred approach: persist accepted and poor-quality samples, but not empty/rejected sessions.

## 12. Config Flow Management UX

The Bermuda config flow should gain a new management section without disturbing current setup behavior.

Suggested top-level menu addition:

- `Calibration Samples`

Suggested information shown in that section:

- total sample count
- sample count by room
- sample count by device
- sample count for current anchor layout
- most recent samples

Suggested actions:

- delete one sample
- clear samples for a device
- clear samples for current anchor layout
- clear all samples

This is management only. No runtime location logic changes should happen here.

## 13. Automation And Script Usage

The service-based recorder should be easy to call from an automation or script.

Example use cases:

- a manual script the user launches from Home Assistant
- a voice assistant script that prompts the user, then starts recording
- a future guided workflow layered on top of the same service

This flexibility is the primary reason the recorder should be a service/action instead of a config-flow-only feature.

## 14. Non-Breaking Constraints

This phase must preserve current behavior.

Specifically:

- do not remove legacy calibration options yet
- do not change the current distance or area pipelines
- do not change trilateration output
- do not change entity names or meanings
- do not make calibration samples required for normal operation

The new feature should be additive only.

## 15. Suggested Implementation Order

1. Add a calibration storage manager.
2. Add a calibration session recorder.
3. Add `bermuda.record_calibration_sample`.
4. Persist finalized samples with quality metadata.
5. Add config flow sample-management views.
6. Add tests for storage, capture, and deletion flows.

## 16. Test Scope For This Phase

Tests should cover:

- service validation
- session start/finish lifecycle
- observation aggregation
- quality classification
- persistent sample save/load
- sample deletion
- anchor layout hash generation
- config flow management steps

Tests should explicitly confirm:

- no effect on current area determination
- no effect on current trilateration outputs
- no config-entry migration required

## 17. Deferred Work

The following is intentionally out of scope for this phase:

- deriving a fitted calibration model
- using samples in trilateration solves
- using samples to map coordinates to rooms
- replacing or removing legacy configuration paths
- adding a Lovelace UI

Those can be built on top of this storage and capture foundation in a later phase.
