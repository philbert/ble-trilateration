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
MIN_SCANNER_SLOPE_ROWS = 15
MIN_SCANNER_SLOPE_SPAN_M = 2.0
MIN_SCANNER_SLOPE_DISTANCE_BUCKETS = 3
SCANNER_SLOPE_BUCKET_SIZE_M = 1.5
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
    scanner_slope_intercept_db: dict[str, float]
    scanner_slope_db_per_log10_m: dict[str, float]
    scanner_path_loss_exponent: dict[str, float]
    global_rssi_rmse_db: float
    training_rows: int


class BermudaRangingModel:
    """Fit and serve sample-derived distance estimates."""

    def __init__(self, calibration: BermudaCalibrationManager) -> None:
        """Initialise model wrapper."""
        self._calibration = calibration
        self._models: dict[str, _LayoutModel] = {}

    async def async_rebuild(self) -> None:
        """Rebuild all layout models from saved calibration samples.

        NOTE: _fit_layout calls numpy.linalg.lstsq synchronously. For typical
        home deployments (tens of samples) this is negligible. If sample counts
        grow large enough to block the event loop, move _fit_layout into
        hass.async_add_executor_job.
        """
        grouped_rows: dict[str, list[_TrainingRow]] = {}
        runtime_layout_hash_for_sample = getattr(self._calibration, "runtime_layout_hash_for_sample", None)
        current_layout_hash = getattr(self._calibration, "current_anchor_layout_hash", "")
        current_anchor_index_fn = getattr(self._calibration, "_current_anchor_identity_index", None)
        current_anchor_index = current_anchor_index_fn() if callable(current_anchor_index_fn) else None
        for sample in self._calibration.samples():
            if sample.get("quality", {}).get("status") == "rejected":
                continue
            if callable(runtime_layout_hash_for_sample):
                layout_hash = runtime_layout_hash_for_sample(
                    sample,
                    current_anchor_index=current_anchor_index,
                    current_layout_hash=current_layout_hash,
                )
            else:
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
        live_packet_count: int | None = None,
        timestamp_health_penalty: float = 0.0,
    ) -> RangeEstimate | None:
        """Estimate distance and uncertainty for one scanner/device reading."""
        if filtered_rssi is None:
            return None
        model = self._models.get(layout_hash)
        if model is None:
            return None
        scanner_key = scanner_address.lower()
        device_bias_db = model.device_bias_db.get(device_id, 0.0) if device_id is not None else 0.0

        if scanner_key in model.scanner_slope_db_per_log10_m:
            intercept_dbm = model.scanner_slope_intercept_db[scanner_key]
            slope = model.scanner_slope_db_per_log10_m[scanner_key]
            path_loss_exponent = model.scanner_path_loss_exponent[scanner_key]
            observed_rssi = float(filtered_rssi) - device_bias_db
        else:
            slope = model.slope_db_per_log10_m
            path_loss_exponent = model.path_loss_exponent
            intercept_dbm = model.intercept_dbm + model.scanner_bias_db.get(scanner_key, 0.0) + device_bias_db
            observed_rssi = float(filtered_rssi)

        if abs(slope) < 1e-9 or path_loss_exponent <= 0:
            return None

        log10_distance = (observed_rssi - intercept_dbm) / slope
        range_m = 10 ** log10_distance
        range_m = max(MIN_DISTANCE_M, min(range_m, MAX_DISTANCE_M))

        sigma_rssi = model.scanner_rssi_rmse_db.get(scanner_key, model.global_rssi_rmse_db)
        if live_rssi_dispersion is not None:
            # Live-window dispersion is a direct multipath/noise signal, but the
            # calibration RMSE already captures some baseline spread. Weight the
            # live component slightly lower so calm windows are rewarded while
            # noisy windows still widen the likelihood band quickly.
            live_dispersion = max(0.0, float(live_rssi_dispersion))
            sigma_rssi = math.sqrt((sigma_rssi * sigma_rssi) + ((0.8 * live_dispersion) ** 2))
        if live_packet_count is None:
            sigma_rssi *= 1.35
        elif live_packet_count > 0:
            packet_count = max(1, int(live_packet_count))
            # More packets should earn a tighter estimate, but cap the reward
            # to avoid overconfidence from bursty scanners.
            packet_factor = math.sqrt(5.0 / float(packet_count))
            sigma_rssi *= max(0.75, min(2.5, packet_factor))
        if timestamp_health_penalty > 0.0:
            sigma_rssi *= 1.0 + float(timestamp_health_penalty)
        sigma_m = sigma_rssi * range_m * math.log(10) / (10 * path_loss_exponent)
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
        # Dummy-variable encoding: scanner_terms[0] and device_terms[0] are the
        # reference levels (bias fixed at 0.0). All others get a coefficient
        # relative to that reference. The reference is chosen by alphabetical
        # sort, which is arbitrary — its idiosyncrasies are absorbed into the
        # global intercept rather than any named scanner bias term.
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

        scanner_slope_intercept_db: dict[str, float] = {}
        scanner_slope_db_per_log10_m: dict[str, float] = {}
        scanner_path_loss_exponent: dict[str, float] = {}
        for scanner_address, count in scanner_counts.items():
            if count < MIN_SCANNER_SLOPE_ROWS:
                continue
            scanner_rows = [row for row in rows if row.scanner_address == scanner_address]
            if len(scanner_rows) < MIN_SCANNER_SLOPE_ROWS:
                continue
            distance_values = [row.distance_m for row in scanner_rows]
            if (max(distance_values) - min(distance_values)) < MIN_SCANNER_SLOPE_SPAN_M:
                continue
            bucket_count = len(
                {
                    int(math.floor(max(row.distance_m, MIN_DISTANCE_M) / SCANNER_SLOPE_BUCKET_SIZE_M))
                    for row in scanner_rows
                }
            )
            if bucket_count < MIN_SCANNER_SLOPE_DISTANCE_BUCKETS:
                continue

            sub_design = np.asarray(
                [[1.0, math.log10(max(row.distance_m, MIN_DISTANCE_M))] for row in scanner_rows],
                dtype=float,
            )
            sub_observed = np.asarray(
                [
                    row.rssi_dbm - device_bias_db.get(row.device_id, 0.0)
                    for row in scanner_rows
                ],
                dtype=float,
            )
            sub_coeffs, *_subrest = np.linalg.lstsq(sub_design, sub_observed, rcond=None)
            scanner_intercept_dbm = float(sub_coeffs[0])
            scanner_slope = float(sub_coeffs[1])
            if scanner_slope >= -1e-6:
                continue
            scanner_slope_intercept_db[scanner_address] = scanner_intercept_dbm
            scanner_slope_db_per_log10_m[scanner_address] = scanner_slope
            scanner_path_loss_exponent[scanner_address] = max(0.01, -scanner_slope / 10.0)

        return _LayoutModel(
            intercept_dbm=intercept_dbm,
            slope_db_per_log10_m=slope_db_per_log10_m,
            path_loss_exponent=path_loss_exponent,
            scanner_bias_db=scanner_bias_db,
            device_bias_db=device_bias_db,
            scanner_rssi_rmse_db=scanner_rssi_rmse_db,
            scanner_slope_intercept_db=scanner_slope_intercept_db,
            scanner_slope_db_per_log10_m=scanner_slope_db_per_log10_m,
            scanner_path_loss_exponent=scanner_path_loss_exponent,
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
            "scanner_slope_count": len(model.scanner_slope_db_per_log10_m),
        }
