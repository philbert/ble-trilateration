# Living Room to Sophia's Room Wander Analysis

Date investigated: 2026-03-13

Related captures:
- `/Users/phil/Downloads/home-assistant_2026-03-13T17-37-50.750Z.log`
- `/Users/phil/Downloads/history.csv`

## Summary

This was not a floor-selection failure. The device remained on `ground_floor` throughout the bad `Living Room -> Sophia's room -> Living Room` sequence.

The bad room assignment was caused by a same-floor room-classification failure while the solved trilateration point wandered substantially even though the device was stationary on the living room sofa.

Most likely failure chain:

1. A false `Sophia's room` room challenger appeared while same-floor geometry was already weak.
2. The room-switch dwell logic allowed that challenger to persist long enough to become the stable room.
3. After the switch, the drifting solved point later moved into the geometric neighborhood of the single `Sophia's room` sample, reinforcing the wrong assignment.
4. The device eventually drifted back toward the living room sample cloud and switched back to `Living Room`.

## Timeline

All timestamps below are UTC unless noted.

- `2026-03-13T17:28:56.966Z`
  - Area returned to `Living Room` after the earlier garage-side failure.
- `2026-03-13T17:34:00.971Z`
  - Area changed `Living Room -> Sophia's room`.
  - Matching local log timestamp: `2026-03-13 18:34:00.968`.
- `2026-03-13T17:36:24.972Z`
  - Area changed `Sophia's room -> Living Room`.
  - Matching local log timestamp: `2026-03-13 18:36:24.969`.

## What The Log Proves

The log shows this was not a bad floor switch:

- `2026-03-13 18:33:54.969`
  - `selected=ground_floor challenger=None ... evidence=[ground_floor=28.743, top_floor=19.089, basement=14.362]`
- `2026-03-13 18:34:17.963`
  - `selected=ground_floor challenger=None ... evidence=[ground_floor=30.540, top_floor=16.763, basement=14.420]`
- `2026-03-13 18:34:39.974`
  - `selected=ground_floor challenger=None ... evidence=[ground_floor=40.742, top_floor=22.352, basement=18.893]`
- `2026-03-13 18:35:24.970`
  - `selected=ground_floor challenger=None ... evidence=[ground_floor=45.613, top_floor=26.203, basement=23.388]`
- `2026-03-13 18:36:10.969`
  - `selected=ground_floor challenger=None ... evidence=[ground_floor=37.593, top_floor=23.146, basement=18.516]`

There was no floor challenger during the Sophia's room interval.

## What The History Shows

While the device was stationary, the solved point wandered dramatically:

- `x` moved from about `10.7` at `17:33:45Z` to about `5.1` at `17:34:47Z`, then up to about `18.5` by `17:36:24Z`
- `y` moved from about `0.9` to `7.6`, then down to `-2.6`, then back to about `1.5`
- `z` moved from about `2.35` down to about `0.91`, then back to about `2.65`

This amount of motion is inconsistent with the device remaining stationary on the sofa.

At the same time:

- `Position Confidence` stayed relatively high, mostly around `6.6` to `7.0`
- `Geometry Quality` stayed poor, mostly around `1.0` to `2.5`, and degraded further toward `0.7` to `1.1`

This indicates a trust mismatch: the room pipeline was still willing to act on room challengers even though solve quality was poor.

## Calibration Context

From `/Users/phil/Code/ha/bermuda/bermuda.calibration_samples.sparse`:

- `Living Room` has 8 samples:
  - `(12.5, 7.2, 3.7)`
  - `(12.5, 4.2, 3.7)`
  - `(15.0, 7.2, 3.7)`
  - `(15.0, 4.2, 3.7)`
  - `(17.5, 4.2, 3.7)`
  - `(17.5, 7.2, 3.7)`
  - `(17.0, 2.6, 3.7)`
  - `(17.0, 0.6, 3.7)`
- `Sophia's room` has 1 sample:
  - `(4.5, 6.7, 3.7)`

Important implication:

- At the exact room-switch timestamp (`17:34:00.971Z`), the solved point was still around `(9.918, 2.227, 1.964)`.
- That position is too far from the `Sophia's room` sample for geometry alone to justify a switch.
- Later, once the solve drifted near `(5.131, 7.637, 1.596)`, `Sophia's room` became geometrically plausible and the wrong assignment was reinforced.

## Transition-Strength Context

The current learned room-transition strength between `Living Room` and `Sophia's room` is low:

- approximately `0.134`

That means the current switch logic should require extra dwell before allowing the switch.

Relevant code:

- room score fusion: `/Users/phil/Code/ha/bermuda/custom_components/bermuda/room_classifier.py`
- room switch dwell logic: `/Users/phil/Code/ha/bermuda/custom_components/bermuda/coordinator.py`
- learned transition strength construction: `/Users/phil/Code/ha/bermuda/custom_components/bermuda/room_classifier.py`

## Current Best Hypothesis

The most likely explanation is:

1. A false fingerprint preference for `Sophia's room` appeared while geometry was weak.
2. The hybrid classifier accepted that room as a challenger even though geometric support was poor.
3. The stable-room guard delayed but did not block the switch.
4. After the switch, the drifting solved point later moved close enough to the `Sophia's room` sample that geometry also started favoring the wrong room.

This is why the assignment looked like a random room wander even though no floor transition happened.

## What Was Missing In The Capture

The existing log included:

- floor diagnostics
- solve summaries
- area transition logs

But it did not include the room-classifier score breakdown in the targeted log stream. The classifier summary existed only in `device.diag_area_switch`, which was not emitted to the targeted logger.

That meant the capture could not directly answer:

- whether the initial challenger was geometry-led or fingerprint-led
- what the exact `best_score`, `second_score`, `geometry_score`, and `fingerprint_score` were at switch time
- whether the switch spent its dwell interval in `hold=room_switch_dwell(...)`

## Logging Added After This Analysis

Targeted room-classifier logging has now been added for devices listed in `DEBUG_DEVICES`.

New targeted log line:

- `Trilat room diag: ...`

It now emits:

- current floor
- stable room
- current room challenger
- candidate room
- resolved room
- full `diag_area_switch` summary, including:
  - `reason`
  - `best`
  - `score`
  - `geom`
  - `fp`
  - `second`
  - `topk_used`
  - `transition`
  - `hold=...` if applicable

This should make the next replay of this failure mode much easier to diagnose.

## Follow-Up Investigation Items

- Determine whether the initial `Sophia's room` challenger was fingerprint-led, geometry-led, or both.
- Compare `Position Confidence` vs `Geometry Quality` and decide whether room switching needs an explicit quality gate.
- Test whether same-floor room switching should be blocked when:
  - geometry quality is poor, and
  - challenger geometry score is weak, and
  - only fingerprint evidence is pushing the switch
- Revisit sample coverage:
  - `Living Room` has many samples
  - `Sophia's room` has only one sample
  - further calibration may change classifier behavior, but calibration asymmetry alone does not explain the initial switch timing

## Working Conclusion

This event is a distinct bug from the `Guest Room -> Garage front` floor-collapse issue.

The garage failure was:

- floor challenger
- floor switch
- anchor collapse
- loss of `z`
- wrong room

This `Living Room -> Sophia's room` failure was:

- stable floor
- poor same-floor geometry
- likely false room challenger
- dwell expiry
- solve wander reinforcing the wrong room

They should be investigated and fixed separately.
