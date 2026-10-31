"""Executable synthetic study: hydropower flexibility under electrical congestion."""

import argparse
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import platform
import sys

import numpy as np
import pandas as pd

from . import POWERGAMA_COMMIT
from .network import ACLine, Bus, Network, ThermalUnit
from ..experiments import demo_cascade, synthetic_series, carryover_values
from ..scheduling import optimize
from ..simulation import simulate

CASES = ("unconstrained", "congested", "high_wind", "dry", "wet", "reinforced")
POLICIES = ("inflow_following", "price_based", "joint")


def build_case(name="congested", steps=48, seed=42, corridor_scale=None):
    if name not in CASES or not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
        raise ValueError("Choose a known case and a positive integer horizon")
    cascade = demo_cascade()
    inflows, prices = synthetic_series(steps, seed)
    hour = np.arange(steps) % 24
    wind = np.maximum(0, 65 + 40 * np.cos((hour - 4) * np.pi / 12))
    renewable = np.column_stack((wind, np.zeros(steps), np.zeros(steps)))
    demand = np.column_stack(
        (np.full(steps, 20), np.full(steps, 15), 130 + 35 * np.cos((hour - 18) * np.pi / 12))
    )
    if name == "high_wind":
        renewable *= 1.8
    if name == "dry":
        inflows *= 0.45
    if name == "wet":
        inflows *= 1.6
    scale = 2.5 if name == "reinforced" else 1.0
    if corridor_scale is not None:
        if not np.isfinite(corridor_scale) or corridor_scale < 0:
            raise ValueError("corridor_scale must be finite and nonnegative")
        scale = corridor_scale
    unconstrained = name == "unconstrained" and corridor_scale is None
    lines = (
        ACLine("north_internal", "north_a", "north_b", 0.08, 1000 if unconstrained else 150),
        ACLine("corridor_a", "north_a", "south", 0.20, 1000 if unconstrained else 20 * scale),
        ACLine("corridor_b", "north_b", "south", 0.10, 1000 if unconstrained else 30 * scale),
    )
    network = Network(
        (Bus("north_a", "north", 64, 16), Bus("north_b", "north", 62, 16), Bus("south", "south", 58, 15)),
        lines,
        (ThermalUnit("north_thermal", "north_b", 60, 55), ThermalUnit("south_thermal", "south", 200, 120)),
        {"upper": "north_a", "middle": "north_b", "lower": "north_b"},
        demand,
        renewable,
    )
    return cascade, network, inflows, prices


def _digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def plot_study(summary, sensitivity, congestion_lines, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "svg.fonttype": "none",
            "svg.hashsalt": "hydro-grid-v1",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "font.size": 10,
        }
    )
    palette = {"inflow_following": "#a9b4bd", "price_based": "#ec9a46", "joint": "#087f8c"}
    fig, axes = plt.subplots(2, 2, figsize=(13, 9), layout="constrained")
    fig.patch.set_facecolor("#f4f7f9")
    x = np.arange(len(CASES))
    for j, policy in enumerate(POLICIES):
        frame = summary[summary.policy == policy].set_index("case").loc[list(CASES)]
        axes[0, 0].bar(
            x + (j - 1) * 0.24,
            frame.terminal_adjusted_cost_eur / 1000,
            0.24,
            label=policy.replace("_", " "),
            color=palette[policy],
        )
    axes[0, 0].set_xticks(x, [n.replace("_", " ") for n in CASES], rotation=20, ha="right")
    axes[0, 0].set_ylabel("Terminal-adjusted system cost (thousand EUR)")
    axes[0, 0].set_title("One water balance, three scheduling policies", fontweight="bold")
    axes[0, 0].legend(frameon=False, fontsize=8)
    for policy in POLICIES:
        frame = summary[summary.policy == policy].set_index("case").loc[list(CASES)]
        axes[0, 1].plot(
            x,
            frame.renewable_curtailed_mwh,
            marker="o",
            color=palette[policy],
            label=policy.replace("_", " "),
        )
    axes[0, 1].set_xticks(x, [n.replace("_", " ") for n in CASES], rotation=20, ha="right")
    axes[0, 1].set_ylabel("Available renewable energy not used (MWh)")
    axes[0, 1].set_title("Congestion changes the use of flexibility", fontweight="bold")
    axes[1, 0].plot(
        sensitivity.corridor_scale,
        sensitivity.operating_cost_eur / 1000,
        marker="o",
        color="#087f8c",
        label="Thermal operating cost",
    )
    axes[1, 0].plot(
        sensitivity.corridor_scale,
        sensitivity.terminal_adjusted_cost_eur / 1000,
        marker="s",
        color="#6665a8",
        label="Terminal-adjusted cost",
    )
    axes[1, 0].set_xlabel("Illustrative corridor capacity multiplier")
    axes[1, 0].set_ylabel("Study-horizon cost (thousand EUR)")
    axes[1, 0].set_title("Operational benefit; no investment-cost claim", fontweight="bold")
    axes[1, 0].legend(frameon=False, fontsize=8)
    for line_id, frame in congestion_lines.groupby("line_id"):
        axes[1, 1].plot(frame.step, 100 * frame.loading_fraction, label=line_id)
    axes[1, 1].axhline(100, linestyle="--", color="#a95b65", linewidth=1)
    axes[1, 1].set_xlabel("Elapsed dispatch interval")
    axes[1, 1].set_ylabel("Line loading (% of capacity)")
    axes[1, 1].set_title("Joint scheduling: congested network", fontweight="bold")
    axes[1, 1].legend(frameon=False, fontsize=8)
    for ax in axes.flat:
        ax.grid(axis="y", alpha=0.15)
    fig.suptitle(
        "Hydropower Flexibility Under Grid Congestion\nSynthetic three-bus DC network • fixed-head cascade • perfect foresight",
        fontsize=15,
        fontweight="bold",
    )
    fig.savefig(output, metadata={"Date": None}, dpi=150)
    plt.close(fig)


def run_study(output, steps=48, seed=42):
    from .backend import solve_grid, dependency_provenance

    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Study output must be empty; preserve previous run evidence")
    output.mkdir(parents=True, exist_ok=True)
    summaries = []
    congestion_lines = None
    for name in CASES:
        cascade, network, inflows, prices = build_case(name, steps, seed)
        values = carryover_values(cascade)
        targets = {plant.id: 0 for plant in cascade.plants}
        baseline, _, _ = simulate(cascade, inflows)
        price_schedule = optimize(cascade, inflows, prices, terminal_min=targets, terminal_value=values)
        input_path = output / name / "inputs.json"
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(
            json.dumps(
                dict(
                    cascade=cascade.to_dict(),
                    network=network.to_dict(),
                    inflows_m3s=inflows.tolist(),
                    exogenous_price_eur_mwh=prices.tolist(),
                    dt_seconds=3600,
                    terminal_min_m3=targets,
                    terminal_value_eur_m3=values,
                ),
                indent=2,
            ),
            encoding="utf-8",
        )
        for policy, fixed in (
            ("inflow_following", baseline),
            ("price_based", price_schedule),
            ("joint", None),
        ):
            result = solve_grid(
                cascade,
                network,
                inflows,
                terminal_min=targets,
                terminal_value=values,
                fixed_schedule=fixed,
                output=output / name / policy,
            )
            summaries.append(dict(case=name, policy=policy, **result.audit))
            if name == "congested" and policy == "joint":
                congestion_lines = result.lines
    summary = pd.DataFrame(summaries)
    sensitivity_rows = []
    for scale in (0.5, 1.0, 1.5, 2.0, 2.5, 4.0):
        cascade, network, inflows, _ = build_case("congested", steps, seed, corridor_scale=scale)
        result = solve_grid(
            cascade,
            network,
            inflows,
            terminal_min={p.id: 0 for p in cascade.plants},
            terminal_value=carryover_values(cascade),
        )
        sensitivity_rows.append(dict(corridor_scale=scale, **result.audit))
    sensitivity = pd.DataFrame(sensitivity_rows)
    summary.to_csv(output / "summary.csv", index=False)
    sensitivity.to_csv(output / "corridor_sensitivity.csv", index=False)
    plot_study(summary, sensitivity, congestion_lines, output / "grid_study.svg")
    # Verify nested feasible sets before publishing any claim of optimization value.
    for case, frame in summary.groupby("case"):
        joint = frame[frame.policy == "joint"].terminal_adjusted_cost_eur.iloc[0]
        if joint > frame.terminal_adjusted_cost_eur.min() + 1e-4:
            raise RuntimeError(f"Joint solve did not dominate fixed controls for {case}")
    if np.diff(sensitivity.terminal_adjusted_cost_eur).max(initial=0) > 1e-4:
        raise RuntimeError("Relaxing corridor constraints unexpectedly increased the optimal objective")
    root = Path(__file__).resolve().parents[1]
    code = {p.relative_to(root.parent).as_posix(): _digest(p) for p in sorted(root.rglob("*.py"))}
    artifacts = {
        p.relative_to(output).as_posix(): _digest(p)
        for p in sorted(output.rglob("*"))
        if p.is_file() and p.suffix != ".sqlite"
    }
    manifest = dict(
        dataset="synthetic; not Swedish bidding-zone calibration",
        seed=seed,
        steps=steps,
        dt_seconds=3600,
        information="known-future single-window optimization and fixed schedule assessments",
        backend=dependency_provenance(),
        powergama_commit=POWERGAMA_COMMIT,
        python=sys.version,
        platform=platform.platform(),
        dependencies={name: metadata.version(name) for name in ("numpy", "scipy", "pandas", "matplotlib")},
        electrical_model="lossless meshed DC network; no AC voltage/reactive/security assessment",
        water_model="one cascade in m3; mapped PowerGAMA native storage disabled",
        terminal_assumptions="same minimum targets and generation-equivalent EUR 65/MWh values for all policies",
        code_sha256=code,
        artifact_sha256=artifacts,
        native_sqlite="Retained in local runs, excluded from content hashes and Git evidence export",
    )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
    print(
        summary[
            ["case", "policy", "operating_cost_eur", "terminal_adjusted_cost_eur", "renewable_curtailed_mwh"]
        ].to_string(index=False)
    )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the pinned PowerGAMA hydro-grid congestion study")
    parser.add_argument("--output", default="runs/grid-study")
    parser.add_argument("--steps", type=int, default=48)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    run_study(args.output, args.steps, args.seed)


if __name__ == "__main__":
    main()
