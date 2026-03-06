"""Lightweight trilateration helpers for Bermuda."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class AnchorMeasurement:
    """A single range observation from one fixed scanner anchor."""

    scanner_address: str
    x_m: float
    y_m: float
    range_m: float
    z_m: float | None = None


@dataclass(frozen=True)
class SolveResult:
    """Result from a trilateration solve attempt."""

    ok: bool
    x_m: float | None
    y_m: float | None
    z_m: float | None
    residual_rms_m: float | None
    iterations: int
    reason: str


def anchor_centroid(anchors: list[AnchorMeasurement]) -> tuple[float, float]:
    """Return the unweighted centroid of anchor coordinates."""
    if not anchors:
        return (0.0, 0.0)
    total_x = 0.0
    total_y = 0.0
    for anchor in anchors:
        total_x += anchor.x_m
        total_y += anchor.y_m
    count = float(len(anchors))
    return (total_x / count, total_y / count)


def anchor_centroid_3d(anchors: list[AnchorMeasurement]) -> tuple[float, float, float]:
    """Return the unweighted centroid of anchor coordinates for 3D solve."""
    if not anchors:
        return (0.0, 0.0, 0.0)
    total_x = 0.0
    total_y = 0.0
    total_z = 0.0
    for anchor in anchors:
        total_x += anchor.x_m
        total_y += anchor.y_m
        total_z += anchor.z_m if anchor.z_m is not None else 0.0
    count = float(len(anchors))
    return (total_x / count, total_y / count, total_z / count)


def residual_rms_m(x_m: float, y_m: float, anchors: list[AnchorMeasurement]) -> float:
    """Compute root-mean-square residual in meters for a solved point."""
    if not anchors:
        return 0.0
    err_sq_sum = 0.0
    for anchor in anchors:
        dx = x_m - anchor.x_m
        dy = y_m - anchor.y_m
        dist = math.hypot(dx, dy)
        residual = dist - anchor.range_m
        err_sq_sum += residual * residual
    return math.sqrt(err_sq_sum / len(anchors))


def residual_rms_m_3d(x_m: float, y_m: float, z_m: float, anchors: list[AnchorMeasurement]) -> float:
    """Compute root-mean-square residual in meters for a solved 3D point."""
    if not anchors:
        return 0.0
    err_sq_sum = 0.0
    for anchor in anchors:
        anchor_z = anchor.z_m if anchor.z_m is not None else 0.0
        dx = x_m - anchor.x_m
        dy = y_m - anchor.y_m
        dz = z_m - anchor_z
        dist = math.sqrt((dx * dx) + (dy * dy) + (dz * dz))
        residual = dist - anchor.range_m
        err_sq_sum += residual * residual
    return math.sqrt(err_sq_sum / len(anchors))


def solve_2d_soft_l1(
    anchors: list[AnchorMeasurement],
    initial_guess: tuple[float, float] | None = None,
    max_iterations: int = 18,
    tolerance_m: float = 1e-3,
    soft_l1_scale_m: float = 1.0,
) -> SolveResult:
    """
    Solve 2D trilateration using a compact IRLS Gauss-Newton loop.

    The objective is robustified with soft-l1 style weighting:
    weight = 1 / sqrt(1 + (r / soft_l1_scale_m)^2)
    """
    if len(anchors) < 3:
        return SolveResult(
            ok=False,
            x_m=None,
            y_m=None,
            z_m=None,
            residual_rms_m=None,
            iterations=0,
            reason="insufficient_anchors",
        )

    x_m, y_m = initial_guess if initial_guess is not None else anchor_centroid(anchors)

    for iteration in range(1, max_iterations + 1):
        # Normal equations for 2x2 update:
        # (J^T W J) * delta = -(J^T W r)
        jt_w_j_00 = 0.0
        jt_w_j_01 = 0.0
        jt_w_j_11 = 0.0
        jt_w_r_0 = 0.0
        jt_w_r_1 = 0.0

        for anchor in anchors:
            dx = x_m - anchor.x_m
            dy = y_m - anchor.y_m
            distance = math.hypot(dx, dy)
            distance = max(distance, 1e-6)
            residual = distance - anchor.range_m
            weight = 1.0 / math.sqrt(1.0 + (residual / soft_l1_scale_m) ** 2)

            grad_x = dx / distance
            grad_y = dy / distance

            jt_w_j_00 += weight * grad_x * grad_x
            jt_w_j_01 += weight * grad_x * grad_y
            jt_w_j_11 += weight * grad_y * grad_y
            jt_w_r_0 += weight * grad_x * residual
            jt_w_r_1 += weight * grad_y * residual

        # Small damping for numerical stability.
        jt_w_j_00 += 1e-6
        jt_w_j_11 += 1e-6

        det = (jt_w_j_00 * jt_w_j_11) - (jt_w_j_01 * jt_w_j_01)
        if abs(det) < 1e-9:
            return SolveResult(
                ok=False,
                x_m=None,
                y_m=None,
                z_m=None,
                residual_rms_m=None,
                iterations=iteration,
                reason="degenerate_geometry",
            )

        inv_00 = jt_w_j_11 / det
        inv_01 = -jt_w_j_01 / det
        inv_11 = jt_w_j_00 / det

        step_x = -(inv_00 * jt_w_r_0 + inv_01 * jt_w_r_1)
        step_y = -(inv_01 * jt_w_r_0 + inv_11 * jt_w_r_1)

        x_m += step_x
        y_m += step_y

        if math.hypot(step_x, step_y) <= tolerance_m:
            break

    rms = residual_rms_m(x_m, y_m, anchors)
    return SolveResult(
        ok=True,
        x_m=x_m,
        y_m=y_m,
        z_m=None,
        residual_rms_m=rms,
        iterations=iteration,
        reason="ok",
    )


def solve_3d_soft_l1(
    anchors: list[AnchorMeasurement],
    initial_guess: tuple[float, float, float] | None = None,
    max_iterations: int = 20,
    tolerance_m: float = 1e-3,
    soft_l1_scale_m: float = 1.0,
) -> SolveResult:
    """
    Solve 3D trilateration using a compact IRLS Gauss-Newton loop.

    Requires anchors with known z coordinates.
    """
    if len(anchors) < 4:
        return SolveResult(
            ok=False,
            x_m=None,
            y_m=None,
            z_m=None,
            residual_rms_m=None,
            iterations=0,
            reason="insufficient_anchors",
        )
    if any(anchor.z_m is None for anchor in anchors):
        return SolveResult(
            ok=False,
            x_m=None,
            y_m=None,
            z_m=None,
            residual_rms_m=None,
            iterations=0,
            reason="missing_anchor_z",
        )

    x_m, y_m, z_m = initial_guess if initial_guess is not None else anchor_centroid_3d(anchors)

    for iteration in range(1, max_iterations + 1):
        # Normal equations for 3x3 update:
        # (J^T W J) * delta = -(J^T W r)
        jt_w_j_00 = 0.0
        jt_w_j_01 = 0.0
        jt_w_j_02 = 0.0
        jt_w_j_11 = 0.0
        jt_w_j_12 = 0.0
        jt_w_j_22 = 0.0
        jt_w_r_0 = 0.0
        jt_w_r_1 = 0.0
        jt_w_r_2 = 0.0

        for anchor in anchors:
            anchor_z = anchor.z_m if anchor.z_m is not None else 0.0
            dx = x_m - anchor.x_m
            dy = y_m - anchor.y_m
            dz = z_m - anchor_z
            distance = math.sqrt((dx * dx) + (dy * dy) + (dz * dz))
            distance = max(distance, 1e-6)
            residual = distance - anchor.range_m
            weight = 1.0 / math.sqrt(1.0 + (residual / soft_l1_scale_m) ** 2)

            grad_x = dx / distance
            grad_y = dy / distance
            grad_z = dz / distance

            jt_w_j_00 += weight * grad_x * grad_x
            jt_w_j_01 += weight * grad_x * grad_y
            jt_w_j_02 += weight * grad_x * grad_z
            jt_w_j_11 += weight * grad_y * grad_y
            jt_w_j_12 += weight * grad_y * grad_z
            jt_w_j_22 += weight * grad_z * grad_z
            jt_w_r_0 += weight * grad_x * residual
            jt_w_r_1 += weight * grad_y * residual
            jt_w_r_2 += weight * grad_z * residual

        # Small damping for numerical stability.
        jt_w_j_00 += 1e-6
        jt_w_j_11 += 1e-6
        jt_w_j_22 += 1e-6

        a = jt_w_j_00
        b = jt_w_j_01
        c = jt_w_j_02
        d = jt_w_j_11
        e = jt_w_j_12
        f = jt_w_j_22

        det = (a * ((d * f) - (e * e))) - (b * ((b * f) - (c * e))) + (c * ((b * e) - (c * d)))
        if abs(det) < 1e-9:
            return SolveResult(
                ok=False,
                x_m=None,
                y_m=None,
                z_m=None,
                residual_rms_m=None,
                iterations=iteration,
                reason="degenerate_geometry",
            )

        inv_00 = ((d * f) - (e * e)) / det
        inv_01 = ((c * e) - (b * f)) / det
        inv_02 = ((b * e) - (c * d)) / det
        inv_11 = ((a * f) - (c * c)) / det
        inv_12 = ((b * c) - (a * e)) / det
        inv_22 = ((a * d) - (b * b)) / det

        step_x = -((inv_00 * jt_w_r_0) + (inv_01 * jt_w_r_1) + (inv_02 * jt_w_r_2))
        step_y = -((inv_01 * jt_w_r_0) + (inv_11 * jt_w_r_1) + (inv_12 * jt_w_r_2))
        step_z = -((inv_02 * jt_w_r_0) + (inv_12 * jt_w_r_1) + (inv_22 * jt_w_r_2))

        x_m += step_x
        y_m += step_y
        z_m += step_z

        if math.sqrt((step_x * step_x) + (step_y * step_y) + (step_z * step_z)) <= tolerance_m:
            break

    rms = residual_rms_m_3d(x_m, y_m, z_m, anchors)
    return SolveResult(
        ok=True,
        x_m=x_m,
        y_m=y_m,
        z_m=z_m,
        residual_rms_m=rms,
        iterations=iteration,
        reason="ok",
    )
