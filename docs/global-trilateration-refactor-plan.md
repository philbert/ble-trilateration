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
- a full "global 3D first" rewrite is not yet justified by data.

This document now reflects that narrower, safer, and more testable direction.

## Goal

Evolve Bermuda from a floor-gated localization pipeline into a softer, more physically plausible system that:

- stops hard-discarding useful cross-floor anchors,
- preserves solver continuity across floor changes,
- uses cross-floor fingerprint evidence to resolve split-level ambiguity,
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

## Revised Target Architecture

The revised target architecture is:

- keep current soft floor evidence scoring as the coarse prior,
- remove hard anchor exclusion from the solve path,
- preserve solve continuity across floor changes,
- add cross-floor fingerprint scoring as a parallel floor/room signal,
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
- non-adjacent-floor anchors may use a larger inflation factor,
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

### Stage 5: Hybrid floor inference

After diagnostics prove useful, floor inference should become a posterior built from:

- current RSSI floor evidence,
- cross-floor fingerprint output, with fingerprint-primary arbitration in split-level ambiguity,
- continuity from the previous stable room/floor,
- solved `z` only where `z` observability is validated,
- optional topology priors for connector groups.

This is still "floor first enough" for operational stability, but no longer relies on a single floor vote.

Recommended arbitration rule:

- when fingerprint produces a strong global `(room, floor)` candidate and geometry quality is weak or ambiguous, let fingerprint dominate floor selection,
- when fingerprint is ambiguous and geometry quality is strong, let geometry constrain the room within the current coarse floor posterior,
- when both are weak, hold the previous stable room/floor instead of forcing a switch.

### Stage 6: Optional global 3D-first solve

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
- optional topology multiplier for non-adjacent floors,
- robust residual downweighting from the solver.

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
- previous stable room/floor,
- optional connector topology priors,
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
- add side-by-side diagnostics for:
  - floor evidence,
  - included vs penalized anchors,
  - would-have-been-rejected anchors,
  - previous-state continuity,
  - fingerprint-global candidate output.

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
  - rejected cross-floor anchors and their hypothetical inflated sigma,
  - current room result,
  - global fingerprint candidate result,
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
- compare its inferred floor against the current pipeline on replay traces.

Expected payoff:

- evidence on whether fingerprint can resolve split-level ambiguity better than geometry.

### Phase 4: Hybrid floor arbitration

- if diagnostics validate fingerprint usefulness, feed fingerprint floor evidence into the floor posterior,
- require an explicit replay threshold before promoting it:
  - target `>85%` floor-correct top-room on known traces,
  - target score gap or confidence margin large enough to avoid frequent ambiguity,
- allow it to overrule ambiguous RSSI-only floor outcomes in limited conditions,
- keep explicit ambiguity states where signals disagree strongly.

### Phase 5: Optional room-classifier expansion

- if replay shows the global fingerprint signal is consistently good,
- expand room inference from floor-gated to floor-soft,
- keep strong hysteresis and ambiguity handling.

### Phase 6: Optional global 3D-first evaluation

- only after Experiments 2 and 4 below pass,
- gated behind a separate feature flag,
- compare against the softer hybrid pipeline rather than replacing it blindly.

## Testing Plan

### Unit tests

- floor evidence scoring remains unchanged in the baseline path,
- floor switch does not clear solve state,
- adjacent-floor anchors are included with sigma inflation when the feature flag is on,
- non-adjacent-floor anchors remain diagnostic-only in the first rollout,
- downgraded 3D-to-2D paths preserve `z` prior and reduce confidence,
- global fingerprint scoring can produce a room/floor candidate outside the current selected floor,
- hybrid floor arbitration can hold ambiguity instead of forcing a wrong room,
- geometry quality and residual consistency reduce the influence of weak solves.

### Replay tests

Use saved `history.csv`, logs, calibration stores, and current anchor config.

Required scenarios:

- stable `Guest Room` occupancy near `street_level` scanners,
- `Guest Room -> Garage front` failure replay,
- split-level stair traversal between basement, street, and ground,
- outdoor transition into `garage_front`,
- sparse-anchor periods where 3D temporarily drops to 2D.

### Regression targets

- no immediate collapse from `Guest Room` to `Garage front` caused solely by a floor challenger,
- no full solver cold restart after a brief floor flip,
- same-floor anchors are not lost merely because floor evidence briefly favored another level,
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
- fingerprint-global candidate.

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

### Experiment 4: Cross-floor range bias characterization

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

### Experiment 5: State reset elimination

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

## Recommended First Implementation Slice

The first slice should be intentionally narrow and directly driven by the review findings:

1. Add diagnostics for floor evidence, would-be-rejected anchors, and fingerprint-global candidates.
2. Remove floor-switch cold reset and preserve priors through a challenger.
3. Add adjacent-floor soft anchor inclusion behind a feature flag.
4. Add cross-floor fingerprint diagnostics behind a feature flag.
5. Replay the `Guest Room` and `Garage front` traces before changing final room assignment behavior.

This is the minimum change set that tests the central hypotheses without committing to a full global 3D-first rewrite.

## Acceptance Criteria

- Brief floor challengers no longer wipe the entire solve state.
- Split-level replay traces stop producing catastrophic room jumps caused by anchor-count collapse.
- Cross-floor fingerprint diagnostics provide useful floor/room evidence on known traces.
- Adjacent-floor soft anchor inclusion improves continuity without materially worsening residual quality.
- Geometry quality and residual consistency are used to suppress low-trust geometry from forcing room/floor changes.
- A decision on true global 3D-first solving is deferred until replay data demonstrates that it is both observable and beneficial.

---

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
