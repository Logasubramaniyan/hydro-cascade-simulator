"""Transparent weekly-lag price baseline and empirical residual paths, with an issue-time firewall."""

from dataclasses import dataclass
from hashlib import sha256
import numpy as np
import pandas as pd

from .market_data import utc


@dataclass
class PriceForecast:
    frame: pd.DataFrame
    scenarios: np.ndarray
    audit: dict


def price_series(frame):
    if not {"timestamp", "price_eur_mwh"} <= set(frame) or not len(frame):
        raise ValueError("Need timestamp and price_eur_mwh observations")
    # Reject naive source strings instead of silently assuming local market time means UTC.
    timestamps = pd.DatetimeIndex([utc(value) for value in frame.timestamp])
    prices = np.asarray(frame.price_eur_mwh, float)
    if timestamps.has_duplicates or not timestamps.is_monotonic_increasing or not np.isfinite(prices).all():
        raise ValueError("Price observations must be ordered, unique and finite")
    if (timestamps.asi8 % (900 * 10**9)).any() or (
        len(timestamps) > 1 and not (np.diff(timestamps.asi8) == 900 * 10**9).all()
    ):
        raise ValueError("Quarter-hour observations must be contiguous and aligned")
    return pd.Series(prices, index=timestamps)


def make_forecast(frame, issued_at, delivery_start, steps=96, history_days=21, alpha=0.2, max_age_hours=2):
    """No claims of conformal coverage: serially dependent daily residual paths are empirical.

    Only prices for intervals ending on/before issued_at are used, even if future auction
    prices happen to be present in the input file. The full source is validated for gaps.
    """
    issued_at, delivery_start = utc(issued_at), utc(delivery_start)
    if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1 or steps > 672:
        raise ValueError("Forecast steps must be an integer from 1 to 672")
    if (
        not isinstance(history_days, int)
        or history_days < 2
        or not 0 < alpha < 1
        or not np.isfinite(max_age_hours)
        or max_age_hours < 0
    ):
        raise ValueError("Invalid residual history, interval level or freshness limit")
    if issued_at >= delivery_start or delivery_start.value % (900 * 10**9):
        raise ValueError("Forecast issue must precede aligned delivery")
    series = price_series(frame)
    observed = series[series.index + pd.Timedelta(minutes=15) <= issued_at]
    if not len(observed) or issued_at - (observed.index[-1] + pd.Timedelta(minutes=15)) > pd.Timedelta(
        hours=max_age_hours
    ):
        raise ValueError("Stale or missing observations at forecast issue time")
    future = pd.date_range(delivery_start, periods=steps, freq="15min")
    lagged = future - pd.Timedelta(days=7)
    if not lagged.isin(observed.index).all():
        raise ValueError("Weekly-lag inputs are not all available before forecast issue")
    point = observed.reindex(lagged).to_numpy()
    residual = observed - observed.reindex(observed.index - pd.Timedelta(days=7)).set_axis(observed.index)
    residual = residual[(residual.index >= issued_at - pd.Timedelta(days=history_days))].dropna()
    daily = [group.to_numpy() for _, group in residual.groupby(residual.index.floor("D")) if len(group) == 96]
    if len(daily) < 2:
        raise ValueError("Need at least two complete pre-issue residual days")
    # Preserve within-day dependence rather than drawing independent quarter-hour noise.
    slots = (future.hour * 4 + future.minute // 15).to_numpy()
    paths = point + np.asarray(daily)[:, slots]
    low, high = np.quantile(paths, [alpha / 2, 1 - alpha / 2], axis=0)
    prefix = pd.DataFrame({"timestamp": observed.index.astype(str), "price_eur_mwh": observed.values}).to_csv(
        index=False, lineterminator="\n"
    )
    return PriceForecast(
        pd.DataFrame(
            {"timestamp": future, "point_eur_mwh": point, "lower_eur_mwh": low, "upper_eur_mwh": high}
        ),
        paths,
        dict(
            model="weekly UTC lag plus complete historical daily residual paths",
            issued_at_utc=issued_at.isoformat(),
            latest_observed_interval_end_utc=(observed.index[-1] + pd.Timedelta(minutes=15)).isoformat(),
            observed_prefix_sha256=sha256(prefix.encode()).hexdigest(),
            observed_intervals=len(observed),
            residual_days=len(daily),
            nominal_interval_coverage=1 - alpha,
            information="only intervals ending before issue; no future prices, no fitted site inflow model",
            coverage_claim="empirical intervals; no distribution-free or operational guarantee",
        ),
    )


def backtest_prices(frame, delivery_days, history_days=21):
    """Rolling-origin next-day forecasts issued at 10:00 UTC on the preceding day."""
    observed = price_series(frame)
    rows, predictions = [], []
    for day in delivery_days:
        start = utc(day)
        issued = start - pd.Timedelta(hours=14)
        forecast = make_forecast(frame, issued, start, history_days=history_days)
        actual = observed.reindex(pd.DatetimeIndex(forecast.frame.timestamp))
        if actual.isna().any():
            raise ValueError("Missing held-out observations")
        predicted = forecast.frame.assign(actual_eur_mwh=actual.to_numpy(), issued_at_utc=issued.isoformat())
        errors = np.abs(predicted.point_eur_mwh - predicted.actual_eur_mwh)
        coverage = (
            (predicted.actual_eur_mwh >= predicted.lower_eur_mwh)
            & (predicted.actual_eur_mwh <= predicted.upper_eur_mwh)
        ).mean()
        # Empirical-ensemble CRPS: E|X-y| - 0.5 E|X-X'|, no probabilistic calibration claim.
        paths = forecast.scenarios
        crps = np.abs(paths - actual.to_numpy()).mean(axis=0) - 0.5 * np.abs(
            paths[:, None] - paths[None, :]
        ).mean(axis=(0, 1))
        rows.append(
            dict(
                delivery_start_utc=start.isoformat(),
                issued_at_utc=issued.isoformat(),
                mae_eur_mwh=float(errors.mean()),
                empirical_coverage=float(coverage),
                nominal_coverage=0.8,
                mean_interval_width_eur_mwh=float((predicted.upper_eur_mwh - predicted.lower_eur_mwh).mean()),
                empirical_crps_eur_mwh=float(crps.mean()),
                residual_days=forecast.audit["residual_days"],
            )
        )
        predictions.append(predicted)
    if not rows:
        raise ValueError("Need at least one held-out delivery day")
    return pd.DataFrame(rows), pd.concat(predictions, ignore_index=True)


def forecast_health(scores, issued_at, recent_days=3, minimum_coverage=0.7):
    """A transparent review trigger using only completed held-out days before this issue."""
    if not isinstance(recent_days, int) or recent_days < 1 or not 0 <= minimum_coverage <= 1:
        raise ValueError("Invalid forecast review thresholds")
    issued = utc(issued_at)
    starts = pd.to_datetime(scores.delivery_start_utc, utc=True)
    eligible = (
        scores[starts + pd.Timedelta(days=1) <= issued].sort_values("delivery_start_utc").tail(recent_days)
    )
    if len(eligible) < recent_days:
        return dict(status="hold", reason="insufficient completed backtest days", observed_days=len(eligible))
    coverage = float(eligible.empirical_coverage.mean())
    return dict(
        status="hold" if coverage < minimum_coverage else "review",
        reason="recent empirical interval undercoverage"
        if coverage < minimum_coverage
        else "heuristic forecast threshold met; review required",
        observed_days=len(eligible),
        observed_coverage=coverage,
        minimum_coverage=minimum_coverage,
        mean_mae_eur_mwh=float(eligible.mae_eur_mwh.mean()),
        latest_scored_day=str(eligible.delivery_start_utc.iloc[-1]),
        interpretation="review trigger, not a statistical guarantee or permission to trade",
    )
