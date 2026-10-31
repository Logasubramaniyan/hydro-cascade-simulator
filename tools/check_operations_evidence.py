"""Verify public data, study hashes, timing, physical checks and documented numerical claims."""

from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def verify(root, directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["steps"] == 96 and manifest["dt_seconds"] == 900
    assert manifest["financial_scenarios"] == 18 and manifest["security_scenarios"] == 18
    assert set(manifest["code_sha256"]) == {
        p.relative_to(root).as_posix() for p in (root / "hydrocascade").rglob("*.py")
    }
    for path, expected in manifest["code_sha256"].items():
        assert "\\" not in path and digest(root / path) == expected, f"Changed source: {path}"
    for path, expected in manifest["artifact_sha256"].items():
        assert "\\" not in path and digest(directory / path) == expected, f"Changed artifact: {path}"
    source = root / "data" / "market" / "se4_2026-09-20_2026-10-28.csv"
    provenance = json.loads(source.with_suffix(".provenance.json").read_text())
    assert digest(source) == provenance["csv_sha256"] == manifest["source_prices_sha256"]
    assert digest(source.with_suffix(".raw.json")) == provenance["raw_sha256"]
    assert "CC BY 4.0" in provenance["licence"]
    prices = pd.read_csv(source)
    assert len(prices) == 3744 and (prices.price_eur_mwh < 0).sum() == 10
    timestamps = pd.to_datetime(prices.timestamp, utc=True)
    assert not timestamps.duplicated().any() and timestamps.diff().dropna().dt.total_seconds().eq(900).all()
    forecast = json.loads((directory / "forecast_audit.json").read_text())
    assert pd.Timestamp(forecast["latest_observed_interval_end_utc"]) <= pd.Timestamp(
        forecast["issued_at_utc"]
    )
    scores = pd.read_csv(directory / "forecast_backtest.csv")
    report = json.loads((directory / "decision_report.json").read_text())
    assert report["status"] == "research_only" and report["forecast_health"]["status"] == "hold"
    eligible = scores[
        pd.to_datetime(scores.delivery_start_utc, utc=True) + pd.Timedelta(days=1)
        <= pd.Timestamp(forecast["issued_at_utc"])
    ].tail(3)
    assert np.isclose(eligible.empirical_coverage.mean(), report["forecast_health"]["observed_coverage"])
    summary = pd.read_csv(directory / "summary.csv").set_index("policy")
    assert set(summary.index) == {"energy_only", "mean_profit", "tail_aware", "stress_constrained"}
    assert summary.max_water_residual_m3.max() < 1e-5 and summary.max_delivery_error_mw.max() < 1e-5
    assert summary.loc["mean_profit", "expected_profit_eur"] + 1e-4 >= summary.expected_profit_eur.max()
    mean = summary.loc["mean_profit"]
    assert (
        summary.loc["tail_aware", "utility_eur"] + 1e-4
        >= 0.45 * mean.expected_profit_eur + 0.55 * mean.lower_tail_profit_eur
    )
    for name in summary.index:
        outcomes = pd.read_csv(directory / name / "outcomes.csv")
        assert np.isclose(outcomes.probability.sum(), 1, atol=1e-10, rtol=0)
        assert outcomes.query("role == 'security'").probability.eq(0).all()
        for column in outcomes:
            if column.startswith("max_") and ("residual" in column or "violation" in column):
                assert outcomes[column].max() < 1e-5, (name, column)
        value = (
            outcomes.day_ahead_eur
            + outcomes.capacity_eur
            + outcomes.activation_eur
            + outcomes.terminal_water_eur
            - outcomes.release_variation_cost_eur
        )
        np.testing.assert_allclose(value, outcomes.terminal_adjusted_profit_eur, atol=1e-5)
        assert np.isclose((outcomes.probability * value).sum(), summary.loc[name, "expected_profit_eur"])
    stress = pd.read_csv(directory / "stress_assessment.csv")
    assert len(stress) == 15 and stress[stress.policy == "stress_constrained"].passes.all()
    assert stress[stress.policy == "headroom_only"].total_delivery_error_mwh.max() > 1
    grid = pd.read_csv(directory / "grid_assessment.csv").set_index("network_case")
    assert grid.loc["intact", "grid_can_absorb_schedule"]
    assert not grid.loc["connection_derated", "grid_can_absorb_schedule"]
    assert grid.max_water_residual_m3.max() < 1e-5 and grid.max_nodal_balance_residual_mw.max() < 1e-5
    settlement = pd.read_csv(directory / "heldout_day_ahead_cash.csv")
    np.testing.assert_allclose(
        settlement.heldout_day_ahead_cash_eur - settlement.forecast_expected_day_ahead_cash_eur,
        settlement.cash_difference_eur,
        atol=1e-6,
    )
    document = (root / "docs" / "OPERATIONS_CASE_STUDY.md").read_text(encoding="utf-8")
    for row in summary.itertuples():
        assert f"{row.expected_profit_eur:,.2f}" in document
        assert f"{row.lower_tail_profit_eur:,.2f}" in document
    return summary


def main():
    root = Path(__file__).resolve().parents[1]
    verify(root, root / "docs" / "operations-evidence")
    print(
        "Operations evidence: sources, market licence, timing, physical/economic audits and headline claims verified."
    )


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, ValueError, OSError, KeyError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
