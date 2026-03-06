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
  - This foundation is already largely implemented, so the first real migration work is config-flow cleanup rather than new sample-capture plumbing.

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
- optional per-device bias term when there are enough samples for that device (same K ≥ 3 threshold as scanner bias, fitted as additional categorical columns in the same lstsq design matrix as the scanner bias terms).

That model directly replaces the combined effect of:

- `ref_power`,
- `attenuation`,
- `rssi_offset`.

Recommended implementation shape:

- add `custom_components/bermuda/ranging_model.py`,
- load and fit from `BermudaCalibrationStore`,
- fit the initial model with `numpy.linalg.lstsq` against raw `(log10(distance), rssi_median)` rows,
- expose one runtime method such as:
  - `estimate_range(scanner_address, device_address, filtered_rssi) -> {range_m, sigma_m, source}`

The intended first model is still simple:

```text
RSSI = A - 10 * n * log10(d) + scanner_bias
```

That keeps Phase 2 linear in the unknowns and avoids over-engineering the first implementation.

Important detail:

- models must be keyed by `anchor_layout_hash`,
- because once anchors move, old geometric truth is no longer valid.

Minimum fitting thresholds should be explicit constants in `ranging_model.py`:

- do not fit the global model until there are at least 5 distinct `(distance, RSSI)` training pairs,
- do not fit a per-scanner bias term unless that scanner appears in at least 3 samples,
- scanners below the threshold should keep zero bias until enough data exists.

Model rebuild triggers should also be explicit:

- build once at coordinator startup,
- rebuild whenever a sample is added or deleted,
- rebuild whenever the active `anchor_layout_hash` changes.

### How `sigma_m` is derived

`sigma_m` is the model's per-anchor range uncertainty in metres at the current estimated range. It is derived in two steps.

**Step 1 — fit-time: compute per-scanner RSSI RMSE from training residuals.**

After `lstsq` fitting, for each scanner compute the root mean squared error over its training rows:

```
rssi_rmse_scanner = sqrt(mean((rssi_predicted - rssi_observed)²))
```

If a scanner has fewer than the minimum sample threshold, use the global RSSI RMSE across all training rows as its fallback. Store these per-scanner RMSE values in the fitted model object.

**Step 2 — runtime: propagate RSSI uncertainty to range uncertainty.**

The derivative of the log-distance model with respect to distance is:

```
d(RSSI)/d(distance) = -10 * n / (distance * ln(10))
```

Inverting this gives the range uncertainty for a given RSSI uncertainty:

```
sigma_m = sigma_rssi * distance * ln(10) / (10 * n)
```

At runtime, `sigma_rssi` is the per-scanner training RMSE from step 1. This makes `sigma_m` grow with range, which is physically correct: a 3 dB RSSI error at 10 m implies a much larger range error than the same 3 dB error at 1 m.

Optionally, blend with live RSSI dispersion from `BermudaAdvert.rssi_dispersion` if that value is available:

```
sigma_rssi_effective = max(sigma_rssi_model, live_rssi_dispersion)
```

This ensures that a temporarily noisy signal inflates the uncertainty gate even when the model itself was well-fitted.

Transitional fallback behavior must be explicit:

- if there is no fitted model for the current `anchor_layout_hash`, keep using the legacy RSSI-to-distance formula temporarily,
- surface that via `source="legacy_fallback"`,
- clamp resulting trilat confidence to low,
- do not delete `rssi_to_metres()` until this fallback is no longer needed or an explicit no-model `unknown` path is in place.

`sigma_m` should also feed the existing confidence interface directly:

- fold `sigma_m` into the existing trilat confidence calculation alongside residual, anchor count, and solver dimension,
- continue writing the result into `BermudaDevice.trilat_confidence` and `BermudaDevice.trilat_confidence_level`,
- avoid introducing a parallel confidence path.

### 2. Make room attribution trilat-first, not strongest-scanner-first

Right now room attribution still comes from scanner competition plus distance/radius gates. That is the main legacy behavior I agree with removing.

The replacement should be:

1. estimate ranges from the learned model,
2. solve position from all valid anchors,
3. classify room from solved position,
4. only fall back to floor-only or `Unknown` when confidence is low.

There is no HA-native room geometry in this integration, so the calibration samples should define the room map.

Recommended first classifier:

- filter candidate rooms to the already-resolved trilat floor,
- treat room geometry as learned only from calibration samples for the current `anchor_layout_hash`,
- require a minimum of 3 samples before a room is considered trained,
- compute one centroid `(x, y, z)` and one support radius per trained room,
- define support radius as the max centroid-to-sample distance plus a small slack margin,
- classify the solved point by nearest centroid among rooms whose support radius contains it,
- require a centroid-distance margin before accepting the winning room,
- return `Unknown` when the point is inside multiple room envelopes without enough margin.

Recommended initial thresholds:

- minimum samples per room: `3`,
- slack margin around support radius: about `0.5 m`,
- winner margin versus second-best centroid: about `0.5 m`.

After that works, improve it with:

- per-room covariance / elliptical bounds,
- convex hull or alpha-shape room envelopes,
- k-nearest-neighbor or density-weighted scoring,
- confidence penalties when `sigma_m` or solve residual is high,
- confidence penalties when the solved point is far from all labeled samples.

Implementation details that should be part of the plan, not deferred:

- precompute room centroids, radii, and sample counts once in a coordinator-owned `RoomClassifier`,
- invalidate and rebuild that classifier only when samples are added or deleted,
- do not rebuild room geometry on every coordinator cycle,
- add a new device write path for position-based attribution, for example `apply_position_classification(area_id)`,
- that path should call the existing area/floor update logic without inventing a fake winning scanner,
- audit downstream sensors and tracker code that currently assume `area_advert` is populated.

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
- geometry quality is deferred in Phase 3; anchor count is the practical proxy until a stronger geometric metric is added later,
- room-classifier margin.

That becomes the basis for:

- whether an anchor participates,
- whether a trilat solution is `ok`, `low_confidence`, or `unknown`,
- whether a room label should be applied or withheld.

`get_scanner_max_radius()` is load-bearing in both current pipelines:

- it gates contenders in `_refresh_area_by_min_distance()`,
- it gates anchors in `_refresh_trilateration_for_device()`,
- both call paths and both test suites need to move to the new uncertainty gates together.

For Phase 3, uncertainty should be used first as a binary gate rather than a weighted solve input:

- include an anchor only when `sigma_m` is below a threshold,
- exclude it otherwise,
- defer any solver change that adds per-anchor weights to a later phase.

That keeps Phase 3 compatible with the current `AnchorMeasurement` and solver signatures.

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

Notes:

- this phase is partly de-risked already because the calibration sample lifecycle is implemented,
- `globalopts` also has an already-commented menu-routing remnant, so cleanup can remove dead code rather than changing active behavior.

### Phase 2: add the learned ranging model

1. Implement a calibration-sample reader that converts saved samples into training rows.
2. Fit the first global-plus-scanner-bias model.
3. Expose a coordinator-owned ranging-model service object.
4. Wire rebuild triggers from calibration sample add/delete operations and anchor-layout changes.
5. Add tests with synthetic geometry: place an anchor at a known position, create a synthetic sample at a known position with a specific `rssi_median`, verify that after fitting `estimate_range()` returns a value within 15% of the true geometric distance, and that this error is smaller than what `rssi_to_metres()` produces with default parameters for the same RSSI value. Also test that fitting is refused when fewer than 5 training pairs exist, and that `sigma_m` grows with range for a fixed `sigma_rssi`.

### Phase 3: switch trilat to the learned model

1. Replace manual range computation in `BermudaAdvert`.
2. Feed the learned range and uncertainty into trilateration.
3. Replace `max_radius` gates with uncertainty and residual gates.
4. If no fitted model exists for the current `anchor_layout_hash`, use the transitional legacy fallback and cap confidence low.
5. The area resolver continues to operate normally during this phase since room attribution has not yet changed. It is deleted at the start of Phase 4, not retained beyond it.

### Phase 4: room attribution from solved position

1. Build and cache a `RoomClassifier` from sample-derived room centroids/radii keyed by `anchor_layout_hash`.
2. Use trilat position as the primary room input.
3. Reuse the existing per-device `AreaDecisionState` by adding position-challenger fields there rather than inventing a second parallel state holder.
4. Apply a dwell gate before committing room transitions so solve jitter does not flap rooms at boundaries.
5. Delete the nearest-scanner area winner path at the start of this phase. Room attribution comes exclusively from solved position hereafter.
6. Use this explicit fallback chain:
   - trilat has a usable solve and the classifier has trained rooms on this floor: use position-based room attribution,
   - trilat has a usable solve but the floor has no trained rooms on this layout: keep floor attribution separate and report room/area as `Unknown`,
   - trilat is `unknown`: keep floor attribution separate and report room/area as `Unknown`,
   - classifier result exists but misses the margin requirement: report room/area as `Unknown`,
   - no samples exist for the current `anchor_layout_hash`: keep floor attribution separate and report room/area as `Unknown` until the layout is trained.
7. Add a calibration-samples warning when the current anchor layout has fewer trained rooms than the previous layout, so anchor moves clearly signal recapture work.

### Phase 5: automatic mobility mode

1. Add a runtime `mobility_baseline` artifact derived from calibration samples, either in a new `mobility_baseline.py` or as a dedicated class alongside the ranging model and room classifier.
2. Add inferred mobility features and a short rolling classifier.
3. Convert mobility control from manual-only to `auto | stationary | moving`.
4. Tune area/trilat hysteresis against effective mobility, not the user’s static choice.
5. Add regression tests for:
   - stable stationary sample windows,
   - genuine motion,
   - boundary jitter,
   - room transitions.

### Phase 6: delete the old calibration internals

1. Remove `rssi_offset`, `attenuation`, `max_radius`, and `ref_power` plumbing.
2. Remove legacy number entities and constants.
3. Bump the config entry version and strip legacy option keys in `async_migrate_entry()` so stored entries do not retain orphaned calibration settings.
4. Remove the old RSSI-to-distance utility and its tests once the no-samples path is explicitly handled.
5. Rewrite remaining tests around:
   - learned ranges,
   - trilat confidence,
   - sample-based room attribution,
   - inferred mobility.

Phase 6 should ship as one release or one PR:

- removing entities and constants,
- config-entry migration,
- and stored-option cleanup

should land together so manually calibrated users see one intentional breaking change rather than a staggered partial removal.

## Risks to manage

- Sparse samples can produce overconfident room labels. The classifier must prefer `Unknown` over guessing.
- Anchor moves invalidate prior geometry. Every model and room map must be scoped by `anchor_layout_hash`.
- Anchor moves also make rooms appear to disappear until the new layout is resampled. The UI should warn about this explicitly.
- Some devices will have few or no per-device samples. The model must back off cleanly to global or scanner-level parameters.
- Full deletion of `ref_power` should happen only after the learned model is proven to cover low-sample devices acceptably.
- Rooms with no calibration samples on the current floor are untrained. The device should keep floor attribution separate and report room/area as `Unknown` rather than fabricating a room from the floor name.
- Manually calibrated users will lose `attenuation`, `rssi_offset`, and `ref_power` as durable user settings in Phase 6. That is a deliberate breaking change and should be called out in release notes.
- Users who want the new model to reflect their environment should capture calibration samples before or during the Phase 3 to Phase 6 transition.

## Bottom line

I agree with removing the old config-flow calibration workflow immediately.

I also agree with removing `max_radius`, `attenuation`, `rssi_offset`, and eventually `ref_power`, but only after the sample-driven range estimator is active, because those settings still provide the live distance input that the current trilat path depends on.

The right replacement is not another manual tuning screen. It is:

- learned range estimation from calibration samples,
- trilat-first positioning,
- room classification from labeled sample coordinates,
- automatic mobility inference from stationary sample baselines plus live motion evidence.
