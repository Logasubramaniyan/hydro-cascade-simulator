from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hydrocascade.operations.forecast import make_forecast
from hydrocascade.operations.io import case_dict, read_case, run_plan, write_json
from hydrocascade.operations.state import advance_dispatch, DispatchCheckpoint
from hydrocascade.operations.study import build_case, run_study, main

ROOT = Path(__file__).resolve().parents[1]
PRICES = ROOT / "data" / "market" / "se4_2026-09-20_2026-10-28.csv"


def small_case():
    frame = pd.read_csv(PRICES)
    forecast = make_forecast(frame, "2026-10-26T10:00Z", "2026-10-27T00:00Z", steps=8)
    c, state, terms, limits, financial, _, targets, values = build_case(forecast, 8)
    scenario = replace(financial[0], probability=1.0)
    return c, state, [scenario], terms, limits, targets, values


def test_company_json_plan_roundtrip_and_timestamped_checkpoint_resume(tmp_path):
    c, state, scenarios, terms, limits, targets, values = small_case()
    config = tmp_path / "case.json"
    write_json(
        config, case_dict(c, scenarios, terms, limits, state, terminal_min=targets, terminal_value=values)
    )
    result = run_plan(config, tmp_path / "first")
    assert result.audit["deliverable_on_tested_paths"]
    assert result.audit["resolved_inputs_sha256"]
    checkpoint = advance_dispatch(
        c,
        state,
        scenarios[0].inflows_m3s,
        result.schedules[scenarios[0].name],
        4,
        start_utc="2026-10-27T00:00Z",
    )
    checkpoint.save(tmp_path / "checkpoint.json")
    loaded = DispatchCheckpoint.load(tmp_path / "checkpoint.json", c, 900, expected_as_of="2026-10-27T01:00Z")
    assert loaded.as_of_utc == "2026-10-27T01:00:00+00:00"
    assert loaded.last_release_m3s == pytest.approx(checkpoint.last_release_m3s)
    resumed = run_plan(config, tmp_path / "resumed", tmp_path / "checkpoint.json", "2026-10-27T01:00Z")
    resolved = json.loads((tmp_path / "resumed" / "inputs.json").read_text())
    assert resolved["state"]["queues_m3"] == loaded.state.queues_m3
    assert resolved["operating_limits"]["initial_release_m3s"] == pytest.approx(loaded.last_release_m3s)
    assert resumed.audit["execution_boundary"]["checkpoint_sha256"]
    with pytest.raises(ValueError, match="time mismatch"):
        DispatchCheckpoint.load(tmp_path / "checkpoint.json", c, 900, "2026-10-27T02:00Z")
    with pytest.raises(ValueError, match="execution boundary"):
        run_plan(config, tmp_path / "bad", tmp_path / "checkpoint.json")
    with pytest.raises(ValueError, match="empty"):
        run_plan(config, tmp_path / "first")


def test_complete_offline_study_checks_risk_stress_forecast_and_hashes(tmp_path):
    output = tmp_path / "study"
    summary, stress = run_study(output, PRICES, steps=8)
    assert len(summary) == 4 and len(stress) == 15
    assert summary.max_delivery_error_mw.max() < 1e-5
    assert summary.max_water_residual_m3.max() < 1e-5
    assert stress[stress.policy == "stress_constrained"].passes.all()
    report = json.loads((output / "decision_report.json").read_text())
    assert report["forecast_health"]["status"] == "hold"
    assert report["status"] == "research_only"
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["financial_scenarios"] == 18 and manifest["security_scenarios"] == 18
    assert set(manifest["code_sha256"]) == {
        p.relative_to(ROOT).as_posix() for p in (ROOT / "hydrocascade").rglob("*.py")
    }
    assert all("\\" not in name for name in manifest["artifact_sha256"])
    with pytest.raises(ValueError, match="empty"):
        run_study(output, PRICES, steps=8)


def test_case_schema_and_wrong_execution_controls_are_rejected(tmp_path):
    c, state, scenarios, terms, limits, targets, values = small_case()
    config = tmp_path / "case.json"
    write_json(config, {"schema_version": 1, "data_kind": "synthetic", "unknown_option": 1})
    with pytest.raises(ValueError, match="documented"):
        read_case(config)
    with pytest.raises(ValueError, match="8 to 96"):
        build_case(None, 1)
    with pytest.raises(ValueError, match="positive"):
        advance_dispatch(c, state, scenarios[0].inflows_m3s, pd.DataFrame(), 0)
    with pytest.raises(ValueError, match="unique"):
        advance_dispatch(c, state, scenarios[0].inflows_m3s, pd.DataFrame(), 1)
    write_json(
        config, case_dict(c, scenarios, terms, limits, state, terminal_min=targets, terminal_value=values)
    )
    with pytest.raises(ValueError, match="requires a checkpoint"):
        run_plan(config, tmp_path / "bad", as_of="2026-10-27T00:00Z")


def test_operations_cli_plan_dispatches_the_documented_workflow(tmp_path, capsys):
    c, state, scenarios, terms, limits, targets, values = small_case()
    config = tmp_path / "case.json"
    write_json(
        config, case_dict(c, scenarios, terms, limits, state, terminal_min=targets, terminal_value=values)
    )
    main(["plan", "--config", str(config), "--output", str(tmp_path / "result")])
    assert "expected_profit_eur" in capsys.readouterr().out
    assert (tmp_path / "result" / "offers.csv").exists()


def test_data_raw_and_csv_hashes_remain_verifiable():
    from hashlib import sha256
    from hydrocascade.operations.market_data import parse_prices

    provenance = json.loads(PRICES.with_suffix(".provenance.json").read_text())
    raw = PRICES.with_suffix(".raw.json").read_bytes()
    assert sha256(raw).hexdigest() == provenance["raw_sha256"]
    assert sha256(PRICES.read_bytes()).hexdigest() == provenance["csv_sha256"]
    parsed = parse_prices(json.loads(raw), provenance["start_utc"], provenance["end_exclusive_utc"])
    assert len(parsed) == 3744 and np.isfinite(parsed.price_eur_mwh).all()
