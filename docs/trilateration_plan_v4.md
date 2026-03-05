# Multi-Story Trilateration Implementation Plan (V4)

## 1. Scope and intent
- Add trilateration as a diagnostic localization pipeline for Bermuda in multi-story homes.
- Keep current area/room assignment pipeline as primary in phase 1.
- Do not replace area selection with trilateration unless measured real-world accuracy justifies it later.

## 2. Phase 0 (must-do cleanup before integration)
- Remove all import-time/demo side effects from `custom_components/bermuda/trilateration.py`.
- Keep only pure functions/classes; no hardcoded arrays, no prints, no execution at import.
- Remove sklearn usage from runtime path.
- Keep dependency footprint limited to `numpy` and `scipy` (already acceptable in HA ecosystem).

## 3. Coordinate model (explicit and fixed)
- Use a user-defined local Cartesian frame in meters.
- `scanner_x_m`, `scanner_y_m`, `scanner_z_m` are physical positions in meters.
- Origin is arbitrary but must be consistent across all scanners.
- `floor_id` / HA floor level are grouping metadata only, not geometric distance.
- In phase 1 solver, `z` is not solved; it is used only for floor grouping and future 3D readiness.

## 4. Data model and config additions
- Per scanner config:
  - `scanner_anchor_enabled` (default `false`): included in trilat anchor set when true.
  - `scanner_x_m`, `scanner_y_m`, `scanner_z_m` (optional floats).
- Global trilat config:
  - `trilat_enabled` (default `false`).
  - `trilat_cross_floor_penalty_db` (default conservative value, e.g. `8`).
  - `trilat_min_anchors` fixed at `3` for valid solve.
- No per-scanner floor-penalty override in phase 1.
- No user-exposed `trilat_update_interval_s`; run with coordinator cadence.
- Residual rejection uses a fixed internal phase-1 constant:
  - `_TRILAT_MAX_RESIDUAL_M = 5.0` (not user-configurable yet).

## 5. Input signals and smoothing strategy
- Trilateration uses `rssi_distance_raw` as input to avoid double-smoothing lag.
- Add trilat-specific per-anchor EWMA range:
  - moving: faster alpha.
  - stationary: slower alpha.
- Reset trilat EWMA buffers when selected floor changes to avoid stale cross-floor carryover.
- Existing Bermuda distance smoothing remains unchanged for current entities and area logic.

## 6. Anchor qualification rules (deterministic)
- Anchor eligible only if all true:
  - scanner enabled and `scanner_anchor_enabled == true`.
  - scanner has valid `x/y` (and `z` stored if provided).
  - latest advert not stale.
  - advert passes existing validity/radius sanity checks.
- Floor-specific solver anchor set in phase 1:
  - only anchors on chosen floor are passed to 2D solver.
- If fewer than 3 eligible anchors on chosen floor:
  - trilat result becomes Unknown (reason: `insufficient_anchors`).
- Exactly 2 anchors is always Unknown in phase 1.

## 7. Floor determination algorithm (explicit)
- Compute per-floor evidence from valid scanner RSSI streams.
- For each candidate scanner sample:
  - base evidence from filtered RSSI score (same monotonic mapping already used in coordinator).
  - apply binary cross-floor penalty:
    - if scanner floor == candidate floor: no penalty.
    - else subtract `trilat_cross_floor_penalty_db` once from RSSI before score conversion.
- Clarification: floor scoring uses `rssi_filtered`; solver range input uses
  `rssi_distance_raw` with trilat-specific EWMA (Section 5). These are separate pipelines.
- Sum evidence per floor; best floor competes with current floor.
- Floor switch uses mobility-aware hysteresis policy (same pattern as area policy):
  - moving: shorter dwell / lower margin.
  - stationary: longer dwell / higher margin.
- Floor decision state is isolated in `TrilatDecisionState` and must not share timers with `AreaDecisionState`.

## 8. Solver design (phase 1)
- Solver type: 2D nonlinear least-squares on chosen floor anchors.
- Objective: minimize residuals between solved point and trilat-EWMA ranges.
- Robust loss: `soft_l1` (or Huber), with uniform anchor weights.
- Phase 1 weighting is explicitly uniform for deterministic behavior.
- Quality gates:
  - minimum 3 anchors.
  - residual threshold to reject bad geometry/outlier sets
    (`_TRILAT_MAX_RESIDUAL_M = 5.0` in phase 1).
  - on rejection: trilat result Unknown (reason: `high_residual`).

## 9. Performance gate (solve-skip logic)
- Cache last solved anchor set and per-anchor `range_ewma_m`.
- Skip solver when all are true:
  - anchor ID set unchanged,
  - still at least 3 valid anchors,
  - each anchor EWMA delta is `< 0.2 m` since last solve.
- Solve immediately when:
  - anchor set changes, or
  - any anchor EWMA delta is `>= 0.2 m`, or
  - floor decision changes.

## 10. Unknown behavior and startup semantics
- On HA startup/restart, trilat sensors start as Unknown until enough fresh anchors arrive.
- No restoration of stale last coordinates.
- Unknown reasons are diagnostic and explicit:
  - `insufficient_anchors`
  - `ambiguous_floor`
  - `high_residual`
  - `stale_inputs`
- Unknown is sticky only until new evidence passes gates.

## 11. Entity exposure
- Diagnostic sensors per tracked device:
  - `trilat_x`, `trilat_y` (phase 1)
  - `trilat_floor`
  - `trilat_anchor_count`
  - `trilat_status` / `trilat_reason`
  - optional `trilat_residual`
- `scanner_z_m` stored from phase 1 even though 3D solve is deferred.
- Keep existing area/distance/scanner entities backward compatible.

## 12. Repairs and UX safeguards
- Add one aggregated repair issue when trilat is enabled but no valid anchors are configured.
- Add clear diagnostics when anchors are configured partially (missing coordinates/floor metadata).
- `scanner_anchor_enabled` only affects trilateration, never existing area resolution.

## 13. Logging and privacy
- Keep trilat debug targeted to selected device(s) only to avoid log flood.
- Keep centralized secret redaction in logging path for IRK/macirk values.
- Trilat debug lines should log:
  - floor evidence,
  - anchor set,
  - solve/skip decision,
  - residual and unknown reason.
- Avoid dependency logger spam by keeping third-party loggers at higher levels unless explicitly enabled.

## 14. Testing plan
- Unit tests:
  - floor evidence with binary cross-floor penalty.
  - anchor qualification and 2-anchor Unknown behavior.
  - trilat EWMA uses raw range and resets on floor change.
  - solve skip when all deltas `< 0.2 m`; solve runs when threshold crossed.
  - residual gate triggers Unknown correctly.
  - startup state Unknown until valid anchors available.
- Coordinator integration tests:
  - trilat state isolation from area hysteresis state.
  - no regressions in existing area assignment pipeline.
- Replay infrastructure:
  - scoped as separate deliverable (phase B), not required to ship phase 1.

## 15. Rollout phases
- Phase 1:
  - 2D floor-first trilat diagnostics, no area replacement.
- Phase 2:
  - evaluate accuracy on real homes; consider optional weighted solver only if data supports it.
- Phase 3:
  - optional 3D solve using `scanner_z_m` plus strict quality gating.
- Phase 4:
  - only consider feeding trilat into room selection if measured accuracy materially exceeds current method.

## 16. Acceptance criteria
- No regressions in current Bermuda area pipeline.
- Stable diagnostics with explicit Unknown reasons.
- Solver runtime bounded by skip logic under normal coordinator cadence.
- Real-home diagnostic accuracy target: p95 positional jitter around 4–6 m baseline in phase 1.
- Decision on trilat-to-area integration deferred until empirical results show room-level usefulness.

---

## Review notes (Claude, 2026-03-05)

### Comment 1 — Floor evidence vs. solver input use different RSSI streams; state this explicitly

Section 7 derives floor evidence from `rssi_filtered`; Section 5 uses `rssi_distance_raw` (+ trilat EWMA) as solver range input. Both choices are correct and intentional, but an implementer reading them separately may conflate the two pipelines. Suggest adding a clarifying note in Section 7:

> "Floor scoring uses `rssi_filtered` (the existing smoothed RSSI); solver range input uses `rssi_distance_raw` with the trilat-specific EWMA (Section 5). These are separate pipelines."

### Comment 2 — Residual rejection threshold has no specified value or config entry

Section 8 refers to a "residual threshold to reject bad geometry/outlier sets" but `trilat_max_residual_m` does not appear in the global config (Section 4) and no default value is given. Either:

- add `trilat_max_residual_m` (suggested default `5.0 m`) to Section 4 as a user-configurable option, or
- explicitly state it will be a fixed internal constant in phase 1 (e.g. `_TRILAT_MAX_RESIDUAL_M = 5.0`) and not exposed until real-world data informs a better value.

Leaving it implicit risks inconsistent implementation across PRs.
