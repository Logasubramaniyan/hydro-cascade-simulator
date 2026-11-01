import json
import pytest

pytest.importorskip("powergama")

from hydrocascade.operations.study import run_study

pytestmark = pytest.mark.powergama


def test_offer_workbench_replays_into_real_powergama_grid(tmp_path):
    summary, _ = run_study(tmp_path / "study", steps=8, with_grid=True)
    report = json.loads((tmp_path / "study" / "decision_report.json").read_text())
    assert report["grid_checked"]
    assert summary.max_water_residual_m3.max() < 1e-5
    assert (tmp_path / "study" / "grid_check" / "intact" / "powergama.sqlite").exists()
