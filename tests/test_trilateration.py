"""Tests for trilateration helper module."""

from __future__ import annotations

import math

from custom_components.ble_trilateration.trilateration import (
    AnchorMeasurement,
    SolvePrior2D,
    SolvePrior3D,
    anchor_centroid,
    anchor_centroid_3d,
    residual_rms_m,
    residual_rms_m_3d,
    solve_quality_metrics_2d,
    solve_quality_metrics_3d,
    solve_2d_soft_l1,
    solve_3d_soft_l1,
)


def test_anchor_centroid():
    """Centroid should be the arithmetic mean of anchor coordinates."""
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, 1.0),
        AnchorMeasurement("b", 2.0, 0.0, 1.0),
        AnchorMeasurement("c", 0.0, 2.0, 1.0),
    ]
    assert anchor_centroid(anchors) == (2 / 3, 2 / 3)


def test_solve_2d_soft_l1_returns_expected_point():
    """Solver should recover the circumcenter of a right triangle exactly."""
    # Right triangle A(0,0) B(6,0) C(0,8): circumcenter is the midpoint of the
    # hypotenuse at (3, 4), equidistant from all three vertices at 5 m.
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, 5.0),
        AnchorMeasurement("b", 6.0, 0.0, 5.0),
        AnchorMeasurement("c", 0.0, 8.0, 5.0),
    ]
    result = solve_2d_soft_l1(anchors, initial_guess=(4.0, 4.0))
    assert result.ok
    assert result.x_m is not None
    assert result.y_m is not None
    assert result.residual_rms_m is not None
    assert abs(result.x_m - 3.0) < 0.1
    assert abs(result.y_m - 4.0) < 0.1
    assert result.residual_rms_m < 0.01


def test_solve_2d_soft_l1_rejects_two_anchor_case():
    """Two anchors are insufficient and must be rejected."""
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, 3.0),
        AnchorMeasurement("b", 4.0, 0.0, 3.0),
    ]
    result = solve_2d_soft_l1(anchors)
    assert not result.ok
    assert result.reason == "insufficient_anchors"


def test_residual_rms_m():
    """Residual RMS should be near zero when the point satisfies all anchors exactly."""
    # Same right-triangle geometry: circumcenter (3, 4) is exactly 5 m from each vertex.
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, 5.0),
        AnchorMeasurement("b", 6.0, 0.0, 5.0),
        AnchorMeasurement("c", 0.0, 8.0, 5.0),
    ]
    rms = residual_rms_m(3.0, 4.0, anchors)
    assert rms < 1e-3


def test_anchor_centroid_3d():
    """3D centroid should include z coordinates."""
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, 1.0, 0.0),
        AnchorMeasurement("b", 2.0, 0.0, 1.0, 2.0),
        AnchorMeasurement("c", 0.0, 2.0, 1.0, 4.0),
    ]
    assert anchor_centroid_3d(anchors) == (2 / 3, 2 / 3, 2.0)


def test_solve_3d_soft_l1_returns_expected_point():
    """3D solver should recover a point with non-coplanar anchors."""
    target = (1.0, 1.0, 1.0)
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, math.sqrt(3.0), 0.0),
        AnchorMeasurement("b", 2.0, 0.0, math.sqrt(3.0), 0.0),
        AnchorMeasurement("c", 0.0, 2.0, math.sqrt(3.0), 0.0),
        AnchorMeasurement("d", 0.0, 0.0, math.sqrt(3.0), 2.0),
    ]
    result = solve_3d_soft_l1(anchors, initial_guess=(0.8, 0.9, 0.7))
    assert result.ok
    assert result.x_m is not None
    assert result.y_m is not None
    assert result.z_m is not None
    assert result.residual_rms_m is not None
    assert abs(result.x_m - target[0]) < 0.1
    assert abs(result.y_m - target[1]) < 0.1
    assert abs(result.z_m - target[2]) < 0.1
    assert result.residual_rms_m < 0.01


def test_solve_3d_soft_l1_rejects_three_anchor_case():
    """Three anchors are insufficient for 3D and must be rejected."""
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, 3.0, 0.0),
        AnchorMeasurement("b", 4.0, 0.0, 3.0, 0.0),
        AnchorMeasurement("c", 0.0, 4.0, 3.0, 2.0),
    ]
    result = solve_3d_soft_l1(anchors)
    assert not result.ok
    assert result.reason == "insufficient_anchors"


def test_residual_rms_m_3d():
    """3D residual RMS should be near zero at the true point."""
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, math.sqrt(3.0), 0.0),
        AnchorMeasurement("b", 2.0, 0.0, math.sqrt(3.0), 0.0),
        AnchorMeasurement("c", 0.0, 2.0, math.sqrt(3.0), 0.0),
        AnchorMeasurement("d", 0.0, 0.0, math.sqrt(3.0), 2.0),
    ]
    rms = residual_rms_m_3d(1.0, 1.0, 1.0, anchors)
    assert rms < 1e-3


def test_solve_quality_metrics_reward_well_spread_geometry():
    """Well-spread anchors should score better than nearly collinear anchors."""
    good = [
        AnchorMeasurement("a", 0.0, 0.0, 5.0, sigma_m=1.0),
        AnchorMeasurement("b", 6.0, 0.0, 5.0, sigma_m=1.0),
        AnchorMeasurement("c", 0.0, 8.0, 5.0, sigma_m=1.0),
    ]
    poor = [
        AnchorMeasurement("a", 0.0, 0.0, 2.0, sigma_m=1.0),
        AnchorMeasurement("b", 4.0, 0.2, 2.0, sigma_m=1.0),
        AnchorMeasurement("c", 8.0, 0.4, 6.0, sigma_m=1.0),
    ]

    good_metrics = solve_quality_metrics_2d(3.0, 4.0, good)
    poor_metrics = solve_quality_metrics_2d(3.0, 0.1, poor)

    assert good_metrics.geometry_quality_01 > poor_metrics.geometry_quality_01
    assert good_metrics.gdop is not None
    assert poor_metrics.condition_number is not None


def test_solve_quality_metrics_penalize_inconsistent_residuals():
    """Residual consistency should fall when normalized residuals diverge badly."""
    consistent = [
        AnchorMeasurement("a", 0.0, 0.0, 5.0, sigma_m=1.0),
        AnchorMeasurement("b", 6.0, 0.0, 5.0, sigma_m=1.0),
        AnchorMeasurement("c", 0.0, 8.0, 5.0, sigma_m=1.0),
    ]
    inconsistent = [
        AnchorMeasurement("a", 0.0, 0.0, 5.0, sigma_m=1.0),
        AnchorMeasurement("b", 6.0, 0.0, 2.0, sigma_m=1.0),
        AnchorMeasurement("c", 0.0, 8.0, 8.0, sigma_m=1.0),
    ]

    consistent_metrics = solve_quality_metrics_2d(3.0, 4.0, consistent)
    inconsistent_metrics = solve_quality_metrics_2d(3.0, 4.0, inconsistent)

    assert consistent_metrics.residual_consistency_01 > inconsistent_metrics.residual_consistency_01
    assert consistent_metrics.normalized_residual_rms is not None
    assert inconsistent_metrics.normalized_residual_rms is not None
    assert consistent_metrics.normalized_residual_rms < inconsistent_metrics.normalized_residual_rms


def test_solve_quality_metrics_3d_expose_geometry_and_residuals():
    """3D quality metrics should be populated for non-coplanar anchors."""
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, math.sqrt(3.0), 0.0, sigma_m=1.0),
        AnchorMeasurement("b", 2.0, 0.0, math.sqrt(3.0), 0.0, sigma_m=1.0),
        AnchorMeasurement("c", 0.0, 2.0, math.sqrt(3.0), 0.0, sigma_m=1.0),
        AnchorMeasurement("d", 0.0, 0.0, math.sqrt(3.0), 2.0, sigma_m=1.0),
    ]

    metrics = solve_quality_metrics_3d(1.0, 1.0, 1.0, anchors)

    assert metrics.geometry_quality_01 > 0.0
    assert metrics.residual_consistency_01 > 0.9
    assert metrics.gdop is not None
    assert metrics.condition_number is not None


def test_solve_2d_soft_l1_prior_pulls_noisy_solution_toward_previous_state():
    """A soft prior should pull a weakly-constrained solve toward the previous plausible position."""
    target = (10.0, 12.0)
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, math.hypot(target[0], target[1]), sigma_m=4.0),
        AnchorMeasurement("b", 6.0, 0.0, math.hypot(target[0] - 6.0, target[1]), sigma_m=4.0),
        AnchorMeasurement("c", 0.0, 8.0, math.hypot(target[0], target[1] - 8.0), sigma_m=4.0),
    ]
    prior = SolvePrior2D(x_m=3.0, y_m=4.0, sigma_x_m=0.8, sigma_y_m=0.8)

    unprior_result = solve_2d_soft_l1(anchors, initial_guess=(9.0, 10.0))
    prior_result = solve_2d_soft_l1(anchors, initial_guess=(prior.x_m, prior.y_m), prior=prior)

    assert unprior_result.ok
    assert prior_result.ok
    assert prior_result.x_m is not None
    assert prior_result.y_m is not None
    assert unprior_result.x_m is not None
    assert unprior_result.y_m is not None

    unprior_distance = math.hypot(unprior_result.x_m - prior.x_m, unprior_result.y_m - prior.y_m)
    prior_distance = math.hypot(prior_result.x_m - prior.x_m, prior_result.y_m - prior.y_m)
    assert prior_distance < unprior_distance


def test_solve_3d_soft_l1_prior_pulls_noisy_solution_toward_previous_state():
    """A soft prior should influence 3D solves when anchor uncertainty is high."""
    target = (3.5, 3.0, 2.8)
    anchors = [
        AnchorMeasurement("a", 0.0, 0.0, math.dist(target, (0.0, 0.0, 0.0)), 0.0, sigma_m=5.0),
        AnchorMeasurement("b", 2.0, 0.0, math.dist(target, (2.0, 0.0, 0.0)), 0.0, sigma_m=5.0),
        AnchorMeasurement("c", 0.0, 2.0, math.dist(target, (0.0, 2.0, 0.0)), 0.0, sigma_m=5.0),
        AnchorMeasurement("d", 0.0, 0.0, math.dist(target, (0.0, 0.0, 2.0)), 2.0, sigma_m=5.0),
    ]
    prior = SolvePrior3D(
        x_m=1.0,
        y_m=1.0,
        z_m=1.0,
        sigma_x_m=0.9,
        sigma_y_m=0.9,
        sigma_z_m=0.9,
    )

    unprior_result = solve_3d_soft_l1(anchors, initial_guess=target)
    prior_result = solve_3d_soft_l1(
        anchors,
        initial_guess=(prior.x_m, prior.y_m, prior.z_m),
        prior=prior,
    )

    assert unprior_result.ok
    assert prior_result.ok
    assert unprior_result.x_m is not None
    assert unprior_result.y_m is not None
    assert unprior_result.z_m is not None
    assert prior_result.x_m is not None
    assert prior_result.y_m is not None
    assert prior_result.z_m is not None

    unprior_distance = math.dist(
        (unprior_result.x_m, unprior_result.y_m, unprior_result.z_m),
        (prior.x_m, prior.y_m, prior.z_m),
    )
    prior_distance = math.dist(
        (prior_result.x_m, prior_result.y_m, prior_result.z_m),
        (prior.x_m, prior.y_m, prior.z_m),
    )
    assert prior_distance < unprior_distance
