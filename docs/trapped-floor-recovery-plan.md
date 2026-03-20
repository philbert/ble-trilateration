# Trapped Floor Recovery Plan

Date drafted: 2026-03-20

Related documents:
- `docs/topology-gated-floor-inference-design.md`
- `docs/topology-gated-floor-inference-gap-analysis.md`
- `docs/room-wander-analysis.md`

## Purpose

This document proposes a recovery path for a specific failure mode that still exists after
topology-gated floor inference was added:

- a device is physically outside or near `garage_front`,
- Bermuda collapses it onto `ground_floor`,
- room inference starts returning `Guest Room` / `Ana's Office`,
- the reachability gate then prevents the device from escaping back to the correct floor.

The current gate is doing its original job, but it has no model for how to recover once a wrong
floor has already been accepted.

This plan is intentionally targeted at recovery. It does not replace the topology-gated design.

## Problem Summary

The current implementation treats the current floor estimate as authoritative enough to block
challenger floors whenever:

- `floor_confidence` is above the gate threshold,
- a recent `last_good_position` exists,
- transition-zone coverage exists for the floor pair.

That is correct for a real indoor device, but it breaks when the current floor was itself reached
through weak or contradictory evidence.

The resulting loop is:

1. Device is mis-solved onto the wrong floor.
2. `last_good_position` is updated from that wrong solve.
3. The reachability gate uses that wrong indoor reference position.
4. The correct floor challenger is blocked for lacking fresh transition evidence.
5. Room inference keeps selecting plausible rooms on the wrong floor.

This makes the wrong floor estimate self-sealing.

## Constraints

Any fix must preserve the useful behavior that already exists:

- a real phone sleeping in `Guest Room` must not teleport to `Garage front`,
- floor changes should still require topology support when the current floor is trustworthy,
- Bermuda should continue to publish a concrete best guess rather than falling back to `unknown`.

The user preference here is explicit:

- always make a guess,
- do not hide bad states behind `unknown`,
- expose enough signal to understand what needs fixing.

## Core Design Rule

Separate these two ideas:

1. **Current floor guess**
2. **Current floor trust**

Bermuda should always publish a current floor guess.

But only a **trusted** current floor should be allowed to hard-block challengers via topology.

That is the main change.

## Proposed State Additions

Extend `TrilatDecisionState` with recovery-oriented trust state:

- `last_trusted_floor_id`
- `last_trusted_position`
- `last_trusted_position_at`
- `floor_trust_level` or `floor_trusted: bool`
- `floor_trust_reason`
- `suspect_floor_since`
- `same_floor_valid_anchor_count`
- `other_floor_valid_anchor_count`
- `area_churn_score` or equivalent short-window room instability metric

The existing `last_good_position` remains useful, but it should no longer be the only reference
used to decide whether the current floor may imprison the device.

## Trust Model

### When a floor becomes trusted

A floor should earn trust only when it is supported by structurally credible evidence such as:

- a recent transition traversal matching the floor pair,
- sustained fingerprint support for that floor,
- at least 2 stable same-floor valid anchors,
- geometry quality above a configurable threshold,
- limited room churn after the switch.

Not all of these need to be mandatory, but the model should require more than a single decent
solve on the selected floor.

### When a floor loses trust

A selected floor should be demoted from trusted to suspect when contradiction persists, for
example:

- geometry quality remains low,
- same-floor valid anchors stay below 2 for a sustained period,
- stable anchors are mostly `valid_other_floor`,
- room assignment flips repeatedly between multiple rooms on the same floor,
- fingerprint support no longer clearly favors the selected floor,
- the selected floor was never reached via trusted evidence in the first place.

This is the key difference between:

- a real phone in `Guest Room`,
- a brown bin that has been numerically trapped in `Guest Room`.

## Recovery Rule

When the current floor is **trusted**:

- keep the current reachability gate behavior,
- require transition evidence or motion-budget plausibility,
- continue blocking impossible teleports.

When the current floor is **suspect or untrusted**:

- allow the challenger floor to form even if the reachability gate would normally block it,
- allow challenger dwell and challenger motion budget to accumulate,
- use the gate as a diagnostic signal, not as a hard veto.

This is not a blind bypass. It is a recovery mode that activates only when the current floor is
already contradicted by the rest of the estimator.

## Recovery Shortcut To The Last Trusted Floor

Add an explicit escape hatch:

If all of the following are true:

- the current floor is suspect or untrusted,
- the challenger floor matches `last_trusted_floor_id`,
- challenger evidence persists for the configured dwell,
- current-floor contradiction remains active,

then allow the switch back to `last_trusted_floor_id` without requiring a fresh transition
traversal.

This is the main mechanism that should free a trapped bin from `Guest Room` / `Ana's Office`
and let it return to `garage_front` or `street_level`.

## Why This Should Differentiate Phone vs Bin

### Guest Room phone overnight

A real phone in `Guest Room` typically shows:

- stable `ground_floor`,
- stable `Guest Room`,
- multiple same-floor valid anchors,
- strong residual consistency,
- adequate position confidence,
- no repeated churn across the guest-room / office manifold.

That should keep the floor in the trusted state, so the topology gate remains strict.

### Trapped brown bin overnight

The trapped bin shows a very different pattern:

- poor geometry quality,
- only one stable same-floor valid anchor,
- one stable `valid_other_floor` anchor,
- room churn between `Guest Room` and `Ana's Office`,
- mediocre raw position confidence,
- strong filtered continuity despite weak observability.

That is exactly the profile that should prevent the indoor floor from earning or keeping trust.

## Important Non-Goal

This plan does **not** primarily aim to improve `Guest Room` vs `Ana's Office` classification.

That room distinction is downstream.

The main goal is:

- prevent a wrong indoor floor from becoming authoritative enough to trap the device.

Once the floor recovers, room classification should naturally stop selecting indoor rooms for the
bin.

## Proposed Implementation Phases

### Phase 1: Add trust state and diagnostics

- Add floor trust state fields to `TrilatDecisionState`.
- Expose diagnostics for:
  - current floor trust,
  - last trusted floor,
  - same-floor vs other-floor valid anchor counts,
  - suspect-floor reason flags,
  - area churn indicator.
- Do not change switching behavior yet.

Goal:

- verify from logs that phone and bin produce clearly different trust signatures.

### Phase 2: Stop promoting weak states into trusted references

- Split `last_good_position` from `last_trusted_position`.
- Update `last_trusted_position` only when trust criteria are satisfied.
- Keep `last_good_position` for general continuity and diagnostics.

Goal:

- prevent weak indoor collapses from immediately becoming authoritative challenger references.

### Phase 3: Make the reachability gate trust-aware

- If current floor is trusted, keep hard gate behavior.
- If current floor is suspect/untrusted, let challenger formation proceed.
- Preserve gate diagnostics so blocked-vs-recovery decisions remain visible in logs.

Goal:

- enable recovery without weakening the normal anti-teleport protection.

### Phase 4: Add recovery shortcut to the last trusted floor

- If challenger matches `last_trusted_floor_id` and contradiction persists, permit a switch back
  after dwell even without a new traversal event.
- Reset challenger and trust state cleanly after the recovery switch.

Goal:

- let trapped devices return to the last structurally credible floor.

### Phase 5: Tune thresholds with real captures

Use at least these comparison captures:

- brown bin `garage_front -> street_side -> garage_front`,
- trapped brown bin overnight,
- wife's phone overnight in `Guest Room`.

Tune using data, not intuition, for:

- minimum same-floor valid anchor count,
- geometry threshold for floor trust,
- room-churn threshold,
- contradiction hold duration,
- recovery dwell duration.

## Suggested Initial Heuristics

These are starting points, not final values:

- trusted floor requires either:
  - recent transition traversal, or
  - `same_floor_valid_anchor_count >= 2` and `geometry_quality >= 0.30`
- suspect floor if, for at least 5-10 minutes:
  - `same_floor_valid_anchor_count < 2`, and
  - `other_floor_valid_anchor_count >= 1`, and
  - geometry quality remains poor, and
  - room churn persists
- recovery switch allowed when:
  - challenger equals `last_trusted_floor_id`,
  - contradiction persists,
  - challenger dwell expires,
  - no strong fingerprint veto remains on the current floor

These thresholds should be logged and iterated against captures before being treated as stable.

## Risks

### Over-recovery

If trust is demoted too aggressively, real indoor devices may become eligible for floor escape when
they should remain blocked.

Mitigation:

- require multiple contradiction signals,
- require sustained duration,
- prefer recovery only toward `last_trusted_floor_id`.

### Never-trusted devices

Some low-signal devices may never build a trusted floor if the trust bar is too high.

Mitigation:

- keep publishing a best guess,
- use trust only for gate hardness, not for whether a floor may be shown.

### Hidden complexity

Adding trust state can create another implicit state machine.

Mitigation:

- log trust transitions explicitly,
- keep state model simple,
- make trust reasons inspectable in diagnostics.

## Expected Outcome

If this plan works, Bermuda should behave like this:

- a phone genuinely in `Guest Room` stays on `ground_floor` and remains topology-protected,
- a bin wrongly trapped on `ground_floor` can recover back to `garage_front` / `street_level`,
- the software continues to emit a concrete best guess at all times,
- the diagnostics become more honest about whether the current floor is trustworthy enough to gate
  future transitions.

## Recommendation

Implement this before physical anchor optimization.

The current captures suggest there is still meaningful software value available from:

- better separation of floor guess vs floor trust,
- better handling of contradictory anchor sets,
- and an explicit recovery path out of self-sealed bad states.

Anchor placement will still matter, but it should not be used to compensate for a state machine
that cannot recover once it is wrong.
