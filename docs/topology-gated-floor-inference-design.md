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

---

## Design Advisory — 2026-03-14

*Reviewed against: topology-gated-floor-inference-design.md, split-level-topology-analysis-for-claude.md, global-trilateration-refactor-plan.md, coordinator.py, room_classifier.py, calibration.py.*

---

### 1. Executive Verdict

The direction is right. The framing shift from veto-heavy RSSI-first to topology-gated state estimation is the correct architectural correction, and it is materially better than adding more veto rules.

The key reason it is better is not complexity — it is irreversibility. Veto logic delays a switch; topology logic prevents one. A veto that expires is a timer, not a constraint. If the challenger persists long enough, any veto will eventually exhaust itself. A topology gate that says "this transition has not been geometrically possible in the past N seconds" does not expire. The physical invariant does not weaken with time.

That said, there is one structural problem in this design that is serious enough to affect correctness before the first line is written. The document does not address what happens to the position estimate during the exact failure episode. That omission needs to be resolved before coding starts.

The short version: **the design anchors the reachability gate to the current position estimate, but the current position estimate is precisely what degrades during a floor-ambiguity episode.** If both the floor inference and the geometry solve are degraded at the same time, the reachability gate is computed from a corrupted position — which is the scenario where you need the gate most.

That is not a reason to abandon the design. It is a reason to anchor the reachability budget to the **last confident position**, not the current one.

---

### 2. Strongest Ideas

**The reachability budget concept.** Computing reachability as `motion budget vs distance to transition zone` is the right abstraction. It is physical, simple, and does not require path planning. Euclidean distance is a valid lower bound because no real path is shorter than straight-line distance. If the straight-line path is already unreachable, the real path is definitely unreachable.

**The position of topology in the pipeline.** Stage 3 (reachability gate) runs before Stage 4 (floor evidence fusion). This is the correct ordering. It means evidence only competes among floors the device could actually be on — which eliminates the entire class of failures where RSSI evidence overwhelms physical constraints.

**Separating the two questions.** The design's core statement — "which floors are physically reachable?" and then "among those, which is best supported?" — is a clean separation of concerns. This decomposition survives architectural change.

**The semantic shift for transition zones.** Calling transition zones "topology primitives" rather than "calibration samples with special status" is conceptually correct and will guide the implementation toward the right use. The existing calibration infrastructure is a good implementation substrate, but the semantics are different and the design is right to make that explicit.

**Hard gate when unreachable, soft when weakly reachable.** The graduated response is correct in principle. A floor change should not require extraordinary evidence when the transition is clearly possible (you just crossed the stairwell). It should be much harder when the transition is questionable. It should be prevented entirely when the transition is geometrically impossible. This covers all three cases correctly.

**The fallback when no zones are configured.** The graceful degradation path to existing soft floor behavior is essential for correctness as a general-purpose system. Without it, the gate would break setups that have not configured transition zones.

---

### 3. Weak Spots

**The circular position trust problem.** This is the most important gap in the design. The reachability gate asks: given the current position estimate, how far is the nearest transition zone? But in the split-level failure scenario, the current position estimate is not reliable. When Bermuda is in a floor-ambiguity episode, the anchor set may have collapsed, the solver may have drifted, and the position estimate may be anywhere. The transition-zone distance check is then computed from a position that may be significantly wrong. In the worst case: the position estimate drifts toward street_level, and from that drifted position the transition zone appears nearby, so the gate passes a transition that should have been blocked. This is the same failure mode through a different path.

The fix is to anchor reachability to the **last stable floor state's position**, not the current degraded estimate. The moment floor ambiguity begins (challenger appears), freeze the reference position. The reachability budget grows from that frozen reference point. This makes the gate immune to position degradation that co-occurs with the floor ambiguity.

**The room_context_match circular dependency in the existing implementation.** The current `transition_support_diagnostics()` in calibration.py (line 1022) requires `room_context_match AND supports_challenger` for `support_01 > 0`. Room context match means the current assigned room matches the transition zone's declared room. But in the failure scenario, the room assignment is already drifting (it is part of the failure cascade). If the room is wrong, the transition zone never fires — which means the proximity check is coupled to the room assignment, not to the position. The new design says "evaluate transition-zone proximity independently of room assignment" but does not explicitly break this coupling. It should do so explicitly.

**"Weakly reachable" is unspecified and risky.** The tristate `reachable / weakly_reachable / unreachable` is a good conceptual model but the middle state is not defined. "Allowed only with strong supporting evidence" will, during implementation, become another fingerprint threshold check. That is the same problem as before. The design should either define the weakly-reachable condition precisely, or simplify to a binary: either geometrically possible or not. The binary version is less precise but more robust and much harder to tune into a mess.

**Recent transition memory window is unspecified.** The design tracks "most recent credible transition-zone proximity" but does not define the validity window. This is the most consequential unspecified parameter. If it is too short (say 5 seconds), a legitimate floor change gets blocked because the person walked through the stairwell quickly. If it is too long (say 120 seconds), a person who briefly passed the stairwell 90 seconds ago still has the gate open even though they have returned to Guest Room and been stationary for 80 seconds. The window should be grounded in the physical transit time for this house: how long does it take to complete a full floor change from the nearest transition zone to any room on the target floor? That is a house-specific constant, not a generic one.

**Position uncertainty is mentioned but not defined.** The reachability budget formula includes `uncertainty_budget_m` but the design does not say how to compute it, or what drives it. Position uncertainty from the trilateration solve can be derived from residual RMS and GDOP. But it should not be so large that it makes every transition zone "reachable" in uncertainty. There should be a cap on how much uncertainty is allowed to inflate the reachability budget.

**The "conservative_motion_budget_m" is underspecified.** The formula uses `min(max_speed_budget_m, conservative_motion_budget_m)` but does not define the conservative motion budget. Is it integrated velocity? Average velocity over the challenger window? Cumulative displacement? Each gives different answers. For a stationary device, integrated velocity is near zero (correct). For a walking device, it approximates actual displacement (also correct). But velocity in the state machine is just a current estimate, not an integral over the challenger window. The design needs to say whether to maintain a short velocity history or use a cruder approximation.

---

### 4. Reachability Gate

**Is it the right abstraction?** Yes. The core idea — compare reachability budget to geometric distance — is the right abstraction for this problem. It directly encodes the physical invariant: humans cannot teleport, and they can only reach places via traversable paths. Euclidean distance gives a conservative lower bound on path length, so any floor change blocked by Euclidean distance is genuinely impossible.

**Is Euclidean distance sufficient?** For the specific failure cases described, yes. Guest Room and Ana's Office are not adjacent to the Garage front transition point. The Euclidean distance from those rooms to any valid ground-floor → street-level transition zone is long enough that the reachability budget (velocity × elapsed time) will not cover it when the device has been stationary. You do not need to model actual paths to block impossible transitions for a stationary device.

There is one case where Euclidean distance is insufficient: rooms that are geometrically close to a transition zone but separated by a wall. In that case, the device could be in the room, the zone is nearby in Euclidean distance, and the gate would allow a transition that the wall makes impossible. For the current failure cases, this is not the problem (the distances are large). For future cases in other configurations, it may matter. Accept this limitation now; it can be handled later with wall-aware routing if needed.

**Should the gate be hard, near-hard, or probabilistic?** Hard when unreachable, with a clear threshold. The design's graduated response is correct in spirit, but the weakly-reachable probabilistic middle is the danger zone. Simplify for the first implementation: if the transition zone is farther than `max_speed × elapsed + position_uncertainty_cap`, the challenger cannot advance. Otherwise, normal evidence competition applies. Do not add a separate "weakly reachable requires elevated evidence" condition in the first version. That escalation is where complexity will creep back in.

**What position source?** This is the most important implementation decision. The answer is: **last stable floor state position**, not current position estimate. Specifically:

- When a floor challenge begins, record the current position estimate and timestamp as the "challenge origin."
- Compute the reachability budget from the challenge origin position, growing at `max_speed × elapsed` + `position_uncertainty_cap`.
- If the challenge origin position was already low quality (GDOP high, residual large), use the last high-quality position before the challenge started.
- Do not update the reference position during the challenge unless a new high-quality solve is available that has not already triggered a floor change.

This makes the gate computation independent of position degradation that occurs during the ambiguity episode.

---

### 5. Transition Zone Model

**Is the hybrid model correct?** Yes. Topology first, learned evidence second is the right priority ordering. The zone's existence and geometry define what floor changes are possible. The captured RSSI/fingerprint observations help detect proximity robustly. Do not invert this: if proximity detection were the primary role, you would end up with the current situation where a transition zone behaves like a slightly-special calibration sample.

**What geometry?** Centroid + support radius is the minimum viable model and is sufficient for the first implementation. It is enough for distance checks, uncertainty-expanded proximity tests, and conservative reachability bounds. The question is whether a sphere (3D) or a cylinder (2D + floor range) is more appropriate. For a stairwell, a capsule would be more physically accurate (elongated in the direction of travel). But a sphere with a generous radius covers the key use case without requiring a more complex geometry model. Keep the sphere for now.

One specific concern about the current implementation: the support radius is a per-sample value declared at capture time. If the user declared a tight radius (say 1.0 m) and the position estimate is off by 1.5 m (plausible for a low-quality solve), the zone will appear missed even when the device was genuinely present. The proximity check should add a position-uncertainty term to the zone radius rather than using the declared radius as a hard boundary. Something like `effective_radius = sample_radius_m + position_uncertainty_m`, capped at some maximum. Otherwise, the zone detection will have false negatives precisely when geometry quality is low — which is when you most need it.

**Cluster envelope vs single centroid?** For an initial implementation, the single centroid is adequate. If multiple captures exist for the same named transition, averaging their positions gives a better centroid. A cluster envelope (convex hull, or bounding sphere over multiple captures) would be more accurate but adds implementation complexity. Defer until single-centroid proves insufficient on replay traces.

**Minimum viable geometry**: centroid derived from the mean of all captures for the same named transition, a support radius that is the max of the declared sample radii plus a position-uncertainty buffer, and floor connectivity from the declared transition_floor_ids. That is enough.

---

### 6. Pipeline Order

The proposed order is correct for the goals stated. A specific comment on each stage:

**Stage 1 (geometry solve)**: Correct. Solve first; this provides the position estimate for everything downstream. Critically, record solve quality here — GDOP, residual RMS — because these determine how much to inflate the uncertainty budget in Stage 3.

**Stage 2 (transition-zone proximity inference)**: Correct placement, but important clarification: this should be evaluated against the **stable reference position** (see Section 4), not the current solve output. If the current solve is degraded (low quality, floor ambiguous), using it for proximity inference will give wrong distances. The proximity inference should also maintain recent-transition memory independently of the current position estimate — even if the current solve places the device far from the zone, the memory from a recent high-quality proximity match should persist for the configured window.

**Stage 3 (reachability gate)**: Correct. This is the key addition. One implementation note: the gate should operate on the challenger floor identity, not on the challenger floor's dwell. The gate should prevent the challenger from accumulating dwell, not just from crossing the dwell threshold. A challenger that cannot advance should not be aging in the background.

**Stage 4 (floor evidence fusion)**: Correct. Evidence should only compete among geometrically reachable floors. This is the right place for fingerprint evidence to operate — it is discriminating among plausible candidates, not overruling physical constraints.

**Stage 5 (room inference)**: Correct placement. Keep it floor-scoped in the first implementation; the topology gate should reduce the floor error rate enough that floor-scoped room inference works reliably.

**Stage 6 (hysteresis)**: Correctly placed last and correctly framed as a stability layer, not a safety layer. Once topology is gating impossible transitions, hysteresis only handles legitimate boundary ambiguity, which is its proper role.

The one gap in this ordering: there is no explicit "update position reference" step after a confident floor state is achieved. When the floor becomes stable again (challenger cleared, high-quality solve), the stable reference position should be updated. This is state management, not a pipeline stage, but it needs to be explicit.

---

### 7. What To Change Before Coding

**Make the position reference explicit.** Add a field `last_stable_position_at_floor_challenge_start` (or equivalent) to `TrilatDecisionState`. Define exactly when it is set (when a new challenger first appears and geometry quality is adequate), when it is updated (when floor stabilizes at high quality), and when it is used (as the origin for all reachability budget calculations). Without this, implementations will default to using the current position estimate, which is the source of the circular failure described above.

**Remove the room_context_match requirement from transition proximity detection.** The current `transition_support_diagnostics()` in calibration.py returns `support_01 = 0` unless `room_context_match` is True. This must change. Proximity detection should be position-based only. The zone's declared room_area_id should be metadata for logging but should not gate the proximity check. Write this down in the design doc as an explicit requirement before implementation starts.

**Define the recent-transition memory window as a house constant, not a generic default.** Add an open question in the design: "What is the expected full transit time for a floor change in this house?" Set the memory window as 2× that. For a house where the stairwell transit is approximately 15 seconds, use a 30-second window. Make it configurable. Do not default to the floor dwell seconds — they measure a different thing.

**Define the uncertainty budget cap explicitly.** State in the design that `uncertainty_budget_m` is bounded above by some value (suggest: 3.0 m for the first implementation, based on typical split-level trilateration error). Without a cap, a sufficiently poor solve could make every transition zone "reachable" and the gate would never fire.

**Collapse "weakly reachable" to a note, not a first-class state.** Remove the three-state model from the first implementation specification. Replace with: if `distance_to_zone <= max_speed_budget + uncertainty_cap`, the challenger may advance normally; otherwise, the challenger is blocked. Defer the graduated evidence requirement for the middle case until the binary version has been validated on replay traces. The three-state model is correct in principle but will be implemented as more threshold tuning unless the boundaries are pre-defined.

**Add an explicit "no transition zones configured" path description.** The design mentions graceful degradation but does not describe what the system looks like for the user when zones are not configured. Since the failure is happening right now without zones, the design should address whether there is any benefit before zones are configured, or whether this entire design only helps once zones are added.

---

### 8. Safest First Implementation Slice

The minimum slice that validates the core hypothesis with minimum regression risk:

Add a `challenger_reference_position` to `TrilatDecisionState` — `(x_m, y_m, z_m, stamp, geometry_quality_01)`. When a new floor challenger appears (challenger_id changes from None to a value, or changes to a new identity), and the current geometry quality is above a threshold (say 0.30), record the current position as the reference. When geometry quality is below threshold, look back at `last_solution_xy` / `last_solution_z` from before the challenger started (which is already in state).

Then, in the floor challenger advancement path (around coordinator.py:2901-2915), before computing `challenger_effective_dwell_s`, compute:

1. For the current `floor_challenger_id`, query the transition sample store for all samples that declare this floor as a valid destination.
2. If no samples exist for this layout, skip the gate (existing behavior, no change).
3. If samples exist, compute Euclidean distance from `challenger_reference_position` to the centroid of each relevant sample, using `sample_radius_m + uncertainty_cap` as the effective zone radius.
4. If the nearest relevant sample is closer than `max_speed × elapsed_since_reference_stamp + uncertainty_cap`, set `reachability_gate = "reachable"`.
5. Otherwise, set `reachability_gate = "blocked"` and prevent the challenger from advancing (do not update the dwell timer, do not fire the switch).

Log both states (`reachable` / `blocked`), the computed distance, the budget, and the reference position quality.

Make this the entire first slice. Do not implement the "weakly reachable" intermediate state. Do not touch room assignment, fingerprint logic, or the existing veto machinery. Run it against the known failure traces. If it blocks the Guest Room → Garage front transition without creating new false blocks, proceed. If it creates false blocks (legitimate floor changes now stuck), that tells you where the uncertainty or memory window is wrong.

This slice is self-contained, reversible, and produces direct evidence on whether the core hypothesis is correct before anything else is changed.

---

### 9. What Not To Do

**Do not use the current position estimate as the reachability gate's reference.** This is the one thing that will silently break the gate in exactly the cases where it is needed most. The reference must be from before the floor ambiguity began.

**Do not implement the "weakly reachable requires elevated evidence" condition in the first slice.** It will become another fingerprint threshold tuning exercise. You will spend time calibrating the boundary between "normal evidence" and "elevated evidence" instead of measuring whether the gate itself works.

**Do not couple transition proximity detection to room assignment.** The existing implementation does this (room_context_match requirement). If you carry this pattern into the new design, the gate will be disabled every time room assignment is wrong — which is precisely the failure scenario.

**Do not remove the existing fingerprint veto machinery until the topology gate is validated.** Run both in parallel during the first implementation. The fingerprint veto is imperfect but it has been working as a partial defense. Removing it before the topology gate is proven creates a regression window.

**Do not make the uncertainty_budget_m unbounded.** Assign a hard cap early (3.0 m is a reasonable first estimate). Without it, a degraded solve will make every transition zone appear nearby, the gate will never fire, and the design will appear not to work when actually the position input was the problem.

**Do not skip the exact-switch-moment logging.** Before or simultaneously with implementing the gate, add the switch-time decision log described in the previous advisory. If you implement the gate and the failures stop, you won't know why. If they continue, you won't know what went wrong. The log gives the ability to distinguish "gate fired and was bypassed" from "gate never evaluated" from "gate computed wrong distance due to bad reference position." That distinction determines what to fix next.

The one thing that must not be compromised during implementation: **the reference position for the reachability computation must be from before the floor uncertainty episode began.** Everything else in this design can be tuned. That one principle is what makes the gate a physical constraint rather than another dwell modifier.
