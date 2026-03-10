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
- Doorway/topology as transition priors

---

## What to Correct vs Earlier Proposals

| Earlier claim | Correction |
|---|---|
| "Replace one-shot WLS with IRLS" | IRLS is already there. Feed it better inputs instead. |
| "Fit per-scanner (RSSI₀_s, n_s) for each scanner" | With typical sample counts this will overfit. Keep global slope; only allow per-scanner slope when a scanner has enough calibration rows. |
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

**Per-scanner slope:** Only fit a scanner-specific path-loss exponent `n_s` when that scanner has enough calibration rows (suggest: minimum 15–20 samples spanning at least 2 m of distance range). Otherwise fall back to the global slope. This prevents overfitting on sparse data.

This stage converts each scanner from a brittle metre value into a `(distance, σ_effective)` pair — a real likelihood band the solver can use properly.

---

### Stage 3 — Prior-Aware IRLS Solve (`trilateration.py`)

The IRLS solver is kept. Add a prior term.

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

**Effect:** The existing IRLS solver now receives better-weighted inputs (real likelihood bands from Stage 2) and a prior that prevents wandering when evidence is weak.

---

### Stage 4 — Hybrid Room Attribution (`room_classifier.py`)

Run two attribution methods in parallel and fuse their scores.

**Geometry score:**
- Derived from the solved `(x, y, z)` position and room polygons/volumes
- Existing approach

**Fingerprint score:**
- Build a live RSSI vector from the windowed medians of all currently-visible scanners
- Compare this vector to the stored RSSI vectors from calibration samples using weighted Euclidean distance in RSSI space
- Each calibration sample votes for its labelled room, weighted by similarity
- Missing scanners in the live vector are handled by a distance penalty

**Fusion:**
```
room_score(r) = α * fingerprint_score(r) + (1 - α) * geometry_score(r)
```

Start with α ≈ 0.65 (fingerprint-dominant). The fingerprint implicitly encodes wall attenuation and geometry that the coordinate-based approach cannot see.

**Why fingerprinting works:** Two rooms that are geometrically close but separated by a wall have very different RSSI fingerprints. The fingerprint comparison bypasses the RSSI → distance → position → room chain entirely and therefore does not accumulate its noise.

---

### Stage 5 — Room Hysteresis and Topology Constraints

**Room hysteresis:**
- Hold the current room attribution through weak evidence
- Require N consecutive update cycles attributing to a new room before committing to the switch
- N can be small for adjacent rooms (e.g., 3 cycles), larger for non-adjacent rooms (e.g., 6 cycles)

**Doorway / connector topology:**
- Define doorways and floor connectors (staircases, lifts) as transition priors, not as room samples
- Model: `room_A --connector_D--> room_B` with an associated transition cost
- If the proposed new room is not reachable from the current room via a known connector, apply a heavy transition penalty
- Do not hard-block — a device can legitimately sit in a doorway and appear ambiguous between two rooms
- Floor transitions should only pass through labelled connectors; cross-floor jumps without a connector are heavily penalised

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
| 3 | Add stationary-mode prior + speed cap to existing IRLS solve | `trilateration.py` |
| 4 | Fingerprint k-NN room score + geometry score fusion | `room_classifier.py` |
| 5 | Room hysteresis (N-cycle confirmation) + doorway transition priors | `room_classifier.py` / `coordinator.py` |
| Later | Particle filter for full posterior | New module |

---

## Design Principles

1. **Improve inputs to the existing solver, don't replace the solver.** IRLS is already there; feed it likelihood bands instead of point estimates.

2. **Don't overfit per-scanner path-loss exponents.** Use calibration RMSE + live dispersion to widen uncertainty instead. Only fit scanner-specific slopes with enough data.

3. **Calm by default.** The stationary-mode prior is the primary mechanism for stability. The speed cap is a backstop, not the front line.

4. **Room attribution is partially independent of position estimation.** Fingerprint matching can produce good room attribution even when the geometric solve is uncertain. Run them in parallel and fuse.

5. **Doorways are transition priors, not sample points.** A device in a doorway should be ambiguous between two rooms — that is the correct output, not a bug.

6. **Hysteresis is not a hack.** It reflects the physics: humans do not teleport, and room attribution should reflect that.
