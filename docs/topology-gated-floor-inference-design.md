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

- given the recent estimated motion of the device,
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

1. Track recent device motion over a short time window.
2. For a challenger floor, find the nearest transition zone that allows that floor change.
3. Compute whether the device could plausibly have reached that zone recently.

### Reachability Budget

Let:

- `elapsed_time_s` = time since the challenger began, or since the last stable floor state,
- `velocity_budget_m` = recent integrated motion estimate,
- `max_speed_budget_m` = `elapsed_time_s * max_speed_m_per_s`,
- `uncertainty_budget_m` = position uncertainty allowance,
- `reachable_budget_m` = `min(max_speed_budget_m, conservative_motion_budget_m) + uncertainty_budget_m`.

Then:

- if the nearest valid transition zone is farther away than `reachable_budget_m`,
- the challenger floor is physically implausible.

This can be done with Euclidean distance only.

That is sufficient because Euclidean distance is a lower bound on real travel distance. If even the straight-line path is unreachable, the real path is unreachable too.

## State To Track

For each tracked device, the floor estimator should track:

- current stable floor,
- current stable room,
- last stable position estimate,
- recent position/velocity history,
- current challenger floor,
- challenger start time,
- most recent credible transition-zone proximity,
- last transition zone that was plausibly traversed,
- uncertainty bounds on recent position.

This is still a small state machine. It does not require a full smoother or factor graph.

## Proposed Pipeline

### Stage 1: Geometry Solve

Run the best available solve using the current trilateration pipeline.

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

This stage should be able to succeed even if room assignment is lagging.

### Stage 3: Reachability Gate

Given:

- current and recent position estimates,
- recent motion budget,
- nearest valid transition zone for a challenger floor,

decide whether that challenger floor is:

- reachable,
- weakly reachable,
- unreachable.

Recommended semantics:

- `reachable`: normal competition allowed
- `weakly_reachable`: allowed only with strong supporting evidence
- `unreachable`: challenger cannot advance

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

The transition gate should not be fully hard in every situation.

Recommended rule:

- if no transition zones are configured for the current layout, fall back to existing soft floor behavior
- if transition zones are configured and a challenger floor is clearly unreachable, block challenger advancement
- if transition zones are configured and a challenger floor is only weakly reachable, require stronger fingerprint evidence than usual

So the topology gate is:

- hard when the transition is impossible,
- soft when the transition is plausible but uncertain.

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

1. Add exact switch-time diagnostics for reachability decisions.
2. Implement transition-zone proximity and recent-transition memory without changing assignment.
3. Add the reachability gate behind a feature flag.
4. Replay the known failure traces.
5. Only then simplify or remove older veto machinery.

This keeps regression risk controlled.

## Open Design Questions

These should be answered before implementation:

1. What position source should the reachability gate trust most:
   - current solve,
   - last stable solve,
   - or an uncertainty envelope over both?
2. How should uncertainty inflate transition-zone radius during weak geometry?
3. What is the best conservative motion budget:
   - recent integrated velocity,
   - average velocity over challenger dwell,
   - or capped max-speed budget?
4. How long should recent transition memory remain valid?
5. Should room assignment remain floor-scoped initially, or should floor-soft room evidence already be introduced at the same time?

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
