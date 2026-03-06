"""Sample-derived RSSI ranging model for Bermuda."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .calibration import BermudaCalibrationManager


MIN_LAYOUT_TRAINING_ROWS = 5
MIN_SCANNER_BIAS_ROWS = 3
MIN_DEVICE_BIAS_ROWS = 3
MIN_DISTANCE_M = 0.1
MAX_DISTANCE_M = 100.0


@dataclass(frozen=True)
class RangeEstimate:
    """Distance estimate for one advert/anchor measurement."""

    range_m: float
    sigma_m: float
    source: str


@dataclass(frozen=True)
class _TrainingRow:
    """One fitted RSSI/distance training example."""

    scanner_address: str
    device_id: str
    distance_m: float
    rssi_dbm: float


@dataclass(frozen=True)
class _LayoutModel:
    """Fitted model for one anchor layout."""

    intercept_dbm: float
    slope_db_per_log10_m: float
    path_loss_exponent: float
    scanner_bias_db: dict[str, float]
    device_bias_db: dict[str, float]
    scanner_rssi_rmse_db: dict[str, float]
    global_rssi_rmse_db: float
    training_rows: int


class BermudaRangingModel:
    """Fit and serve sample-derived distance estimates."""

    def __init__(self, calibration: BermudaCalibrationManager) -> None:
        """Initialise model wrapper."""
        self._calibration = calibration
        self._models: dict[str, _LayoutModel] = {}

    async def async_rebuild(self) -> None:
        """Rebuild all layout models from saved calibration samples."""
        grouped_rows: dict[str, list[_TrainingRow]] = {}
        for sample in self._calibration.samples():
            if sample.get("quality", {}).get("status") == "rejected":
                continue
            layout_hash = str(sample.get("anchor_layout_hash") or "")
            device_id = str(sample.get("device_id") or "")
            position = sample.get("position") or {}
            sample_x = position.get("x_m")
            sample_y = position.get("y_m")
            sample_z = position.get("z_m")
            if not layout_hash or not device_id or sample_x is None or sample_y is None or sample_z is None:
                continue

            for scanner_address, anchor in (sample.get("anchors") or {}).items():
                anchor_position = anchor.get("anchor_position") or {}
                anchor_x = anchor_position.get("x_m")
                anchor_y = anchor_position.get("y_m")
                anchor_z = anchor_position.get("z_m")
                rssi_median = anchor.get("rssi_median")
                if anchor_x is None or anchor_y is None or anchor_z is None or rssi_median is None:
                    continue

                distance_m = math.sqrt(
                    ((float(sample_x) - float(anchor_x)) ** 2)
                    + ((float(sample_y) - float(anchor_y)) ** 2)
                    + ((float(sample_z) - float(anchor_z)) ** 2)
                )
                grouped_rows.setdefault(layout_hash, []).append(
                    _TrainingRow(
                        scanner_address=str(scanner_address).lower(),
                        device_id=device_id,
                        distance_m=max(distance_m, MIN_DISTANCE_M),
                        rssi_dbm=float(rssi_median),
                    )
                )

        models: dict[str, _LayoutModel] = {}
        for layout_hash, rows in grouped_rows.items():
            fitted = self._fit_layout(rows)
            if fitted is not None:
                models[layout_hash] = fitted
        self._models = models

    def estimate_range(
        self,
        *,
        layout_hash: str,
        scanner_address: str,
        device_id: str | None,
        filtered_rssi: float | None,
        live_rssi_dispersion: float | None = None,
    ) -> RangeEstimate | None:
        """Estimate distance and uncertainty for one scanner/device reading."""
        if filtered_rssi is None:
            return None
        model = self._models.get(layout_hash)
        if model is None:
            return None
        slope = model.slope_db_per_log10_m
        if abs(slope) < 1e-9 or model.path_loss_exponent <= 0:
            return None

        bias_db = model.scanner_bias_db.get(scanner_address.lower(), 0.0)
        if device_id is not None:
            bias_db += model.device_bias_db.get(device_id, 0.0)
        log10_distance = (float(filtered_rssi) - model.intercept_dbm - bias_db) / slope
        range_m = 10 ** log10_distance
        range_m = max(MIN_DISTANCE_M, min(range_m, MAX_DISTANCE_M))

        sigma_rssi = model.scanner_rssi_rmse_db.get(scanner_address.lower(), model.global_rssi_rmse_db)
        if live_rssi_dispersion is not None:
            sigma_rssi = max(sigma_rssi, float(live_rssi_dispersion))
        sigma_m = sigma_rssi * range_m * math.log(10) / (10 * model.path_loss_exponent)
        sigma_m = max(0.001, sigma_m)

        return RangeEstimate(range_m=range_m, sigma_m=sigma_m, source="learned")

    def has_model(self, layout_hash: str) -> bool:
        """Return whether a fitted model exists for the layout."""
        return layout_hash in self._models

    def _fit_layout(self, rows: list[_TrainingRow]) -> _LayoutModel | None:
        """Fit one linear log-distance model using least squares."""
        if len(rows) < MIN_LAYOUT_TRAINING_ROWS:
            return None

        scanner_counts: dict[str, int] = {}
        device_counts: dict[str, int] = {}
        for row in rows:
            scanner_counts[row.scanner_address] = scanner_counts.get(row.scanner_address, 0) + 1
            device_counts[row.device_id] = device_counts.get(row.device_id, 0) + 1

        scanner_terms = sorted(addr for addr, count in scanner_counts.items() if count >= MIN_SCANNER_BIAS_ROWS)
        device_terms = sorted(dev for dev, count in device_counts.items() if count >= MIN_DEVICE_BIAS_ROWS)
        scanner_columns = scanner_terms[1:]
        device_columns = device_terms[1:]

        matrix_rows: list[list[float]] = []
        targets: list[float] = []
        for row in rows:
            features = [1.0, math.log10(max(row.distance_m, MIN_DISTANCE_M))]
            for scanner_address in scanner_columns:
                features.append(1.0 if row.scanner_address == scanner_address else 0.0)
            for device_id in device_columns:
                features.append(1.0 if row.device_id == device_id else 0.0)
            matrix_rows.append(features)
            targets.append(row.rssi_dbm)

        design = np.asarray(matrix_rows, dtype=float)
        observed = np.asarray(targets, dtype=float)
        coeffs, *_rest = np.linalg.lstsq(design, observed, rcond=None)

        intercept_dbm = float(coeffs[0])
        slope_db_per_log10_m = float(coeffs[1])
        if slope_db_per_log10_m >= -1e-6:
            return None
        path_loss_exponent = max(0.01, -slope_db_per_log10_m / 10.0)

        scanner_bias_db = {scanner_terms[0]: 0.0} if scanner_terms else {}
        for index, scanner_address in enumerate(scanner_columns, start=2):
            scanner_bias_db[scanner_address] = float(coeffs[index])

        device_bias_offset = 2 + len(scanner_columns)
        device_bias_db = {device_terms[0]: 0.0} if device_terms else {}
        for offset, device_id in enumerate(device_columns, start=device_bias_offset):
            device_bias_db[device_id] = float(coeffs[offset])

        predicted = design @ coeffs
        residuals = predicted - observed
        global_rssi_rmse_db = float(np.sqrt(np.mean(np.square(residuals))))
        scanner_rssi_rmse_db: dict[str, float] = {}
        for scanner_address, count in scanner_counts.items():
            if count < MIN_SCANNER_BIAS_ROWS:
                continue
            scanner_residuals = [
                residual
                for row, residual in zip(rows, residuals, strict=False)
                if row.scanner_address == scanner_address
            ]
            if scanner_residuals:
                scanner_rssi_rmse_db[scanner_address] = float(
                    np.sqrt(np.mean(np.square(np.asarray(scanner_residuals, dtype=float))))
                )

        return _LayoutModel(
            intercept_dbm=intercept_dbm,
            slope_db_per_log10_m=slope_db_per_log10_m,
            path_loss_exponent=path_loss_exponent,
            scanner_bias_db=scanner_bias_db,
            device_bias_db=device_bias_db,
            scanner_rssi_rmse_db=scanner_rssi_rmse_db,
            global_rssi_rmse_db=global_rssi_rmse_db,
            training_rows=len(rows),
        )

    def describe_layout(self, layout_hash: str) -> dict[str, Any]:
        """Return a small summary for tests and diagnostics."""
        model = self._models.get(layout_hash)
        if model is None:
            return {"available": False}
        return {
            "available": True,
            "training_rows": model.training_rows,
            "path_loss_exponent": model.path_loss_exponent,
            "scanner_bias_count": len(model.scanner_bias_db),
            "device_bias_count": len(model.device_bias_db),
        }
