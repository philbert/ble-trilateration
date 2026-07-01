#!/usr/bin/env python3
# ruff: noqa: T201, D103, D213, EXE001
"""Offline calibration/anchor coverage report for Bermuda trilateration storage.

Reads the same Home Assistant storage file copies used by scripts/audit_storage.py
and scripts/generate_anisotropy_map.py and produces a per-room, per-floor, and
per-transition-zone coverage punch list:

- calibration sample count and spatial spread (bounding-box diagonal) per room
- average/min anchors actually observed per sample, per room
- anchor count per floor (from floor_config bands), flagging thin floors
- transition-zone capture counts, flagging zones with too few walk-through captures

This is a read-only diagnostic script; it does not modify any storage files or
change runtime behavior.

Usage:
    python scripts/coverage_report.py [storage_dir]

storage_dir must contain Home Assistant storage wrapper JSON files named
calibration_samples, scanner_anchors, floor_config, transition_zones.
Defaults to the repository root.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from statistics import mean

REPO_ROOT = Path(__file__).resolve().parent.parent

# Thresholds. These are starting points, not tuned constants from the runtime code.
MIN_SAMPLES_GOOD = 10
MIN_SAMPLES_WARN = 5
MIN_AVG_ANCHORS_VISIBLE = 3.0
MIN_SPREAD_M = 0.75
MIN_FLOOR_ANCHORS = 4
MIN_TRANSITION_CAPTURES = 2


def load_storage(path: Path) -> dict:
    with path.open() as handle:
        return json.load(handle)["data"]


def build_floor_bands(floors_data: dict) -> list[tuple[str, float, float]]:
    """Return (floor_id, z_min, z_max) bands sorted by z, matching audit_storage.py's approach."""
    floors = {
        floor_id: cfg.get("floor_z_m")
        for floor_id, cfg in (floors_data.get("floors") or {}).items()
        if cfg.get("floor_z_m") is not None
    }
    ordered = sorted(floors.items(), key=lambda row: row[1])
    bands = []
    for index, (floor_id, floor_z) in enumerate(ordered):
        next_z = ordered[index + 1][1] if index + 1 < len(ordered) else floor_z + 3.5
        bands.append((floor_id, floor_z, next_z + 0.5))
    return bands


def floor_for_z(bands: list[tuple[str, float, float]], z_m: float | None) -> str | None:
    if z_m is None or not bands:
        return None
    for floor_id, z_min, z_max in bands:
        if z_min <= z_m < z_max:
            return floor_id
    # Fall back to nearest band edge rather than leaving it unassigned.
    first_id, first_min, _ = bands[0]
    last_id, _, last_max = bands[-1]
    return first_id if z_m < first_min else last_id


def load_anchors(anchors_data: dict, bands: list[tuple[str, float, float]]) -> list[dict]:
    anchors = []
    for address, payload in anchors_data.get("scanners", {}).items():
        coords = payload.get("coordinates") or {}
        z_m = coords.get("anchor_z_m")
        anchors.append(
            {
                "address": address,
                "name": payload.get("name", address),
                "x_m": coords.get("anchor_x_m"),
                "y_m": coords.get("anchor_y_m"),
                "z_m": z_m,
                "floor_id": floor_for_z(bands, z_m),
            }
        )
    return anchors


def load_rooms(cal_data: dict, bands: list[tuple[str, float, float]]) -> dict[str, dict]:
    rooms: dict[str, dict] = {}
    for sample in cal_data.get("samples", []):
        if (sample.get("quality") or {}).get("status") == "rejected":
            continue
        room_key = sample.get("room_area_id") or sample.get("room_name") or "unknown"
        position = sample.get("position") or {}
        x_m, y_m, z_m = position.get("x_m"), position.get("y_m"), position.get("z_m")
        anchors_seen = list((sample.get("anchors") or {}).keys())

        room = rooms.setdefault(
            room_key,
            {
                "room_name": sample.get("room_name") or room_key,
                "sample_count": 0,
                "xs": [],
                "ys": [],
                "floors": set(),
                "anchors_per_sample": [],
                "anchor_union": set(),
            },
        )
        room["sample_count"] += 1
        if x_m is not None:
            room["xs"].append(x_m)
        if y_m is not None:
            room["ys"].append(y_m)
        room["floors"].add(floor_for_z(bands, z_m))
        room["anchors_per_sample"].append(len(anchors_seen))
        room["anchor_union"].update(anchors_seen)
    return rooms


def room_metrics(room: dict) -> dict:
    xs, ys = room["xs"], room["ys"]
    if len(xs) >= 2 and len(ys) >= 2:
        spread_m = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
    else:
        spread_m = 0.0
    avg_anchors = mean(room["anchors_per_sample"]) if room["anchors_per_sample"] else 0.0
    min_anchors = min(room["anchors_per_sample"]) if room["anchors_per_sample"] else 0

    flags = []
    if room["sample_count"] < MIN_SAMPLES_WARN:
        flags.append("LOW_SAMPLES")
    elif room["sample_count"] < MIN_SAMPLES_GOOD:
        flags.append("THIN_SAMPLES")
    if avg_anchors < MIN_AVG_ANCHORS_VISIBLE:
        flags.append("SPARSE_ANCHORS")
    if room["sample_count"] >= 2 and spread_m < MIN_SPREAD_M:
        flags.append("CLUSTERED_AT_POINT")
    if len([f for f in room["floors"] if f is not None]) > 1:
        flags.append("SPANS_MULTIPLE_FLOOR_BANDS")

    return {
        "room_name": room["room_name"],
        "sample_count": room["sample_count"],
        "floors": sorted(f for f in room["floors"] if f is not None),
        "spread_m": round(spread_m, 2),
        "avg_anchors_visible": round(avg_anchors, 1),
        "min_anchors_visible": min_anchors,
        "distinct_anchors_ever_seen": len(room["anchor_union"]),
        "flags": flags,
    }


def floor_metrics(
    bands: list[tuple[str, float, float]],
    anchors: list[dict],
    rooms_by_metrics: list[dict],
) -> list[dict]:
    results = []
    for floor_id, z_min, z_max in bands:
        floor_anchors = [a for a in anchors if a["floor_id"] == floor_id]
        floor_rooms = [r for r in rooms_by_metrics if floor_id in r["floors"]]
        flags = []
        if len(floor_anchors) < MIN_FLOOR_ANCHORS:
            flags.append("THIN_ANCHORS")
        results.append(
            {
                "floor_id": floor_id,
                "z_band": (round(z_min, 2), round(z_max, 2)),
                "anchor_count": len(floor_anchors),
                "anchor_names": [a["name"] for a in floor_anchors],
                "room_count": len(floor_rooms),
                "sample_count": sum(r["sample_count"] for r in floor_rooms),
                "flags": flags,
            }
        )
    return results


def transition_metrics(zones_data: dict) -> list[dict]:
    results = []
    for zone in zones_data.get("zones", []):
        capture_count = len(zone.get("captures", []))
        floor_pair_count = len(zone.get("floor_pairs", [])) // 2
        flags = []
        if capture_count < MIN_TRANSITION_CAPTURES:
            flags.append("NEEDS_MORE_CAPTURES")
        results.append(
            {
                "name": zone.get("name"),
                "capture_count": capture_count,
                "floor_pair_count": floor_pair_count,
                "flags": flags,
            }
        )
    return results


def format_text(
    anchors: list[dict],
    floors: list[dict],
    rooms: list[dict],
    transitions: list[dict],
) -> str:
    lines = []

    lines.append("=== Floors ===")
    for floor in floors:
        flag_str = f" [{', '.join(floor['flags'])}]" if floor["flags"] else ""
        lines.append(
            f"{floor['floor_id']:<14s} z={floor['z_band'][0]:>5.2f}..{floor['z_band'][1]:<5.2f} "
            f"anchors={floor['anchor_count']:<3d} rooms={floor['room_count']:<3d} "
            f"samples={floor['sample_count']:<4d}{flag_str}"
        )
        if floor["anchor_names"]:
            lines.append("    anchors: " + ", ".join(floor["anchor_names"]))

    unassigned = [a for a in anchors if a["floor_id"] is None]
    if unassigned:
        lines.append("")
        lines.append("=== Anchors with no floor band match ===")
        lines.extend(f"  {a['name']} x={a['x_m']} y={a['y_m']} z={a['z_m']}" for a in unassigned)

    lines.append("")
    lines.append("=== Rooms (worst first) ===")
    ranked = sorted(rooms, key=lambda r: (-len(r["flags"]), r["sample_count"]))
    for room in ranked:
        flag_str = f" [{', '.join(room['flags'])}]" if room["flags"] else " [ok]"
        lines.append(
            f"{room['room_name']:<28s} samples={room['sample_count']:<4d} "
            f"spread={room['spread_m']:>5.2f}m avg_anchors={room['avg_anchors_visible']:<4.1f} "
            f"min_anchors={room['min_anchors_visible']:<2d} "
            f"distinct_anchors_seen={room['distinct_anchors_ever_seen']:<3d} "
            f"floor={','.join(room['floors']) or '?'}{flag_str}"
        )

    lines.append("")
    lines.append("=== Transition zones ===")
    if not transitions:
        lines.append("  (none captured)")
    for zone in transitions:
        flag_str = f" [{', '.join(zone['flags'])}]" if zone["flags"] else " [ok]"
        lines.append(
            f"  {zone['name']:<20s} captures={zone['capture_count']:<3d} "
            f"floor_pairs={zone['floor_pair_count']:<2d}{flag_str}"
        )

    lines.append("")
    lines.append("=== Punch list ===")
    punch_items = [
        f"[floor] {floor['floor_id']}: {flag} (anchor_count={floor['anchor_count']})"
        for floor in floors
        for flag in floor["flags"]
    ]
    punch_items.extend(f"[room]  {room['room_name']}: {flag}" for room in ranked for flag in room["flags"])
    punch_items.extend(
        f"[trans] {zone['name']}: {flag} (captures={zone['capture_count']})"
        for zone in transitions
        for flag in zone["flags"]
    )
    if punch_items:
        lines.extend(f"  - {item}" for item in punch_items)
    else:
        lines.append("  (nothing flagged)")

    return "\n".join(lines)


def main() -> int:
    storage_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT

    cal_data = load_storage(storage_dir / "calibration_samples")
    anchors_data = load_storage(storage_dir / "scanner_anchors")
    floors_data = load_storage(storage_dir / "floor_config")
    zones_path = storage_dir / "transition_zones"
    zones_data = load_storage(zones_path) if zones_path.exists() else {"zones": []}

    bands = build_floor_bands(floors_data)
    anchors = load_anchors(anchors_data, bands)
    rooms = {key: room_metrics(room) for key, room in load_rooms(cal_data, bands).items()}
    room_list = list(rooms.values())
    floors = floor_metrics(bands, anchors, room_list)
    transitions = transition_metrics(zones_data)

    print(format_text(anchors, floors, room_list, transitions))
    print()
    print(
        "Note: this only reports on rooms/floors that already have calibration data or "
        "configured anchors. It cannot detect rooms in the real house with zero samples, "
        "since there is no independent room list in these storage files."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
