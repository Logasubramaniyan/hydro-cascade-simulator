"""A deliberately small, identifiable efficiency fit with a chronological held-out boundary."""

from dataclasses import dataclass, replace
import numpy as np
import pandas as pd

from .assets import integer
from .calendar import instant, validate_timestamps
from .evaluation import numeric


@dataclass
class EfficiencyFit:
    fitted_unit: object
    predictions: pd.DataFrame
    audit: dict


def fit_unit_efficiency(
    unit, curve, observations, training_end_utc, dt_seconds=900, minimum_training_intervals=8
):
    """Fit one multiplier on a supplied unit efficiency curve, with known geometry and discharge.

    This does not infer inflows, geometry, travel delays or unit commitment. Data must be individual
    unit metering, not aggregate plant generation copied to every unit. Future rows only score fit.
    """
    if unit.plant_id != curve.plant_id:
        raise ValueError("Unit and reservoir curve must refer to the same plant")
    integer(minimum_training_intervals, "minimum_training_intervals", 2)
    if "timestamp" not in observations:
        raise ValueError("Calibration observations require timestamp keys")
    stamps = validate_timestamps(observations.timestamp, dt_seconds=dt_seconds)
    names = ("start_storage_m3", "end_storage_m3", "flow_m3s", "power_mw")
    measured = numeric(observations, names, names)
    cutoff = instant(training_end_utc)
    training = stamps + pd.Timedelta(seconds=dt_seconds) <= cutoff
    heldout = stamps >= cutoff
    if training.sum() < minimum_training_intervals:
        raise ValueError("Insufficient completed training intervals before cutoff")
    if not heldout.any():
        raise ValueError("At least one later held-out interval is required")
    predicted = np.array(
        [
            curve.power(unit, q, a, b)
            for a, b, q in zip(measured["start_storage_m3"], measured["end_storage_m3"], measured["flow_m3s"])
        ]
    )
    x, y = predicted[training], measured["power_mw"][training]
    if np.dot(x, x) < 1e-12:
        raise ValueError("Active generation is required to identify training efficiency")
    if ((predicted == 0) & (measured["power_mw"] > 1e-6)).any():
        raise ValueError("Positive measured generation with zero discharge is inconsistent")
    low, high = 0.1, 1 / max(unit.efficiency)
    unconstrained = float(np.dot(x, y) / np.dot(x, x))
    scale = float(np.clip(unconstrained, low, high))
    fitted = replace(unit, efficiency=tuple(min(e * scale, 1.0) for e in unit.efficiency))
    error = predicted * scale - measured["power_mw"]
    predictions = observations.copy()
    predictions["predicted_power_mw"] = predicted * scale
    predictions["partition"] = np.where(
        training, "training", np.where(heldout, "held_out", "crosses_cutoff_excluded")
    )
    audit = dict(
        efficiency_scale=scale,
        unconstrained_scale=unconstrained,
        at_parameter_bound=bool(unconstrained <= low or unconstrained >= high),
        training_end_utc=cutoff.isoformat(),
        latest_training_interval_end_utc=(
            stamps[training][-1] + pd.Timedelta(seconds=dt_seconds)
        ).isoformat(),
        training_intervals=int(training.sum()),
        heldout_intervals=int(heldout.sum()),
        excluded_boundary_intervals=int((~training & ~heldout).sum()),
        training_mae_mw=float(np.abs(error[training]).mean()),
        heldout_mae_mw=float(np.abs(error[heldout]).mean()),
        heldout_bias_mw=float(error[heldout].mean()),
        heldout_rmse_mw=float(np.sqrt(np.mean(error[heldout] ** 2))),
        scope="one efficiency multiplier; independently supplied geometry and unit metering; no full-asset calibration claim",
    )
    return EfficiencyFit(fitted, predictions, audit)
