# Gap Analysis: Current Architecture vs Topology-Gated Floor Inference Design

This document compares the current Bermuda implementation against the target architecture
described in `topology-gated-floor-inference-design.md` and identifies what needs to change.

## Summary of Gaps

| Area | Current State | Target State | Gap Size |
|---|---|---|---|
| Geometry solve dimensionality | 2D when cross-floor anchors present | Always full 3D with all scanners | Medium |
| Z as floor evidence | Z unused as floor signal, derived after XY from same-floor only | Z from full 3D solve fed into Stage 4 as geometry-derived floor hint | Medium |
| Per-floor Z config | Not in config flow | `floor_z_m` per HA floor | Small |
| Phone-height Z prior | None | `[floor_z, floor_z + 1.2]` band per floor | Small |
| Per-floor X/Y envelope | None | Radius-expanded bounding box from samples | Small |
| Reachability gate | Absent — no physical plausibility check | Hard gate: distance-to-zone vs motion budget | Large |
| Transition zones as topology | Evidence hints only (reduce dwell) | First-class topology primitives (gate entry) | Large |
| Challenger reference position | Not tracked | Frozen at floor-ambiguity onset | Medium |
| Motion budget tracking | Max velocity clamp on position jumps only | Integrated velocity budget since challenger began | Medium |
| Signal priority order | RSSI-first, fingerprints as veto | Topology gate → fingerprints → RSSI → geometry | Large |
| Unknown/stale source floor | Not handled — gate would trap wrong floor | Bypass gate when `floor_confidence` below threshold or floor is `None` | Small |
| Background proximity tracker | No zone proximity tracked outside active challenger | High-quality live solves continuously record zone proximity; recent traversal overrides gate | Medium |

---

## Detailed Gap Analysis

### 1. Geometry Solve Dimensionality

**Current** (`coordinator.py`, `trilateration.py`):

The solve is **2D by default when cross-floor anchors are involved**. The `can_solve_3d` flag
is only set true when all included anchors are on the same floor and there are ≥ 4 of them.
When cross-floor anchors are soft-included, only a 2D XY solve runs; Z is then resolved
separately using same-floor anchors only.

**Target**:

Always run a full 3D Cartesian solve using all available scanners regardless of floor. There is
no physical justification for degrading to 2D when cross-floor scanners are present. BLE signal
propagation through a floor is not categorically different from through a wall.

**What needs to change**:

- Remove the `can_solve_3d = (no cross-floor anchors)` condition.
- Always use `solve_3d_soft_l1()` when ≥ 4 anchors with Z coordinates are available.
- Remove the post-hoc "solve Z separately from same-floor anchors" fallback.
- Cross-floor anchors should still receive inflated sigma to reflect uncertainty, but they should
  participate in the unified 3D solve rather than being excluded from Z.

---

### 2. Z Resolution Order

**Current**:

Z is an afterthought. The pipeline builds floor evidence first from RSSI, selects a floor via the
challenger protocol, then solves XY on that floor, then optionally resolves Z using same-floor
anchors. The floor decision gates which anchors are included, meaning Z is never used as an input
to the floor decision — only as an output.

**Target**:

Z is the most structurally informative coordinate and should be fed into Stage 4 (Floor Evidence
Fusion) as the highest-weight geometry-derived hint. However, Z from Stage 1 is **evidence, not
a pre-confirmation**. The reachability gate (Stage 3) still runs before floor evidence is
resolved. A bad Z estimate under poor geometry must not bypass the topology constraint.

The per-floor Z prior (phone-height band) tightens the Z estimate and sharpens its value as
floor evidence, but the floor decision remains gated by topology first.

**What needs to change**:

- Feed the full 3D solve's Z output into Stage 4 as a geometry-derived floor hint, weighted by
  solve quality.
- Apply the per-floor Z prior as a `SolvePrior3D` input to Stage 1, not as a post-hoc floor
  selector.
- Remove the pattern of RSSI floor selection gating which anchors are included in the solve.
  All anchors participate in the solve; topology gates the floor decision.

---

### 3. Per-Floor Z Configuration

**Current**:

No concept of floor surface Z height exists anywhere in the codebase or config flow. The only
Z data comes from scanner anchor positions and calibration sample positions, which are in the
same absolute coordinate system but are not associated with named floors.

**Target**:

Each Home Assistant floor should have a user-declared `floor_z_m` (fixed) or
`floor_z_min_m / floor_z_max_m` (range, for floors with terrain variation such as street level).
This is collected in the config flow.

**What needs to change**:

- Add a new config flow step that iterates the HA floor registry and prompts for `floor_z_m`
  per floor. A range option should be available.
- Persist per-floor Z config (new key in `ConfigEntry` options or a dedicated store alongside
  `scanner_anchor_store` and `calibration_store`).
- Street level (or any floor the user marks) should default to range mode.
- This config is a prerequisite for sections 4 and 5 (phone-height prior and X/Y envelope).
  It is **not** a prerequisite for the topology gate (section 6), which operates on transition
  zone geometry and the challenger reference position independently of floor Z values.

---

### 4. Phone-Height Z Prior

**Current**:

The trilateration solver runs unconstrained in Z. There is a max vertical speed clamp
(`_TRILAT_MAX_VERTICAL_SPEED_MPS = 1.5 m/s`) that limits Z jumps between successive solves, but
there is no prior anchoring Z to physically plausible values for a given floor.

**Target**:

For each floor, derive the phone-height band automatically from `floor_z_m`:

```
z_prior_min = floor_z_m
z_prior_max = floor_z_m + 1.2
```

Apply this as a `SolvePrior3D` (the infrastructure already exists in `trilateration.py`). This
is a strong prior: a phone spends the overwhelming majority of its time between the floor surface
and ~1.2m above it.

The prior must not be keyed on the current assigned floor alone. If the stable floor is already
wrong, injecting a prior anchored to it will reinforce the error. Instead:

- **When topology and floor evidence agree**: inject the phone-height prior for the agreed floor
  as a continuity aid.
- **When a challenger is active**: run the solve with per-candidate priors for both the stable
  floor and the challenger floor, and compare the residuals. Feed the residual difference into
  Stage 4 as an additional geometry-derived hint rather than pre-selecting a floor.
- **When floor state is unknown or ambiguous**: run the solve unconstrained in Z and rely on
  the topology gate and fingerprint evidence to resolve the floor.

**What needs to change**:

- After per-floor Z config is available (section 3), construct a `SolvePrior3D` per floor with
  the Z band as a Gaussian prior centred at `floor_z_m + 0.6` (mid-band) with appropriate sigma.
- During an active challenger, run two solves (one per candidate floor prior) and diff the
  residuals rather than injecting the stable floor's prior unconditionally.
- For floors with a Z range (street level), use the range midpoint and widen the sigma accordingly.

---

### 5. Per-Floor X/Y Envelope Constraints

**Current**:

No floor footprint concept exists. The XY solve is unconstrained; a position can be returned
anywhere in the coordinate space regardless of where the confirmed floor physically is.

**Target**:

Once a floor is confirmed, constrain the XY solve to the radius-expanded bounding box of
calibration samples on that floor:

```
x_min = min(sample_x - sample_radius_m)
x_max = max(sample_x + sample_radius_m)
y_min = min(sample_y - sample_radius_m)
y_max = max(sample_y + sample_radius_m)
```

Scanner anchor positions on the floor supplement this envelope for floors with sparse or no
calibration samples. Street level is exempt — no XY constraint applies.

**What needs to change**:

- Add a `FloorEnvelope` data class (or equivalent) computed from calibration samples and scanner
  anchors per floor, refreshed when the calibration layout changes.
- Apply as a `SolvePrior2D` or soft clamp in the XY solve after floor is confirmed.
- Prefer soft clamping (penalise positions outside the envelope) over hard clipping, since
  devices near the floor boundary are legitimate.
- Flag floors with no calibration samples and no scanner anchors as unconstrained.

---

### 6. Reachability Gate

**Current**:

No reachability gate exists. The floor challenger protocol has:
- a margin test (RSSI score gap),
- a dwell timer,
- fingerprint veto,
- transition support veto (reduces dwell time if transition samples support the move).

None of these ask whether the floor change is *physically plausible* given the device's recent
position and the time elapsed since the challenger appeared. A device stable and far from any
staircase can still switch floors if RSSI drifts and dwell expires.

**Target**:

Before a challenger floor can advance, a reachability gate must pass:

1. At challenger onset, freeze a `challenger_reference_position` from the last confident
   pre-challenge position estimate.
2. Track motion budget since challenger onset: `min(elapsed * max_speed, integrated_velocity)
   + uncertainty_budget` (capped at 3m initially).
3. For the challenger floor, find the nearest configured transition zone that permits that floor.
4. If `distance(challenger_reference_position, nearest_zone) > reachable_budget`: block the
   challenger from advancing regardless of RSSI or dwell.

The gate applies **per `(from_floor_id, to_floor_id)` pair**, not per layout. Partial topology
coverage is the common case:

- if no zone covers this specific pair, fall back to soft behaviour for that transition only,
- if at least one zone covers the pair, apply the hard gate for that pair.

A zone covering `ground_floor → street_level` implies nothing about `ground_floor → top_floor`.
Each pair is evaluated independently. This prevents the gate from either over-blocking uncovered
transitions or being silently bypassed where it matters most.

**What needs to change**:

- Add `challenger_reference_position: tuple[float, float, float] | None` to
  `TrilatDecisionState`.
- Add `challenger_motion_budget_m: float` tracking integrated velocity since challenger onset.
- Add a `TransitionZone` data class with a list of per-capture `(x, y, z, radius_m)` tuples
  and allowed `(from_floor_id, to_floor_id)` pairs. No single centroid — the effective geometry
  is the union of capture discs.
- Add a `ReachabilityGate` that evaluates the above at each floor decision cycle.
- Wire the gate into the floor challenger protocol before the dwell and veto checks.
- The gate must also handle the case where `from_floor` is unknown or unreliable (see gap 11).
- Gate should be behind a feature flag until validated on replay traces.

---

### 7. Transition Zones as Topology Primitives

**Current**:

Transition samples (`calibration_store.py`) are evidence points, not topology gates. They
influence the `transition_support_01` score which *reduces* the required dwell time when a
transition is supported, or contributes to a soft veto when it is not. They do not define
where floor changes are physically allowed.

**Target**:

Transition zones are first-class topology objects that define *where floor changes are allowed*.
Their primary role is to constrain state transitions, not to provide fingerprint evidence
(though they may still carry RSSI observations for proximity detection).

**What needs to change**:

- Introduce a `TransitionZone` model separate from calibration samples:
  - stable internal ID,
  - one or more captures, each with `(x, y, z, radius_m)` — no averaging or centroiding,
  - allowed `(from_floor_id, to_floor_id)` pairs.
- Add a `transition_zone_store` alongside `calibration_store`.
- Update the recording service to distinguish transition zone captures from position samples.
- In the reachability gate (section 6), look up `TransitionZone` objects, not raw transition
  samples, when evaluating whether a floor change is physically possible.
- The existing transition sample evidence (RSSI fingerprints at transition zones) can be
  preserved as supporting data within the zone, but the zone's gate role must not depend on
  fingerprint evidence quality.

---

### 8. Challenger Reference Position

**Current**:

`TrilatDecisionState` has `last_solution_xy: tuple[float, float]` which is updated on every
successful solve. There is no concept of freezing a reference position at the onset of a floor
challenge. The current position at the time the reachability gate is evaluated would drift as
the device moves, making the gate window incorrect.

**Target**:

When a floor challenger first appears, capture the last confident pre-challenge position as
`challenger_reference_position`. This is the starting point for the reachability budget
calculation and must not update while the challenge is active.

**What needs to change**:

- Add `challenger_reference_position: tuple[float, float, float] | None` to
  `TrilatDecisionState`.
- Set it once when `floor_challenger_id` transitions from `None` to a challenger floor,
  using the current best position if confidence is sufficient.
- Clear it when the challenge resolves (switch accepted or challenger abandoned).

---

### 9. Motion Budget Tracking

**Current**:

There is a max velocity clamp (`_TRILAT_MAX_POSITION_SPEED_MPS`) applied to position jumps per
update cycle. This is a per-frame sanity check, not an accumulated motion budget over the
challenger dwell period.

**Target**:

Track `challenger_motion_budget_m` from challenger onset: integrate estimated velocity or use
`elapsed_time * max_speed_m_per_s`, whichever is smaller, and add an uncertainty budget (capped
at 3m for first rollout).

**What needs to change**:

- Add `challenger_motion_budget_m: float` and `challenger_onset_time: float` to
  `TrilatDecisionState`.
- Each update cycle while a challenger is active: update the motion budget from elapsed time
  and recent velocity estimates.
- Use `challenger_motion_budget_m` in the reachability gate distance check.

---

### 10. Signal Priority Order

**Current priority** (implicit in the pipeline):

1. RSSI floor evidence (primary floor selector)
2. Margin test gate
3. Dwell timer
4. Fingerprint hold/veto (secondary — can block but not initiate)
5. Transition support (tertiary — reduces dwell, can soft-veto)

**Target priority** (from design doc):

1. Physical reachability through transition zones
2. Fingerprint-global floor evidence
3. RSSI floor evidence
4. Geometry-derived floor hints (Z from full 3D solve)
5. Hysteresis and continuity

**What needs to change**:

- The reachability gate (section 6) must run before any evidence competition is allowed.
- Fingerprint evidence should become the primary tie-breaker among reachable floors, not a veto
  on RSSI-selected floors.
- Z from the full 3D solve should contribute as a geometry-derived floor hint (currently unused
  as a floor signal).
- Hysteresis (dwell timers) should be the last line of defence, not a primary gate.
- This is a pipeline restructure in `coordinator.py` lines ~2750-2938 (floor inference block).

---

### 11. Unknown or Stale Source Floor at Gate Evaluation

**Current**:

Not handled. The floor challenger protocol assumes `state.floor_id` is a trustworthy source
floor. At startup, after long absence, or after a tracking failure, `floor_id` may be `None`,
stale, or already wrong. A strict `(from_floor, to_floor)` gate in that state can trap the
estimator on an incorrect floor indefinitely.

**Target**:

When `from_floor` is unknown, missing, or flagged as low-confidence, the gate must not block
floor assignment. The correct behaviour is:

- if `state.floor_id is None`: bypass the gate entirely, allow normal floor evidence competition
  to establish an initial floor,
- if `state.floor_id` was set under low confidence and has not been re-confirmed: treat it as
  untrustworthy, apply no gate, allow evidence to override it freely,
- once a floor is established with sufficient confidence and confirmed by the topology gate at
  least once, the gate becomes active for subsequent challengers.

**What needs to change**:

- Add a `floor_confidence: float` field to `TrilatDecisionState` tracking confidence in the
  current stable floor.
- The reachability gate checks `floor_confidence` before evaluating the `(from_floor, to_floor)`
  pair. Below a configurable threshold the gate is bypassed for that cycle.
- This prevents the gate from cementing an already-wrong floor into place at startup or recovery.

---

### 12. Background Transition Proximity Tracker

**Current**:

The state machine tracks `most recent credible transition-zone proximity` (listed in the design
doc's State To Track section) but no mechanism is defined for populating it outside an active
challenger. If the device has already passed through the zone before a challenger appears,
the challenger reference position may be far from the zone, and the gate would incorrectly block
a transition that already occurred.

**Target**:

A lightweight background tracker should continuously record transition-zone proximity using only
high-quality live solves (not challenger-period estimates). This populates a
`last_zone_proximity_at: float` timestamp and `last_zone_id: str` for each device, independent
of any active challenger.

When a challenger appears, the reachability gate checks not only the distance from the challenger
reference position, but also whether the background tracker recorded a recent zone traversal
within a configurable recency window (e.g. 30 seconds). If so, the gate treats the transition as
plausible regardless of the current budget calculation.

This is the mechanism that allows legitimate transitions to succeed even when the challenger
forms a few seconds after the device has already passed the zone.

**What needs to change**:

- Add `last_zone_proximity_at: float | None` and `last_zone_id: str | None` to
  `TrilatDecisionState`.
- Each update cycle, if solve quality is above a threshold and the current position is within
  a zone's union envelope, record the proximity timestamp.
- In the reachability gate, check the recency of background proximity alongside the
  budget calculation. Recent background proximity overrides an otherwise-blocked gate.
- The recency window should be configurable and default conservatively (e.g. 30s).

---

## Implementation Order

The phases below are sequenced so the topology gate — the core hypothesis — is validated in
isolation before the geometry rewrite is introduced. Coupling both changes in the first slice
would make it impossible to know which one fixed or broke any given failure.

### Phase 1 — Topology gate (minimal slice)

1. **Challenger reference position** (gap 8): Add field to `TrilatDecisionState`, set on
   challenger onset. No gate logic yet — just capture.
2. **Motion budget tracking** (gap 9): Add accumulation alongside challenger state. No gate yet.
3. **Floor confidence tracking** (gap 11): Add `floor_confidence` field to `TrilatDecisionState`.
   Gate bypasses when confidence is below threshold or floor is `None`.
4. **Background transition proximity tracker** (gap 12): Each update cycle, record proximity to
   transition zones using high-quality live solves. Store `last_zone_proximity_at` and
   `last_zone_id` per device. No gate logic yet.
5. **TransitionZone data model** (gap 7, partial): Define `TransitionZone` class with per-capture
   geometry (union model, no centroiding) and `(from_floor_id, to_floor_id)` pairs. Add store.
   Migrate existing transition sample recordings to populate zones. No gate logic yet.
6. **Transition-zone proximity inference**: Evaluate proximity using challenger reference
   position, not the live estimate. Log proximity decisions as diagnostics only.
7. **Reachability gate** (gap 6): Implement `ReachabilityGate` per `(from_floor, to_floor)` pair.
   Gate checks both budget distance and recent background proximity (recency window).
   Wire into floor challenger protocol behind a feature flag. Partial topology coverage falls back
   per-pair to soft behaviour. Unknown/low-confidence `from_floor` bypasses the gate.
8. **Replay validation**: Test against known failure traces (`Guest Room → Garage front`,
   `Ana's Office → Garage front`). Gate must block both before proceeding.

### Phase 2 — Geometry improvements

7. **Per-floor Z config** (gap 3): Add config flow step, persist `floor_z_m` per floor.
8. **Phone-height Z prior** (gap 4): Inject `SolvePrior3D` per floor. Feed Z output into Stage 4
   as geometry-derived floor evidence (not a pre-confirmation).
9. **Full 3D solve with all scanners** (gap 1 + gap 2): Remove the `can_solve_3d` cross-floor
   restriction. Always solve 3D. Validate on replay traces before enabling by default.
10. **Per-floor X/Y envelope** (gap 5): Compute `FloorEnvelope` from calibration samples +
    scanner anchors. Apply as soft prior in XY solve.

### Phase 3 — Signal priority reorder

11. **Signal priority reorder** (gap 10): Restructure floor inference block so topology gate
    runs first, fingerprints become primary evidence among reachable floors, RSSI secondary,
    Z-derived hint tertiary. Only after phase 1 gate and phase 2 geometry are validated.

### Phase 4 — Cleanup

12. Remove or demote the older fingerprint veto and transition-support dwell-reduction machinery
    once the topology gate provides equivalent coverage more cleanly.

---

## Files Most Affected

| File | Changes |
|---|---|
| `coordinator.py` | Floor inference pipeline restructure (lines ~2750–2938), challenger state fields, reachability gate call, Z prior injection |
| `trilateration.py` | Remove `can_solve_3d` cross-floor restriction, always 3D, Z prior wiring |
| `config_flow.py` | New per-floor Z config step |
| `calibration_store.py` | Extend or supplement with `TransitionZone` store |
| `bermuda_device.py` | New `TrilatDecisionState` fields |
| *(new)* `floor_config_store.py` | Per-floor Z config persistence |
| *(new)* `transition_zone_store.py` | `TransitionZone` model and store |
| *(new)* `reachability_gate.py` | `ReachabilityGate` implementation |
| *(new)* `floor_envelope.py` | `FloorEnvelope` computation from samples + anchors |
