"""Verify the source/input/output hash chain and the study's economic/physical invariants."""

from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def verify(directory, root):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for path, expected in manifest["code_sha256"].items():
        assert "\\" not in path, "Manifest paths must be portable"
        assert digest(root / path) == expected, f"Changed study source: {path}"
    for path, expected in manifest["artifact_sha256"].items():
        assert digest(directory / path) == expected, f"Changed study artifact: {path}"
    summary = pd.read_csv(directory / "summary.csv")
    sensitivity = pd.read_csv(directory / "corridor_sensitivity.csv")
    assert len(summary) == 18
    assert set(summary.policy) == {"inflow_following", "price_based", "joint"}
    assert summary.mapped_native_storage_count.eq(0).all()
    assert summary.max_water_residual_m3.max() < 1e-5
    assert summary.max_nodal_balance_residual_mw.max() < 1e-5
    assert summary.max_dc_flow_residual_mw.max() < 1e-5
    assert summary.max_capacity_violation_mw.max() < 1e-5
    for case, frame in summary.groupby("case"):
        joint = frame[frame.policy == "joint"].terminal_adjusted_cost_eur.iloc[0]
        assert joint <= frame.terminal_adjusted_cost_eur.min() + 1e-4, case
    assert np.diff(sensitivity.terminal_adjusted_cost_eur).max(initial=0) < 1e-4
    document = (root / "docs" / "GRID_CASE_STUDY.md").read_text(encoding="utf-8")
    for row in summary.query("policy == 'joint'").itertuples():
        assert f"{row.terminal_adjusted_cost_eur:,.2f}" in document
    return summary


def main():
    root = Path(__file__).resolve().parents[1]
    verify(root / "docs" / "grid-evidence", root)
    print("Grid-study source, artifact hashes, headline values and physical/economic invariants verified.")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, KeyError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
