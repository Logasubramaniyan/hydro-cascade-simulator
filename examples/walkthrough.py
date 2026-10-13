"""Run after `pip install -e .`: python examples/walkthrough.py."""

from pathlib import Path
import json
import numpy as np
from hydrocascade import Scenario, optimize, stochastic_schedule
from hydrocascade.data import load_cascade, load_timeseries


def main():
    root = Path(__file__).resolve().parent
    cascade = load_cascade(root / "cascade.json")
    frame = load_timeseries(root / "hourly.csv", cascade, 3600)
    inflows = frame[[f"inflow_{plant.id}_m3s" for plant in cascade.plants]].to_numpy()
    prices = frame.price_eur_mwh.to_numpy()
    perfect = optimize(cascade, inflows, prices)
    print("Known-future benchmark revenue:", round(perfect.revenue_eur, 2))
    # Only the current interval is known; future regimes branch after the shared root.
    dry = inflows.copy()
    dry[1:] *= 0.7
    paths = [
        Scenario(
            "normal", 0.5, inflows, prices, tuple(["root"] + [f"normal-{i}" for i in range(1, len(frame))])
        ),
        Scenario("dry", 0.5, dry, prices, tuple(["root"] + [f"dry-{i}" for i in range(1, len(frame))])),
    ]
    results, audit = stochastic_schedule(cascade, paths)
    first = results["normal"].records.query("step == 0").turbine_m3s.to_numpy()
    alternative = results["dry"].records.query("step == 0").turbine_m3s.to_numpy()
    assert np.allclose(first, alternative)
    print("Shared first-stage turbine flows (m3/s):", first)
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
