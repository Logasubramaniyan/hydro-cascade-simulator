"""Seeded synthetic experiments: forecasts never access future evaluation observations."""

from dataclasses import replace
import numpy as np
import pandas as pd
from .model import Cascade, Plant, Link
from .simulation import CascadeState, simulate, step
from .scheduling import Scenario, optimize, stochastic_schedule, terminal_worth


def demo_cascade():
    return Cascade(
        (
            Plant("upper", "Upper reservoir", 2_000_000, 1_000_000, 65, 90, min_release_m3s=3, zone="north"),
            Plant("middle", "Middle reservoir", 800_000, 400_000, 75, 55, min_release_m3s=4, zone="north"),
            Plant("lower", "Lower reservoir", 500_000, 250_000, 85, 35, min_release_m3s=5, zone="south"),
        ),
        (Link("upper", "middle", 1), Link("middle", "lower", 1)),
    )


def synthetic_series(steps=48, seed=42):
    """Synthetic hourly inflows/prices, correlated through a common AR(1) weather factor."""
    rng = np.random.default_rng(seed)
    weather = np.zeros(steps)
    for t in range(steps):
        weather[t] = (0.85 * weather[t - 1] if t else 0) + rng.normal(0, 0.7)
    hour = np.arange(steps) % 24
    inflows = np.maximum(0, np.column_stack((28 + 7 * weather, 5 + 1.5 * weather, 3 + weather)))
    prices = 65 + 28 * np.cos((hour - 18) * np.pi / 12) - 5 * weather + rng.normal(0, 4, steps)
    return inflows, prices


def carryover_values(cascade, reference_price=65):
    """Illustrative cumulative generation value, not a fitted seasonal value function."""
    energy = np.array([p.mw_per_m3s / 3600 for p in cascade.plants])
    for i in reversed(cascade.order):
        for link in cascade.links:
            if link.source == cascade.plants[i].id:
                energy[i] += link.fraction * energy[cascade.index[link.target]]
    return {p.id: float(energy[i] * reference_price) for i, p in enumerate(cascade.plants)}


def forecasts(current_inflow, current_price, training_inflows, training_prices, horizon):
    """Persistence decaying toward training-only means. Current interval is already observed."""
    weights = 0.8 ** np.arange(horizon)
    flow = weights[:, None] * current_inflow + (1 - weights[:, None]) * training_inflows.mean(axis=0)
    price = weights * current_price + (1 - weights) * training_prices.mean()
    paths = []
    for name, prob, factor in (("dry", 0.25, 0.7), ("central", 0.5, 1.0), ("wet", 0.25, 1.3)):
        f, p = flow.copy(), price.copy()
        f[1:] *= factor
        p[1:] += (1 - factor) * 15
        paths.append(
            Scenario(name, prob, f, p, tuple(["observed-root"] + [f"{name}-{t}" for t in range(1, horizon)]))
        )
    return paths


def rolling_policy(
    cascade,
    inflows,
    prices,
    training_inflows,
    training_prices,
    horizon=12,
    stochastic=True,
    dt_seconds=3600,
    terminal_value=None,
):
    if not isinstance(horizon, int) or horizon < 1:
        raise ValueError("horizon must be a positive integer")
    state = CascadeState.initial(cascade)
    terminal_value = carryover_values(cascade) if terminal_value is None else terminal_value
    training_inflows = np.asarray(training_inflows)
    training_prices = np.asarray(training_prices)
    if (
        training_inflows.ndim != 2
        or training_inflows.shape[1] != len(cascade.plants)
        or len(training_prices) != len(training_inflows)
        or len(training_prices) == 0
    ):
        raise ValueError("Training data must be nonempty with matching dimensions")
    if (
        not np.isfinite(training_inflows).all()
        or not np.isfinite(training_prices).all()
        or (training_inflows < 0).any()
    ):
        raise ValueError("Invalid training observations")
    inflows = np.asarray(inflows)
    prices = np.asarray(prices)
    if prices.shape != (len(inflows),):
        raise ValueError("Rolling prices need one observed price per interval")
    records, audits = [], []
    terminal_min = {p.id: p.min_storage_m3 for p in cascade.plants}
    for t in range(len(inflows)):
        paths = forecasts(
            inflows[t], prices[t], training_inflows, training_prices, min(horizon, len(inflows) - t)
        )
        if stochastic:
            schedules, audit = stochastic_schedule(
                cascade, paths, dt_seconds, state, terminal_min, terminal_value
            )
            schedule = schedules["central"]
        else:
            path = paths[1]
            schedule = optimize(
                cascade, path.inflows, path.prices, dt_seconds, state, terminal_min, terminal_value
            )
            audit = schedule.audit
        first = (
            schedule.records[schedule.records.step == 0]
            .set_index("plant_id")
            .loc[[p.id for p in cascade.plants]]
        )
        state, frame, mass = step(
            cascade,
            state,
            inflows[t],
            first.turbine_m3s.to_numpy(),
            dt_seconds,
            first.bypass_m3s.to_numpy(),
            first.spill_m3s.to_numpy(),
            True,
        )
        frame["price_eur_mwh"] = prices[t]
        frame["revenue_eur"] = frame.energy_mwh * prices[t]
        records.append(frame.reset_index().assign(step=t))
        audits.append(dict(step=t, **mass, solver_seconds=audit["solve_seconds"]))
    return pd.concat(records, ignore_index=True), pd.DataFrame(audits), state


def evaluate_policies(trials=8, steps=48, horizon=12, seed=2026):
    """Held-out seeded paths; same initial states and terminal accounting for every policy."""
    if trials < 1 or steps < 1:
        raise ValueError("trials and steps must be positive")
    cascade = demo_cascade()
    training_flow, training_price = synthetic_series(168, seed)
    values = carryover_values(cascade)
    rows = []
    for trial in range(trials):
        inflows, prices = synthetic_series(steps, seed + 1000 + trial)
        baseline, audit, end = simulate(cascade, inflows)
        baseline["revenue_eur"] = baseline.energy_mwh * baseline.step.map(dict(enumerate(prices)))
        outputs = {"inflow_following": (baseline, audit, end)}
        for stochastic, name in ((False, "rolling_deterministic"), (True, "rolling_stochastic")):
            outputs[name] = rolling_policy(
                cascade,
                inflows,
                prices,
                training_flow,
                training_price,
                horizon,
                stochastic,
                terminal_value=values,
            )
        oracle = optimize(
            cascade, inflows, prices, terminal_min={p.id: 0 for p in cascade.plants}, terminal_value=values
        )
        outputs["perfect_foresight"] = (
            oracle.records,
            pd.DataFrame({"water_residual_m3": [oracle.audit["max_water_residual_m3"]]}),
            oracle.final_state,
        )
        for name, (frame, audit, end) in outputs.items():
            revenue = float(frame.revenue_eur.sum())
            shortfall = float(frame.environmental_shortfall_m3s.sum() * 3600)
            terminal = terminal_worth(cascade, end, values)
            rows.append(
                dict(
                    trial=trial,
                    policy=name,
                    revenue_eur=revenue,
                    terminal_value_eur=terminal,
                    environmental_shortfall_m3=shortfall,
                    objective_eur=revenue + terminal - 1000 * shortfall,
                    final_storage_m3=float(sum(end.storage_m3)),
                    max_water_residual_m3=float(audit.water_residual_m3.abs().max()),
                )
            )
    return pd.DataFrame(rows)


def water_value_sensitivity(cascade, inflows, prices, increment_m3=1000):
    """Finite-difference objective sensitivity with fixed terminal targets."""
    if not np.isfinite(increment_m3) or increment_m3 <= 0:
        raise ValueError("increment_m3 must be positive and finite")
    targets = {p.id: p.initial_storage_m3 for p in cascade.plants}
    base = optimize(cascade, inflows, prices, terminal_min=targets)
    rows = []
    for i, p in enumerate(cascade.plants):
        if p.initial_storage_m3 + increment_m3 > p.storage_capacity_m3:
            continue
        plants = list(cascade.plants)
        plants[i] = replace(p, initial_storage_m3=p.initial_storage_m3 + increment_m3)
        alternative = optimize(Cascade(tuple(plants), cascade.links), inflows, prices, terminal_min=targets)
        rows.append(
            dict(
                plant_id=p.id,
                increment_m3=increment_m3,
                marginal_value_eur_m3=(alternative.objective_eur - base.objective_eur) / increment_m3,
            )
        )
    return pd.DataFrame(rows)
