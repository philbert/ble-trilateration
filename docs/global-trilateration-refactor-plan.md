# Global Trilateration Refactor Plan

## Goal

Replace Bermuda's current floor-first, same-floor-only trilateration pipeline with a global localization pipeline that:

- uses all fresh scanner data when estimating position,
- infers floor from solved geometry and room evidence instead of pre-filtering by floor,
- preserves off-floor anchors as useful geometric constraints instead of rejecting them outright,
- handles houses with intermediate levels such as `street_level` between `basement` and `ground_floor`.

This plan is aimed at the current failure mode where a device physically on `ground_floor` can be pulled into `street_level`, causing same-floor scanners to be marked `rejected_wrong_floor` and room classification to collapse onto the wrong level.

## Problem Summary

The current coordinator pipeline is structurally floor-gated:

1. Choose a floor from scanner RSSI grouped by `scanner.floor_id`.
2. Reject adverts from scanners on other floors as `rejected_wrong_floor`.
3. Trilaterate using only anchors on the chosen floor.
4. Run room classification using only rooms and fingerprints on that floor.

That architecture is too lossy for multi-level homes where:

- levels are vertically close together,
- some rooms are physically nearer to scanners on another level,
- the Home Assistant floor model is a coarse abstraction over continuous height,
- intermediate floors such as `street_level` are not true isolated storeys.

In that environment, floor should be an output of the localization pipeline, not a hard gate applied before it.

## Design Principles

- Global first: use all fresh anchors by default.
- Soft penalties over hard rejection: off-floor anchors should be downweighted, not discarded.
- Continuous height model: use solved `z` as a first-class signal.
- Floor as a posterior: infer floor after solving position, not before.
- Room as a posterior: score rooms across all floors, then use floor as a soft prior if needed.
- Preserve diagnostics: keep explicit reasons for stale inputs, low confidence, poor geometry, or ambiguity.
- Ship incrementally: stage the new pipeline behind a feature flag until real-world replay data shows it is better.

## Non-Goals

- Do not remove existing Bermuda sensors or current diagnostics in the first step.
- Do not force the new model on all users immediately.
- Do not assume Home Assistant floors are evenly spaced or geometrically meaningful beyond optional priors.

## Proposed Target Pipeline

### Stage 1: Build a global anchor set

For each fresh advert:

- require non-stale advert,
- require usable anchor coordinates,
- require usable range estimate,
- keep the scanner even if its `floor_id` differs from the current best floor,
- attach quality metadata:
  - scanner floor,
  - anchor coordinates,
  - range sigma,
  - advert age,
  - optional floor mismatch penalty term.

Hard rejection should remain only for:

- `rejected_stale`,
- `rejected_missing_anchor`,
- `rejected_no_range`.

`rejected_wrong_floor` should no longer remove anchors from the solve path.

### Stage 2: Run a global 3D solve when possible

Preferred mode:

- use all eligible anchors in a single robust 3D solve,
- keep the current soft-L1 and sigma-based weighting model,
- inflate uncertainty for cross-floor anchors rather than discarding them,
- allow geometry from basement, street, ground, and top-floor anchors to constrain one shared solution.

Fallback mode:

- if fewer than 4 anchors have known `z`, still solve globally in 2D using all anchors,
- retain the last reliable `z` as a prior rather than dropping height instantly,
- publish reduced confidence instead of pretending the floor decision is final.

### Stage 3: Derive floor from solved position

Floor selection should combine:

- solved `z`,
- distance to per-floor room samples,
- live fingerprint similarity,
- optional floor priors from scanner RSSI totals,
- optional hysteresis from the previous solved floor.

The floor model should be continuous, not a categorical scanner vote.

Recommended approach:

- define each floor as a soft vertical band,
- infer those bands from calibration samples or Home Assistant floor level ordering,
- score each floor from solved `z` against those bands,
- blend the vertical score with room posterior support.

This is important for homes with an intermediate level such as `street_level`, where `z` may legitimately lie between basement and ground-floor clusters.

### Stage 4: Derive room across all floors

Room classification should no longer pre-filter to one floor before scoring.

Instead:

- score all trained rooms from geometry using solved `x/y/z`,
- score all trained rooms from live fingerprints,
- optionally add a soft floor prior from Stage 3,
- rank all rooms globally,
- derive final floor from the chosen room if that improves consistency.

This preserves the ability for a room on `ground_floor` to beat a `street_level` room even when some strong anchors are on `street_level`.

## Weighting Model

The main architectural change is not a new solver, but a different treatment of anchor relevance.

Recommended weighting terms per anchor:

- base measurement weight from `sigma_m`,
- advert age multiplier,
- robust loss from residual size,
- cross-floor sigma inflation,
- optional vertical-separation penalty relative to predicted `z`,
- optional topology penalty for floors that are not adjacent.

Important detail:

- cross-floor influence should be reduced, not zeroed,
- strong nearby off-floor anchors may still carry real geometric information,
- the solver should be allowed to discover when the current floor assumption is wrong.

## Floor Model Options

There are three viable ways to map solved `z` to floor:

### Option A: Home Assistant floor level priors

Use Home Assistant floor ordering as a weak prior only.

Pros:

- simple,
- no extra calibration structure required.

Cons:

- Home Assistant floors are semantic, not geometric,
- intermediate levels may not match equal height spacing.

### Option B: Sample-derived vertical bands

Infer floor `z` ranges from calibration samples.

Pros:

- uses real observed data,
- adapts to split-level homes naturally.

Cons:

- depends on enough samples across floors.

### Option C: Hybrid

Start from Home Assistant floor ordering, then refine with sample-derived `z` clusters as samples accumulate.

Recommended choice: Option C.

## Migration Strategy

### Phase 0: Design and diagnostics

- add a feature flag such as `global_trilat_pipeline`,
- log side-by-side diagnostics:
  - current floor-first result,
  - proposed global solve result,
  - anchor set differences,
  - floor posterior differences,
  - room posterior differences.

No user-visible behavior change yet.

### Phase 1: Global solve behind a flag

- keep existing entities,
- use all eligible anchors in the solve path,
- stop hard-filtering anchors by selected floor,
- compute a provisional global `x/y/z`,
- keep old room/floor logic available for comparison.

### Phase 2: Soft floor inference

- replace hard floor vote with a floor posterior model,
- use solved `z` plus room/sample support,
- expose floor confidence separately from position confidence.

### Phase 3: Global room classifier

- remove same-floor-only room prefiltering,
- classify across all trained rooms,
- feed floor posterior in as a prior, not a gate.

### Phase 4: Remove old floor-first path

- once replay data and live testing show clear improvement,
- remove `rejected_wrong_floor` from the solve path,
- retain it only as a diagnostic label if needed for UI compatibility.

## Proposed Code Changes

### `custom_components/bermuda/coordinator.py`

- replace the current floor-first anchor filtering path,
- build one global eligible anchor list,
- compute global solve inputs before final floor assignment,
- introduce floor posterior computation after solving,
- update diagnostics so anchors are marked with soft weighting state rather than binary wrong-floor rejection.

### `custom_components/bermuda/trilateration.py`

- keep the current robust 2D and 3D solvers,
- extend anchor weighting inputs if needed,
- optionally add a solver wrapper that accepts per-anchor prior penalties or sigma inflation terms.

### `custom_components/bermuda/room_classifier.py`

- remove the requirement that only rooms on `floor_id` are scored,
- add an optional floor prior instead,
- support a global ranking mode across all trained rooms.

### Calibration and sample handling

- add helper methods to derive per-floor `z` bands from calibration samples,
- optionally compute per-room vertical centroids and variance,
- preserve `anchor_layout_hash` scoping exactly as today.

## Testing Plan

### Unit tests

- global solve uses anchors from multiple floors,
- off-floor anchors are downweighted instead of dropped,
- floor posterior uses solved `z`,
- intermediate floors such as `street_level` can exist between basement and ground floor,
- room classifier can score rooms across all floors,
- floor prior influences room ranking without hard exclusion.

### Replay tests

Use saved `history.csv`, logs, and calibration stores to replay real traces.

Required cases:

- stable `ground_floor` bedroom occupancy near `street_level` scanners,
- transition from guest room to garage-front outdoors,
- split-level stair traversal,
- sparse-anchor moments where 3D temporarily degrades to 2D.

### Regression targets

- no more immediate same-floor scanner rejection after a bad floor flip,
- no `Guest Room -> Garage front` switch caused solely by early floor gating,
- `z` continuity preserved through temporary anchor loss,
- floor confidence drops before room assignment becomes absurd.

## Risks

- Off-floor anchors can be badly biased by slabs and walls, so careless global solving may overfit to NLOS ranges.
- Global room scoring may need stronger ambiguity handling to avoid unstable room switching.
- Sample coverage may be uneven across floors, requiring conservative defaults.
- Diagnostics will become more complex because anchor participation is no longer binary.

## Open Questions

- How should floor `z` bands be initialized before enough calibration samples exist?
- Should cross-floor sigma inflation depend only on floor difference count, or also on absolute vertical metres?
- Should room classification use a single global posterior, or a two-stage floor-then-room posterior with soft coupling?
- How should outdoor areas such as `garage_front` and `driveway` influence floor inference?
- Should `street_level` be treated as a true floor, or as a transitional vertical band with looser room priors?

## Recommended First Implementation Slice

The first slice should be intentionally narrow:

1. Add a feature flag for the global pipeline.
2. Build a global anchor set without `rejected_wrong_floor` filtering.
3. Run the existing robust solver on all anchors.
4. Compute a provisional floor posterior from solved `z` plus current RSSI floor evidence.
5. Log old and new outputs side by side.
6. Replay the bedroom and garage traces before changing room assignment behavior.

This will validate the central hypothesis before the room-classifier rewrite.

## Acceptance Criteria

- Global solve remains numerically stable on existing homes.
- Floor inference improves on split-level and intermediate-level homes.
- Same-floor scanners are no longer discarded because of an early bad floor vote.
- Room assignment becomes more physically plausible in cases where rooms are near anchors on adjacent levels.
- The new path can be compared against the current path with clear diagnostics before rollout.
