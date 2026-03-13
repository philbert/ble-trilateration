# Proposed Estimation Pipeline for Bermuda Indoor Tracking

**Date:** 2026-03-10
**Authors:** Claude + Codex (synthesised) — corrections from project owner
**Context:** Current trilateration compresses too early; each step discards signal.
**Goal:** Better measurements in, better uncertainty out — improve the pipeline around the existing solver rather than replacing it.

---

## What the Current Code Already Does Correctly

Before describing changes, it is important to note what is already in place:

- **`trilateration.py`** already uses IRLS. The solver is not the bottleneck.
- **`ranging_model.py`** already has the right hierarchical shape:
  - Global slope/intercept
  - Per-scanner bias
  - Per-device bias
  - Per-scanner RMSE
- The correct next step is to **feed the existing solver better measurements and a prior**, not to rewrite it.

---

## What to Keep (No Change Needed)

- Rolling per-scanner windows (needs building, described below)
- Motion prior + speed cap
- Fingerprint-based room attribution in parallel with geometry
- Room hysteresis
- Soft transition priors derived from room behavior, not hand-labeled doorways

---

## What to Correct vs Earlier Proposals

| Earlier claim | Correction |
|---|---|
| "Replace one-shot WLS with IRLS" | IRLS is already there. Feed it better inputs instead. |
| "Fit per-scanner (RSSI₀_s, n_s) for each scanner" | With typical sample counts this will overfit. Keep global slope by default; only allow per-scanner slope when a scanner has enough rows and enough distance spread. |
| Particle filter as next step | Good long-term architecture, not the next thing to build. |

---

## Converged Pipeline Design

### Stage 1 — Windowed Scanner Aggregates (`coordinator.py`)

Replace per-packet processing with a short adaptive window per `(device, scanner)`.

**Window length is adaptive, not fixed:**
- Stationary device: 4–8 seconds
- Moving device: 2–4 seconds

**Compute per window:**
- Median RSSI (robust central estimate)
- MAD or IQR (dispersion — proxy for multipath / environment noise)
- Packet count (sparse adverts → less reliable)
- Age of most recent packet (staleness decay)
- Timestamp-health penalty (irregular inter-packet timing signals scanner congestion or a flaky link)

All five of these feed into the uncertainty term for Stage 2.

---

### Stage 2 — Stronger Uncertainty Model (`ranging_model.py`)

Keep the existing hierarchical model (global slope + per-scanner bias + per-device bias). Improve what it outputs.

**Current output:** a single distance estimate.

**Improved output:** a distance estimate plus a real uncertainty / likelihood band.

Compose the uncertainty term from:
1. **Calibration RMSE** for this scanner (already stored as per-scanner RMSE — use it)
2. **Live window dispersion** (MAD/IQR from Stage 1 — high dispersion → wider band)
3. **Packet count penalty** (fewer packets → inflate uncertainty)
4. **Timestamp-health penalty** (poor timing → inflate uncertainty)

```
σ_effective = f(calibration_RMSE, window_MAD, packet_count, timestamp_health)
```

The exact functional form can start simple (e.g., RSS combination) and be tuned empirically.

**Per-scanner slope:** Only fit a scanner-specific path-loss exponent `n_s` when that scanner has enough calibration support, for example:
- at least 15–20 usable calibration rows, and
- at least 3 distinct distance buckets / enough spread to constrain a slope.

Otherwise fall back to the global slope and continue learning only per-scanner bias and noise. This prevents overfitting on sparse data.

This stage converts each scanner from a brittle metre value into a `(distance, σ_effective)` pair — a real likelihood band the solver can use properly.

---

### Stage 3 — Prior-Aware Solve Policy (`coordinator.py` + `trilateration.py`)

The IRLS solver is kept in `trilateration.py`. The motion prior, stationary logic, and speed policy live in `coordinator.py`, which prepares solver inputs and applies the estimation policy around the generic math solver.

**Prior from previous state:**
- Previous `(x, y, z, vx, vy, vz)` is propagated forward by elapsed time
- The prior contributes as an additional pseudo-observation with its own uncertainty

**Stationary-mode prior:**
- Compute a "movement evidence" score each cycle from the proposed displacement relative to the combined measurement uncertainty
- If movement evidence is weak → apply a strong stationary prior; the posterior stays near the previous position
- This is the primary mechanism for making the system calm; it acts before the speed cap

**Speed cap:**
- Remains as a final hard guard, not the primary mechanism
- If the posterior update would imply speed > max_speed, clip the displacement
- Suggested limits: 1.5 m/s nominal, 2.5 m/s absolute maximum

**Effect:** The existing IRLS solver receives better-weighted inputs (real likelihood bands from Stage 2) and a prior-aware policy that prevents wandering when evidence is weak, while keeping the solver module itself generic.

---

### Stage 4 — Hybrid Room Attribution (`room_classifier.py`)

Run two attribution methods in parallel and fuse their scores.

**Geometry score:**
- Derived from the solved `(x, y, z)` position and Bermuda's existing calibration-sample KDE geometry score
- This reuses the current room-classifier machinery rather than introducing room polygons/volumes as a new dependency
- Geometry should be treated as a secondary consistency check for room attribution, not the only gatekeeper

**Fingerprint score:**
- Build a live RSSI vector from the windowed medians of all currently-visible scanners
- Gate by floor first, then compare only against calibration samples from the selected floor
- Compare this vector to the stored RSSI vectors from calibration samples using weighted Euclidean distance in RSSI space
- Each calibration sample votes for its labelled room, weighted by similarity
- Missing scanners in the live vector are handled by a distance penalty

**Fusion:**
```
room_score(r) = α * fingerprint_score(r) + (1 - α) * geometry_score(r)
```

Start with α ≈ 0.65 (fingerprint-dominant). The fingerprint implicitly encodes wall attenuation and geometry that the coordinate-based approach cannot see, so it should be the primary room signal with geometry acting as a secondary consistency check.

**Why fingerprinting works:** Two rooms that are geometrically close but separated by a wall have very different RSSI fingerprints. The fingerprint comparison bypasses the RSSI → distance → position → room chain entirely and therefore does not accumulate its noise.

---

### Stage 5 — Room Hysteresis and Soft Transition Priors

**Room hysteresis:**
- Hold the current room attribution through weak evidence
- Require cumulative evidence over time rather than a fixed number of update cycles
- Use short dwell/evidence windows (for example 2–5 seconds), because update cadence can vary
- Adjacent-room transitions can use a shorter dwell/easier threshold; non-adjacent transitions should require stronger sustained evidence
- If room evidence is weak or contradictory, prefer holding the previous stable room over dropping immediately to `Unknown`

**Soft transition priors:**
- Prefer learned transition zones and adjacency inferred from calibration support over hand-labeled doorways
- Use room overlap / ambiguity regions from the sample clouds to identify where transitions are plausible
- Outside those regions, apply a heavier penalty to room changes unless the new room has strong sustained evidence
- Do not hard-block — a device near a boundary should be allowed to remain ambiguous
- Floor transitions can still use soft connector priors where they are already configured, but room-to-room transitions should not depend on manual doorway labeling

---

### Stage 6 — Geometry and Residual Quality Signals

Add explicit solve-quality heuristics so Bermuda distinguishes “many anchors” from “good solve”.

**Geometry quality:**
- Compute a DOP-like or conditioning score from the active anchor layout
- Poorly distributed anchors should reduce trust in the solve even when anchor count is high

**Residual consistency:**
- Use solver residuals and per-anchor disagreement as first-class confidence inputs
- If several anchors are mutually inconsistent, reduce room/position confidence rather than trusting the point estimate blindly

These signals should influence:
- overall position confidence
- the geometry portion of room scoring
- how strongly hysteresis favors holding the previous room

---

## What to Leave for Later

**Particle filter:**
- Correct long-term architecture for a full posterior over `(x, y, z, room)`
- Handles multimodal uncertainty (e.g., could be in kitchen or hallway with equal probability)
- Not the next thing to build; the improvements above should be validated first

---

## Implementation Summary

| Phase | What | Where |
|---|---|---|
| 1 | Adaptive windowed aggregate: median RSSI, MAD, packet count, age, timestamp health | `coordinator.py` |
| 2 | Compose `σ_effective` from calibration RMSE + live dispersion + count + health; conditional per-scanner slope | `ranging_model.py` |
| 3 | Add stationary-mode prior + speed cap policy around the existing IRLS solve | `coordinator.py` + `trilateration.py` |
| 4 | Fingerprint k-NN room score + existing KDE geometry score fusion, floor-gated first | `room_classifier.py` |
| 5 | Room hysteresis (time/evidence based) + soft transition priors / learned transition zones | `room_classifier.py` / `coordinator.py` |
| 6 | Geometry-quality and residual-consistency confidence signals | `coordinator.py` / `trilateration.py` |
| Later | Particle filter for full posterior | New module |

---

## Design Principles

1. **Improve inputs to the existing solver, don't replace the solver.** IRLS is already there; feed it likelihood bands instead of point estimates.

2. **Don't overfit per-scanner path-loss exponents.** Use calibration RMSE + live dispersion to widen uncertainty instead. Only fit scanner-specific slopes with enough data.

3. **Calm by default.** The stationary-mode prior is the primary mechanism for stability. The speed cap is a backstop, not the front line.

4. **Room attribution is partially independent of position estimation.** Fingerprint matching can produce good room attribution even when the geometric solve is uncertain. Run it in parallel with Bermuda's existing KDE geometry score and fuse the results.

5. **Use soft transition priors, not brittle doorway labels.** A device at a boundary should be allowed to remain ambiguous between rooms; learned transition zones are preferable to hand-maintained door definitions.

6. **Hysteresis is not a hack.** It reflects the physics: humans do not teleport, and room attribution should reflect that.

7. **Use soft penalties wherever possible.** Aside from timestamp-invalid / stale inputs, weak measurements should widen uncertainty or reduce weight rather than being hard-rejected.

8. **Anchor count is not a quality metric.** Geometry quality and residual consistency must be tracked explicitly; many anchors can still produce a poor solve.

---

## Senior Engineer Review — 2026-03-13

*Reviewed against current coordinator.py, trilateration.py, room_classifier.py and the "global trilateration first, floor and room second" proposal.*

### Preliminary Note

The plan file `/config/custom_components/bermuda/docs/global-trilateration-refactor-plan.md` referenced in discussion does not exist. This document (`ESTIMATION_PIPELINE_PROPOSAL.md`) is treated as the primary written plan.

---

### 1. Executive Verdict

The direction is **partially right but justified with the wrong reasoning, and the proposed mechanism is likely to create new failure modes it claims to fix.**

The current hard floor rejection (coordinator.py:2462-2463: `if scanner.floor_id != selected_floor_id: continue`) is a real bug and is correctly identified as the root cause. But "global trilateration first" is the wrong fix because it treats cross-floor RSSI as geometry, which it isn't. Cross-floor RSSI is dominated by structural attenuation, not distance. Feeding it as a "soft-penalized range" to the solver gives the optimizer actively misleading constraints, not weakly-useful ones.

The better framing: **the floor evidence selection is already soft (the `_score_rssi` exponential already discounts wrong-floor scanners by ~63% per 8 dB penalty), but the anchor inclusion after that selection is hard. Fix the anchor inclusion, not the floor selection order.** The secondary failure is state reset on floor switch (lines 2435-2454), which is entirely unaddressed by this proposal. Fixing those two things—soft anchor inclusion and no state reset—would likely eliminate 80% of the described pathology without touching the pipeline order or requiring a 3D solve rewrite.

---

### 2. Critical Findings

**Finding 1: The floor evidence scoring is already soft. The proposal fixes the wrong thing.**

The description implies the current floor selection is a hard gate. It isn't. coordinator.py:2319-2360:

```python
penalty_db = self.trilat_cross_floor_penalty_db()  # = 8.0 dB default
adjusted_rssi = rssi if same_floor else rssi - penalty_db
evidence += self._score_rssi(adjusted_rssi)
```

`_score_rssi` is `exp((rssi + 90) / 8)`. An 8 dB penalty means wrong-floor scanners contribute `exp(-1) ≈ 37%` of their unpenalized weight to floor evidence. Floor **selection** is already soft.

The hard rejection happens **only in anchor inclusion** at line 2462-2463, **after** floor selection. These are two different stages. The "global trilateration" concept conflates them and proposes solving the wrong one with the wrong mechanism.

**Finding 2: RSSI through floors is not geometric distance. Treating it as such poisons the solver.**

`rssi_distance_raw` is derived from a path-loss model calibrated for line-of-sight or light-NLOS. A concrete floor/ceiling produces 10–15 dB of additional attenuation independent of distance. A scanner directly below the device at 1.5 m through one floor can report the same RSSI as a same-floor scanner 6–8 m away.

If you feed cross-floor RSSI as a range estimate (even penalized) to `solve_2d_soft_l1` or `solve_3d_soft_l1`, you're adding a constraint that says "device is ~6–8 m from scanner directly below it." That constraint actively **pulls the solution away** from the correct position — it's not noise, it's bias with a specific incorrect direction. The soft-L1 robustification in the IRLS loop helps with outliers, but not with systematically biased measurements where the bias direction correlates with the true position.

**Finding 3: Vertical observability is structurally limited. "Solve z then infer floor" requires z to be constrained.**

`solve_3d_soft_l1` requires 4+ anchors with known z coordinates. Even with that, the z component of the Jacobian is `(z_device - z_anchor) / distance`. If most or all anchors are on the same floor (same z), the Jacobian is near rank-deficient in z. The determinant check at trilateration.py:429 catches `|det| < 1e-9`, but not the far more common case where the solve converges to a garbage z with a finite determinant.

Cross-floor anchors at different z do help observability, but only if their range estimates are correct — which they aren't (see Finding 2). You'd be using biased observations to determine the one coordinate (z) that's most sensitive to model error.

**Finding 4: Stage 4 still hard-gates room classification by floor. It contradicts the "floor second" framing.**

Stage 4 says: "Gate by floor first, then compare only against calibration samples from the selected floor." This is also enforced in code: room_classifier.py:172-173 filters `[sample for sample in ... if sample.floor_id == floor_id]`.

If the geometry solve finishes and produces (x,y,z) with uncertain or wrong z, and then room classification requires a `floor_id` pre-filter, the failure mode is identical: wrong z → wrong floor inference → wrong sample set → wrong room. Same failure, different trigger.

The proposal never resolves how `floor_id` is determined before being passed to `classify()` in the new world where trilateration is global.

**Finding 5: State reset on floor switch is a primary instability source. It is not addressed anywhere in this document.**

Lines 2435-2454: when `selected_floor_id != prev_floor_id`, the code clears `trilat_range_ewma_m`, `last_solution_xy`, `last_solution_z`, all velocity state, all residual history. The solver starts completely cold on the new floor.

This is the mechanism that turns a brief floor mis-detection into a position catastrophe. When the device is in Guest Room and the floor selector briefly picks street_level:

1. All state cleared
2. First solve attempt uses only street_level anchors — probably insufficient count → `low_confidence` with centroid fallback
3. Centroid of street_level anchors ≠ Guest Room → wrong position
4. Room classifier assigns Garage front
5. If floor reverts to ground_floor: state cleared again, restart cold

The oscillation between two floors produces position chaos that persists for `floor_dwell_seconds` (8–24 s) after each flip.

**Finding 6: The street_level problem is a topology problem, not a geometry problem.**

street_level is physically between basement and ground_floor in z but is not a full floor plate. Scanners on street_level and ground_floor may overlap in z range. A device in Guest Room on ground_floor is physically close to some street_level scanners. No amount of z-solving will disambiguate these because the z coordinates are not cleanly separated. The only reliable discriminator is wall attenuation (the fingerprint), not geometry.

---

### 3. Hidden Assumptions

**A: Cross-floor anchors improve trilateration accuracy under typical BLE/NLOS conditions.**
Unverified and likely false. Concrete floor attenuation produces systematic bias, not noise. Bias cannot be robustified away with soft-L1 weights.

**B: Solved z is reliable enough to drive floor inference.**
The proposal never discusses anchor geometry requirements for z observability. For single-floor scanner deployments, solved z is essentially unobservable — dominated by the prior initialization, not measurement. Needs validation via GDOP measurement on the actual anchor layout.

**C: Fingerprint matching is independent of floor.**
room_classifier.py:158-173 takes `floor_id` as a required input and hard-gates on it. The current implementation cannot do cross-floor fingerprint matching. Stage 4 even says "Gate by floor first" explicitly. The floor-independent fingerprint idea exists in principle but is not implemented.

**D: The penalty_db approach can be tuned to the right value.**
The default 8.0 dB is a magic constant. Actual floor penetration loss in this house (concrete vs wood framing, specific scanner positions) may be 5 dB or 18 dB. A single global penalty_db cannot be correct for all scanner pairs across all floor boundaries.

**E: HA floor metadata is a useful z proxy.**
HA floors are ordinal labels, not physical z coordinates. The code has no knowledge of actual heights. "Floor from solved z" requires explicit configuration of z ranges per floor, which does not exist in the current system.

---

### 4. What This Proposal Gets Right

- Identifying hard anchor rejection (line 2462-2463) as the correct bug location.
- Stage 2 effective sigma composition is the right abstraction and should survive any architectural change.
- Fingerprint scoring is partially independent of geometric solve quality — the strongest idea in this document.
- Room hysteresis should be time-based, not cycle-based. Correct.
- `trilateration.py` already has `solve_quality_metrics_2d/3d` computing GDOP, condition number, and residual consistency. These are underused and should gate room classification confidence.

---

### 5. Recommended Revised Architecture

**Better framing: "Soft anchor inclusion, no state reset on floor change, cross-floor fingerprint scoring."**

Pipeline (same order as current, but modified):

1. **Floor evidence scoring** — unchanged. Already soft via `_score_rssi` exponential.

2. **Anchor inclusion** — replace line 2462-2463 hard skip with sigma inflation: include cross-floor anchors with `sigma_m *= K` where K=4 for adjacent floors, K=8 for non-adjacent. Solver down-weights them further via IRLS if residuals are large.

3. **Prior continuity across floor changes** — replace state clear (lines 2435-2454) with prior sigma inflation (multiply sigma by 2–3x). Solver can move but preserves directional momentum. Eliminates cold-restart oscillation.

4. **Cross-floor fingerprint scoring** — remove floor gate from fingerprint scoring (or run it separately across all floors). Return the best `(room, floor)` pair. This is the primary mechanism for resolving the street_level/ground_floor ambiguity — wall attenuation creates discriminating fingerprints that geometry cannot.

5. **Floor inference from fingerprint output** — `floor_id` used for room classification comes from the fingerprint classifier's result, not from RSSI evidence scoring. Fingerprint + room → floor is more reliable than RSSI evidence → floor for the split-level case.

6. **Geometry as consistency check** — trilateration position is used to disambiguate rooms with similar fingerprints (those near boundaries), not as the primary room signal.

**Where each decision lives:**
- All-anchor 3D solving: not recommended until z observability is validated; keep 2D as primary
- Floor inference: from fingerprint classification output, not from RSSI evidence
- Room inference: fingerprint-primary (cross-floor), geometry-secondary (within inferred floor)
- Hard rejection: eliminated; replaced with sigma inflation for wrong-floor anchors
- State reset: eliminated; replaced with prior sigma inflation on floor change

---

### 6. Incremental Migration Plan

**Step 1 (highest info gain, zero behavior change): Add cross-floor anchor diagnostic logging.**
For every rejected anchor (line 2462-2463), log what its sigma_m, rssi_distance_raw, and floor_id would have been. Replay against the Guest Room / Garage front session. Determine whether cross-floor anchors have consistent range estimates or are biased.

**Step 2 (low risk, targeted): Eliminate state reset on floor switch.**
Lines 2435-2454: instead of clearing all state, multiply prior sigma by 3x in the next solve. This is a ~10-line change. Measure whether floor-switch-induced oscillation decreases.

**Step 3 (medium risk): Add cross-floor fingerprint scoring as a parallel diagnostic output.**
Add a flag to `BermudaRoomClassifier.classify()` to run fingerprint scoring against all floors. Log output alongside current floor-gated output. Don't use it for assignment yet — measure how often it agrees or contradicts the current assignment.

**Step 4 (medium risk): Soft anchor inclusion with feature flag.**
Include wrong-floor anchors with `sigma_m *= K_cross_floor` (configurable, default 4.0). Log solve residuals and quality metrics both ways. Compare position accuracy on known-location sessions.

**Step 5 (pending Step 3 validation): Use cross-floor fingerprint for floor inference.**
If Step 3 shows cross-floor fingerprint is reliable (>85% floor accuracy on known-location sessions), route floor inference through fingerprint output.

*Skip global 3D trilateration entirely until z observability is validated. It is the highest-complexity change with the most architectural risk and the weakest theoretical justification.*

---

### 7. Required Experiments

**Experiment 1: Guest Room failure replay.**
Capture a full session with device in Guest Room. Log per-update: all scanner RSSI values, `floor_evidence` dict, `selected_floor_id`, which anchors are included/rejected, `anchor_count` after rejection, solved position, room classification output. Determine whether failure is (a) wrong floor selected, (b) correct floor but insufficient anchors after rejection, or (c) correct floor and anchors but wrong room classification.

**Experiment 2: Vertical observability measurement.**
Run `solve_quality_metrics_3d` on the current anchor layout for positions at each floor's representative z. Log `gdop` and `condition_number`. Success: GDOP < 5. Failure: GDOP > 10 or condition_number > 1e4, meaning z is not observable with current anchors.

**Experiment 3: Cross-floor fingerprint accuracy.**
Using existing calibration samples, run `_fingerprint_room_scores` without floor gate against all samples for representative known-location RSSI snapshots. Measure: does the top room have the correct floor_id, and what is the score gap? Success: >85% floor-correct top-room, score gap > 0.3.

**Experiment 4: Cross-floor range estimate characterization.**
For scanners on different floors, compute `rssi_distance_raw` when device is at a known location on an adjacent floor. Compare to geometric distance. Success/failure: if mean bias > 2 m or variance > 3 m, cross-floor range estimates are too biased to include even with soft sigma.

**Experiment 5: State reset elimination.**
Before any other changes, disable the state reset (lines 2435-2454), run a session crossing between rooms near the street_level/ground_floor boundary. Measure whether oscillation frequency decreases. Requires no algorithmic change and gives direct evidence on whether state reset is a primary instability driver.

---

### 8. Open Questions

1. What is the physical z range (in meters) of each HA floor level? Does street_level z overlap with basement or ground_floor? Without this, "floor from z" cannot be implemented at all.

2. Does the current anchor layout make z observable? Run Experiment 2 before designing any 3D-solve-dependent feature.

3. How many anchors are visible from Guest Room, and on which floors? If Guest Room typically sees 1–2 ground_floor anchors and 2–3 street_level anchors, floor evidence scoring will systematically prefer street_level regardless of algorithmic changes.

4. What is the actual floor penetration loss in this house per floor pair? The 8.0 dB default for `CONF_TRILAT_CROSS_FLOOR_PENALTY_DB` — is it calibrated, or inherited from a default?

5. Is the Guest Room / Garage front failure consistently reproducible? If it only happens under specific RF conditions (congestion, time of day), the root cause may be scanner congestion, not architecture.

6. Stage 4 says "Gate by floor first" — is this a deliberate design choice or an oversight? It directly contradicts the "floor second" framing and needs to be resolved before implementation starts.

7. Has `CONF_TRILAT_CROSS_FLOOR_PENALTY_DB` ever been tuned in this installation? At 8.0 dB, wrong-floor scanners contribute 37% weight. If street_level has more visible scanners from Guest Room, they can outvote ground_floor scanners even with the penalty.
