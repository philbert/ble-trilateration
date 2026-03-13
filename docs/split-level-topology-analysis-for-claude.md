# Split-Level Topology Analysis For Claude Review

## Purpose

This note is a deliberate step back from the recent implementation work.

The current effort may be optimizing a local maximum: tuning floor challenger logic inside a floor-first architecture, instead of solving the underlying physical reasoning problem.

The goal of this review is to critique the problem framing itself and propose a better target architecture for split-level homes.

## Core Claim

The repeated `ground_floor -> street_level -> garage_front` failures do not look like isolated threshold bugs.

They look like a topology failure:

- Bermuda allows a floor change without strong evidence that the device plausibly passed through a real floor-transition region.
- Once the floor flips, room inference becomes trapped on the new floor and drifts toward plausible rooms there.
- This means downstream room errors are often consequences of an earlier impossible floor transition, not independent classification errors.

## Physical Reality

In this house, floor changes are not arbitrary.

- The house has `basement`, `street_level`, `ground_floor`, and `top_floor`.
- `street_level` is an intermediate split level, not a full separate floor plate.
- A device cannot move from `Guest Room` or `Ana's Office` directly to `Garage front`.
- The only physically valid way to change floors is to pass through one of a small set of real transition points or transition zones.

That physical constraint is stronger than any current RSSI-only floor challenger.

## Recent Evidence

### Failure 1: Guest Room -> Garage front

From March 13, 2026:

- `22:31:15Z`: area becomes `Guest Room`
- `23:31:20` local in the HA log:
  - `selected=ground_floor`
  - `challenger=street_level`
  - `fp_floor=ground_floor`
  - `fp_conf=0.619`
  - `transition_support=0.000`
- `22:31:33Z`: floor becomes `Street level`
- `22:31:40Z`: area becomes `Garage front`

Interpretation:

- Bermuda was still on the ground floor and still had ground-floor fingerprint evidence.
- No transition sample support was active.
- The floor still flipped.
- Once the floor flipped, the room followed.

### Failure 2: Ana's Office -> Garage front

From March 13, 2026:

- `22:49:24Z`: area becomes `Ana's Office`
- `23:49:28` local in the HA log:
  - `selected=ground_floor`
  - `fp_floor=ground_floor`
  - `fp_conf=0.603`
  - `transition_support=0.000`
- `23:50:12` local:
  - `selected=ground_floor`
  - `challenger=street_level`
  - `fp_floor=street_level`
  - `fp_conf=0.572`
  - room still resolves to `Ana's Office`
  - `transition_support=0.000`
- `22:50:16Z`: floor becomes `Street level`
- `22:50:24Z`: area becomes `Garage front`

Interpretation:

- The same impossible transition happened from a different ground-floor room.
- This strongly suggests the bug is not specific to `Guest Room`.
- It is also not fixed by the current transition-sample hook, because transition diagnostics remained zero throughout the challenger.

## Why The Current Direction Looks Like A Local Maximum

Recent work has improved observability and removed some obvious bugs:

- floor-switch cold resets were removed,
- diagnostics are much better,
- cross-floor anchor inclusion experiments were run,
- cross-floor fingerprint guidance was added,
- transition samples were added,
- transition dwell reduction and later no-route veto logic were added.

But the core failure still persists.

That suggests the current optimization target may be wrong:

- The architecture still begins from a floor-first worldview.
- Transition evidence is being used as a modifier on challenger timing.
- Physical route plausibility is still not the primary state constraint.

In other words, the system still asks:

- "Which floor currently has the strongest evidence?"

before it asks:

- "Is this floor change physically plausible from where the device was recently estimated to be?"

For a split-level house, that order may be backwards.

## Stronger Problem Framing

This looks more like a constrained state-estimation problem than a pure instantaneous classification problem.

At each time step, Bermuda should not only infer:

- current `x/y/z`,
- current floor,
- current room,

it should also enforce transition plausibility over time:

- what floor changes are reachable from the recent path,
- whether the estimated motion could have reached a floor-transition point,
- whether the elapsed time is enough to traverse a valid path at realistic speed.

This does not necessarily require a mathematically heavy solution, but it does require the topology model to be first-class.

## Signals Bermuda Already Has

The system already appears to have enough information to reason much better:

- scanner anchor `x/y/z`,
- per-floor scanner metadata,
- solved or partially solved `x/y/z`,
- room calibration samples,
- transition sample `x/y/z` points or zones,
- timestamps,
- velocity estimates,
- maximum speed constraints,
- fingerprint evidence,
- RSSI floor evidence,
- continuity from prior stable room/floor.

The problem may not be missing data. It may be insufficient use of the spatial and temporal structure already available.

## Key Design Question

What should a transition sample actually be?

There are at least three plausible models:

### Option A: Transition sample as a calibration-like learned zone

Treat transition samples similarly to room calibration samples:

- they are real observation windows at known `x/y/z`,
- they collect fingerprints and quality metrics over time,
- runtime checks ask whether the current live fingerprint and geometry resemble a known transition region.

Strength:

- naturally uses live evidence, not just declared geometry.

Weakness:

- may still be too "sample matching" oriented if what is really needed is a stronger path constraint.

### Option B: Transition sample as an explicit topology node or zone

Treat transition samples primarily as topology primitives:

- each one defines a place where floor changes are physically allowed,
- each one has geometry and supported destination floors,
- floor changes are only plausible when the recent estimated path intersects such a zone within time and speed limits.

Strength:

- directly matches physical reality.

Weakness:

- may need a more explicit motion/path model than Bermuda currently has.

### Option C: Hybrid

Use transition samples as both:

- learned evidence regions,
- topology constraints.

This may be the most realistic approach:

- the geometry defines what transitions are physically possible,
- the learned fingerprints define whether the device was actually near the transition point.

## Topology Heuristic That Seems Necessary

A strong heuristic that appears justified:

- a floor change should be strongly disfavored, or outright vetoed, if there is high confidence that the device has not recently passed through, moved toward, or been near a valid transition point for that floor change.

This should still be flexible enough to tolerate latency and imperfect room assignment.

That implies some form of recent route memory:

- if a device was near a valid transition point recently, a floor change may remain plausible for a short window,
- if it was not, a cross-floor challenger from a remote room should be heavily constrained.

## Kinematic Reasoning

Velocity and max-speed constraints likely matter here.

If Bermuda has:

- current and prior `x/y/z`,
- estimated velocity,
- a max speed limit,
- known transition point coordinates,

then it should be possible to reason about whether a claimed floor change is reachable.

Example:

- If the device was recently stable in `Guest Room`,
- and the nearest valid path to `street_level` requires moving through `Entrance Hall` / stairwell / door transition points,
- and the elapsed time is too short for that route,
- then a `street_level` challenger should be very hard to accept.

This does not require perfect route planning. Even a coarse reachable-within-time heuristic may be much better than the current challenger logic.

## Concern About The Current Transition Hook

The recent implementation treated transition support mostly as:

- dwell reduction when transition evidence is positive,
- later, some veto logic when evidence is absent.

That may still be too downstream and too weak.

The deeper issue may be:

- transition plausibility should not merely adjust challenger timing,
- it should shape the state space of plausible floor changes before room assignment becomes trapped on the wrong floor.

## Questions For Claude To Critique

Please critique the following directly:

1. Is the problem primarily a topology / route-plausibility problem rather than a floor-threshold problem?
2. Is it a mistake to keep trying to tune the current floor-first challenger architecture?
3. Should transition samples be modeled more like:
   - calibration-like learned evidence regions,
   - explicit topology nodes/zones,
   - or a hybrid of both?
4. Should a floor change be impossible, or only extremely unlikely, unless a valid transition point was observed recently enough?
5. What is the simplest useful way to incorporate time, velocity, and maximum speed into floor-change plausibility?
6. Is there a better formulation here:
   - HMM/state machine,
   - constrained graph traversal,
   - route plausibility scoring,
   - factor graph / smoother,
   - or something simpler?
7. Given the repeated `Guest Room -> Garage front` and `Ana's Office -> Garage front` failures, what architecture change is most likely to solve the actual problem instead of refining a local maximum?

## What A Good Answer Would Provide

A useful critique should:

- challenge the framing if it is wrong,
- say whether the recent implementation path is fundamentally mis-prioritized,
- propose the right level of topology modeling for Bermuda,
- define how strong floor-transition constraints should be,
- say whether transition samples should primarily be evidence, topology, or both,
- recommend the next architectural slice with the highest information gain.

## Bottom Line

The repeated failures now suggest:

- the issue is not one bad room,
- the issue is not one missing reset fix,
- the issue is not just missing transition samples,
- the issue is that Bermuda still allows floor changes that are not physically plausible from the recent path.

That is the part that needs to be reviewed and likely redesigned.
