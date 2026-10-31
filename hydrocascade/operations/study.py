"""Reproducible industrial decision-support experiment; no orders are sent to a market."""

import argparse
from dataclasses import replace
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import platform
import sys

import numpy as np
import pandas as pd

from ..experiments import carryover_values
from ..model import Cascade, Link, Plant
from ..simulation import CascadeState
from .forecast import make_forecast, backtest_prices, forecast_health
from .io import write_json, write_result, case_dict, run_plan
from .market_data import fetch_prices
from .model import MarketScenario, MarketTerms, OperatingLimits
from .optimizer import optimize_offers, assess_offer
from .state import advance_dispatch


def build_case(forecast, steps=96):
    """Synthetic three-plant asset, explicit maintenance and a changing connection limit."""
    if not isinstance(steps, int) or isinstance(steps, bool) or not 8 <= steps <= 96:
        raise ValueError("Study horizon must contain 8 to 96 quarter-hours")
    head = 1e6 / (1000 * 9.81 * 0.9)
    cascade = Cascade(
        (
            Plant(
                "upper", "Illustrative upper", 80000, 35000, 20, head, min_storage_m3=1000, min_release_m3s=2
            ),
            Plant(
                "middle",
                "Illustrative middle",
                45000,
                15000,
                15,
                head,
                min_storage_m3=1000,
                min_release_m3s=2,
            ),
            Plant(
                "lower", "Illustrative lower", 18000, 9000, 10, head, min_storage_m3=1000, min_release_m3s=2
            ),
        ),
        (Link("upper", "middle", 2), Link("middle", "lower", 1)),
    )
    state = CascadeState.initial(cascade)
    state.last_turbine_m3s[:] = [6, 7.5, 8]
    state.queues_m3 = [[5400.0, 5400.0], [6750.0]]
    limits = OperatingLimits.standard(cascade, steps)
    availability = np.ones((steps, 3))
    availability[steps // 3 : steps // 2, 1] = 0.5
    availability[2 * steps // 3 : 3 * steps // 4, 2] = 0.4
    max_storage = limits.max_storage_m3.copy()
    max_storage[steps // 2 :] *= 0.9
    limits = replace(
        limits,
        max_release_m3s=np.tile([30, 28, 22], (steps, 1)),
        availability=availability,
        max_storage_m3=max_storage,
        initial_release_m3s=np.array([6, 7.5, 8]),
        release_ramp_m3s_per_hour=np.array([12, 14, 16]),
        variation_cost_eur_per_m3s=np.array([0.25, 0.2, 0.15]),
    )
    export = np.full(steps, 36.0)
    export[steps // 2 : 2 * steps // 3] = 24
    terms = MarketTerms(
        np.full(steps, 18.0), np.full(steps, 12.0), np.full(steps, 8.0), np.full(steps, 8.0), export
    )
    # Medoids of three equal-count bins in daily-mean residual rank, with empirical bin weights.
    groups = np.array_split(np.argsort(forecast.scenarios.mean(axis=1)), 3)
    paths = [
        (forecast.scenarios[group[len(group) // 2], :steps], len(group) / len(forecast.scenarios))
        for group in groups
    ]
    start, duration = steps // 3, min(4, steps // 3)
    up_burst, down_burst = np.zeros(steps), np.zeros(steps)
    up_burst[start : start + duration] = 1
    down_burst[start : start + duration] = 1
    activation = (
        ("idle", 0.5, np.zeros(steps), np.zeros(steps)),
        ("up_burst", 0.25, up_burst, np.zeros(steps)),
        ("down_burst", 0.25, np.zeros(steps), down_burst),
    )
    financial, security = [], []
    t = np.arange(steps)
    base_inflows = np.column_stack(
        (6 + 1.5 * np.sin(t * np.pi / 48), np.full(steps, 1.8), np.full(steps, 0.9))
    )
    for price_id, (prices, probability) in enumerate(paths):
        for hydro_id, multiplier in enumerate((0.7, 1.2)):
            inflows = base_inflows.copy()
            inflows[min(8, steps // 2) :] *= multiplier
            for name, activation_probability, up, down in activation:
                financial.append(
                    MarketScenario(
                        f"p{price_id}_h{hydro_id}_{name}",
                        probability * 0.5 * activation_probability,
                        inflows,
                        prices,
                        up,
                        down,
                        np.full(steps, 120.0),
                        np.full(steps, 35.0),
                    )
                )
            # Zero-probability paths constrain feasibility without pretending to estimate
            # the likelihood of a full-horizon sustained activation.
            for name, up, down in (
                ("sustained_up", np.ones(steps), np.zeros(steps)),
                ("sustained_down", np.zeros(steps), np.ones(steps)),
                (
                    "reversal",
                    (np.arange(steps) // 4 % 2 == 0).astype(float),
                    (np.arange(steps) // 4 % 2 == 1).astype(float),
                ),
            ):
                security.append(
                    MarketScenario(
                        f"p{price_id}_h{hydro_id}_{name}",
                        0.0,
                        inflows,
                        prices,
                        up,
                        down,
                        np.full(steps, 120.0),
                        np.full(steps, 35.0),
                        role="security",
                    )
                )
    targets = {p.id: 0.8 * p.initial_storage_m3 for p in cascade.plants}
    return cascade, state, terms, limits, financial, security, targets, carryover_values(cascade)


def plot_study(summary, stress, backtest, forecast, observed, output, price_label):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "svg.fonttype": "none",
            "svg.hashsalt": "hydro-operations-v1",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "font.size": 9,
        }
    )
    fig, axes = plt.subplots(2, 2, figsize=(13, 9), layout="constrained")
    fig.patch.set_facecolor("#f4f7f9")
    x = np.arange(len(summary))
    axes[0, 0].bar(x - 0.18, summary.expected_profit_eur / 1000, 0.36, label="Scenario mean", color="#087f8c")
    axes[0, 0].bar(
        x + 0.18, summary.lower_tail_profit_eur / 1000, 0.36, label="Lower 10% tail", color="#6964a5"
    )
    axes[0, 0].set_xticks(x, summary.policy.str.replace("_", " "), rotation=15, ha="right")
    axes[0, 0].set_ylabel("Terminal-adjusted value (thousand EUR)")
    axes[0, 0].set_title("Modelled cashflows; assumed accepted quantities", fontweight="bold")
    axes[0, 0].legend(frameon=False)
    names = list(stress.policy.unique())
    for k, name in enumerate(stress.stress.unique()):
        values = stress[stress.stress == name].set_index("policy").loc[names].total_delivery_error_mwh
        axes[0, 1].bar(np.arange(len(names)) + (k - 1) * 0.23, values, 0.23, label=name.replace("_", " "))
    axes[0, 1].set_xticks(
        np.arange(len(names)), [n.replace("_", " ") for n in names], rotation=15, ha="right"
    )
    axes[0, 1].set_ylabel("Minimum total delivery error (MWh)")
    axes[0, 1].set_title("Separate stress paths, including headroom-only offers", fontweight="bold")
    axes[0, 1].legend(frameon=False, fontsize=8)
    hours = np.arange(len(forecast.frame)) / 4
    axes[1, 0].fill_between(
        hours,
        forecast.frame.lower_eur_mwh,
        forecast.frame.upper_eur_mwh,
        color="#087f8c",
        alpha=0.15,
        label="Empirical 80% interval",
    )
    axes[1, 0].plot(hours, forecast.frame.point_eur_mwh, label="Weekly-lag forecast", color="#087f8c")
    axes[1, 0].plot(hours, observed, label="Held-out observed price", color="#dc9040")
    axes[1, 0].set_ylabel("Day-ahead price (EUR/MWh)")
    axes[1, 0].set_xlabel("Delivery hours from UTC start")
    axes[1, 0].set_title("Forecast issued before delivery; errors remain visible", fontweight="bold")
    axes[1, 0].legend(frameon=False, fontsize=8)
    axes[1, 1].plot(np.arange(len(backtest)), backtest.empirical_coverage * 100, marker="o", color="#6964a5")
    axes[1, 1].axhline(80, color="#a95b65", linestyle="--", label="Nominal 80%")
    axes[1, 1].set_ylim(0, 105)
    axes[1, 1].set_xticks(
        np.arange(len(backtest)),
        pd.to_datetime(backtest.delivery_start_utc).dt.strftime("%d %b"),
        rotation=25,
        ha="right",
    )
    axes[1, 1].set_ylabel("Held-out interval coverage (%)")
    axes[1, 1].set_title("Forecast quality is a deployment dependency", fontweight="bold")
    axes[1, 1].legend(frameon=False)
    for ax in axes.flat:
        ax.grid(axis="y", alpha=0.16)
    fig.suptitle(
        f"Hydro Reserve Offer Workbench\n{price_label} • synthetic assets and activations • quarter-hour dispatch",
        fontsize=15,
        fontweight="bold",
    )
    fig.savefig(output, metadata={"Date": None})
    plt.close(fig)


def run_study(output, prices_path=None, steps=96, with_grid=False, all_records=False):
    root = Path(__file__).resolve().parents[2]
    source_hashes = {
        p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "hydrocascade").rglob("*.py"))
    }
    prices_path = (
        Path(prices_path) if prices_path else root / "data" / "market" / "se4_2026-09-20_2026-10-28.csv"
    )
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Study output must be empty")
    frame = pd.read_csv(prices_path)
    provenance_path = prices_path.with_suffix(".provenance.json")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8")) if provenance_path.exists() else {}
    if provenance:
        if (
            sha256(prices_path.read_bytes()).hexdigest() != provenance["csv_sha256"]
            or sha256(prices_path.with_suffix(".raw.json").read_bytes()).hexdigest()
            != provenance["raw_sha256"]
        ):
            raise ValueError("Market archive differs from its recorded raw/CSV provenance")
        if "cc by 4.0" not in provenance.get("licence", "").lower():
            raise ValueError("Public study evidence requires openly licensed source prices")
    price_label = (
        f"Observed {provenance['zone']} prices" if provenance else "User-supplied prices; unverified origin"
    )
    forecast = make_forecast(frame, "2026-10-26T10:00Z", "2026-10-27T00:00Z", steps=steps)
    backtest, predictions = backtest_prices(
        frame, pd.date_range("2026-10-18T00:00Z", "2026-10-28T00:00Z", freq="D")
    )
    health = forecast_health(backtest, forecast.audit["issued_at_utc"])
    cascade, state, terms, limits, financial, security, targets, values = build_case(forecast, steps)
    output.mkdir(parents=True, exist_ok=True)
    write_json(
        output / "case.json",
        case_dict(
            cascade,
            financial + security,
            terms,
            limits,
            state,
            terminal_min=targets,
            terminal_value=values,
            risk_weight=0.55,
        ),
    )
    backtest.to_csv(output / "forecast_backtest.csv", index=False, lineterminator="\n")
    predictions.to_csv(output / "forecast_predictions.csv", index=False, lineterminator="\n")
    forecast.frame.to_csv(output / "forecast.csv", index=False, lineterminator="\n")
    write_json(output / "forecast_audit.json", forecast.audit)
    write_json(
        output / "source_prices.json",
        provenance or {"data_kind": "user-supplied; unverified origin and bidding zone"},
    )
    policies = (
        ("energy_only", 0.0, False),
        ("mean_profit", 0.0, False),
        ("tail_aware", 0.55, False),
        ("stress_constrained", 0.55, True),
    )
    results, summaries = {}, []
    for name, risk, robust in policies:
        market = (
            replace(terms, max_up_mw=np.zeros(steps), max_down_mw=np.zeros(steps))
            if name == "energy_only"
            else terms
        )
        result = optimize_offers(
            cascade,
            financial + security if robust else financial,
            market,
            limits,
            state=state,
            terminal_min=targets,
            terminal_value=values,
            risk_weight=risk,
        )
        chosen = {
            result.outcomes.query("role == 'forecast'")
            .sort_values("terminal_adjusted_profit_eur")
            .scenario.iloc[i]
            for i in (0, -1)
        }
        write_result(result, output / name, selected=None if all_records else chosen)
        summaries.append(
            dict(
                policy=name,
                **result.audit,
                mean_up_capacity_mw=float(result.offers.reserve_up_mw.mean()),
                mean_down_capacity_mw=float(result.offers.reserve_down_mw.mean()),
            )
        )
        results[name] = result
    # Same financial problem and feasible set: the mean optimum bounds every other mean,
    # while the CVaR optimum bounds the mean/CVaR utility of the risk-neutral incumbent.
    mean, cautious = results["mean_profit"].audit, results["tail_aware"].audit
    if (
        mean["expected_profit_eur"] + 1e-4 < cautious["expected_profit_eur"]
        or cautious["utility_eur"] + 1e-4
        < 0.45 * mean["expected_profit_eur"] + 0.55 * mean["lower_tail_profit_eur"]
    ):
        raise RuntimeError("Mean/CVaR economic dominance check failed")
    naive = results["energy_only"].offers.copy()
    naive.reserve_up_mw = np.minimum(terms.max_up_mw, np.maximum(terms.export_limit_mw - naive.energy_mw, 0))
    naive.reserve_down_mw = np.minimum(terms.max_down_mw, naive.energy_mw)
    naive.to_csv(output / "headroom_only_offers.csv", index=False, lineterminator="\n")
    # Stress evaluation is separate from forecast cashflows; no financial reward is
    # attributed to an offer with failed delivery or to a zero-probability stress path.
    stress_rows = []
    base = financial[0]
    patterns = (
        ("sustained_up", np.ones(steps), np.zeros(steps)),
        ("sustained_down", np.zeros(steps), np.ones(steps)),
        (
            "reversal",
            (np.arange(steps) // 4 % 2 == 0).astype(float),
            (np.arange(steps) // 4 % 2 == 1).astype(float),
        ),
    )
    for policy, offers in [(name, result.offers) for name, result in results.items()] + [
        ("headroom_only", naive)
    ]:
        for name, up, down in patterns:
            s = replace(base, name=name, probability=1.0, activation_up=up, activation_down=down)
            assessed = assess_offer(
                cascade, [s], terms, limits, offers, state=state, terminal_min=targets, terminal_value=values
            )
            row = assessed.outcomes.iloc[0]
            stress_rows.append(
                dict(
                    policy=policy,
                    stress=name,
                    max_delivery_error_mw=assessed.audit["max_delivery_error_mw"],
                    total_delivery_error_mwh=float(row.undelivered_energy_mwh + row.excess_energy_mwh),
                    passes=assessed.audit["deliverable_on_tested_paths"],
                    max_water_residual_m3=assessed.audit["max_water_residual_m3"],
                    interpretation="optimistic trajectory-existence test; not an out-of-sample executable policy",
                )
            )
    summary, stress = pd.DataFrame(summaries), pd.DataFrame(stress_rows)
    summary.to_csv(output / "summary.csv", index=False, lineterminator="\n")
    stress.to_csv(output / "stress_assessment.csv", index=False, lineterminator="\n")
    selected_name = (
        results["stress_constrained"]
        .outcomes.query("role == 'forecast'")
        .sort_values("terminal_adjusted_profit_eur")
        .scenario.iloc[0]
    )
    selected = next(s for s in financial if s.name == selected_name)
    checkpoint = advance_dispatch(
        cascade,
        state,
        selected.inflows_m3s,
        results["stress_constrained"].schedules[selected_name],
        max(1, steps // 2),
        start_utc=forecast.frame.timestamp.iloc[0],
    )
    checkpoint.save(output / "executed_prefix_checkpoint.json")
    grid = []
    if with_grid:
        from .grid_check import check_demo_grid

        grid_name = max(
            results["stress_constrained"].schedules,
            key=lambda name: (
                results["stress_constrained"].schedules[name].groupby("step").power_mw.sum().max()
            ),
        )
        grid_scenario = next(s for s in financial + security if s.name == grid_name)
        grid = check_demo_grid(
            cascade,
            grid_scenario,
            results["stress_constrained"].schedules[grid_name],
            state,
            output / "grid_check",
            targets,
            values,
        )
        pd.DataFrame(grid).to_csv(output / "grid_assessment.csv", index=False, lineterminator="\n")
    write_json(
        output / "decision_report.json",
        dict(
            status="research_only",
            forecast_health=health,
            reserve_stress_failures=stress[~stress.passes][
                ["policy", "stress", "total_delivery_error_mwh"]
            ].to_dict("records"),
            grid_checked=with_grid,
            grid_absorption_failures=[r["network_case"] for r in grid if not r["grid_can_absorb_schedule"]],
            deployment_gaps=[
                "synthetic plant/inflow/activation data",
                "empirical price baseline requires broader validation",
                "TSO prequalification and executable bid rules not implemented",
                "no unit commitment, variable head or SCADA control",
            ],
            purpose="engineering assessment, data/model acceptance and operator review; no order submission",
        ),
    )
    actual = (
        pd.Series(frame.price_eur_mwh.to_numpy(), index=pd.to_datetime(frame.timestamp, utc=True))
        .reindex(pd.DatetimeIndex(forecast.frame.timestamp))
        .to_numpy()
    )
    settlement_rows = []
    for name, result in results.items():
        expected_da = float((result.outcomes.probability * result.outcomes.day_ahead_eur).sum())
        heldout_da = float((result.offers.energy_mw.to_numpy() * actual).sum() * 0.25)
        settlement_rows.append(
            dict(
                policy=name,
                forecast_expected_day_ahead_cash_eur=expected_da,
                heldout_day_ahead_cash_eur=heldout_da,
                cash_difference_eur=heldout_da - expected_da,
                interpretation="fixed pre-auction quantities, observed held-out DA prices; not total realized profit",
            )
        )
    pd.DataFrame(settlement_rows).to_csv(
        output / "heldout_day_ahead_cash.csv", index=False, lineterminator="\n"
    )
    plot_study(
        summary,
        stress,
        backtest,
        forecast,
        actual,
        output / "operations_study.svg",
        price_label,
    )
    if source_hashes != {
        p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "hydrocascade").rglob("*.py"))
    }:
        raise RuntimeError("Package sources changed during the study; reproduce from a stable checkout")
    manifest = dict(
        schema_version=1,
        study="quarter-hour reserve offer workbench",
        dt_seconds=900,
        steps=steps,
        source_prices_sha256=sha256(prices_path.read_bytes()).hexdigest(),
        public_prices=price_label + "; other market and plant data synthetic",
        code_sha256=source_hashes,
        artifact_sha256={
            p.relative_to(output).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in sorted(output.rglob("*"))
            if p.is_file() and p.suffix != ".sqlite"
        },
        python=sys.version,
        platform=platform.platform(),
        dependencies={name: metadata.version(name) for name in ("numpy", "scipy", "pandas", "matplotlib")},
        native_sqlite="optional grid databases retained in runs/CI artifacts, excluded from Git evidence",
        financial_scenarios=len(financial),
        security_scenarios=len(security),
        full_dispatch_exports=all_records,
    )
    write_json(output / "manifest.json", manifest)
    print(
        summary[
            [
                "policy",
                "expected_profit_eur",
                "lower_tail_profit_eur",
                "mean_up_capacity_mw",
                "mean_down_capacity_mw",
            ]
        ].to_string(index=False)
    )
    print(f"Forecast health: {health['status']} ({health['reason']}). Output is research-only.")
    return summary, stress


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hydro reserve offer research and public-data workflows")
    commands = parser.add_subparsers(dest="command", required=True)
    study = commands.add_parser("study")
    study.add_argument("--output", required=True)
    study.add_argument("--prices")
    study.add_argument("--steps", type=int, default=96)
    study.add_argument("--with-grid", action="store_true")
    study.add_argument("--all-records", action="store_true")
    fetch = commands.add_parser("fetch-prices")
    for name in ("zone", "start", "end", "output"):
        fetch.add_argument(f"--{name}", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--config", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--checkpoint")
    plan.add_argument("--as-of")
    args = parser.parse_args(argv)
    if args.command == "study":
        run_study(args.output, args.prices, args.steps, args.with_grid, args.all_records)
    elif args.command == "fetch-prices":
        _, provenance = fetch_prices(args.zone, args.start, args.end, args.output)
        print(json.dumps(provenance, indent=2))
    else:
        result = run_plan(args.config, args.output, args.checkpoint, args.as_of)
        print(json.dumps(result.audit, indent=2))


if __name__ == "__main__":
    main()
