"""Tests for trilateration helper module."""

from __future__ import annotations

import math

from custom_components.bermuda.trilateration import (
    AnchorMeasurement,
    anchor_centroid,
    anchor_centroid_3d,
    residual_rms_m,
    residual_rms_m_3d,
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
