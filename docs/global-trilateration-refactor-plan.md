# Global Trilateration Refactor Plan

## Status

This plan was revised on 2026-03-13 after review of the current implementation in:

- `custom_components/bermuda/coordinator.py`
- `custom_components/bermuda/trilateration.py`
- `custom_components/bermuda/room_classifier.py`
- `custom_components/bermuda/ESTIMATION_PIPELINE_PROPOSAL.md`

The key correction from that review is important:

- current floor evidence scoring is already soft,
- the most damaging hard gate is anchor exclusion after floor selection,
- the second major instability source is full state reset on floor switch,
- Home Assistant has no native transition-point model, so split-level movement priors need to be Bermuda-native,
- a full "global 3D first" rewrite is not yet justified by data.

This document now reflects that narrower, safer, and more testable direction.

## Goal

Evolve Bermuda from a floor-gated localization pipeline into a softer, more physically plausible system that:

- stops hard-discarding useful cross-floor anchors,
- preserves solver continuity across floor changes,
- uses cross-floor fingerprint evidence to resolve split-level ambiguity,
- adds soft Bermuda-native transition priors for split-level movement points,
- treats full global 3D trilateration as a later experiment rather than the first implementation step.

For the current split-level failure mode, the primary target is:

- avoid `ground_floor` occupancy collapsing into `street_level`,
- avoid same-floor anchors becoming unusable because of an early floor flip,
- avoid `Guest Room -> Garage front` style room jumps caused by cold restarts and floor-gated room scoring.

## Problem Summary

The current failure is not best described as "hard floor selection first."

### What the current code already does correctly

Floor evidence is already soft:

- floor evidence is built from all fresh scanner RSSI observations,
- wrong-floor scanners are not ignored during floor evidence scoring,
- they are penalized by `CONF_TRILAT_CROSS_FLOOR_PENALTY_DB` before `_score_rssi()` conversion,
- this means floor evidence is already a weighted competition, not a binary same-floor filter.

### What the current code does badly

The main problems happen after floor evidence is computed:

1. **Hard anchor exclusion**
   - once `selected_floor_id` is chosen, anchors from other floors are skipped from the solve path,
   - this is the `scanner.floor_id != selected_floor_id` hard gate in the coordinator,
   - it turns an imperfect floor decision into an abrupt geometric failure.

2. **Cold restart on floor switch**
   - when the selected floor changes, the coordinator clears EWMA ranges, last solution, velocity state, residual state, and quality state,
   - a brief wrong floor decision therefore wipes all continuity and restarts the solver cold on the new floor.

3. **Room classification is hard floor-gated**
   - room samples and fingerprints are filtered to the chosen floor before scoring,
   - if floor selection is wrong, the correct room is invisible to the classifier.

### Why split-level homes are uniquely affected

This architecture is especially weak in homes where:

- `street_level` is between `basement` and `ground_floor`,
- the Home Assistant floor model is semantic rather than geometric,
- rooms on one level are physically close to anchors on another level,
- wall and slab attenuation are stronger discriminators than Euclidean distance.

In this environment, the main bug is not that floor evidence happens first. The main bug is that the post-floor pipeline is too hard and too lossy.

## Design Principles

1. **Fix the hard gates before reordering the whole pipeline.**
   - The first changes should target anchor exclusion and floor-switch cold restarts.

2. **Use soft penalties instead of binary rejection.**
   - Except for stale, missing-anchor, and no-range conditions, weak measurements should be downweighted rather than dropped.

3. **Prefer fingerprint evidence where geometry is structurally weak.**
   - Split-level ambiguity is often driven by wall attenuation patterns, not solvable geometry.

4. **Preserve continuity.**
   - A brief floor challenger should not erase the entire state of the solver.

5. **Do not assume `z` is observable just because 3D code exists.**
   - `solve_3d_soft_l1()` is available, but actual anchor geometry may still make `z` poorly constrained.

6. **Use experiments to earn the right to bigger rewrites.**
   - Global all-anchor 3D solving should be treated as a hypothesis, not as the default next step.

7. **Anchor count is not a quality metric.**
   - Geometry quality and residual consistency must explicitly gate how much Bermuda trusts the geometric solve.

8. **Transition knowledge is house metadata, not room evidence.**
   - Transition points should be stored separately from ordinary room calibration samples.

9. **Transition priors must stay soft.**
   - Missing a short stairwell dwell must not hard-lock Bermuda onto the wrong floor.

## Revised Target Architecture

The revised target architecture is:

- keep current soft floor evidence scoring as the coarse prior,
- remove hard anchor exclusion from the solve path,
- preserve solve continuity across floor changes,
- add cross-floor fingerprint scoring as a parallel floor/room signal,
- add Bermuda-native transition samples as soft floor-switch priors,
- promote global 3D solving only if range bias and `z` observability are validated.

### Stage 1: Keep floor evidence scoring, improve diagnostics

The current floor evidence path should remain the first coarse signal for now.

Immediate improvements:

- log `floor_evidence` per update for replay analysis,
- log challenger floor margin and dwell state,
- log which anchors would have participated if not excluded,
- separate "floor evidence confidence" from "solver confidence."

This keeps the existing floor competition model while making its failures measurable.

### Stage 2: Replace hard wrong-floor exclusion with soft anchor inclusion

The solve path should stop treating wrong-floor anchors as unusable by definition.

Instead:

- same-floor anchors keep their normal `sigma_m`,
- adjacent-floor anchors are included with inflated `sigma_m`,
- non-adjacent-floor anchors are deferred for later evaluation and would require a larger inflation factor if enabled,
- stale or invalid anchors remain excluded.

This is the core refactor.

The purpose is not to claim that cross-floor RSSI is good geometry. It is not. The purpose is:

- let the solver see all the evidence,
- let biased cross-floor ranges become weak constraints instead of missing constraints,
- avoid catastrophic anchor-count collapse after a bad floor choice.

Initial rollout constraint:

- the first soft-inclusion rollout should be limited to adjacent floors,
- non-adjacent floors should remain diagnostic-only until cross-floor range bias is measured,
- this keeps the change targeted to the actual `street_level` vs `ground_floor` and `basement` cases without assuming distant levels are useful.

### Stage 3: Remove cold reset on floor switch

Floor changes should no longer clear the full trilateration state.

Replace hard reset with soft continuity management:

- retain last `x/y`,
- retain last `z` as a prior when available,
- retain velocity state,
- retain EWMA range state where safe,
- inflate prior uncertainty when floor changes,
- lower confidence during the transition instead of restarting cold.

This change is expected to reduce oscillation even before any broader algorithmic work.

### Stage 4: Add cross-floor fingerprint scoring

Fingerprint classification should run across all floors as a parallel signal.

Initial behavior:

- keep current geometry-driven room scoring behavior unchanged,
- add a cross-floor fingerprint mode that returns the best `(room, floor)` candidate globally,
- expose it in diagnostics first,
- compare it against the current floor-gated classifier on replay traces.

Rationale:

- in a split-level house, fingerprint evidence may distinguish `Guest Room` from `Garage front` better than geometry,
- walls, floors, and layout create room-specific attenuation patterns,
- this is likely the strongest discriminator where solved `z` is weak or ambiguous.

In this stage, geometry should be treated as a consistency check and tie-breaker:

- fingerprint provides the primary `(room, floor)` candidate in split-level ambiguity,
- geometry is used to reject impossible candidates and disambiguate near-boundary rooms,
- low geometry quality should reduce the influence of the trilat point instead of forcing a wrong room.

### Stage 5: Add Bermuda-native transition samples

Add a separate transition-sample model for floor-transition points that exist inside normal Home Assistant areas.

This is explicitly **not** an HA area model extension and **not** a normal room sample.

Proposed MVP capture shape:

```yaml
action: bermuda.record_transition_sample
data:
  device_id: ...
  room_area_id: entrance
  transition_name: stairwell
  x_y_z_m: 1.2,1.5,3.7
  sample_radius_m: 1.0
  duration_s: 60
  transition_floor_ids:
    - basement
    - top_floor
```

Semantics:

- the transition point belongs to `room_area_id`,
- `transition_name` is user-facing text,
- Bermuda derives an internal stable key from `(room_area_id, transition_name)` and does not expose that key,
- multiple captures for the same pair should merge into one transition-point model,
- transition samples are stored separately from room samples and must not feed ordinary room kernels or fingerprints.

Runtime behavior:

- if the current solved position or fingerprint candidate is near a transition sample whose `transition_floor_ids` includes the challenger floor, reduce floor-switch penalty / dwell,
- if the challenger floor has no nearby supporting transition sample, increase penalty or preserve ambiguity,
- transition support is advisory only and must never hard-block a switch.

Why this is the right scope:

- Home Assistant only models floors and areas,
- split-level houses often need multiple distinct transition points inside the same area,
- this adds house-specific movement knowledge without forcing a full room-adjacency graph into the first implementation.

### Stage 6: Hybrid floor inference

After diagnostics prove useful, floor inference should become a posterior built from:

- current RSSI floor evidence,
- cross-floor fingerprint output, with fingerprint-primary arbitration in split-level ambiguity,
- transition-sample support for the challenger floor,
- continuity from the previous stable room/floor,
- solved `z` only where `z` observability is validated,
- geometry quality and residual consistency as gating signals.

This is still "floor first enough" for operational stability, but no longer relies on a single floor vote.

Recommended arbitration rule:

- when fingerprint produces a strong global `(room, floor)` candidate and geometry quality is weak or ambiguous, let fingerprint dominate floor selection,
- when fingerprint and previous stable room/floor agree on the challenger floor, allow that pair to overrule RSSI-only floor evidence unless geometry quality is both strong and contradictory,
- when a challenger floor is supported by a nearby matching transition sample, lower the required switch penalty or dwell but do not bypass ambiguity checks,
- when fingerprint is weak and RSSI floor evidence is weak, hold the previous stable room/floor instead of letting transition support force a switch by itself,
- when fingerprint is ambiguous and geometry quality is strong, let geometry constrain the room within the current coarse floor posterior,
- when both are weak, hold the previous stable room/floor instead of forcing a switch.

### Stage 7: Optional global 3D-first solve

This remains a later research track, not the first implementation slice.

Only pursue it if experiments show:

- cross-floor range bias is small enough to be usable after sigma inflation,
- current anchor layout makes `z` observable with acceptable GDOP / condition number,
- replay traces show materially better floor/room accuracy than the softer 2D-first hybrid approach.

If those conditions are not met, Bermuda should remain 2D-primary with stronger fingerprint and continuity logic.

## Weighting Model

The weighting model should be explicit about cross-floor measurements being biased, not just noisy.

Per-anchor effective uncertainty should include:

- base `sigma_m` from the ranging model,
- advert age multiplier,
- live dispersion / packet health where available,
- floor mismatch multiplier,
- robust residual downweighting from the solver.

Transition support does **not** belong in per-anchor solve weighting.

It should instead act later as a floor-challenger prior:

- lower switch penalty / dwell when a nearby transition sample supports the challenger floor,
- preserve ambiguity or increase switch penalty when no nearby transition sample supports the challenger floor.

Important constraint:

- cross-floor anchors must not be treated as equally geometric as same-floor anchors,
- the first implementation should prefer sigma inflation over inventing a fake corrected distance model.
- geometry quality and residual consistency must be tracked separately from raw anchor count.

### Recommended floor mismatch weighting

Start with simple multiplicative inflation:

- same floor: `1x`
- adjacent floor: `4x`
- non-adjacent floor: `8x`

These values are placeholders for replay tuning, not fixed truths.

## Quality Signals

The current solver already exposes quality signals that should become first-class decision inputs:

- GDOP / geometry quality,
- condition number,
- residual consistency,
- normalized residual RMS.

These should influence:

- overall position confidence,
- whether geometry is allowed to overrule fingerprint evidence,
- how strongly room hysteresis holds the previous stable room,
- whether floor arbitration is allowed to switch immediately or must remain ambiguous.

Recommended rule of thumb:

- strong fingerprint + weak geometry: fingerprint leads,
- strong geometry + weak fingerprint: geometry may refine within the coarse floor posterior,
- weak fingerprint + weak geometry: hold state and lower confidence,
- disagreement between strong signals: emit ambiguity rather than forcing a room flip.

## Floor Model

The revised plan does **not** assume floor should be inferred from solved `z` alone.

### Immediate approach

Use a hybrid floor posterior composed of:

- existing RSSI floor evidence,
- fingerprint-inferred floor from global room scoring as the primary discriminator for split-level ambiguity,
- nearby transition-sample support for challenger floors,
- previous stable room/floor,
- geometry quality and residual consistency as gating signals on how much trilat can influence the result.

### Later approach

Use solved `z` as an input only after validating:

- physical floor heights or reliable sample-derived `z` clusters,
- acceptable `z` observability from the anchor layout,
- stability under partial anchor loss.

### Why this changed

Home Assistant floor levels are semantic labels, not geometric heights. Until physical or sample-derived vertical structure is established, "floor from z" is underspecified.

## Proposed Code Changes

### `custom_components/bermuda/coordinator.py`

Required first changes:

- replace hard same-floor anchor skip with soft cross-floor sigma inflation,
- stop clearing solve state on floor switch,
- keep `_apply_soft_vertical_prior` floor-scoped or disable it while early cross-floor anchor inclusion is enabled,
- decide whether inflated cross-floor sigmas should be excluded from `mean_sigma_m` so fallback anchors do not depress confidence disproportionately,
- add side-by-side diagnostics for:
  - floor evidence,
  - included vs penalized anchors,
  - would-have-been-rejected anchors,
  - previous-state continuity,
  - fingerprint-global candidate output,
  - nearby matching transition-sample support.

Follow-on changes:

- add a hybrid floor posterior stage,
- retain a distinct floor-confidence signal,
- wire geometry quality / residual consistency into confidence and arbitration,
- keep `rejected_wrong_floor` only as a diagnostic concept if needed for compatibility.

### `custom_components/bermuda/trilateration.py`

No solver rewrite is required initially.

Keep:

- current 2D and 3D IRLS solvers,
- current quality metric functions,
- current residual-based robust weighting.

Add only what the coordinator needs:

- cleaner support for externally-inflated per-anchor sigma,
- optional richer diagnostics around residual contribution by anchor.

No early rewrite should replace the current solver with a different optimizer.

### `custom_components/bermuda/room_classifier.py`

Initial changes:

- add cross-floor fingerprint scoring mode,
- allow returning the best `(room, floor)` fingerprint candidate globally,
- keep geometry scoring floor-scoped in the first step if needed for safety,
- add diagnostics comparing:
  - current floor-gated outcome,
  - fingerprint-global outcome,
  - fused outcome.

The intended near-term fusion model is:

- fingerprint-primary for split-level floor discrimination,
- geometry-secondary as a plausibility and boundary disambiguation signal,
- ambiguity / hold behavior when neither signal is decisive.

Later changes:

- support a fully global room posterior if replay evidence justifies it,
- use floor as a soft prior instead of a hard pre-filter.

### Calibration and sample handling

Do not immediately depend on per-floor `z` bands.

Near-term additions:

- helper tooling to inspect room/floor sample density,
- replay utilities for known-location sessions,
- optional reporting on whether a room has enough fingerprint support to participate in cross-floor scoring.

Add a separate transition-sample path:

- new Bermuda-native service such as `bermuda.record_transition_sample`,
- separate persistence from normal room calibration samples,
- internal transition-point key derived from `(room_area_id, transition_name)`,
- support multiple transition points inside the same HA area,
- runtime proximity/support checks that can be surfaced in diagnostics before they influence assignment logic.

Longer-term additions:

- sample-derived floor `z` clusters,
- per-room vertical centroids and variance,
- optional explicit physical floor-height config if sample coverage is insufficient.

## Migration Strategy

### Phase 0: Diagnostics and replay instrumentation

- add a feature flag such as `soft_cross_floor_pipeline`,
- keep behavior unchanged,
- log:
  - `floor_evidence`,
  - challenger state,
  - whether floor-switch cold reset fired and how many times per session,
  - rejected cross-floor anchors and their hypothetical inflated sigma,
  - current room result,
  - global fingerprint candidate result,
  - nearby transition-sample matches for challenger floors when transition samples exist,
  - solver quality metrics.

This phase should produce the data needed to decide whether broader changes are justified.

### Phase 1: Eliminate floor-switch cold reset

This is the smallest, safest, highest-information change.

- preserve solve state across floor changes,
- inflate prior uncertainty instead of clearing state,
- keep all user-visible behavior otherwise unchanged.

Expected payoff:

- reduced oscillation,
- less catastrophic position collapse after brief floor challengers.

### Phase 2: Soft anchor inclusion behind a flag

- include adjacent-floor anchors with inflated sigma,
- keep non-adjacent-floor anchors diagnostic-only in the first rollout,
- keep existing floor evidence scoring,
- compare residuals, geometry quality, and room outcomes with and without the flag.

Expected payoff:

- fewer "insufficient anchors after floor flip" failures,
- better continuity when the chosen floor is briefly wrong.

### Phase 3: Cross-floor fingerprint diagnostics

- run global fingerprint scoring in parallel,
- do not use it for assignment yet,
- this phase can run in parallel with Phase 2 because it only depends on RSSI vectors, not solver output,
- compare its inferred floor against the current pipeline on replay traces.

Expected payoff:

- evidence on whether fingerprint can resolve split-level ambiguity better than geometry.
- if Experiment 3 fails, do not proceed to production hybrid floor arbitration until the fingerprint model is reworked.

### Phase 4: Transition-sample storage and diagnostics

- add `bermuda.record_transition_sample`,
- store transition samples separately from room samples,
- derive an internal transition-point key from `(room_area_id, transition_name)`,
- compute transition proximity/support diagnostics in parallel with the existing pipeline,
- do not let transition samples affect assignment yet.

Expected payoff:

- Bermuda can represent split-level movement points that Home Assistant cannot model directly,
- known stair / entry transitions become measurable without creating hard gates.

### Phase 5: Hybrid floor arbitration

- if diagnostics validate fingerprint usefulness, feed fingerprint floor evidence into the floor posterior,
- feed transition-sample support into floor challenger arbitration as a soft prior,
- require an explicit replay threshold before promoting it:
  - target `>85%` floor-correct top-room on known traces,
  - target score gap or confidence margin large enough to avoid frequent ambiguity,
- allow it to overrule ambiguous RSSI-only floor outcomes in limited conditions,
- keep explicit ambiguity states where signals disagree strongly.

### Phase 6: Optional room-classifier expansion

- if replay shows the global fingerprint signal is consistently good,
- expand room inference from floor-gated to floor-soft,
- keep strong hysteresis and ambiguity handling.

### Phase 7: Optional global 3D-first evaluation

- only after the Vertical observability and Cross-floor range bias experiments pass,
- gated behind a separate feature flag,
- compare against the softer hybrid pipeline rather than replacing it blindly.

### Phase decision gates

- After Phase 1:
  Measure whether cold-reset elimination alone removes most catastrophic `Guest Room -> Garage front` collapses. If yes, defer later phases unless there is still a clear split-level accuracy gap.
- After Phase 2:
  Compare replay continuity and residual quality with and without adjacent-floor soft inclusion. If continuity improves but residual quality degrades materially, keep the feature diagnostic-only.
- After Phase 3:
  Treat Experiment 3 as a go/no-go gate. If global fingerprint scoring cannot reliably discriminate split-level rooms, do not promote fingerprint into production floor arbitration.
- After Phase 4:
  Confirm transition samples improve diagnostics without increasing "stuck on previous floor" failures. If not, keep them diagnostic-only and do not feed them into arbitration.
- After Phase 5:
  Promote hybrid floor arbitration only if replay traces show lower false floor flips without creating longer lock-in on the wrong floor.

## Testing Plan

### Unit tests

- floor evidence scoring remains unchanged in the baseline path,
- floor switch does not clear solve state,
- adjacent-floor anchors are included with sigma inflation when the feature flag is on,
- non-adjacent-floor anchors remain diagnostic-only in the first rollout,
- downgraded 3D-to-2D paths preserve `z` prior and reduce confidence,
- global fingerprint scoring can produce a room/floor candidate outside the current selected floor,
- transition samples are stored separately from room samples and do not affect room kernels directly,
- a challenger floor with nearby matching transition support gets reduced switch penalty / dwell,
- a challenger floor without nearby matching transition support does not get forced through,
- hybrid floor arbitration can hold ambiguity instead of forcing a wrong room,
- geometry quality and residual consistency reduce the influence of weak solves.

### Replay tests

Use saved `history.csv`, logs, calibration stores, and current anchor config.

Required scenarios:

- stable `Guest Room` occupancy near `street_level` scanners,
- `Guest Room -> Garage front` failure replay,
- split-level stair traversal between basement, street, and ground,
- split-level traversal where Bermuda misses an explicit stairwell dwell but should still recover through soft evidence,
- outdoor transition into `garage_front`,
- sparse-anchor periods where 3D temporarily drops to 2D.

### Regression targets

- no immediate collapse from `Guest Room` to `Garage front` caused solely by a floor challenger,
- no full solver cold restart after a brief floor flip,
- same-floor anchors are not lost merely because floor evidence briefly favored another level,
- supported floor changes are easier near declared transition points without becoming mandatory checkpoints,
- floor confidence degrades before room assignment becomes physically implausible,
- fingerprint-global diagnostics are measurable against the current assignment logic.

## Required Experiments

### Experiment 1: Guest Room failure replay

Per update, log:

- all scanner RSSI values,
- `floor_evidence`,
- `selected_floor_id`,
- included anchors,
- penalized anchors,
- anchor count,
- solve result,
- room result,
- fingerprint-global candidate,
- nearby transition-sample support for the challenger floor when present.

Goal:

- identify whether the failure is dominated by floor challenger behavior,
- anchor-count collapse,
- cold reset,
- or room-classifier floor gating.

### Experiment 2: Vertical observability measurement

Use current anchor layout to measure:

- `gdop`,
- condition number,
- residual stability,
- sensitivity of solved `z` at representative positions on each level.

Goal:

- determine whether current anchors make `z` observable enough to justify global 3D-first work.

Suggested thresholds:

- success target: GDOP `< 5` at representative known positions,
- warning zone: GDOP `5-10` or unstable condition numbers,
- failure threshold: GDOP `> 10` or condition number `> 1e4`.

### Experiment 3: Cross-floor fingerprint accuracy

Using current calibration samples:

- run fingerprint scoring without floor gate,
- evaluate whether the best room/floor candidate matches known-location traces,
- measure score gap and ambiguity frequency.

Goal:

- decide whether fingerprint can become a floor arbiter in split-level cases.

Suggested success threshold:

- `>85%` floor-correct top candidate on known traces,
- usable score gap or confidence margin on the majority of replayed samples.

### Experiment 4: Transition-sample utility

For known stair / entry / landing traversals:

- compare floor-switch latency with and without transition-sample priors,
- measure whether supported transitions reduce false ambiguity,
- measure whether unsupported floor jumps become less frequent,
- include replays where the device was not explicitly solved inside the transition point for long enough.

Goal:

- confirm that transition samples help floor arbitration without becoming hard gates,
- validate that a missed transition dwell does not trap the device on the previous floor.

Suggested success threshold:

- lower false floor-switch rate on split-level traces,
- no increase in "stuck on previous floor" failures when transition occupancy is brief or missed,
- improved stability on `Guest Room -> Garage front` style replays.

### Experiment 5: Cross-floor range bias characterization

For cross-floor scanners in known locations:

- compare `rssi_distance_raw` to true geometric distance,
- quantify mean bias and variance by floor pair,
- separate adjacent-floor from non-adjacent-floor behavior.

Goal:

- determine whether cross-floor ranges are merely weak or actively misleading,
- tune sigma inflation accordingly.

Suggested decision rule:

- if adjacent-floor bias is moderate, keep adjacent soft inclusion,
- if non-adjacent-floor bias is large, continue excluding non-adjacent floors from the solve path in production.

Suggested thresholds:

- if mean bias exceeds `2 m` or variance exceeds `3 m` on adjacent-floor traces, even adjacent inclusion may need to stay diagnostic-only,
- if non-adjacent-floor bias exceeds those thresholds, keep non-adjacent floors out of the production solve path.

### Experiment 6: State reset elimination

Before broader algorithmic changes:

- disable floor-switch state reset only,
- replay boundary sessions,
- measure oscillation frequency and room stability.

Goal:

- verify whether state reset is a primary instability driver.

## Risks

- Cross-floor RSSI may be so biased that even inflated-sigma inclusion still hurts some layouts.
- Adjacent-floor inclusion may be beneficial while non-adjacent-floor inclusion is harmful, which would require floor-distance-specific behavior.
- Global fingerprint scoring may overfit rooms with sparse calibration coverage.
- Transition samples may be sparse, misplaced, or incomplete enough to help some routes while leaving others ambiguous.
- Overweighting transition priors could make Bermuda too reluctant to switch floors away from declared movement points.
- Hybrid floor arbitration may become harder to debug unless diagnostics are explicit.
- `street_level` may overlap vertically with both basement and ground-floor regions, making pure `z` classification inherently ambiguous.
- The safest short-term solution may improve split-level stability without ever justifying a full global 3D-first rewrite.

## Open Questions

1. What are the physical z ranges of `basement`, `street_level`, `ground_floor`, and `top_floor` in metres?
2. Does the current anchor layout make `z` observable at all, or does it only make `x/y` observable?
3. What is the actual floor penetration loss by floor pair in this house?
4. From `Guest Room`, how many anchors are usually visible on each level, and with what quality?
5. Is the `Guest Room -> Garage front` failure consistently reproducible?
6. Should cross-floor fingerprint be diagnostic-only first, or directly participate in floor arbitration under a feature flag?
7. Should non-adjacent floor anchors be softly included, or should the first rollout limit soft inclusion to adjacent floors only?
8. Is there enough calibration coverage per room and per floor to support global fingerprint ranking?
9. What geometry-quality or residual-consistency thresholds should suppress geometry-led room changes?
10. How close must a solve or fingerprint candidate be to a transition sample before it should reduce floor-switch penalty / dwell?
11. Should transition support be computed from solved position only, fingerprint-global room/floor candidate only, or the stronger of the two?
12. What is the right merge strategy for multiple captures of the same `(room_area_id, transition_name)` point?

## Recommended First Implementation Slice

The first slice should be intentionally narrow and directly driven by the review findings:

1. Add diagnostics for floor evidence, would-be-rejected anchors, and fingerprint-global candidates.
2. Remove floor-switch cold reset and preserve priors through a challenger.
3. Add adjacent-floor soft anchor inclusion behind a feature flag.
4. Add cross-floor fingerprint diagnostics behind a feature flag.
5. Add transition-sample storage plus diagnostic-only proximity/support checks.
6. Replay the `Guest Room` and `Garage front` traces before changing final room assignment behavior.

This is the minimum change set that tests the central hypotheses without committing to a full global 3D-first rewrite.

## Acceptance Criteria

- Brief floor challengers no longer wipe the entire solve state.
- Split-level replay traces stop producing catastrophic room jumps caused by anchor-count collapse.
- Cross-floor fingerprint diagnostics provide useful floor/room evidence on known traces.
- Adjacent-floor soft anchor inclusion improves continuity without materially worsening residual quality.
- Transition samples remain a soft prior and do not dilute ordinary room classification.
- Geometry quality and residual consistency are used to suppress low-trust geometry from forcing room/floor changes.
- A decision on true global 3D-first solving is deferred until replay data demonstrates that it is both observable and beneficial.

---

## Historical Review Notes

These review notes are retained for traceability. Some stage and phase numbers below refer to the earlier revision and should be read as historical context, not the current stage numbering above.

## Engineer Review — 2026-03-13 (revision)

*Reviewed against the revised plan. The major concerns from the first review have been correctly incorporated: the problem statement is now accurate, the phase ordering is right, global 3D is correctly deferred, and Phase 1 (cold reset elimination) is correctly prioritised as the first change. The following comments address remaining implementation-level issues specific to this revision.*

---

### Stage 2 implementation detail: `_apply_soft_vertical_prior` will misfire with cross-floor anchors

When cross-floor anchors are included in Stage 2, the coordinator computes `anchor_z_bounds` from all included anchors (coordinator.py:2498-2503). This tuple feeds directly into `_apply_soft_vertical_prior` (lines 2040-2061), which pulls solved z toward the anchor height band.

With only same-floor anchors, `anchor_z_bounds` spans the ceiling heights of that floor — a physically meaningful band. With cross-floor anchors included, `anchor_z_bounds` spans from the lowest-floor ceiling to the highest-floor ceiling. `_apply_soft_vertical_prior` will then pull z toward the centroid of all floor heights, which does not correspond to any real floor the device is on.

This means Stage 2 needs an explicit decision about what to do with `_apply_soft_vertical_prior`:

- **Option A:** Exclude cross-floor anchors from the `anchor_z_bounds` calculation (keep z bounds floor-scoped) while still including them in the 2D solve.
- **Option B:** Disable `_apply_soft_vertical_prior` when cross-floor anchors are present.
- **Option C:** Accept the multi-floor z bounds as a wider comfort zone and rely on the prior sigma instead.

Option A is probably cleanest for the Stage 2 flag: include cross-floor anchors in the 2D solve path but continue computing `anchor_z_bounds` only from same-floor anchors until Stage 6 changes the z model.

---

### Stage 2 implementation detail: sigma chain application point and effect on `mean_sigma_m`

The current sigma chain in the coordinator (lines 2473-2476):

```
effective_sigma_m = base_sigma or default (8.0 m)
effective_sigma_m *= age_multiplier (1x–3x)
```

The floor mismatch multiplier should be applied after age inflation, making the full chain:

```
effective_sigma_m *= age_multiplier * floor_mismatch_multiplier
```

One consequence: `mean_sigma_m` (line 2497) averages sigma across all anchors and feeds into `_compute_trilat_confidence`. When cross-floor anchors with 4x–8x inflated sigma are included, `mean_sigma_m` rises significantly, which reduces raw confidence even if the same-floor anchors are perfectly good. The plan should decide whether cross-floor anchors participate in `mean_sigma_m` (dragging confidence down even in healthy situations) or are excluded from the confidence calculation while still contributing to the solve.

The simplest approach: include cross-floor anchors in the solve, exclude them from `mean_sigma_m`. This separates "confidence in the same-floor measurement quality" from "did we include cross-floor anchors as a fallback."

---

### Phase 3 (cross-floor fingerprint diagnostics) does not depend on Phase 2

The plan sequences Phase 3 after Phase 2. Fingerprint scoring is independent of the trilateration solver — it only needs RSSI vectors, not a completed solve. Phase 3 could run in parallel with or even before Phase 2, which would let fingerprint experiment results inform how aggressively to pursue soft anchor inclusion.

If fingerprint cross-floor accuracy is strong (Experiment 3 passes), it reduces the pressure to get Phase 2 sigma tuning exactly right. If it is weak, that changes the priorities for Phase 4. Running Phase 3 diagnostics earlier would produce this decision data sooner.

---

### Stage 4 / room_classifier.py: current interface requires floor_id and returns early if None

`room_classifier.classify()` takes `floor_id` as a parameter (line 158) and returns `RoomClassification(area_id=None, reason="missing_floor")` immediately if `floor_id is None` (lines 169-170). The existing interface has no path for cross-floor scoring.

Adding the cross-floor fingerprint mode described in Stage 4 requires a new calling convention — either a separate method, an `all_floors: bool` flag, or making `floor_id` optional with different behavior when absent. This is a small design choice but it should be made explicitly so the diagnostic path and the eventual production path use the same interface rather than being bolted on separately.

---

### The first implementation slice is correct as written

The five-step slice (diagnostics → cold reset fix → soft anchor inclusion → fingerprint diagnostics → replay before changing room assignment) is the right sequence and scope. One addition worth including in step 1: log whether `floor_switch cold reset` fired and how many times per session. This single counter will immediately quantify the state-reset instability before any code changes are made, and provides a direct regression metric for Phase 1.

---

## Engineer Review — 2026-03-13 (final)

### Run Experiment 3 before building Phases 3–5

The entire cross-floor fingerprint stack (Stages 4 and 5, Phases 3–5) rests on one assumption: that calibration fingerprints can discriminate `Guest Room` from `Garage front` and `street_level` rooms from `ground_floor` rooms when scored globally. If that assumption fails — sparse calibration coverage, rooms too similar in RSSI space, or floor penetration too variable to be consistent — then Phases 3–5 produce no improvement.

Experiment 3 (cross-floor fingerprint accuracy) can be run right now against existing calibration data with no code changes beyond a test script. It should be treated as a go/no-go gate before any of Phase 3–5 is built, not as a parallel experiment. If it passes, proceed. If it fails, Phases 4 and 5 need to be rethought entirely.

### Each phase needs an exit criterion before the next phase starts

The plan describes what each phase does but never says when to stop. After Phase 1 (cold reset fix), someone needs to evaluate: did this alone resolve most of the `Guest Room -> Garage front` failures? If yes, Phases 2–5 may not be worth building. After Phase 2, someone needs to evaluate: did soft anchor inclusion improve continuity, or did inflated-sigma cross-floor anchors just add noise?

Without explicit go/no-go criteria per phase, the plan risks building all six phases sequentially regardless of whether earlier phases already solved the problem. Add a one-paragraph decision gate to each phase: what metric to measure, what threshold constitutes success, and what happens if the threshold is not met.

### The floor arbitration rule in Stage 5 is still unspecified

Stage 5 builds a floor posterior from RSSI evidence, fingerprint output, continuity, and optionally solved z. The plan does not say what happens when these disagree — which is exactly the split-level case this plan exists to fix. "Hybrid floor posterior" is an implementation strategy, not a decision rule. Before Phase 4 is built, there needs to be an explicit answer to: when RSSI evidence says `street_level` and fingerprint says `ground_floor` and continuity says `ground_floor`, what wins and by how much? That decision rule is the core of the design. Everything else is plumbing.

---

## Engineer Review — 2026-03-13 (second pass, full code read)

*Reviewed against coordinator.py:2300–2554, room_classifier.py, trilateration.py, calibration.py, and the transition-sample capture shape proposed in Stage 5. The previous reviews are correct. The following comments identify remaining blocking issues and things the plan still does not answer.*

---

### 1. Executive Verdict

The direction is basically right. Phase ordering is correct. Deferring global 3D is correct. The first coding slice is correct. Two things are still wrong and one is still unfinished.

**Still wrong:**
- The transition-sample capture shape has an unresolvable coordinate-frame problem that will silently corrupt proximity checks unless fixed before any samples are captured.
- The Stage 6 floor arbitration rule is still described as a priority table, not an algorithm. The "final review" note identified this and the main plan body still has no concrete decision procedure.

**Still unfinished:**
- The plan asks whether cross-floor anchors should be excluded from `mean_sigma_m` (Phase 2 coordinator bullet 4) but never answers. This needs to be a decision in the plan, not a question, because it determines the confidence model for every Phase 2 outcome.

---

### 2. Critical Findings

**Finding 1: `_apply_soft_vertical_prior` misfires under Phase 2 — the plan presents this as a choice when it is not.**

The historical note correctly identifies the problem. coordinator.py:2498-2503 computes `anchor_z_bounds` from all included anchors. With cross-floor anchors included, this spans from basement to top_floor. `_apply_soft_vertical_prior` (lines 2040-2061) then pulls `z` toward the centroid of all floors — which is nowhere a person stands.

Options A/B/C are presented as a choice. Only Option A is safe for Phase 2: compute `anchor_z_bounds` from same-floor anchors only while still including cross-floor anchors in the 2D solve. Option B discards a useful guard. Option C pulls toward a physically meaningless centroid. The plan should commit to Option A explicitly.

**Finding 2: `mean_sigma_m` question is still open but must be answered before Phase 2.**

coordinator.py:2497 averages sigma across all anchors into `mean_sigma_m`, which feeds `_compute_trilat_confidence`. If 3 good same-floor anchors at sigma=1.5 m are joined by 2 cross-floor anchors at sigma=6 m (4× inflation), `mean_sigma_m` rises from 1.5 to 2.7. Every adjacent-floor solve gets a confidence penalty even when the same-floor geometry is healthy.

The answer is: exclude cross-floor anchors from `mean_sigma_m`. Track cross-floor anchor count separately as a diagnostic field. This separates "measurement quality on the chosen floor" from "did we fall back to cross-floor evidence."

**Finding 3: `_build_transition_strengths` already exists in room_classifier.py. The plan never mentions it.**

room_classifier.py:308-349 builds `self._transition_strengths`, an intra-floor room-to-room soft plausibility table derived from sample cloud overlap/gap. `transition_strength()` (lines 137-150) is already a public method.

The proposed transition-sample concept adds explicit inter-floor transition priors. These serve different purposes and should stay separate. But the plan should acknowledge the existing mechanism so implementers do not confuse it with the new one, and so there is no accidental overlap if transition samples are later considered for intra-floor transitions too.

**Finding 4: The fingerprint cross-floor interface problem has a simple fix the plan does not name.**

`_fingerprint_room_scores()` (room_classifier.py:269-306) and `_geometry_room_scores()` (lines 242-267) are pure functions that take a pre-filtered sample list. The floor filter happens at lines 172-173 inside `classify()`, before either method is called.

The cross-floor mode does not need a new calling convention on `classify()`. It needs a second entrypoint — `classify_global()` or `fingerprint_global()` — that calls `_fingerprint_room_scores` with all fingerprints unfiltered and returns `(room, floor_id, score)`. The existing `classify()` remains unchanged. The plan should commit to this interface rather than leaving the choice open.

**Finding 5: EWMA state contamination under Phase 2 is unaddressed.**

When a cross-floor anchor is first included, `advert.trilat_range_ewma_m` is initialized to `advert.rssi_distance_raw` (coordinator.py:2479). That raw distance is biased by floor penetration. If the floor then reverts and the anchor becomes same-floor, its EWMA starts from a contaminated prior. The EWMA decay at line 2481 takes several cycles to flush the bias.

Decision required: when an anchor transitions from cross-floor to same-floor status (i.e., the selected floor changed), clear `advert.trilat_range_ewma_m` for that anchor. Add a per-anchor `last_floor_role` field and reset on role change.

**Finding 6: "Adjacent floor" has no mechanical definition and Phase 2 arrives before transition samples exist.**

Phase 2 uses "adjacent floor: 4×, non-adjacent: 8×" inflation without defining adjacency. HA floors are semantic string IDs with no ordering. The coordinator has no ordered floor list. `transition_floor_ids` in transition samples would solve this — but Phase 2 comes before Phase 4.

For Phase 2: treat all non-selected floors as a single "other" category with one inflation factor. Remove the two-tier inflation from Phase 2 scope and move it to Phase 4 where `transition_floor_ids` can define adjacency properly. This eliminates a dependency that does not exist yet.

---

### 3. Transition-Sample Model: Blocking Problems

The concept is the right abstraction. Two issues must be resolved before building the capture infrastructure.

**Coordinate frame is unspecified — blocking.**

The proposed shape includes `x_y_z_m`. Calibration sample positions are stored under `anchor_layout_hash` (room_classifier.py:86, 109). Positions only have meaning within a layout hash. If the anchor layout changes, all positions under the old hash are deprecated.

Transition samples must store `anchor_layout_hash` alongside `x_y_z_m`. Without it, proximity checks after any layout change produce silently wrong results. This must be decided before capture infrastructure is built, because retroactively migrating position data without knowing which layout it came from is not feasible.

Merge strategy for the same `(room_area_id, transition_name, layout_hash)`: take the position centroid, union the `transition_floor_ids`, keep the larger `sample_radius_m`.

**`duration_s` is ambiguous — remove or rename.**

`duration_s` in the capture shape implies a capture session parameter, not a persistent property of the transition point. The existing `_CalibrationSession` (calibration.py) already has `duration_s` as a session field.

If `duration_s` is intended to control the floor-switch dwell reduction at runtime, that is a policy parameter and belongs in configuration, not house metadata. Remove it from the transition-sample shape. If needed for data quality annotation, put it in a capture-metadata subkey.

**`transition_floor_ids` solves the adjacency problem — keep it.**

Explicitly listing the floors a transition connects is better than trying to infer adjacency from HA floor ordering. It also means Phase 4 data can retrospectively inform Phase 2 adjacency. No change needed here.

**Proximity computation mechanism needs to be specified.**

The plan says "if the current solved position or fingerprint candidate is near a transition sample." The fingerprint candidate is a `(room, floor)` pair, not a position. The check should be: solved position within `sample_radius_m` of the transition point AND `room_area_id` matches the current room AND `transition_floor_ids` includes the challenger floor. When position quality is low, fall back to room match only. Spell this out in the plan.

---

### 4. Concrete Floor Arbitration Rule

Replace the Stage 6 priority-table prose with the following procedure:

```
Inputs at each update cycle:
  rssi_floor_evidence: dict[floor_id → float]         (already computed)
  floor_challenger_id, floor_challenger_since           (existing state)
  fingerprint_global: (floor_id, room_id, score, second_score)
  fingerprint_floor_confidence = score / (score + second_score)   [0..1]
  transition_support: float [0..1]  (nearest matching transition sample for challenger)
  geometry_quality_01, residual_consistency_01
  prev_stable_floor_id, prev_stable_room_id

Rule 1 — No active challenger:
  If floor_challenger_id is None: hold current floor. Done.

Rule 2 — Fingerprint strongly agrees with current floor:
  If fingerprint_floor_confidence > 0.70
  AND fingerprint_global.floor_id == current_floor:
    Clear challenger. Hold current floor. Done.

Rule 3 — Fingerprint strongly supports challenger:
  If fingerprint_floor_confidence > 0.70
  AND fingerprint_global.floor_id == floor_challenger_id:
    effective_required_dwell = required_dwell * 0.5

Rule 4 — Transition sample supports challenger:
  If transition_support > 0.6:
    effective_required_dwell *= (1.0 - 0.4 * transition_support)
    (up to 40% additional reduction, applied after Rule 3 if both trigger)

Rule 5 — Both signals are weak:
  If fingerprint_floor_confidence < 0.40
  AND geometry_quality_01 < 0.30:
    Hold current floor. Lower confidence. Do not advance challenger timer. Done.

Rule 6 — Default:
  Apply existing margin/dwell challenger state machine with effective_required_dwell.
```

Key properties: concrete thresholds, fingerprint and transition support modify the existing dwell/margin parameters rather than creating a parallel decision path, Rule 5 explicitly encodes the "both weak, hold state" case. The output is still "hold vs. advance challenger," not a probability, which keeps the state machine debuggable.

---

### 5. Phase Sequencing: Two Gaps

**Phase 3 (fingerprint diagnostics) should start at the same time as Phase 0, not after Phase 2.**

Fingerprint scoring needs only RSSI vectors, not solver output. Running Experiment 3 as a test script requires ~50 lines of code and no behavior changes. The result is a go/no-go gate for Phases 4 and 5. Getting that data earlier reduces the risk of building Phase 4/5 infrastructure that turns out to be useless. There is no reason to wait for Phase 2.

**Phase 4 has a hard dependency on the coordinate-frame decision.**

Do not start building `bermuda.record_transition_sample` until the `anchor_layout_hash` question is resolved. Once samples are captured in the wrong frame, migration is painful.

**Smallest next implementation that yields the most information:**

Phase 0 diagnostics and Phase 1 cold-reset elimination together in one slice. Phase 0 logging first (no behavior change), then the cold-reset fix. Run Experiment 3 as a test script in parallel. If the `Guest Room → Garage front` failure disappears after Phase 1, Phases 2–5 can be deprioritized. If it does not, the Phase 0 logs identify whether anchor starvation or room classifier gating is the remaining cause.

---

### 6. Concrete Plan Changes Required

1. **Commit to Option A** for `_apply_soft_vertical_prior` in the Phase 2 spec. Remove it as a choice.
2. **Commit to excluding cross-floor anchors from `mean_sigma_m`**. Add a separate `cross_floor_anchor_count` diagnostic.
3. **Remove the two-tier adjacent/non-adjacent inflation from Phase 2**. Use a single non-selected-floor multiplier in Phase 2. Move tiered inflation to Phase 4 where `transition_floor_ids` defines adjacency.
4. **Add `anchor_layout_hash` to the transition-sample capture shape.** Document the merge strategy.
5. **Remove `duration_s` from the transition-sample top-level shape**, or rename to `capture_duration_s` in a metadata subkey.
6. **Replace Stage 6 arbitration prose with the concrete decision procedure** above.
7. **Move the Experiment 3 go/no-go gate into the main Phase 3 section**, not just the historical notes.
8. **Add a note** that `_build_transition_strengths` (room_classifier.py:308-349) already handles intra-floor transition plausibility and that the new transition-sample model is inter-floor only.
9. **Add EWMA role-change clearing** to the Phase 2 coordinator spec: clear `advert.trilat_range_ewma_m` when an anchor's same-floor vs. cross-floor role changes.
10. **Add the `classify_global()` / `fingerprint_global()` interface decision** to the Phase 3 room_classifier.py spec.
