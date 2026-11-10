from dataclasses import replace
import numpy as np
import pandas as pd
import pytest

from hydrocascade.pilot.calendar import ProductRules, check_product, delivery_day, validate_timestamps
from hydrocascade.pilot.evaluation import settle_dispatch, score_asset_observations


@pytest.mark.parametrize("day,count", [("2026-03-29", 92), ("2026-10-25", 100), ("2026-10-27", 96)])
def test_local_market_day_handles_clock_changes(day, count):
    frame = delivery_day(day)
    assert len(frame) == count
    assert not frame.timestamp.duplicated().any()
    assert frame.local_start.str.contains(r"[+-]\d\d:\d\d$", regex=True).all()
    assert validate_timestamps(frame.timestamp).to_series().diff().dropna().dt.total_seconds().eq(900).all()


@pytest.mark.parametrize(
    "values",
    [["2026-10-25T02:00:00"], ["2026-10-25T00:00Z"] * 2, ["2026-10-25T00:00Z", "2026-10-25T00:30Z"], [None]],
)
def test_bad_timestamp_keys_fail_closed(values):
    with pytest.raises(ValueError):
        validate_timestamps(values)


def offers():
    return pd.DataFrame(
        dict(
            timestamp=pd.date_range("2026-10-27T00:00Z", periods=4, freq="15min"),
            energy_mw=[10.0] * 4,
            reserve_up_mw=[2.0] * 4,
            reserve_down_mw=[1.0] * 4,
        )
    )


def test_product_gate_lots_blocks_and_no_silent_rounding():
    rules = ProductRules("illustrative", minimum_mw=1, increment_mw=1, block_steps=4)
    frame = offers()
    assert check_product(frame, rules, "2026-10-26T10:00Z", "2026-10-26T11:00Z")["passes"]
    frame.loc[1, "reserve_up_mw"] = 0.7
    result = check_product(frame, rules, "2026-10-26T11:00Z", "2026-10-26T11:00Z")
    assert not result["passes"]
    assert {"gate_closed", "minimum_quantity", "quantity_increment", "block_quantity"} <= set(
        result["violations"]
    )
    assert frame.loc[1, "reserve_up_mw"] == 0.7
    with pytest.raises(ValueError):
        check_product(frame, replace(rules, block_steps=3), "2026-10-26T10:00Z", "2026-10-26T11:00Z")


def test_product_clock_and_block_alignment_are_explicit():
    frame = offers()
    frame["timestamp"] += pd.Timedelta(minutes=1)
    result = check_product(frame, ProductRules("example"), "2026-10-26T10:00Z", "2026-10-26T11:00Z")
    assert "interval_alignment" in result["violations"]
    frame = offers()
    frame["timestamp"] += pd.Timedelta(minutes=15)
    result = check_product(
        frame, ProductRules("example", block_steps=4), "2026-10-26T10:00Z", "2026-10-26T11:00Z"
    )
    assert "block_alignment" in result["violations"]


def settlement_inputs():
    plan = offers().iloc[:2].copy()
    meter = pd.DataFrame(dict(timestamp=plan.timestamp, actual_mw=[11.0, 8.0]))
    prices = pd.DataFrame(
        dict(
            timestamp=plan.timestamp,
            day_ahead_eur_mwh=[-10.0, 100.0],
            up_capacity_eur_mw_h=[5.0, 5.0],
            down_capacity_eur_mw_h=[2.0, 2.0],
            activation_up=[1.0, 0.0],
            activation_down=[0.0, 1.0],
            up_energy_eur_mwh=[100.0, 100.0],
            down_energy_eur_mwh=[-20.0, -20.0],
            imbalance_sell_eur_mwh=[30.0, 30.0],
            imbalance_buy_eur_mwh=[80.0, -40.0],
            delivery_penalty_eur_mwh=[10.0, 10.0],
        )
    )
    return plan, meter, prices


def test_signed_activation_imbalance_penalty_and_quarter_hour_cashflows():
    plan, meter, prices = settlement_inputs()
    result = settle_dispatch(plan, meter, prices)
    # target 12/9 MW; actual 11/8 -> -0.25 MWh residual each.
    assert result.intervals.target_mw.tolist() == [12, 9]
    assert result.summary["day_ahead_eur"] == pytest.approx(225)
    assert result.summary["capacity_eur"] == pytest.approx(6)
    assert result.summary["activation_eur"] == pytest.approx(55)
    assert result.summary["imbalance_eur"] == pytest.approx(-10)
    assert result.summary["delivery_penalty_eur"] == pytest.approx(5)
    assert result.summary["net_cash_eur"] == pytest.approx(271)
    assert result.summary["absolute_delivery_error_mwh"] == pytest.approx(0.5)


def test_exact_time_join_rejects_missing_extra_duplicate_and_nonfinite_data():
    plan, meter, prices = settlement_inputs()
    with pytest.raises(ValueError, match="match"):
        settle_dispatch(plan, meter.iloc[:1], prices)
    with pytest.raises(ValueError):
        settle_dispatch(plan, pd.concat([meter, meter.iloc[:1]]), prices)
    prices.loc[0, "activation_up"] = np.nan
    with pytest.raises(ValueError, match="finite"):
        settle_dispatch(plan, meter, prices)


def test_bad_activation_and_negative_generation_rejected():
    plan, meter, prices = settlement_inputs()
    prices.loc[0, "activation_down"] = 0.1
    with pytest.raises(ValueError, match="activation"):
        settle_dispatch(plan, meter, prices)
    prices.loc[0, "activation_down"] = 0
    meter.loc[0, "actual_mw"] = -1
    with pytest.raises(ValueError):
        settle_dispatch(plan, meter, prices)


def test_observation_score_keeps_missingness_and_explicit_thresholds():
    times = offers().timestamp
    predicted = pd.DataFrame(
        dict(timestamp=times, plant_id="p", storage_m3=[10, 20, 30, 40], power_mw=[1, 2, 3, 4])
    )
    observed = predicted.copy()
    observed.loc[0, "power_mw"] = 2
    observed.loc[1, "storage_m3"] = np.nan
    result = score_asset_observations(
        predicted, observed, {"power_mw": 0.2, "storage_m3": 1}, minimum_coverage=0.9
    )
    assert not result["passes"]
    scores = result["scores"].set_index("metric")
    assert scores.loc["power_mw", "mae"] == pytest.approx(0.25)
    assert scores.loc["storage_m3", "coverage"] == pytest.approx(0.75)
    assert result["failed_checks"]
    with pytest.raises(ValueError, match="threshold"):
        score_asset_observations(predicted, observed, {})
