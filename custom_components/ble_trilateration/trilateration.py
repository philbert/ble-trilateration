"""Lightweight trilateration helpers for Bermuda."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

_ERR_RANGE_LIKELIHOOD_NO_ANCHORS = "range likelihood requires at least one anchor"
_ERR_RANGE_LIKELIHOOD_MISSING_Z = "3D range likelihood requires anchor Z coordinates"


@dataclass(frozen=True)
class AnchorMeasurement:
    """A single range observation from one fixed scanner anchor."""

    scanner_address: str
    x_m: float
    y_m: float
    range_m: float
    z_m: float | None = None
    sigma_m: float = 1.0


@dataclass(frozen=True)
class SolvePrior2D:
    """Soft Gaussian prior for a 2D solve."""

    x_m: float
    y_m: float
    sigma_x_m: float
    sigma_y_m: float


@dataclass(frozen=True)
class SolvePrior3D:
    """Soft Gaussian prior for a 3D solve."""

    x_m: float
    y_m: float
    z_m: float
    sigma_x_m: float
    sigma_y_m: float
    sigma_z_m: float


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


@dataclass(frozen=True)
class SolveQualityMetrics:
    """Derived quality signals for a solved trilat point."""

    geometry_quality_01: float
    residual_consistency_01: float
    gdop: float | None
    condition_number: float | None
    normalized_residual_rms: float | None


@dataclass(frozen=True)
class RangeLikelihoodMetrics:
    """Sigma-aware costs for comparing candidate range models."""

    gaussian_nll: float
    soft_l1_nll: float


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


def range_likelihood_metrics(
    x_m: float,
    y_m: float,
    anchors: list[AnchorMeasurement],
    *,
    z_m: float | None = None,
) -> RangeLikelihoodMetrics:
    """
    Return sigma-aware range costs at a candidate position.

    The Gaussian cost is a conventional negative log likelihood. The soft-L1
    cost uses ``sqrt(1 + u²) - 1`` for normalized residual ``u`` and includes
    ``log(sigma)`` so a candidate cannot improve merely by inflating every
    uncertainty. Its distribution-normalization constant is omitted because
    candidate floors always compare the same observations.
    """
    if not anchors:
        raise ValueError(_ERR_RANGE_LIKELIHOOD_NO_ANCHORS)

    gaussian_nll = 0.0
    soft_l1_nll = 0.0
    gaussian_constant = 0.5 * math.log(2.0 * math.pi)
    for anchor in anchors:
        if z_m is None:
            distance = math.hypot(x_m - anchor.x_m, y_m - anchor.y_m)
        else:
            if anchor.z_m is None:
                raise ValueError(_ERR_RANGE_LIKELIHOOD_MISSING_Z)
            distance = math.dist((x_m, y_m, z_m), (anchor.x_m, anchor.y_m, anchor.z_m))
        sigma_m = max(anchor.sigma_m, 1e-3)
        normalized_residual = (distance - anchor.range_m) / sigma_m
        gaussian_nll += gaussian_constant + math.log(sigma_m) + (0.5 * normalized_residual**2)
        soft_l1_nll += math.log(sigma_m) + math.sqrt(1.0 + normalized_residual**2) - 1.0

    return RangeLikelihoodMetrics(
        gaussian_nll=gaussian_nll,
        soft_l1_nll=soft_l1_nll,
    )


def solve_quality_metrics_2d(x_m: float, y_m: float, anchors: list[AnchorMeasurement]) -> SolveQualityMetrics:
    """Compute geometry and residual quality metrics for a solved 2D point."""
    return _solve_quality_metrics(x_m=x_m, y_m=y_m, z_m=None, anchors=anchors, dimensions=2)


def solve_quality_metrics_3d(
    x_m: float,
    y_m: float,
    z_m: float,
    anchors: list[AnchorMeasurement],
) -> SolveQualityMetrics:
    """Compute geometry and residual quality metrics for a solved 3D point."""
    return _solve_quality_metrics(x_m=x_m, y_m=y_m, z_m=z_m, anchors=anchors, dimensions=3)


def _solve_quality_metrics(
    *,
    x_m: float,
    y_m: float,
    z_m: float | None,
    anchors: list[AnchorMeasurement],
    dimensions: int,
) -> SolveQualityMetrics:
    """Compute normalized geometry and residual-consistency scores for a solve."""
    if len(anchors) < dimensions + 1:
        return SolveQualityMetrics(
            geometry_quality_01=0.0,
            residual_consistency_01=0.0,
            gdop=None,
            condition_number=None,
            normalized_residual_rms=None,
        )

    jacobian_rows: list[list[float]] = []
    normalized_residuals: list[float] = []
    for anchor in anchors:
        sigma = max(anchor.sigma_m, 1e-3)
        if dimensions == 3:
            if z_m is None or anchor.z_m is None:
                return SolveQualityMetrics(
                    geometry_quality_01=0.0,
                    residual_consistency_01=0.0,
                    gdop=None,
                    condition_number=None,
                    normalized_residual_rms=None,
                )
            dx = x_m - anchor.x_m
            dy = y_m - anchor.y_m
            dz = z_m - anchor.z_m
            distance = math.sqrt((dx * dx) + (dy * dy) + (dz * dz))
            distance = max(distance, 1e-6)
            jacobian_rows.append([dx / distance / sigma, dy / distance / sigma, dz / distance / sigma])
        else:
            dx = x_m - anchor.x_m
            dy = y_m - anchor.y_m
            distance = math.hypot(dx, dy)
            distance = max(distance, 1e-6)
            jacobian_rows.append([dx / distance / sigma, dy / distance / sigma])
        normalized_residuals.append((distance - anchor.range_m) / sigma)

    design = np.asarray(jacobian_rows, dtype=float)
    info = design.T @ design
    try:
        covariance = np.linalg.pinv(info, hermitian=True)
        gdop = float(np.sqrt(np.trace(covariance)))
        condition_number = float(np.linalg.cond(info))
    except np.linalg.LinAlgError:
        gdop = None
        condition_number = None

    gdop_term = 0.0
    if gdop is not None and math.isfinite(gdop):
        gdop_term = 1.0 / (1.0 + max(0.0, gdop - 1.0))

    cond_term = 0.0
    if condition_number is not None and math.isfinite(condition_number):
        cond_term = 1.0 / (1.0 + max(0.0, math.log10(max(condition_number, 1.0))))

    geometry_quality_01 = math.sqrt(max(0.0, gdop_term) * max(0.0, cond_term))
    normalized = np.asarray(normalized_residuals, dtype=float)
    normalized_residual_rms = float(np.sqrt(np.mean(np.square(normalized))))
    residual_consistency_01 = float(np.mean(np.exp(-0.5 * np.clip(np.square(normalized), 0.0, 16.0))))

    return SolveQualityMetrics(
        geometry_quality_01=max(0.0, min(1.0, geometry_quality_01)),
        residual_consistency_01=max(0.0, min(1.0, residual_consistency_01)),
        gdop=gdop,
        condition_number=condition_number,
        normalized_residual_rms=normalized_residual_rms,
    )


def solve_2d_soft_l1(
    anchors: list[AnchorMeasurement],
    initial_guess: tuple[float, float] | None = None,
    prior: SolvePrior2D | None = None,
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
            robust_weight = 1.0 / math.sqrt(1.0 + (residual / soft_l1_scale_m) ** 2)
            sigma = max(anchor.sigma_m, 1e-3)
            measurement_weight = 1.0 / (sigma * sigma)
            weight = robust_weight * measurement_weight

            grad_x = dx / distance
            grad_y = dy / distance

            jt_w_j_00 += weight * grad_x * grad_x
            jt_w_j_01 += weight * grad_x * grad_y
            jt_w_j_11 += weight * grad_y * grad_y
            jt_w_r_0 += weight * grad_x * residual
            jt_w_r_1 += weight * grad_y * residual

        if prior is not None:
            sigma_x = max(prior.sigma_x_m, 1e-3)
            sigma_y = max(prior.sigma_y_m, 1e-3)
            prior_weight_x = 1.0 / (sigma_x * sigma_x)
            prior_weight_y = 1.0 / (sigma_y * sigma_y)
            jt_w_j_00 += prior_weight_x
            jt_w_j_11 += prior_weight_y
            jt_w_r_0 += prior_weight_x * (x_m - prior.x_m)
            jt_w_r_1 += prior_weight_y * (y_m - prior.y_m)

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
    prior: SolvePrior3D | None = None,
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
            robust_weight = 1.0 / math.sqrt(1.0 + (residual / soft_l1_scale_m) ** 2)
            sigma = max(anchor.sigma_m, 1e-3)
            measurement_weight = 1.0 / (sigma * sigma)
            weight = robust_weight * measurement_weight

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

        if prior is not None:
            sigma_x = max(prior.sigma_x_m, 1e-3)
            sigma_y = max(prior.sigma_y_m, 1e-3)
            sigma_z = max(prior.sigma_z_m, 1e-3)
            prior_weight_x = 1.0 / (sigma_x * sigma_x)
            prior_weight_y = 1.0 / (sigma_y * sigma_y)
            prior_weight_z = 1.0 / (sigma_z * sigma_z)
            jt_w_j_00 += prior_weight_x
            jt_w_j_11 += prior_weight_y
            jt_w_j_22 += prior_weight_z
            jt_w_r_0 += prior_weight_x * (x_m - prior.x_m)
            jt_w_r_1 += prior_weight_y * (y_m - prior.y_m)
            jt_w_r_2 += prior_weight_z * (z_m - prior.z_m)

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
