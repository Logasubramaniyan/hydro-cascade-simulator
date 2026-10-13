"""Command-line entry points; all output paths are explicit and independent of cwd."""

import argparse
import json
from pathlib import Path
import os
import numpy as np
from .data import load_cascade, load_timeseries, fetch_usgs
from .experiments import demo_cascade, synthetic_series, evaluate_policies, water_value_sensitivity
from .simulation import simulate
from .scheduling import optimize
from .market import clear_market, Thermal, Interconnector, adequacy_sample
from .reporting import plot_dispatch, plot_policy_comparison, write_manifest
from .validation import score_observations
import pandas as pd


def demo(output, seed=42, trials=8, steps=48, horizon=12):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cascade = demo_cascade()
    inflows, prices = synthetic_series(steps, seed)
    (output / "cascade.json").write_text(json.dumps(cascade.to_dict(), indent=2), encoding="utf-8")
    times = pd.date_range("2025-01-01", periods=steps, freq="h", tz="UTC")
    data = pd.DataFrame(
        {"timestamp": [timestamp.isoformat() for timestamp in times], "price_eur_mwh": prices}
    )
    for i, p in enumerate(cascade.plants):
        data[f"inflow_{p.id}_m3s"] = inflows[:, i]
    data.to_csv(output / "inputs.csv", index=False)
    baseline, baseline_audit, _ = simulate(cascade, inflows)
    schedule = optimize(cascade, inflows, prices)
    baseline.to_csv(output / "baseline.csv", index=False)
    baseline_audit.to_csv(output / "baseline_water_audit.csv", index=False)
    schedule.records.to_csv(output / "optimized.csv", index=False)
    plot_dispatch(baseline, schedule.records, output / "dispatch.svg")
    evaluation = evaluate_policies(trials, steps, horizon, seed + 100)
    evaluation.to_csv(output / "policy_trials.csv", index=False)
    summary = evaluation.groupby("policy").agg(
        trials=("trial", "count"),
        mean_objective_eur=("objective_eur", "mean"),
        std_objective_eur=("objective_eur", "std"),
        mean_revenue_eur=("revenue_eur", "mean"),
        max_water_residual_m3=("max_water_residual_m3", "max"),
    )
    summary.to_csv(output / "policy_summary.csv")
    plot_policy_comparison(evaluation, output / "policies.svg")
    water_value_sensitivity(cascade, inflows, prices).to_csv(output / "water_values.csv", index=False)
    hours = np.arange(steps) % 24
    demand = np.column_stack(
        (60 + 10 * np.cos(hours * np.pi / 12), 95 + 20 * np.cos((hours - 18) * np.pi / 12))
    )
    renewable = np.column_stack((np.full(steps, 15), np.maximum(0, 35 * np.sin((hours - 6) * np.pi / 12))))
    thermal = (Thermal("north_gas", "north", 80, 60), Thermal("south_gas", "south", 110, 95))
    lines = (Interconnector("north", "south", 20),)
    market = clear_market(
        cascade,
        inflows,
        ("north", "south"),
        thermal,
        lines,
        demand,
        renewable,
        reserve_mw=np.full_like(demand, 8),
        investment={"north_gas": {"max_build_mw": 30, "cost_eur_per_mw_for_horizon": 2000}},
    )
    market.zones.to_csv(output / "market_zones.csv", index=False)
    market.thermal.to_csv(output / "market_thermal.csv", index=False)
    market.flows.to_csv(output / "market_flows.csv", index=False)
    market.hydro.to_csv(output / "market_hydro.csv", index=False)
    reliability = adequacy_sample(
        cascade,
        inflows,
        ("north", "south"),
        thermal,
        lines,
        demand,
        renewable,
        [0.05, 0.08],
        trials=20,
        seed=seed,
    )
    reliability.to_csv(output / "adequacy_trials.csv", index=False)
    findings = dict(
        dataset="synthetic",
        seed=seed,
        steps=steps,
        evaluation_trials=trials,
        forecast_horizon=horizon,
        optimizer=schedule.audit,
        market=market.audit,
        builds_mw=market.builds,
        policy_summary=summary.fillna(0).reset_index().to_dict("records"),
        adequacy=dict(
            trials=len(reliability),
            mean_unserved_energy_mwh=float(reliability.unserved_energy_mwh.mean()),
            mean_loss_of_load_hours=float(reliability.loss_of_load_hours.mean()),
            interpretation="Independent synthetic interval outages; not calibrated annual LOLE",
        ),
    )
    (output / "results.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
    write_manifest(
        output,
        dict(
            dataset="synthetic",
            seed=seed,
            dt_seconds=3600,
            trials=trials,
            steps=steps,
            horizon=horizon,
            terminal_rule="fixed illustrative carryover values for evaluation",
            current_interval_inflow_and_price="observed before decisions",
            seasonal_water_values="not estimated",
            training_seed=seed + 100,
            held_out_seed_start=seed + 1100,
        ),
        [output / "cascade.json", output / "inputs.csv"],
        dict(hydro=schedule.audit, market=market.audit),
    )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "revenue_eur": schedule.revenue_eur,
                "max_water_residual_m3": schedule.audit["max_water_residual_m3"],
            },
            indent=2,
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hydro Cascade Lab: simulate, schedule and audit")
    commands = parser.add_subparsers(dest="command", required=True)
    demo_cmd = commands.add_parser("demo", help="Generate offline synthetic case study and evidence")
    demo_cmd.add_argument("--output", default="runs/demo")
    demo_cmd.add_argument("--seed", type=int, default=42)
    demo_cmd.add_argument("--trials", type=int, default=8)
    demo_cmd.add_argument("--steps", type=int, default=48)
    demo_cmd.add_argument("--horizon", type=int, default=12)
    schedule_cmd = commands.add_parser("schedule", help="Optimize an explicit cascade and time-series CSV")
    schedule_cmd.add_argument("--config", required=True)
    schedule_cmd.add_argument("--inputs", required=True)
    schedule_cmd.add_argument("--dt-seconds", type=float, default=3600)
    schedule_cmd.add_argument("--output", default="runs/schedule")
    fetch_cmd = commands.add_parser("fetch-usgs", help="Fetch daily public discharge with provenance")
    fetch_cmd.add_argument("--site", required=True)
    fetch_cmd.add_argument("--start", required=True)
    fetch_cmd.add_argument("--end", required=True)
    fetch_cmd.add_argument("--output", required=True)
    validate_cmd = commands.add_parser("validate", help="Score predictions against independent observations")
    validate_cmd.add_argument("--predictions", required=True)
    validate_cmd.add_argument("--observations", required=True)
    validate_cmd.add_argument("--output", default="runs/validation.csv")
    args = parser.parse_args(argv)
    if args.command == "demo":
        demo(args.output, args.seed, args.trials, args.steps, args.horizon)
    elif args.command == "schedule":
        cascade = load_cascade(args.config)
        frame = load_timeseries(args.inputs, cascade, args.dt_seconds)
        inflows = frame[[f"inflow_{p.id}_m3s" for p in cascade.plants]].to_numpy()
        schedule = optimize(cascade, inflows, frame.price_eur_mwh.to_numpy(), args.dt_seconds)
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        records = schedule.records.copy()
        records["timestamp"] = records.step.map(frame.timestamp.to_dict())
        records.to_csv(output / "schedule.csv", index=False)
        (output / "solver.json").write_text(json.dumps(schedule.audit, indent=2), encoding="utf-8")
        write_manifest(
            output,
            dict(
                dt_seconds=args.dt_seconds,
                information="perfect foresight",
                terminal_rule="preserve configured initial storage",
            ),
            [args.config, args.inputs],
            schedule.audit,
        )
        print(f"Schedule written to {output.resolve()}")
    elif args.command == "fetch-usgs":
        frame = fetch_usgs(args.site, args.start, args.end, args.output, os.environ.get("USGS_API_KEY"))
        print(f"Saved {len(frame)} measured daily observations to {Path(args.output).resolve()}")
    elif args.command == "validate":
        scores = score_observations(pd.read_csv(args.predictions), pd.read_csv(args.observations))
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        scores.to_csv(output, index=False)
        print(scores.to_string(index=False))


if __name__ == "__main__":
    main()
