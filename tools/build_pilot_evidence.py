"""Reproduce a labelled asset pilot benchmark. All metering/geometry except public DA prices is synthetic."""

# ruff: noqa: E402 -- direct script execution adds this checkout before package imports.

import argparse
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hydrocascade import Cascade, Link, Plant
from hydrocascade.operations.io import write_json
from hydrocascade.operations.model import OperatingLimits
from hydrocascade.simulation import CascadeState
from hydrocascade.pilot.assets import ReservoirCurve, Unit
from hydrocascade.pilot.workflow import run_case, run_calibration, run_evaluation


def example_case(steps=32):
    price_path = ROOT / "data/market/se4_2026-09-20_2026-10-28.csv"
    prices = pd.read_csv(price_path)
    times = pd.to_datetime(prices.timestamp, utc=True)
    selected = prices[times >= pd.Timestamp("2026-10-27T00:00Z")].head(steps)
    cascade = Cascade(
        (
            Plant("upper", "Synthetic upper", 90000, 45000, 4, 55, min_storage_m3=5000, min_release_m3s=0.4),
            Plant("lower", "Synthetic lower", 50000, 25000, 4, 36, min_storage_m3=3000, min_release_m3s=0.4),
        ),
        (Link("upper", "lower", delay_steps=2),),
    )
    curves = [
        ReservoirCurve("upper", (5000, 45000, 90000), (150, 155, 160), 100),
        ReservoirCurve("lower", (3000, 25000, 50000), (100, 106, 112), 70),
    ]
    units = [
        Unit(
            "upper_main",
            "upper",
            (1, 2, 3),
            (0.82, 0.91, 0.9),
            min_up_steps=3,
            min_down_steps=2,
            startup_cost_eur=12,
            running_cost_eur_per_hour=2,
            ramp_m3s_per_hour=8,
            hydraulic_loss_m_per_m3s2=0.02,
        ),
        Unit(
            "upper_small",
            "upper",
            (0.5, 1),
            (0.8, 0.87),
            min_up_steps=2,
            min_down_steps=2,
            startup_cost_eur=5,
            ramp_m3s_per_hour=4,
        ),
        Unit(
            "lower_main",
            "lower",
            (1, 2, 4),
            (0.83, 0.9, 0.87),
            min_up_steps=2,
            min_down_steps=2,
            startup_cost_eur=8,
            ramp_m3s_per_hour=8,
        ),
    ]
    state = CascadeState.initial(cascade)
    state.queues_m3 = [[1800, 1800]]
    limits = replace(
        OperatingLimits.standard(cascade, steps),
        release_ramp_m3s_per_hour=np.array([8, 8]),
        max_release_m3s=np.full((steps, 2), 5),
        variation_cost_eur_per_m3s=np.array([0.2, 0.2]),
    )
    availability = np.ones(steps, dtype=int)
    availability[12:20] = 0
    config = dict(
        schema_version=1,
        case_type="asset_dispatch",
        data_kind="synthetic assets, inflows and maintenance; observed SE4 prices used as known-input benchmark, not forecasts",
        start_utc="2026-10-27T00:00:00Z",
        issued_at_utc="2026-10-26T10:00:00Z",
        dt_seconds=900,
        cascade=cascade.to_dict(),
        curves=[asdict(c) for c in curves],
        units=[asdict(u) for u in units],
        state=asdict(state),
        inflows_m3s=np.tile([2.0, 0.2], (steps, 1)),
        prices_eur_mwh=selected.price_eur_mwh.tolist(),
        operating_limits=asdict(limits),
        unit_availability={"upper_small": availability.tolist()},
        export_limit_mw=[3.0] * steps,
        terminal_min_m3={"upper": 45000, "lower": 25000},
        terminal_value_eur_m3={"upper": 0.012, "lower": 0.006},
        hydraulic_tolerance_mw=0.03,
        storage_segments=2,
        flow_segments=2,
        time_limit_seconds=120,
        product_screen=dict(
            rules=dict(
                name="illustrative 0.1 MW energy increments; not actual exchange rules",
                minimum_mw=0.1,
                increment_mw=0.1,
                block_steps=1,
            ),
            gate_closure_utc="2026-10-26T11:00:00Z",
        ),
    )
    return config, price_path


def build(output):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Benchmark output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    config, price_path = example_case()
    write_json(output / "case.json", config)
    result = run_case(output / "case.json", output / "plan")
    energy = pd.read_csv(output / "plan/energy_schedule.csv")
    energy["reserve_up_mw"] = energy["reserve_down_mw"] = 0
    energy.to_csv(output / "accepted_quantities.csv", index=False, lineterminator="\n")
    total = result.records.groupby("timestamp").power_mw.sum().to_numpy()
    meter = pd.DataFrame(
        dict(timestamp=energy.timestamp, actual_mw=total * (1 + 0.01 * np.sin(np.arange(len(total)))))
    )
    meter.to_csv(output / "synthetic_meter.csv", index=False, lineterminator="\n")
    market = pd.DataFrame(dict(timestamp=energy.timestamp, day_ahead_eur_mwh=config["prices_eur_mwh"]))
    for name in (
        "up_capacity_eur_mw_h",
        "down_capacity_eur_mw_h",
        "activation_up",
        "activation_down",
        "up_energy_eur_mwh",
        "down_energy_eur_mwh",
    ):
        market[name] = 0
    market["imbalance_sell_eur_mwh"], market["imbalance_buy_eur_mwh"], market["delivery_penalty_eur_mwh"] = (
        75,
        125,
        50,
    )
    market.to_csv(output / "settlement_inputs.csv", index=False, lineterminator="\n")
    predicted = result.records[["timestamp", "plant_id", "storage_m3", "power_mw"]].copy()
    observed = predicted.copy()
    observed["power_mw"] *= 1 + 0.02 * np.sin(np.arange(len(observed)))
    observed["storage_m3"] += 10 * np.sin(np.arange(len(observed)))
    observed.loc[0, "storage_m3"] = np.nan
    predicted.to_csv(output / "predicted.csv", index=False, lineterminator="\n")
    observed.to_csv(output / "synthetic_observed.csv", index=False, lineterminator="\n")
    write_json(
        output / "evaluation_case.json",
        dict(
            schema_version=1,
            case_type="historical_evaluation",
            data_kind="synthetic metering/settlement/observations; actual public day-ahead prices only",
            offers_csv="accepted_quantities.csv",
            meter_csv="synthetic_meter.csv",
            market_csv="settlement_inputs.csv",
            predicted_csv="predicted.csv",
            observed_csv="synthetic_observed.csv",
            mae_thresholds={"storage_m3": 5, "power_mw": 0.005},
            minimum_coverage=0.95,
        ),
    )
    settlement = run_evaluation(output / "evaluation_case.json", output / "evaluation")
    plot_benchmark(result, settlement, output / "pilot_study.svg")
    unit, curve = Unit(**config["units"][0]), ReservoirCurve(**config["curves"][0])
    measured = pd.DataFrame(
        dict(
            timestamp=pd.date_range("2026-10-20T00:00Z", periods=24, freq="15min"),
            start_storage_m3=np.linspace(20000, 60000, 24),
            end_storage_m3=np.linspace(21000, 61000, 24),
            flow_m3s=np.linspace(1, 3, 24),
        )
    )
    measured["power_mw"] = [
        curve.power(unit, r.flow_m3s, r.start_storage_m3, r.end_storage_m3) * (0.92 if i < 16 else 0.88)
        for i, r in enumerate(measured.itertuples())
    ]
    measured.to_csv(output / "synthetic_unit_observations.csv", index=False, lineterminator="\n")
    write_json(
        output / "calibration_case.json",
        dict(
            schema_version=1,
            case_type="unit_calibration",
            data_kind="synthetic known-scale training and changed-efficiency held-out observations",
            unit=asdict(unit),
            curve=asdict(curve),
            observations_csv="synthetic_unit_observations.csv",
            training_end_utc="2026-10-20T04:00Z",
        ),
    )
    fit = run_calibration(output / "calibration_case.json", output / "calibration")
    study = dict(
        data_kind="synthetic benchmark except observed day-ahead prices; no company validation or achieved profit",
        steps=len(energy),
        dt_seconds=900,
        price_source_sha256=sha256(price_path.read_bytes()).hexdigest(),
        modeled_revenue_eur=result.revenue_eur,
        modeled_objective_eur=result.objective_eur,
        hydraulic_review=result.audit["hydraulic_review_status"],
        max_power_approximation_error_mw=result.audit["max_power_approximation_error_mw"],
        max_water_residual_m3=result.audit["max_water_residual_m3"],
        net_evaluation_cash_eur=settlement.summary["net_cash_eur"],
        fitted_efficiency_scale=fit.audit["efficiency_scale"],
        heldout_calibration_mae_mw=fit.audit["heldout_mae_mw"],
        builder_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    write_json(output / "study.json", study)
    print(json.dumps(study, indent=2))


def plot_benchmark(result, settlement, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "svg.hashsalt": "hydro-pilot-20261110",
            "svg.fonttype": "none",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    fig, axes = plt.subplots(3, 1, figsize=(11, 9), layout="constrained", sharex=True)
    for color, (name, frame) in zip(
        ("#007f8b", "#5864b0", "#ed9b40"), result.unit_records.groupby("unit_id", sort=False)
    ):
        axes[0].step(frame.step / 4, frame.replayed_power_mw, where="post", label=name, color=color)
    axes[0].set_ylabel("Hydraulic replay (MW)")
    axes[0].legend(ncol=3, frameon=False)
    for color, (name, frame) in zip(("#007f8b", "#5864b0"), result.records.groupby("plant_id", sort=False)):
        axes[1].plot(frame.step / 4, frame.storage_m3 / 1000, label=name, color=color)
    axes[1].set_ylabel("End storage (thousand m³)")
    axes[1].legend(ncol=2, frameon=False)
    model = result.records.groupby("step").model_power_mw.sum()
    replay = result.records.groupby("step").power_mw.sum()
    axes[2].plot(
        model.index / 4, 1000 * (model - replay), color="#bc5475", label="MILP minus hydraulic replay"
    )
    axes[2].set_ylabel("Power approximation (kW)")
    axes[2].set_xlabel("Elapsed study hours (15-minute intervals)")
    axes[2].legend(frameon=False)
    for axis in axes:
        axis.grid(alpha=0.18)
    fig.suptitle(
        "Asset-aware hydro pilot | two synthetic reservoirs, three units\nObserved prices used as known inputs; synthetic metering and operating assumptions",
        fontsize=13,
    )
    fig.savefig(output, metadata={"Date": None})
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    build(parser.parse_args().output)
