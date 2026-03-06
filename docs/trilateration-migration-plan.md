# Bermuda Trilateration Migration Plan

## Goal

Replace the remaining manual RSSI calibration system with a trilateration-first pipeline that learns from saved calibration samples, derives room attribution from solved positions, and infers whether a device is moving or stationary from live signal behavior.

This plan separates:

- code that should be removed now because it is only UI/workflow legacy,
- code that should be removed once the new ranging model exists,
- code that should stay because it is already part of the new sample-driven path.

## Legacy inventory and removal decisions

### Remove now

These are legacy user-facing workflow pieces with no long-term value once `Select Devices` and `Calibration Samples` remain as the only options-flow surfaces.

- `custom_components/bermuda/config_flow.py`
  - Remove menu entries for `globalopts`, `calibration1_global`, and `calibration2_scanners`.
  - Remove `async_step_globalopts`.
  - Remove `async_step_calibration1_global`.
  - Remove `async_step_calibration2_scanners`.
  - Remove temporary state used only by those steps:
    - `_last_ref_power`
    - `_last_scanner`
    - `_last_attenuation`
    - `_last_scanner_info`
  - Remove `_get_bermuda_device_from_registry()` from the options flow if it is no longer used elsewhere.

- `custom_components/bermuda/const.py`
  - Remove constants used only by the deleted options-flow steps:
    - `CONF_SAVE_AND_CLOSE`
    - `CONF_SCANNER_INFO`
    - `CONF_SCANNERS`

- `custom_components/bermuda/translations/en.json`
- `custom_components/bermuda/translations/el.json`
- `custom_components/bermuda/translations/ko.json`
- `custom_components/bermuda/translations/nl.json`
- `custom_components/bermuda/translations/pt.json`
  - Remove translation blocks for:
    - `globalopts`
    - `calibration1_global`
    - `calibration2_scanners`
  - Remove field/help text that only exists for those steps.

- `tests/test_config_flow.py`
  - Replace the current options-flow test that drives `globalopts`.
  - New options-flow coverage should only validate the slimmed menu and the remaining `selectdevices` / `calibration_samples` paths.

### Remove when the sample-driven ranging model lands

These are the actual manual-calibration mechanics. I agree with removing them, but not before the new estimator is wired in because they currently produce the only live range input used by both area logic and trilateration.

- `custom_components/bermuda/number.py`
  - Remove:
    - `BermudaScannerRSSIOffset`
    - `BermudaScannerAttenuation`
    - `BermudaScannerMaxRadius`
  - Remove scanner-number creation wiring for those entities from `async_setup_entry()`.

- `custom_components/bermuda/const.py`
  - Remove legacy tuning options and defaults:
    - `CONF_RSSI_OFFSETS`
    - `CONF_ATTENUATION`
    - `DEFAULT_ATTENUATION`
    - `CONF_MAX_RADIUS`
    - `DEFAULT_MAX_RADIUS`
    - `CONF_REF_POWER`
    - `DEFAULT_REF_POWER`
  - I would also stop exposing `ref_power` as a calibration concept once the learned model is active. If a per-device override is still needed later, it should come back as an explicit advanced override, not as part of the old calibration model.

- `custom_components/bermuda/coordinator.py`
  - Remove:
    - `get_scanner_rssi_offset()`
    - `get_scanner_attenuation()`
    - `get_scanner_max_radius()`
  - Replace all call sites that use `max_radius` as a hard gate.
  - Remove the area-resolution dependency on nearest-scanner distance gating once room attribution is trilat-first.

- `custom_components/bermuda/bermuda_advert.py`
  - Remove live config fields and reload logic tied to manual calibration:
    - `conf_rssi_offset`
    - `conf_attenuation`
    - `conf_ref_power`
    - `reload_config()` content that reloads those values
  - Replace `_update_raw_distance()` so it calls a learned range estimator instead of:
    - raw RSSI + manual offset
    - `rssi_to_metres(..., ref_power, attenuation)`

- `custom_components/bermuda/util.py`
  - Remove `rssi_to_metres()` once nothing calls it.

- `tests/test_number.py`
  - Delete tests for scanner `rssi_offset`, `attenuation`, and `max_radius` entities.

- `tests/test_bermuda_advert.py`
  - Rewrite fixtures and assertions that depend on:
    - `get_scanner_rssi_offset()`
    - `get_scanner_attenuation()`
    - manual distance conversion behavior

- `tests/test_coordinator_area_resolution.py`
- `tests/test_coordinator_trilateration.py`
  - Rewrite tests that stub `get_scanner_max_radius()` or depend on radius gating as the primary validity check.

### Keep

These are already aligned with the new direction and should remain.

- `custom_components/bermuda/config_flow.py`
  - Keep the slim options-flow landing page.
  - Keep `Select Devices`.
  - Keep `Calibration Samples` summary and sample-management steps.

- `custom_components/bermuda/calibration.py`
- `custom_components/bermuda/calibration_store.py`
- `custom_components/bermuda/services.yaml`
- `tests/test_calibration.py`
  - Keep the sample capture, storage, deletion, and service plumbing.
  - This is the correct foundation for the new system.

- `custom_components/bermuda/select.py`
- `custom_components/bermuda/number.py`
  - Keep anchor controls:
    - `Trilat Anchor Enabled`
    - `Anchor X`
    - `Anchor Y`
    - `Anchor Z`

- `custom_components/bermuda/trilateration.py`
  - Keep the current solver module and extend around it rather than replacing it.

## What should replace the legacy ranging path

### 1. Build a sample-derived ranging model

The current samples already contain enough information to start:

- room label,
- true position `(x, y, z)`,
- anchor positions,
- per-anchor `rssi_median`,
- per-anchor `rssi_mad`,
- 1-second RSSI buckets across the capture window.

For each sample-anchor pair:

1. Compute the true geometric distance from sample position to anchor position.
2. Use `rssi_median` as the stationary observation for that distance.
3. Fit a log-distance model from real data instead of manual knobs.

Start with the simplest useful model:

- global slope/path-loss term,
- global intercept term,
- per-scanner bias term,
- optional per-device bias term when there are enough samples for that device.

That model directly replaces the combined effect of:

- `ref_power`,
- `attenuation`,
- `rssi_offset`.

Recommended implementation shape:

- add `custom_components/bermuda/ranging_model.py`,
- load and fit from `BermudaCalibrationStore`,
- expose one runtime method such as:
  - `estimate_range(scanner_address, device_address, filtered_rssi) -> {range_m, sigma_m, source}`

Important detail:

- models must be keyed by `anchor_layout_hash`,
- because once anchors move, old geometric truth is no longer valid.

### 2. Make room attribution trilat-first, not strongest-scanner-first

Right now room attribution still comes from scanner competition plus distance/radius gates. That is the main legacy behavior I agree with removing.

The replacement should be:

1. estimate ranges from the learned model,
2. solve position from all valid anchors,
3. classify room from solved position,
4. only fall back to floor-only or `Unknown` when confidence is low.

There is no HA-native room geometry in this integration, so the calibration samples should define the room map.

Recommended first classifier:

- for each room, build a labeled point cloud from its calibration samples,
- classify the solved position with k-nearest-neighbor or centroid-plus-radius logic,
- require minimum support and distance margin between the best and second-best rooms,
- return `Unknown` when outside the sampled envelope.

After that works, improve it with:

- per-room covariance / elliptical bounds,
- convex hull or alpha-shape room envelopes,
- confidence penalties when the solve residual is high,
- confidence penalties when the solved point is far from all labeled samples.

This gives a clean migration path:

- phase 1: trilat solves `(x, y, z)`,
- phase 2: samples turn those coordinates into room labels,
- phase 3: the old nearest-scanner area winner path can be deleted.

### 3. Replace `max_radius` with uncertainty-aware validity

`max_radius` is acting as a manual cut-off for weak or implausible ranges. In the new system it should be replaced by measured uncertainty, not by a hand-entered distance cap.

Use:

- RSSI dispersion from live adverts,
- `rssi_mad` from calibration samples,
- solve residual,
- anchor count,
- geometry quality,
- room-classifier margin.

That becomes the basis for:

- whether an anchor participates,
- whether a trilat solution is `ok`, `low_confidence`, or `unknown`,
- whether a room label should be applied or withheld.

## Using calibration samples to improve mobility mode

The existing mobility mode is manual. The sample set can already define what stationary noise looks like, because every calibration sample is explicitly taken from a fixed position.

### Stationary baseline from samples

For each device class or device id, derive stationary envelopes from the captured samples:

- per-anchor RSSI MAD,
- second-to-second RSSI delta from `buckets_1s`,
- expected trilat jitter when solving from those 1-second bucket medians,
- expected room-classifier jitter while position is fixed.

This gives a data-backed definition of stationary behavior in the real environment.

### Automatic mobility inference

Add an inferred mobility classifier using live windows such as the last 10 to 30 seconds.

Good first-pass features:

- solved position displacement over time,
- estimated speed from trilat coordinates,
- anchor-rank churn,
- RSSI variance compared with the stationary baseline,
- repeated room-classifier boundary crossings,
- solve residual spikes.

Recommended behavior:

- `moving` when speed or variance stays above the stationary envelope for a dwell period,
- `stationary` when the solved position and RSSI settle back inside that envelope,
- `unknown` only internally if needed; user-facing state can stay binary at first.

I do not recommend deleting the mobility control outright. I recommend changing it from a hard manual selector to:

- `auto` as the default,
- `stationary` as an override,
- `moving` as an override.

Then:

- the diagnostic sensor should show the effective mode,
- the coordinator policies should consume the effective mode,
- calibration-derived thresholds should drive `auto`.

## Implementation order

### Phase 1: remove dead UI and freeze the new boundary

1. Delete `globalopts`, `calibration1_global`, and `calibration2_scanners`.
2. Keep only `Select Devices` and `Calibration Samples` in the options flow.
3. Remove the translation and tests tied only to the deleted steps.
4. Leave the live manual-ranging internals in place temporarily so behavior does not break during the rest of the migration.

### Phase 2: add the learned ranging model

1. Implement a calibration-sample reader that converts saved samples into training rows.
2. Fit the first global-plus-scanner-bias model.
3. Expose a coordinator-owned ranging-model service object.
4. Add tests with synthetic anchors and synthetic samples to verify learned ranges are closer to truth than the fixed formula.

### Phase 3: switch trilat to the learned model

1. Replace manual range computation in `BermudaAdvert`.
2. Feed the learned range and uncertainty into trilateration.
3. Replace `max_radius` gates with uncertainty and residual gates.
4. Keep the old area resolver only as a temporary fallback during this phase.

### Phase 4: room attribution from solved position

1. Build a room classifier from sample point clouds keyed by `anchor_layout_hash`.
2. Use trilat position as the primary room input.
3. Apply confidence thresholds and return `Unknown` when the solved point is outside trained support.
4. Delete the nearest-scanner area winner path once sample-based room attribution is stable.

### Phase 5: automatic mobility mode

1. Add inferred mobility features and a short rolling classifier.
2. Convert mobility control from manual-only to `auto | stationary | moving`.
3. Tune area/trilat hysteresis against effective mobility, not the user’s static choice.
4. Add regression tests for:
   - stable stationary sample windows,
   - genuine motion,
   - boundary jitter,
   - room transitions.

### Phase 6: delete the old calibration internals

1. Remove `rssi_offset`, `attenuation`, `max_radius`, and `ref_power` plumbing.
2. Remove legacy number entities and constants.
3. Remove the old RSSI-to-distance utility and its tests.
4. Rewrite remaining tests around:
   - learned ranges,
   - trilat confidence,
   - sample-based room attribution,
   - inferred mobility.

## Risks to manage

- Sparse samples can produce overconfident room labels. The classifier must prefer `Unknown` over guessing.
- Anchor moves invalidate prior geometry. Every model and room map must be scoped by `anchor_layout_hash`.
- Some devices will have few or no per-device samples. The model must back off cleanly to global or scanner-level parameters.
- Full deletion of `ref_power` should happen only after the learned model is proven to cover low-sample devices acceptably.

## Bottom line

I agree with removing the old config-flow calibration workflow immediately.

I also agree with removing `max_radius`, `attenuation`, `rssi_offset`, and eventually `ref_power`, but only after the sample-driven range estimator is active, because those settings still provide the live distance input that the current trilat path depends on.

The right replacement is not another manual tuning screen. It is:

- learned range estimation from calibration samples,
- trilat-first positioning,
- room classification from labeled sample coordinates,
- automatic mobility inference from stationary sample baselines plus live motion evidence.
