import json
from types import SimpleNamespace
import numpy as np
import pandas as pd
import pytest

from hydrocascade.operations.market_data import parse_prices, fetch_prices
from hydrocascade.operations.forecast import make_forecast, backtest_prices, forecast_health


def payload(start="2026-10-24T22:00Z", end="2026-10-25T23:00Z"):
    index = pd.date_range(start, end, freq="15min", inclusive="left")
    return dict(
        license_info="CC BY 4.0 from Bundesnetzagentur | SMARD.de",
        unix_seconds=(index.asi8 // 10**9).tolist(),
        price=[-5.0] * len(index),
        unit="EUR / MWh",
        deprecated=False,
    )


def test_dst_fall_day_has_100_distinct_quarter_hours_and_negative_prices():
    frame = parse_prices(payload(), "2026-10-24T22:00Z", "2026-10-25T23:00Z")
    assert len(frame) == 100
    assert not frame.timestamp.duplicated().any()
    assert (frame.price_eur_mwh == -5).all()
    assert frame.timestamp.dt.tz_convert("Europe/Berlin").dt.date.nunique() == 1


@pytest.mark.parametrize("problem", ["licence", "units", "hourly", "missing", "duplicate", "nan", "naive"])
def test_bad_market_data_not_imputed_or_redistributed(problem):
    p, start = payload(), "2026-10-24T22:00Z"
    if problem == "licence":
        p["license_info"] = "Private and internal use only"
    elif problem == "units":
        p["unit"] = "EUR/kWh"
    elif problem == "hourly":
        p["unix_seconds"], p["price"] = p["unix_seconds"][::4], p["price"][::4]
    elif problem == "missing":
        p["price"], p["unix_seconds"] = p["price"][1:], p["unix_seconds"][1:]
    elif problem == "duplicate":
        p["unix_seconds"][1] = p["unix_seconds"][0]
    elif problem == "nan":
        p["price"][0] = None
    else:
        start = "2026-10-24T22:00"
    with pytest.raises(ValueError):
        parse_prices(p, start, "2026-10-25T23:00Z")


def test_fetch_saves_exact_raw_response_and_licence_provenance(tmp_path):
    p = payload()
    raw = json.dumps(p).encode()
    response = SimpleNamespace(content=raw, json=lambda: p, raise_for_status=lambda: None)
    client = SimpleNamespace(get=lambda *a, **k: response)
    output = tmp_path / "prices.csv"
    frame, provenance = fetch_prices("SE4", "2026-10-24T22:00Z", "2026-10-25T23:00Z", output, client)
    assert len(frame) == 100
    assert output.with_suffix(".raw.json").read_bytes() == raw
    assert provenance["zone"] == "SE4"
    assert "CC BY 4.0" in json.loads(output.with_suffix(".provenance.json").read_text())["licence"]
    with pytest.raises(ValueError, match="new"):
        fetch_prices("SE4", "2026-10-24T22:00Z", "2026-10-25T23:00Z", output, client)


def history():
    index = pd.date_range("2026-09-01T00:00Z", periods=60 * 96, freq="15min")
    t = np.arange(len(index))
    return pd.DataFrame({"timestamp": index, "price_eur_mwh": 40 + 10 * np.cos(t * np.pi / 48) + t / 1000})


def test_future_price_perturbation_cannot_leak_into_forecast():
    frame = history()
    issue, delivery = "2026-10-19T10:00Z", "2026-10-20T00:00Z"
    first = make_forecast(frame, issue, delivery)
    changed = frame.copy()
    changed.loc[changed.timestamp >= pd.Timestamp(issue), "price_eur_mwh"] = 1e8
    second = make_forecast(changed, issue, delivery)
    pd.testing.assert_frame_equal(first.frame, second.frame)
    assert first.scenarios == pytest.approx(second.scenarios)
    assert first.audit == second.audit
    assert pd.Timestamp(first.audit["latest_observed_interval_end_utc"]) <= pd.Timestamp(issue)


def test_rolling_origin_scores_actual_held_out_observations():
    scores, predictions = backtest_prices(history(), ["2026-10-20T00:00Z", "2026-10-21T00:00Z"])
    assert len(scores) == 2 and len(predictions) == 192
    assert scores.mae_eur_mwh.iloc[0] == pytest.approx(0.672)
    assert (scores.empirical_crps_eur_mwh >= -1e-10).all()


def test_forecast_health_cannot_use_delivery_results_from_after_issue():
    scores, _ = backtest_prices(history(), pd.date_range("2026-10-18T00:00Z", periods=5, freq="D"))
    report = forecast_health(scores, "2026-10-21T10:00Z")
    altered = scores.copy()
    altered.loc[
        pd.to_datetime(altered.delivery_start_utc, utc=True) >= pd.Timestamp("2026-10-21T00:00Z"),
        "empirical_coverage",
    ] = 0
    assert report == forecast_health(altered, "2026-10-21T10:00Z")
    altered.loc[:, "empirical_coverage"] = 0
    assert forecast_health(altered, "2026-10-21T10:00Z")["status"] == "hold"
    assert forecast_health(scores, "2026-10-18T10:00Z")["status"] == "hold"


@pytest.mark.parametrize("problem", ["stale", "gap", "short", "issue", "steps"])
def test_forecast_rejects_unusable_information(problem):
    frame, kwargs = history(), {}
    if problem == "stale":
        frame = frame[frame.timestamp < pd.Timestamp("2026-10-18T00:00Z")]
    elif problem == "gap":
        frame = frame.drop(index=4)
    elif problem == "short":
        frame = frame[frame.timestamp >= pd.Timestamp("2026-10-18T00:00Z")]
    elif problem == "issue":
        kwargs["delivery_start"] = "2026-10-19T00:00Z"
    else:
        kwargs["steps"] = 0
    with pytest.raises(ValueError):
        make_forecast(frame, "2026-10-19T10:00Z", kwargs.pop("delivery_start", "2026-10-20T00:00Z"), **kwargs)
