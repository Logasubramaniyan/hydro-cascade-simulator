"""Historical evaluation against supplied metering and explicit commercial assumptions."""

from dataclasses import dataclass
import numpy as np
import pandas as pd

from ..model import finite
from ..validation import score_observations
from .calendar import validate_timestamps


@dataclass
class SettlementResult:
    intervals: pd.DataFrame
    summary: dict


def numeric(frame, columns, nonnegative=()):
    if not set(columns) <= set(frame):
        raise ValueError(f"Missing required columns: {sorted(set(columns) - set(frame))}")
    result = {}
    for column in columns:
        values = np.asarray(frame[column], float)
        if not np.isfinite(values).all():
            raise ValueError(f"{column} must contain finite values")
        if column in nonnegative and (values < 0).any():
            raise ValueError(f"{column} must be nonnegative")
        result[column] = values
    return result


def settle_dispatch(offers, meter, market, dt_seconds=900):
    """Requested activation is settled, residual metering uses supplied buy/sell imbalance prices.

    Accepted energy/reserve quantities are inputs. An optional explicit delivery-error penalty is
    charged on absolute residual MWh; it is not an inferred official TSO penalty or settlement rule.
    """
    finite(dt_seconds, "dt_seconds", 1e-9)
    if any("timestamp" not in f for f in (offers, meter, market)):
        raise ValueError("All evaluation frames require timestamp keys")
    times = validate_timestamps(offers.timestamp, dt_seconds=dt_seconds)
    for frame in (meter, market):
        validate_timestamps(frame.timestamp, expected=times, dt_seconds=dt_seconds)
    quantities = ("energy_mw", "reserve_up_mw", "reserve_down_mw")
    q = numeric(offers, quantities, quantities)
    actual = numeric(meter, ("actual_mw",), ("actual_mw",))["actual_mw"]
    columns = (
        "day_ahead_eur_mwh",
        "up_capacity_eur_mw_h",
        "down_capacity_eur_mw_h",
        "activation_up",
        "activation_down",
        "up_energy_eur_mwh",
        "down_energy_eur_mwh",
        "imbalance_sell_eur_mwh",
        "imbalance_buy_eur_mwh",
        "delivery_penalty_eur_mwh",
    )
    m = numeric(market, columns, ("activation_up", "activation_down", "delivery_penalty_eur_mwh"))
    if (m["activation_up"] + m["activation_down"] > 1 + 1e-10).any():
        raise ValueError("Combined activation fractions must not exceed one")
    if (q["reserve_down_mw"] > q["energy_mw"] + 1e-7).any():
        raise ValueError("Downward reserve exceeds the non-pumping energy baseline")
    hours = dt_seconds / 3600
    up, down = q["reserve_up_mw"] * m["activation_up"], q["reserve_down_mw"] * m["activation_down"]
    target = q["energy_mw"] + up - down
    residual = (actual - target) * hours
    frame = pd.DataFrame(
        dict(
            timestamp=times,
            target_mw=target,
            actual_mw=actual,
            imbalance_mwh=residual,
            day_ahead_eur=q["energy_mw"] * hours * m["day_ahead_eur_mwh"],
            capacity_eur=hours
            * (
                q["reserve_up_mw"] * m["up_capacity_eur_mw_h"]
                + q["reserve_down_mw"] * m["down_capacity_eur_mw_h"]
            ),
            activation_eur=hours * (up * m["up_energy_eur_mwh"] - down * m["down_energy_eur_mwh"]),
            imbalance_eur=np.where(
                residual >= 0, residual * m["imbalance_sell_eur_mwh"], residual * m["imbalance_buy_eur_mwh"]
            ),
            delivery_penalty_eur=np.abs(residual) * m["delivery_penalty_eur_mwh"],
        )
    )
    frame["net_cash_eur"] = (
        frame.day_ahead_eur
        + frame.capacity_eur
        + frame.activation_eur
        + frame.imbalance_eur
        - frame.delivery_penalty_eur
    )
    totals = {col: float(frame[col].sum()) for col in frame if col.endswith("_eur")}
    return SettlementResult(
        frame,
        dict(
            totals,
            intervals=len(frame),
            dt_seconds=dt_seconds,
            absolute_delivery_error_mwh=float(np.abs(residual).sum()),
            short_energy_mwh=float(np.maximum(-residual, 0).sum()),
            excess_energy_mwh=float(np.maximum(residual, 0).sum()),
            convention="accepted quantities; requested activation; metered residual buy/sell imbalance; supplied absolute delivery penalty",
            scope="cashflow evaluation under supplied rules; excludes unspecified charges and asset costs",
        ),
    )


def score_asset_observations(predicted, observed, mae_thresholds, minimum_coverage=0.95):
    """Apply caller-selected pilot thresholds to independent power/storage observations.

    Missing metric values stay missing and reduce coverage. Missing/extra rows remain in the union.
    """
    if not mae_thresholds or not set(mae_thresholds) <= {"storage_m3", "power_mw", "turbine_m3s"}:
        raise ValueError("Explicit supported observation metric thresholds are required")
    finite(minimum_coverage, "minimum_coverage", 0)
    if minimum_coverage > 1:
        raise ValueError("minimum_coverage must be <= 1")
    for value in mae_thresholds.values():
        finite(value, "MAE threshold", 0)
    for frame in (predicted, observed):
        if not {"timestamp", "plant_id"} <= set(frame) or frame.plant_id.isna().any():
            raise ValueError("Observation frames need non-null plant and timestamp keys")
        for _, group in frame.groupby("plant_id"):
            # Missing intervals affect coverage, so only enforce explicit offsets/ordering here.
            from .calendar import instant

            stamps = pd.DatetimeIndex([instant(t) for t in group.timestamp])
            if stamps.has_duplicates or not stamps.is_monotonic_increasing:
                raise ValueError("Observation keys must be unique and ordered per plant")
    scores = score_observations(predicted, observed, tuple(mae_thresholds))
    if scores.empty:
        raise ValueError("No observation records to score")
    failed = []
    for row in scores.itertuples():
        if row.coverage < minimum_coverage or row.matched == 0 or row.mae is None or not np.isfinite(row.mae):
            failed.append(f"{row.plant_id}/{row.metric}: insufficient coverage")
        elif row.mae > mae_thresholds[row.metric]:
            failed.append(f"{row.plant_id}/{row.metric}: MAE exceeds threshold")
    return dict(
        passes=not failed,
        scores=scores,
        failed_checks=failed,
        scope="threshold screen on supplied independent observations; synthetic observations do not establish calibration",
    )
