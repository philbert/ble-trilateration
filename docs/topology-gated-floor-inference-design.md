# Topology-Gated Floor Inference Design

## Purpose

This document proposes a new target architecture for split-level floor inference in Bermuda.

It exists because the current line of work has improved diagnostics and removed some bugs, but has not solved the core user-facing failure:

- a device remains physically on `ground_floor`,
- Bermuda flips it to `street_level`,
- room inference then drifts to `Garage front`.

The key design change is simple:

- **topology and reachability become first-class constraints before a floor change is accepted**
- instead of being treated as late modifiers on an RSSI-driven challenger.

This document is intentionally architectural. It is not an implementation checklist.

## Problem Statement

The current failure is not just "bad floor classification."

It is an **invalid state transition**:

- Bermuda allows a floor change that is not physically plausible from the recent position history.
- Once the floor changes, room inference is forced onto the wrong floor and starts producing plausible rooms there.

That means many room errors are secondary effects. The primary bug is that the floor change was permitted at all.

## Physical Invariants

For a split-level house, the following are true:

1. A device cannot arbitrarily change floors.
2. A floor change must pass through one of a small set of physical transition zones.
3. A device that was recently stable and far from any transition zone cannot legitimately appear on another floor a few seconds later.
4. RSSI alone is too structurally ambiguous to override those constraints.

These invariants should be reflected directly in the estimation pipeline.

## Design Goals

The architecture should:

- prevent impossible floor changes before room inference is trapped on the wrong floor,
- remain tolerant of room-assignment latency and imperfect geometry,
- use the data Bermuda already has rather than requiring a heavyweight rewrite,
- degrade gracefully when transition zones are not configured,
- avoid adding more threshold-only veto machinery to the current floor-first state machine.

## Core Idea

The estimator should separate two questions:

1. **Which floors are physically reachable right now?**
2. **Among the reachable floors, which one is best supported by evidence?**

The first is a topology and motion question.

The second is an evidence fusion question.

The current architecture mostly does question 2 first and tries to repair mistakes afterward. This design reverses that priority.

## Concepts

### Transition Zone

A transition zone is a Bermuda-native object representing a real place where floor changes are physically possible.

Each transition zone has:

- a stable internal id,
- one or more recorded captures,
- geometric support in `x/y/z`,
- a support radius or support envelope,
- a set of allowed destination floors.

Transition zones are not Home Assistant floors or areas. They are house-specific topology primitives.

### Reachability Gate

A reachability gate answers:

- given the device's last confident pre-challenge position,
- the recent estimated motion budget after the challenge began,
- and the configured transition zones,
- is a challenger floor physically reachable within the elapsed time?

This is the primary new mechanism.

### Floor Evidence

Once reachability is known, floor evidence combines:

- fingerprint-global floor evidence,
- RSSI floor evidence,
- continuity priors,
- optionally geometry-derived hints.

But only among floors that are currently reachable.

## Recommended Transition-Zone Model

Transition samples should be treated as a **hybrid**:

- topology first,
- learned evidence second.

That means:

- the geometric existence of the zone defines where floor changes are allowed,
- captured RSSI/fingerprint observations help determine whether the device was actually near that zone,
- but the zone’s primary role is not to behave like another room sample,
- its primary role is to constrain state transitions.

So:

- transition zones are not ordinary room calibration samples,
- but they should still preserve calibration-like evidence because that helps detect proximity robustly.

## Per-Floor X/Y Envelope Constraints

Once `z` is resolved with high confidence and a floor is confirmed, the X/Y solve should be constrained to the physical footprint of that floor.

A device on a confirmed floor cannot be outside the footprint of that floor. Applying this as a constraint after floor confirmation significantly tightens room assignment.

### Deriving the Envelope

The floor envelope is derived from calibration samples on that floor:

- for each sample, expand outward by its `sample_radius_m` in all directions,
- take the bounding box (or convex hull) of the expanded points.

This accounts for the fact that a sample centroid represents a zone, not a point. A device at the edge of a sample's radius is still legitimately on that floor.

Scanner anchor positions on the floor can supplement the envelope, particularly for floors that have no calibration samples yet. This gives a useful footprint estimate before any calibration has been collected.

### Street Level Exception

Street level should be treated as unbounded in X/Y. It may include outdoor areas, slopes, or large open spaces where no meaningful bounding box applies. Either the user marks it explicitly as unbounded in config, or Bermuda infers it from the large spread of its samples.

### Soft vs Hard Clamping

A device confirmed on a floor can legitimately be near the floor boundary. Hard-clipping the solve at the envelope edge risks biasing positions of devices genuinely near walls or windows. Soft clamping — penalising positions outside the envelope rather than rejecting them outright — is preferable.

### Envelope Growth

The envelope is derived from available samples and grows as more calibration is collected. A sparse floor will have a conservative (potentially too small) envelope. The sample radius expansion partially compensates for this, but users should be aware that uncalibrated areas of a floor are not yet constrained.

## Per-Floor Z Configuration

Users should declare the floor surface Z height for each Home Assistant floor during config flow.

This is a minimal config ask with disproportionate value. The user already knows where their floors are; they just need to provide the Z coordinate in Bermuda's coordinate system.

Each floor should accept either:

- a fixed `floor_z_m` value for flat indoor floors,
- or a `floor_z_min_m` / `floor_z_max_m` range for floors with natural Z variation (outdoor areas, slopes, split entries).

Street level is the most common case for the variable range, since it may include outdoor or sloped areas.

### Phone-Height Band

From `floor_z_m`, Bermuda derives a **phone-height band** automatically:

```
phone_band = [floor_z_m, floor_z_m + 1.2]
```

This is grounded in physical reality: a device spends the overwhelming majority of its time between the floor surface and approximately 1.2m above it (pocket, table, or hand height). The range from 1.2m up to the ceiling is rarely occupied.

This band becomes a strong prior for `z` during trilateration and for floor evidence fusion.

### Floor Discrimination

In a multi-storey house the phone-height bands for adjacent floors are separated by the structural gap between ceiling and the floor above. In practice this gap is typically 1.0m or more, which is larger than normal BLE ranging noise. Clean band separation means `z` alone can often discriminate the floor without relying on fingerprint evidence.

For example, in a four-floor house (basement, street level, ground floor, top floor) with ~2.2m floor-to-floor heights, the bands would have no overlap and a meaningful gap between each pair.

### Implications for the Pipeline

- **Stage 1**: use per-floor Z bands as a bounded prior when solving for `z`.
- **Stage 3**: floor surface Z tightens the geometry used to evaluate reachability budget.
- **Transition zones**: user-declared floor Z makes it straightforward to assign Z coordinates to transition zones without ambiguity.

## Minimal Geometry Model

The geometry does not need to be sophisticated.

For the first usable design, each transition zone can be represented as:

- a centroid in `x/y/z`,
- a support radius,
- optionally a confidence or spread from recorded captures.

That is enough for:

- simple distance-to-zone checks,
- uncertainty-expanded proximity checks,
- conservative lower-bound reachability tests.

There is no need to introduce path planning or a full room graph in the first version.

## Reachability Model

The lightest useful reachability model is:

1. When a new floor challenger appears, freeze a **challenger reference position** from the last confident pre-challenge state.
2. Track recent device motion after that point over a short time window.
3. For the challenger floor, find the nearest transition zone that allows that floor change.
4. Compute whether the device could plausibly have reached that zone from the challenger reference position.

### Reachability Budget

Let:

- `elapsed_time_s` = time since the challenger began,
- `velocity_budget_m` = recent integrated motion estimate since the challenger began,
- `max_speed_budget_m` = `elapsed_time_s * max_speed_m_per_s`,
- `uncertainty_budget_m` = position uncertainty allowance, capped for the first rollout,
- `reachable_budget_m` = `min(max_speed_budget_m, velocity_budget_m) + uncertainty_budget_m`.

Then:

- if the nearest valid transition zone is farther away than `reachable_budget_m`,
- the challenger floor is physically implausible.

This can be done with Euclidean distance only.

That is sufficient because Euclidean distance is a lower bound on real travel distance. If even the straight-line path is unreachable, the real path is unreachable too.

For the first rollout:

- `uncertainty_budget_m` should be capped rather than unbounded,
- a reasonable first cap is `3.0 m`,
- the cap should remain configurable because it is layout-dependent.

## State To Track

For each tracked device, the floor estimator should track:

- current stable floor,
- current stable room,
- last stable position estimate,
- challenger reference position captured before the floor-ambiguity episode,
- recent position/velocity history,
- current challenger floor,
- challenger start time,
- most recent credible transition-zone proximity,
- last transition zone that was plausibly traversed,
- uncertainty bounds on recent position.

This is still a small state machine. It does not require a full smoother or factor graph.

## Proposed Pipeline

### Stage 1: Geometry Solve

Run a full 3D Cartesian solve using **all available scanners regardless of floor**.

There is no physical justification for excluding cross-floor scanners from the solve. BLE signal propagation through a floor slab is not categorically different from propagation through a wall. Restricting the solve to same-floor scanners discards real distance information and does not improve accuracy.

`z` should be treated as the most important coordinate to resolve first:

- `z` determines which floor the device is on, which in turn constrains `x/y` and room assignment.
- `z` has a much tighter real-world prior than `x` or `y`: in a typical house, floor surfaces are at discrete, known heights with small spread, whereas `x` and `y` range freely across the floor plate.
- That restricted domain makes `z` easier to resolve with confidence, not harder.

The correct pipeline is therefore: resolve `z` first to confirm floor, then constrain `x/y` to that floor for room assignment.

Outputs:

- estimated `x/y/z`,
- geometry quality,
- residual consistency,
- uncertainty indicators.

This stage should not decide floor changes by itself.

### Stage 2: Transition-Zone Proximity Inference

Independently of room assignment, evaluate:

- which transition zones are near the current estimate,
- which transition zones were near the device recently,
- whether the current evidence supports actual proximity to any transition zone.

This stage must be position-based, not room-context-based.

That means:

- transition proximity must not depend on the current assigned room,
- `room_area_id` may remain useful as metadata for diagnostics,
- but a wrong or lagging room assignment must not disable transition-zone detection.

This stage should be able to succeed even if room assignment is lagging.

### Stage 3: Reachability Gate

Given:

- current and recent position estimates,
- recent motion budget,
- nearest valid transition zone for a challenger floor,

decide whether that challenger floor is reachable or unreachable.

For the first implementation, keep this binary:

- `reachable`: normal floor evidence competition is allowed
- `unreachable`: challenger cannot advance

The "weakly reachable" middle state is plausible in principle, but it should be deferred until the binary gate is validated on replay traces.

### Stage 4: Floor Evidence Fusion

Among reachable floors only, combine:

- fingerprint-global floor evidence as the primary selector,
- RSSI floor evidence as a secondary selector,
- continuity priors,
- optional geometry-derived hints.

This is where split-level ambiguity should be resolved.

### Stage 5: Room Inference

After floor selection:

- perform room inference,
- preferably with a floor-soft rather than floor-hard view over time,
- but it is acceptable to keep room inference mostly floor-scoped in the first version if the floor gate is working correctly.

### Stage 6: Hysteresis

Use hysteresis only for stability at legitimate boundaries.

It should not be the main defense against impossible teleportation.

## Signal Priority

Recommended priority order:

1. Physical reachability through transition zones
2. Fingerprint-global floor evidence
3. RSSI floor evidence
4. Geometry-derived floor hints
5. Hysteresis and continuity tuning

This is intentionally different from the current ordering.

## Hard vs Soft Constraint

The transition gate should not behave the same in every situation.

Recommended rule:

- if no transition zones are configured for the current layout, fall back to existing soft floor behavior
- if transition zones are configured and a challenger floor is unreachable, block challenger advancement

For the first rollout, this should be a binary gate:

- hard when the transition is impossible,
- absent when no transition zones are configured.

A graded "near-hard" or probabilistic middle state can be considered later if replay traces show the binary version is too blunt.

## Why This Is Better Than More Veto Tuning

Threshold tuning in the current model mostly changes:

- how long a challenger waits,
- how much fingerprint evidence is needed to stall it,
- when a veto expires.

That still assumes the challenger is fundamentally eligible to win.

The topology-gated design changes a more important question:

- whether the challenger is even allowed to compete in the first place.

That is the right place to encode physical reality.

## Transition-Zone Semantics

The semantics of a transition zone should be:

- "this is a place where floor changes are allowed"

not:

- "this is just another evidence point that can slightly speed up a switch"

That distinction should guide both storage and runtime use.

## Failure Mode Coverage

This design directly targets the known failures:

### Guest Room -> Garage front

- device is stable on `ground_floor`,
- no recent transition-zone proximity,
- challenger floor `street_level` is unreachable,
- challenger is blocked before room inference can drift.

### Ana's Office -> Garage front

- same logic,
- demonstrates this is a general split-level failure mode, not a room-specific one.

## Minimal Rollout Strategy

This design should be introduced conservatively:

1. Add exact switch-time diagnostics for floor-switch preconditions and reachability decisions.
2. Add challenger reference position capture from the last confident pre-challenge state.
3. Implement transition-zone proximity and recent-transition memory without changing assignment.
4. Add a binary reachability gate behind a feature flag.
5. Replay the known failure traces.
6. Only then simplify or remove older veto machinery.

This keeps regression risk controlled.

## Open Design Questions

These should be answered before implementation:

1. How should uncertainty inflate transition-zone radius during weak geometry?
2. What is the best conservative motion budget:
   - recent integrated velocity,
   - average velocity over challenger dwell,
   - or capped max-speed budget?
3. How long should recent transition memory remain valid for this house?
4. What is the physically reasonable uncertainty cap for transition reachability in this layout?
5. Should room assignment remain floor-scoped initially, or should floor-soft room evidence already be introduced at the same time?
6. How should the user experience look when no transition zones are configured for a layout?
7. Per-floor Z configuration should be collected in the config flow alongside or immediately after transition zone setup. Each HA floor should prompt for a floor surface Z (fixed or range). This is a prerequisite for the phone-height band prior and for well-grounded transition zone placement.

## Recommended Next Review

The next review should focus on this design, not on the older phased refactor plan.

Specifically, review should challenge:

- whether the transition gate should be hard, near-hard, or probabilistic,
- whether Euclidean reachability is sufficient,
- whether the hybrid transition-zone model is the right abstraction,
- what the minimal viable floor-state machine should be,
- and whether this architecture is simpler and safer than continuing to tune challenger veto rules.

## Bottom Line

The split-level problem should be treated as:

- **topology-gated floor state estimation**

not:

- **RSSI-first floor classification with increasingly elaborate veto logic**.

If Bermuda does not first ask whether a floor change is physically plausible, it will continue to make convincing but impossible transitions.
