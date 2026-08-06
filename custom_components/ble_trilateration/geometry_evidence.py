"""
Floor-neutral geometry evaluation and vertical-observability measurement.

Geometry can only be honest floor evidence if it is computed without reference to
the floor it is being asked about. Two things previously broke that:

1. Anchor measurement sigma was inflated for anchors on "other" floors, so each
   candidate was scored with a different noise model. The likelihood then tracked
   which floor owned the most anchors rather than where the device was.
2. The vertical prior pinned z to `floor_z + phone_height` of the candidate, so
   the solve returned the prior and the comparison measured the assumption.

Both are removed here: identical sigmas for every candidate, no continuity prior
from the incumbent, and a z likelihood profiled directly from the ranges.

The profile is also the instrument for deciding whether geometry may ever vote on
floors at all. It reports where the ranges actually place z and how sharply, so
vertical observability becomes a measured trend that improves as anchors are
added, rather than an assumption to be argued about.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .trilateration import AnchorMeasurement

# Z search span for the profile, relative to the anchor z envelope. Wide enough
# to see a peak that sits outside the anchor spread, which is itself diagnostic.
Z_PROFILE_MARGIN_M = 3.0
Z_PROFILE_STEPS = 61
# A profile flatter than this over the search span carries no vertical
# information: every height fits the ranges about equally well.
Z_PROFILE_FLAT_NLL_DELTA = 0.5


@dataclass(frozen=True)
class ZProfilePoint:
    """One height hypothesis and how badly it fits the observed ranges."""

    z_m: float
    nll: float


@dataclass(frozen=True)
class ZObservability:
    """
    What the ranges alone say about height.

    `informative` is the gate that matters: until the profile has a real minimum,
    geometry cannot separate floors no matter how the candidates are scored, and
    any vertical term is reporting its own prior back.
    """

    informative: bool
    best_z_m: float | None
    nll_at_best: float | None
    # Curvature-derived 1-sigma width. Compare against floor separation: a sigma
    # wider than the gap between floors means the floors are not distinguishable.
    sigma_z_m: float | None
    nll_span: float
    profile: tuple[ZProfilePoint, ...] = ()
    reason: str = "ok"

    def as_diagnostics(self) -> dict[str, Any]:
        """Return a JSON-safe summary; the full profile stays out of the dump."""
        return {
            "informative": self.informative,
            "best_z_m": None if self.best_z_m is None else round(self.best_z_m, 3),
            "nll_at_best": None if self.nll_at_best is None else round(self.nll_at_best, 4),
            "sigma_z_m": None if self.sigma_z_m is None else round(self.sigma_z_m, 3),
            "nll_span": round(self.nll_span, 4),
            "profile_points": len(self.profile),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FloorNeutralGeometry:
    """Geometry evidence computed identically for every candidate floor."""

    z_observability: ZObservability
    # Per-floor score derived only from the z profile and each floor's height
    # band. Empty when the profile is uninformative - which is the honest answer,
    # not a reason to fall back on something floor-derived.
    floor_scores: dict[str, float] = field(default_factory=dict)
    reason: str = "ok"

    def as_diagnostics(self) -> dict[str, Any]:
        """Return a JSON-safe view for decision frames."""
        return {
            "reason": self.reason,
            "floor_scores": {key: round(value, 6) for key, value in self.floor_scores.items()},
            "z_observability": self.z_observability.as_diagnostics(),
        }


def _range_nll_at(x_m: float, y_m: float, z_m: float, anchors: list[AnchorMeasurement]) -> float:
    """Return the sigma-weighted negative log-likelihood of one position."""
    total = 0.0
    for anchor in anchors:
        anchor_z = anchor.z_m if anchor.z_m is not None else 0.0
        dx = x_m - anchor.x_m
        dy = y_m - anchor.y_m
        dz = z_m - anchor_z
        predicted = math.sqrt((dx * dx) + (dy * dy) + (dz * dz))
        sigma = max(anchor.sigma_m, 1e-3)
        residual = (predicted - anchor.range_m) / sigma
        # Soft-L1 so a single wild range cannot dominate the profile shape.
        total += 2.0 * (math.sqrt(1.0 + (residual * residual)) - 1.0)
    return total


def _profile_xy_at_z(
    z_m: float,
    anchors: list[AnchorMeasurement],
    seed_xy: tuple[float, float],
) -> tuple[float, float, float]:
    """Minimise over x/y at a fixed height by coordinate descent from a seed."""
    x_m, y_m = seed_xy
    best = _range_nll_at(x_m, y_m, z_m, anchors)
    step = 2.0
    while step > 0.05:
        improved = False
        for dx, dy in ((step, 0.0), (-step, 0.0), (0.0, step), (0.0, -step)):
            candidate = _range_nll_at(x_m + dx, y_m + dy, z_m, anchors)
            if candidate < best:
                x_m, y_m, best = x_m + dx, y_m + dy, candidate
                improved = True
                break
        if not improved:
            step *= 0.5
    return x_m, y_m, best


def measure_z_observability(anchors: list[AnchorMeasurement]) -> ZObservability:
    """
    Profile the range likelihood over height, with x/y minimised out at each step.

    No floor is assumed and no vertical prior is applied, so the result answers
    "what do the ranges say about height" rather than "how well does this height
    match the floor we already picked".
    """
    usable = [anchor for anchor in anchors if anchor.z_m is not None]
    if len(usable) < 3:
        return ZObservability(
            informative=False,
            best_z_m=None,
            nll_at_best=None,
            sigma_z_m=None,
            nll_span=0.0,
            reason="insufficient_anchors",
        )

    anchor_zs = [float(anchor.z_m) for anchor in usable]  # type: ignore[arg-type]
    z_low = min(anchor_zs) - Z_PROFILE_MARGIN_M
    z_high = max(anchor_zs) + Z_PROFILE_MARGIN_M
    seed_xy = (
        sum(anchor.x_m for anchor in usable) / len(usable),
        sum(anchor.y_m for anchor in usable) / len(usable),
    )

    points: list[ZProfilePoint] = []
    step = (z_high - z_low) / max(Z_PROFILE_STEPS - 1, 1)
    for index in range(Z_PROFILE_STEPS):
        z_m = z_low + (index * step)
        _x, _y, nll = _profile_xy_at_z(z_m, usable, seed_xy)
        points.append(ZProfilePoint(z_m=z_m, nll=nll))

    best_point = min(points, key=lambda point: point.nll)
    worst_nll = max(point.nll for point in points)
    nll_span = worst_nll - best_point.nll
    if nll_span < Z_PROFILE_FLAT_NLL_DELTA:
        return ZObservability(
            informative=False,
            best_z_m=best_point.z_m,
            nll_at_best=best_point.nll,
            sigma_z_m=None,
            nll_span=nll_span,
            profile=tuple(points),
            reason="flat_profile",
        )

    # 1-sigma width from the +0.5 NLL crossing either side of the minimum.
    sigma_z_m = _profile_sigma(points, best_point)
    return ZObservability(
        informative=True,
        best_z_m=best_point.z_m,
        nll_at_best=best_point.nll,
        sigma_z_m=sigma_z_m,
        nll_span=nll_span,
        profile=tuple(points),
    )


def _profile_sigma(points: list[ZProfilePoint], best: ZProfilePoint) -> float | None:
    """Return the half-width of the profile at NLL + 0.5, or None if unbounded."""
    threshold = best.nll + 0.5
    below = [point.z_m for point in points if point.nll <= threshold]
    if len(below) < 2:
        return None
    return (max(below) - min(below)) / 2.0


def floor_scores_from_z_profile(
    observability: ZObservability,
    floor_height_bands: dict[str, tuple[float, float]],
) -> dict[str, float]:
    """
    Map a measured z profile onto each floor's plausible occupancy band.

    A floor scores by the best likelihood available anywhere inside its own band,
    so a device is judged against where it could physically be on that floor
    rather than against a single nominal height.
    """
    if not observability.informative or not observability.profile:
        return {}
    scores: dict[str, float] = {}
    for floor_id, (low, high) in floor_height_bands.items():
        inside = [point for point in observability.profile if low <= point.z_m <= high]
        if not inside:
            continue
        best_inside = min(point.nll for point in inside)
        scores[floor_id] = math.exp(-(best_inside - (observability.nll_at_best or 0.0)))
    return scores


def evaluate_floor_neutral_geometry(
    anchors: list[AnchorMeasurement],
    floor_height_bands: dict[str, tuple[float, float]],
) -> FloorNeutralGeometry:
    """Measure vertical observability and score floors from it, assuming no floor."""
    observability = measure_z_observability(anchors)
    if not observability.informative:
        return FloorNeutralGeometry(
            z_observability=observability,
            floor_scores={},
            reason=observability.reason,
        )
    scores = floor_scores_from_z_profile(observability, floor_height_bands)
    return FloorNeutralGeometry(
        z_observability=observability,
        floor_scores=scores,
        reason="ok" if scores else "no_floor_bands",
    )
