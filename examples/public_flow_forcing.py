"""Measured flow drives invented reservoir geometry: a data/physics demo, not calibration."""

from pathlib import Path
import numpy as np
import pandas as pd
from hydrocascade import Cascade, Plant, simulate


def main():
    root = Path(__file__).resolve().parents[1]
    sample = pd.read_csv(root / "data" / "observed" / "usgs_06090800_2023-01.csv")
    cascade = Cascade(
        (
            Plant(
                "illustrative",
                "Invented reservoir",
                40_000_000,
                20_000_000,
                max_flow_m3s=100,
                head_m=50,
                min_release_m3s=20,
            ),
        )
    )
    records, audits, _ = simulate(cascade, sample.flow_m3s.to_numpy()[:, None], dt_seconds=86400)
    assert audits.water_residual_m3.abs().max() < 1e-6
    assert np.isfinite(records.energy_mwh).all()
    print("Measured discharge applied to an invented reservoir; not a plant accuracy claim.")
    print("Daily mean discharge range (m3/s):", sample.flow_m3s.min(), sample.flow_m3s.max())
    print("Largest physical residual (m3):", audits.water_residual_m3.abs().max())


if __name__ == "__main__":
    main()
