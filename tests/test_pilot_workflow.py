from dataclasses import asdict
from hashlib import sha256
import json

import numpy as np
import pandas as pd
import pytest

from hydrocascade import Cascade, Plant
from hydrocascade.operations.io import write_json
from hydrocascade.pilot.assets import ReservoirCurve, Unit
from hydrocascade.pilot.workflow import main, run_case, run_evaluation, verify_run


def case():
    c = Cascade((Plant("p", "Synthetic", 14400, 7200, 2, 100),))
    return dict(
        schema_version=1,
        case_type="asset_dispatch",
        data_kind="synthetic analytical fixture",
        start_utc="2026-10-27T00:00:00Z",
        issued_at_utc="2026-10-26T10:00Z",
        dt_seconds=900,
        cascade=c.to_dict(),
        curves=[asdict(ReservoirCurve("p", (0, 14400), (100, 100), 0))],
        units=[asdict(Unit("u", "p", (1, 2), (1, 1)))],
        inflows_m3s=np.zeros((4, 1)),
        prices_eur_mwh=[10, 100, 100, -10],
        terminal_min_m3={"p": 0},
    )


def test_company_plan_cli_outputs_complete_handoff_and_manifest(tmp_path, capsys):
    config = tmp_path / "case.json"
    write_json(config, case())
    output = tmp_path / "plan"
    main(["plan", "--config", str(config), "--output", str(output)])
    assert "research_only" in capsys.readouterr().out
    assert verify_run(output)["verified"]
    frame = pd.read_csv(output / "plant_dispatch.csv")
    assert len(frame) == 4
    assert pd.Timestamp(frame.timestamp.iloc[0]) == pd.Timestamp("2026-10-27T00:00Z")
    state = json.loads((output / "handoff.json").read_text())
    assert state["as_of_utc"] == "2026-10-27T01:00:00+00:00"
    assert state["unit_state"]["u"]["on"] is False
    assert "queues_m3" in state["state"] and "last_release_m3s" in state
    assert state["state_basis"] == "projected_schedule"
    report = json.loads((output / "decision_report.json").read_text())
    assert report["status"] == "research_only"
    assert report["hydraulic_review"] == "pass"
    assert (
        sha256(config.read_bytes()).hexdigest()
        == json.loads((output / "manifest.json").read_text())["input_sha256"]
    )
    with pytest.raises(ValueError, match="empty"):
        run_case(config, output)
    (output / "unit_dispatch.csv").write_text("tampered")
    with pytest.raises(ValueError, match="hash"):
        verify_run(output)


@pytest.mark.parametrize(
    "field,value",
    [
        ("typo", 1),
        ("start_utc", "2026-10-27"),
        ("issued_at_utc", "2026-10-28T00:00Z"),
        ("dt_seconds", 1000),
        ("schema_version", 2),
    ],
)
def test_strict_schema_and_absolute_time_prevent_wrong_window_plan(tmp_path, field, value):
    data = case()
    data[field] = value
    config = tmp_path / "case.json"
    write_json(config, data)
    with pytest.raises(ValueError):
        run_case(config, tmp_path / "bad")


def test_handoff_resume_requires_same_cascade_and_exact_next_interval(tmp_path):
    config = tmp_path / "case.json"
    write_json(config, case())
    run_case(config, tmp_path / "first")
    data = case()
    data["handoff_path"] = "first/handoff.json"
    write_json(config, data)
    with pytest.raises(ValueError, match="boundary"):
        run_case(config, tmp_path / "bad")
    data["start_utc"] = "2026-10-27T01:00Z"
    write_json(config, data)
    result = run_case(config, tmp_path / "second")
    assert result.audit["max_water_residual_m3"] < 1e-5
    data["cascade"]["plants"][0]["name"] = "different asset"
    write_json(config, data)
    with pytest.raises(ValueError, match="cascade"):
        run_case(config, tmp_path / "bad_hash")


def test_evaluation_cli_uses_supplied_metering_and_records_input_hashes(tmp_path, capsys):
    config = tmp_path / "case.json"
    write_json(config, case())
    run_case(config, tmp_path / "plan")
    offers = pd.read_csv(tmp_path / "plan" / "energy_schedule.csv")
    offers["reserve_up_mw"] = offers["reserve_down_mw"] = 0
    offers.to_csv(tmp_path / "offers.csv", index=False)
    pd.DataFrame(dict(timestamp=offers.timestamp, actual_mw=offers.energy_mw)).to_csv(
        tmp_path / "meter.csv", index=False
    )
    market = pd.DataFrame({"timestamp": offers.timestamp})
    columns = [
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
    ]
    for col in columns:
        market[col] = 0 if col.startswith("activation") else 10
    market.to_csv(tmp_path / "market.csv", index=False)
    evaluation = dict(
        schema_version=1,
        case_type="historical_evaluation",
        data_kind="synthetic meters",
        offers_csv="offers.csv",
        meter_csv="meter.csv",
        market_csv="market.csv",
        dt_seconds=900,
    )
    write_json(tmp_path / "evaluation.json", evaluation)
    main(
        ["evaluate", "--config", str(tmp_path / "evaluation.json"), "--output", str(tmp_path / "evaluation")]
    )
    assert "net_cash_eur" in capsys.readouterr().out
    assert verify_run(tmp_path / "evaluation")["verified"]
    report = json.loads((tmp_path / "evaluation" / "decision_report.json").read_text())
    assert report["data_kind"] == "synthetic meters"
    (tmp_path / "market.csv").write_text("bad")
    with pytest.raises(ValueError):
        run_evaluation(tmp_path / "evaluation.json", tmp_path / "bad_evaluation")


def test_empty_evaluation_and_undeclared_fields_fail(tmp_path):
    config = tmp_path / "evaluation.json"
    write_json(config, dict(schema_version=1, case_type="historical_evaluation", data_kind="x", unknown=True))
    with pytest.raises(ValueError):
        run_evaluation(config, tmp_path / "bad")


def test_nested_river_link_typo_cannot_silently_disconnect_the_cascade(tmp_path):
    data = case()
    data["cascade"]["links_typo"] = [{"source": "p", "target": "missing"}]
    config = tmp_path / "case.json"
    write_json(config, data)
    with pytest.raises(ValueError, match="cascade"):
        run_case(config, tmp_path / "bad")


def test_calibration_cli_keeps_training_and_holdout_separate(tmp_path, capsys):
    unit = Unit("u", "p", (1, 2), (0.8, 0.9))
    curve = ReservoirCurve("p", (0, 10000), (10, 30), 0)
    measured = pd.DataFrame(
        dict(
            timestamp=pd.date_range("2026-01-01T00:00Z", periods=12, freq="15min"),
            start_storage_m3=5000,
            end_storage_m3=5000,
            flow_m3s=1,
        )
    )
    measured["power_mw"] = 0.8 * curve.power(unit, 1, 5000, 5000)
    measured.to_csv(tmp_path / "measured.csv", index=False)
    data = dict(
        schema_version=1,
        case_type="unit_calibration",
        data_kind="synthetic analytical fixture",
        unit=asdict(unit),
        curve=asdict(curve),
        observations_csv="measured.csv",
        training_end_utc="2026-01-01T02:00Z",
    )
    write_json(tmp_path / "calibration.json", data)
    main(["calibrate", "--config", str(tmp_path / "calibration.json"), "--output", str(tmp_path / "fit")])
    assert "heldout_mae_mw" in capsys.readouterr().out
    assert verify_run(tmp_path / "fit")["verified"]
    fitted = json.loads((tmp_path / "fit" / "fitted_unit.json").read_text())
    assert fitted["efficiency"] == pytest.approx([0.64, 0.72])


def test_committed_asset_benchmark_has_verified_claims():
    import runpy
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    verifier = runpy.run_path(str(root / "tools/check_pilot_evidence.py"))
    study = verifier["verify"](root)
    assert study["steps"] == 32 and study["hydraulic_review"] == "pass"
