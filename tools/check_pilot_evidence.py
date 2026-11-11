"""Verify the asset benchmark's source/artifact hashes, data labels and physical/financial claims."""

from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hydrocascade.pilot.workflow import verify_run  # noqa: E402


def verify(root=ROOT):
    root = Path(root)
    directory = root / "docs/pilot-evidence"
    study = json.loads((directory / "study.json").read_text(encoding="utf-8"))
    for name in ("plan", "evaluation", "calibration"):
        verify_run(directory / name, root)
        report = json.loads((directory / name / "decision_report.json").read_text())
        assert report["status"] == "research_only" and "synthetic" in report["data_kind"]
    assert (
        study["builder_sha256"] == sha256((root / "tools/build_pilot_evidence.py").read_bytes()).hexdigest()
    )
    prices = root / "data/market/se4_2026-09-20_2026-10-28.csv"
    assert study["price_source_sha256"] == sha256(prices.read_bytes()).hexdigest()
    case = json.loads((directory / "case.json").read_text())
    assert case == json.loads((directory / "plan/inputs.json").read_text())
    assert case == json.loads((root / "examples/asset_case.json").read_text())
    frame = pd.read_csv(prices)
    selected = frame[pd.to_datetime(frame.timestamp, utc=True) >= pd.Timestamp(case["start_utc"])].head(32)
    np.testing.assert_array_equal(selected.price_eur_mwh.to_numpy(), case["prices_eur_mwh"])
    assert study["steps"] == 32 and study["dt_seconds"] == 900
    audit = json.loads((directory / "plan/audit.json").read_text())
    assert audit["hydraulic_review_status"] == "pass" and audit["max_unit_violation"] < 1e-5
    assert audit["max_water_residual_m3"] < 1e-5 and audit["actual_export_violation_mw"] < 1e-5
    assert audit["max_power_approximation_error_mw"] < case["hydraulic_tolerance_mw"]
    assert np.isclose(study["modeled_revenue_eur"], audit["modeled_revenue_eur"])
    assert np.isclose(study["modeled_objective_eur"], audit["full_objective_eur"])
    units = pd.read_csv(directory / "plan/unit_dispatch.csv")
    assert not units[(units.unit_id == "upper_small") & units.step.between(12, 19)].on.any()
    handoff = json.loads((directory / "plan/handoff.json").read_text())
    assert handoff["state_basis"] == "projected_schedule"
    product = json.loads((directory / "plan/decision_report.json").read_text())["product_screen"]
    assert not product["passes"] and "quantity_increment" in product["violations"]
    evaluation = json.loads((directory / "evaluation/decision_report.json").read_text())
    assert not evaluation["observation_screen"]["passes"]
    settlement = pd.read_csv(directory / "evaluation/settlement.csv")
    reconstructed = (
        settlement.day_ahead_eur
        + settlement.capacity_eur
        + settlement.activation_eur
        + settlement.imbalance_eur
        - settlement.delivery_penalty_eur
    )
    np.testing.assert_allclose(reconstructed, settlement.net_cash_eur, atol=1e-8)
    assert np.isclose(reconstructed.sum(), study["net_evaluation_cash_eur"])
    fit = json.loads((directory / "calibration/audit.json").read_text())
    assert (
        np.isclose(fit["efficiency_scale"], 0.92)
        and fit["training_intervals"] == 16
        and fit["heldout_intervals"] == 8
    )
    assert fit["heldout_mae_mw"] > 0.05 and pd.Timestamp(
        fit["latest_training_interval_end_utc"]
    ) <= pd.Timestamp(fit["training_end_utc"])
    document = (root / "docs/ASSET_CASE_STUDY.md").read_text(encoding="utf-8")
    for key in ("modeled_revenue_eur", "modeled_objective_eur", "net_evaluation_cash_eur"):
        assert f"{study[key]:,.2f}" in document
    assert f"{study['heldout_calibration_mae_mw']:.6f}" in document
    return study


if __name__ == "__main__":
    try:
        verify()
        print(
            "Pilot evidence verified: source/data hashes, hydraulic/unit audits, cashflows and visible acceptance failures."
        )
    except (AssertionError, ValueError, OSError, KeyError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
