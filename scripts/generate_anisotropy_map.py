#!/usr/bin/env python3
"""Generate static XY anisotropy maps from persisted scanner anchors."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median


@dataclass(frozen=True)
class Anchor:
    """One persisted scanner anchor."""

    name: str
    x_m: float
    y_m: float
    z_m: float


@dataclass(frozen=True)
class FloorBand:
    """One z-filtered floor band to map."""

    name: str
    min_z_m: float
    max_z_m: float


@dataclass(frozen=True)
class CalibrationSample:
    """One persisted room calibration sample."""

    room_area_id: str
    room_name: str
    x_m: float
    y_m: float
    z_m: float
    radius_m: float


def _load_anchors(path: Path) -> list[Anchor]:
    payload = json.loads(path.read_text())
    scanners = payload.get("data", {}).get("scanners", {})
    anchors: list[Anchor] = []
    for scanner in scanners.values():
        coords = scanner.get("coordinates") or {}
        try:
            anchors.append(
                Anchor(
                    name=str(scanner.get("name") or "scanner"),
                    x_m=float(coords["anchor_x_m"]),
                    y_m=float(coords["anchor_y_m"]),
                    z_m=float(coords["anchor_z_m"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return anchors


def _load_samples(path: Path) -> list[CalibrationSample]:
    payload = json.loads(path.read_text())
    samples = payload.get("data", {}).get("samples", [])
    loaded: list[CalibrationSample] = []
    for sample in samples:
        if (sample.get("quality") or {}).get("status") == "rejected":
            continue
        position = sample.get("position") or {}
        try:
            loaded.append(
                CalibrationSample(
                    room_area_id=str(sample.get("room_area_id") or ""),
                    room_name=str(sample.get("room_name") or sample.get("room_area_id") or "room"),
                    x_m=float(position["x_m"]),
                    y_m=float(position["y_m"]),
                    z_m=float(position["z_m"]),
                    radius_m=max(float(sample.get("sample_radius_m") or 1.0), 0.1),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return loaded


def _auto_floor_bands(anchors: list[Anchor], gap_m: float) -> list[FloorBand]:
    if not anchors:
        return []
    ordered = sorted(anchor.z_m for anchor in anchors)
    groups: list[list[float]] = [[ordered[0]]]
    for z_m in ordered[1:]:
        if z_m - groups[-1][-1] > gap_m:
            groups.append([z_m])
        else:
            groups[-1].append(z_m)
    return [
        FloorBand(
            name=f"auto_floor_{index + 1}",
            min_z_m=min(group),
            max_z_m=max(group),
        )
        for index, group in enumerate(groups)
    ]


def _parse_floor_band(spec: str) -> FloorBand:
    try:
        name, min_z_s, max_z_s = spec.split(":", 2)
        return FloorBand(name=name, min_z_m=float(min_z_s), max_z_m=float(max_z_s))
    except ValueError as exc:  # pragma: no cover - argparse path
        raise argparse.ArgumentTypeError(
            f"Invalid --floor '{spec}'. Expected name:min_z:max_z"
        ) from exc


def _xy_covariance(anchors: list[Anchor], x_m: float, y_m: float) -> tuple[float, float, float] | None:
    info_00 = 0.0
    info_01 = 0.0
    info_11 = 0.0
    contributing = 0
    for anchor in anchors:
        dx = x_m - anchor.x_m
        dy = y_m - anchor.y_m
        distance = max(math.hypot(dx, dy), 1e-6)
        grad_x = dx / distance
        grad_y = dy / distance
        info_00 += grad_x * grad_x
        info_01 += grad_x * grad_y
        info_11 += grad_y * grad_y
        contributing += 1

    if contributing < 2:
        return None

    info_00 += 1e-6
    info_11 += 1e-6
    det = (info_00 * info_11) - (info_01 * info_01)
    if det <= 1e-12 or not math.isfinite(det):
        return None

    cov_xx = info_11 / det
    cov_xy = -info_01 / det
    cov_yy = info_00 / det
    if not all(math.isfinite(value) for value in (cov_xx, cov_xy, cov_yy)):
        return None
    return max(cov_xx, 0.0), cov_xy, max(cov_yy, 0.0)


def _anisotropy(covariance_xy: tuple[float, float, float] | None) -> tuple[float | None, str | None]:
    if covariance_xy is None:
        return None, None
    cov_xx, _cov_xy, cov_yy = covariance_xy
    min_var = max(min(cov_xx, cov_yy), 1e-6)
    ratio = max(cov_xx, cov_yy) / min_var
    if math.isclose(cov_xx, cov_yy, rel_tol=1e-6, abs_tol=1e-6):
        return ratio, None
    return ratio, ("x" if cov_xx > cov_yy else "y")


def _grid_points(min_value: float, max_value: float, step_m: float) -> list[float]:
    points: list[float] = []
    current = min_value
    while current <= max_value + 1e-9:
        points.append(round(current, 3))
        current += step_m
    return points


def _floor_samples(
    samples: list[CalibrationSample],
    *,
    floor: FloorBand,
) -> list[CalibrationSample]:
    return [sample for sample in samples if floor.min_z_m <= sample.z_m <= floor.max_z_m]


def _point_in_sample_footprint(
    x_m: float,
    y_m: float,
    *,
    samples: list[CalibrationSample],
    clip_margin_m: float,
) -> bool:
    if not samples:
        return True
    for sample in samples:
        if math.hypot(x_m - sample.x_m, y_m - sample.y_m) <= sample.radius_m + clip_margin_m:
            return True
    return False


def _bounds_from_anchors(anchors: list[Anchor], padding_m: float) -> tuple[float, float, float, float]:
    return (
        min(anchor.x_m for anchor in anchors) - padding_m,
        max(anchor.x_m for anchor in anchors) + padding_m,
        min(anchor.y_m for anchor in anchors) - padding_m,
        max(anchor.y_m for anchor in anchors) + padding_m,
    )


def _bounds_from_samples(
    samples: list[CalibrationSample],
    padding_m: float,
) -> tuple[float, float, float, float]:
    return (
        min(sample.x_m - sample.radius_m for sample in samples) - padding_m,
        max(sample.x_m + sample.radius_m for sample in samples) + padding_m,
        min(sample.y_m - sample.radius_m for sample in samples) - padding_m,
        max(sample.y_m + sample.radius_m for sample in samples) + padding_m,
    )


def _build_floor_map(
    anchors: list[Anchor],
    samples: list[CalibrationSample],
    *,
    floor: FloorBand,
    step_m: float,
    padding_m: float,
    risk_threshold: float,
    bounds_source: str,
    clip_to_samples: bool,
    clip_margin_m: float,
) -> dict:
    floor_anchors = [anchor for anchor in anchors if floor.min_z_m <= anchor.z_m <= floor.max_z_m]
    floor_samples = _floor_samples(samples, floor=floor)
    if not floor_anchors:
        return {
            "name": floor.name,
            "min_z_m": floor.min_z_m,
            "max_z_m": floor.max_z_m,
            "anchor_count": 0,
            "sample_count": len(floor_samples),
            "grid": [],
        }

    anchor_bounds = _bounds_from_anchors(floor_anchors, padding_m)
    sample_bounds = _bounds_from_samples(floor_samples, padding_m) if floor_samples else None
    if bounds_source == "samples" and sample_bounds is not None:
        x_min, x_max, y_min, y_max = sample_bounds
    elif bounds_source == "combined" and sample_bounds is not None:
        x_min = min(anchor_bounds[0], sample_bounds[0])
        x_max = max(anchor_bounds[1], sample_bounds[1])
        y_min = min(anchor_bounds[2], sample_bounds[2])
        y_max = max(anchor_bounds[3], sample_bounds[3])
    else:
        x_min, x_max, y_min, y_max = anchor_bounds
    x_points = _grid_points(x_min, x_max, step_m)
    y_points = _grid_points(y_min, y_max, step_m)

    cells: list[dict] = []
    ratios: list[float] = []
    x_risk = 0
    y_risk = 0
    undefined = 0
    masked = 0
    ascii_rows: list[str] = []
    for y_m in reversed(y_points):
        row_chars: list[str] = []
        for x_m in x_points:
            if clip_to_samples and floor_samples and not _point_in_sample_footprint(
                x_m,
                y_m,
                samples=floor_samples,
                clip_margin_m=clip_margin_m,
            ):
                masked += 1
                row_chars.append("-")
                cells.append({"x_m": x_m, "y_m": y_m, "anisotropy_ratio": None, "weak_axis": None, "masked": True})
                continue
            covariance_xy = _xy_covariance(floor_anchors, x_m, y_m)
            ratio, weak_axis = _anisotropy(covariance_xy)
            if ratio is None:
                undefined += 1
                row_chars.append("?")
                cells.append({"x_m": x_m, "y_m": y_m, "anisotropy_ratio": None, "weak_axis": None, "masked": False})
                continue
            ratios.append(ratio)
            if ratio >= risk_threshold and weak_axis == "x":
                x_risk += 1
                symbol = "x"
            elif ratio >= risk_threshold and weak_axis == "y":
                y_risk += 1
                symbol = "y"
            else:
                symbol = "."
            row_chars.append(symbol)
            cells.append(
                {
                    "x_m": x_m,
                    "y_m": y_m,
                    "anisotropy_ratio": round(ratio, 3),
                    "weak_axis": weak_axis,
                    "masked": False,
                }
            )
        ascii_rows.append(f"{y_m:6.2f} " + "".join(row_chars))

    total_defined = max(len(ratios), 1)
    return {
        "name": floor.name,
        "min_z_m": floor.min_z_m,
        "max_z_m": floor.max_z_m,
        "anchor_count": len(floor_anchors),
        "sample_count": len(floor_samples),
        "anchors": [
            {"name": anchor.name, "x_m": anchor.x_m, "y_m": anchor.y_m, "z_m": anchor.z_m}
            for anchor in floor_anchors
        ],
        "rooms": sorted({sample.room_name for sample in floor_samples}),
        "bounds": {"x_min_m": x_min, "x_max_m": x_max, "y_min_m": y_min, "y_max_m": y_max},
        "grid_step_m": step_m,
        "risk_threshold": risk_threshold,
        "bounds_source": bounds_source if sample_bounds is not None else "anchors",
        "clip_to_samples": bool(clip_to_samples and floor_samples),
        "summary": {
            "median_anisotropy_ratio": round(median(ratios), 3) if ratios else None,
            "max_anisotropy_ratio": round(max(ratios), 3) if ratios else None,
            "x_weak_risk_fraction": round(x_risk / total_defined, 3),
            "y_weak_risk_fraction": round(y_risk / total_defined, 3),
            "undefined_fraction": round(undefined / max(len(cells), 1), 3),
            "masked_fraction": round(masked / max(len(cells), 1), 3),
        },
        "ascii_map": ascii_rows,
        "grid": cells,
    }


def _format_text(result: dict) -> str:
    lines = [
        f"Input: {result['input_path']}",
        (
            f"Samples: {result['samples_path']}"
            if result["samples_path"] is not None
            else "Samples: none"
        ),
        f"Legend: '.' = ratio<{result['risk_threshold']}, 'x' = x-weak risk, "
        f"'y' = y-weak risk, '?' = degenerate, '-' = outside calibration footprint",
    ]
    for floor in result["floors"]:
        lines.extend(
            [
                "",
                f"[{floor['name']}] z={floor['min_z_m']:.2f}..{floor['max_z_m']:.2f} "
                f"anchors={floor['anchor_count']} samples={floor['sample_count']}",
            ]
        )
        if floor["anchor_count"] == 0:
            lines.append("  no anchors in band")
            continue
        bounds = floor["bounds"]
        summary = floor["summary"]
        lines.extend(
            [
                "  bounds: "
                f"x={bounds['x_min_m']:.2f}..{bounds['x_max_m']:.2f} "
                f"y={bounds['y_min_m']:.2f}..{bounds['y_max_m']:.2f} "
                f"(source={floor['bounds_source']}, clip_to_samples={floor['clip_to_samples']})",
                "  summary: "
                f"median_ratio={summary['median_anisotropy_ratio']} "
                f"max_ratio={summary['max_anisotropy_ratio']} "
                f"x_weak={summary['x_weak_risk_fraction']:.3f} "
                f"y_weak={summary['y_weak_risk_fraction']:.3f} "
                f"undefined={summary['undefined_fraction']:.3f} "
                f"masked={summary['masked_fraction']:.3f}",
                "  rooms: " + (", ".join(floor["rooms"]) if floor["rooms"] else "none"),
                "  anchors: "
                + ", ".join(
                    f"{anchor['name']}({anchor['x_m']:.1f},{anchor['y_m']:.1f},{anchor['z_m']:.1f})"
                    for anchor in floor["anchors"]
                ),
                "  map:",
            ]
        )
        lines.extend("    " + row for row in floor["ascii_map"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        nargs="?",
        default="bermuda.scanner_anchors",
        help="Path to persisted scanner anchors JSON (default: bermuda.scanner_anchors)",
    )
    parser.add_argument(
        "--samples",
        default="bermuda.calibration_samples.sparse",
        help="Path to persisted calibration samples JSON; set to '' to disable sample-aware bounds",
    )
    parser.add_argument(
        "--floor",
        action="append",
        type=_parse_floor_band,
        help="Explicit floor band as name:min_z:max_z; repeat for multiple floors",
    )
    parser.add_argument(
        "--auto-gap",
        type=float,
        default=1.0,
        help="Maximum z gap (m) before auto floor clustering starts a new band",
    )
    parser.add_argument("--step", type=float, default=1.0, help="Grid step in metres")
    parser.add_argument("--padding", type=float, default=1.0, help="XY padding around anchors in metres")
    parser.add_argument(
        "--bounds-source",
        choices=("anchors", "samples", "combined"),
        default="samples",
        help="How to derive XY bounds when calibration samples are available",
    )
    parser.add_argument(
        "--no-clip-to-samples",
        action="store_true",
        help="Do not mask out cells that fall outside the calibrated sample footprint",
    )
    parser.add_argument(
        "--clip-margin",
        type=float,
        default=0.5,
        help="Extra XY margin in metres around each sample radius when clipping to the calibrated footprint",
    )
    parser.add_argument(
        "--risk-threshold",
        type=float,
        default=1.5,
        help="Anisotropy ratio threshold used to mark x/y risk cells",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format",
    )
    parser.add_argument(
        "--output",
        help="Optional output path; stdout is always used when omitted",
    )
    args = parser.parse_args()

    anchors_path = Path(args.input)
    anchors = _load_anchors(anchors_path)
    if not anchors:
        raise SystemExit(f"No anchors loaded from {args.input}")

    samples: list[CalibrationSample] = []
    samples_path: Path | None = None
    if args.samples:
        candidate_samples_path = Path(args.samples)
        if candidate_samples_path.exists():
            samples_path = candidate_samples_path
            samples = _load_samples(candidate_samples_path)

    floors = args.floor or _auto_floor_bands(anchors, args.auto_gap)
    result = {
        "input_path": str(anchors_path.resolve()),
        "samples_path": str(samples_path.resolve()) if samples_path is not None else None,
        "risk_threshold": args.risk_threshold,
        "floors": [
            _build_floor_map(
                anchors,
                samples,
                floor=floor,
                step_m=args.step,
                padding_m=args.padding,
                risk_threshold=args.risk_threshold,
                bounds_source=args.bounds_source,
                clip_to_samples=not args.no_clip_to_samples,
                clip_margin_m=args.clip_margin,
            )
            for floor in floors
        ],
    }

    output_text = (
        json.dumps(result, indent=2)
        if args.format == "json"
        else _format_text(result)
    )
    if args.output:
        Path(args.output).write_text(output_text + ("\n" if not output_text.endswith("\n") else ""))
    print(output_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
