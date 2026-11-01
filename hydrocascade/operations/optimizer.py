"""Two-stage offers, nonanticipative dispatch and lower-tail cashflow risk using sparse LP."""

from dataclasses import dataclass
import numpy as np
import pandas as pd

from ..lp import LP, OptimizationError
from ..model import finite
from ..scheduling import water_block, replay, terminal_worth
from .model import validate_scenarios


def lower_tail_mean(values, probabilities, confidence=0.9):
    """Exact probability-weighted lower (1-confidence) tail mean, including fractional atoms."""
    values, probabilities = np.asarray(values, float), np.asarray(probabilities, float)
    finite(confidence, "confidence", 0)
    if confidence >= 1 or values.ndim != 1 or values.shape != probabilities.shape or not len(values):
        raise ValueError("Tail inputs must be nonempty vectors and confidence must be below one")
    if (
        not np.isfinite(values).all()
        or not np.isfinite(probabilities).all()
        or (probabilities <= 0).any()
        or not np.isclose(probabilities.sum(), 1, rtol=0, atol=1e-10)
    ):
        raise ValueError("Finite values and positive probabilities summing to one are required")
    remaining, result = 1 - confidence, 0.0
    for i in np.argsort(values):
        weight = min(remaining, probabilities[i])
        result += weight * values[i]
        remaining -= weight
        if remaining <= 1e-14:
            break
    return float(result / (1 - confidence))


@dataclass
class OfferResult:
    offers: pd.DataFrame
    outcomes: pd.DataFrame
    schedules: dict
    final_states: dict
    audit: dict


def _release(block, t, i):
    return {block[kind][t, i]: 1.0 for kind in ("q", "b", "s")}


def _add_limits(lp, block, cascade, limits, steps, dt_seconds):
    variations = {}
    for t in range(steps):
        for i, plant in enumerate(cascade.plants):
            lp.bounds[block["q"][t, i]] = (0, plant.max_flow_m3s * limits.availability[t, i])
            lp.bounds[block["deficit"][t, i]] = (0, 0)  # Strict environmental minimum, no soft compliance.
            col = block["v"][t, i]
            lower, upper = lp.bounds[col]
            lower = max(lower, limits.min_storage_m3[t, i] / 1000)
            upper = min(upper, limits.max_storage_m3[t, i] / 1000)
            if lower > upper:
                raise ValueError("Terminal target conflicts with time-dependent storage limits")
            lp.bounds[col] = (lower, upper)
            release = _release(block, t, i)
            lp.inequality(release, limits.max_release_m3s[t, i])
            lp.inequality({col: -v for col, v in release.items()}, -limits.min_release_m3s[t, i])
            change = release.copy()
            previous = limits.initial_release_m3s[i] if t == 0 else 0.0
            if t:
                change.update({col: -v for col, v in _release(block, t - 1, i).items()})
            ramp = limits.release_ramp_m3s_per_hour[i] * dt_seconds / 3600
            lp.inequality(change, ramp + previous)
            lp.inequality({col: -v for col, v in change.items()}, ramp - previous)
            if limits.variation_cost_eur_per_m3s[i] > 0:
                d = lp.var(("variation", block["q"][t, i]))
                variations[d] = -limits.variation_cost_eur_per_m3s[i]
                lp.inequality(change | {d: -1}, previous)
                lp.inequality({col: -v for col, v in change.items()} | {d: -1}, -previous)
    return variations


def _audit_dispatch(cascade, records, offers, scenario, limits, dt):
    steps, n = len(offers), len(cascade.plants)
    release, storage, turbine = (np.zeros((steps, n)) for _ in range(3))
    for i, plant in enumerate(cascade.plants):
        frame = records[records.plant_id == plant.id].sort_values("step")
        release[:, i] = frame[["turbine_m3s", "bypass_m3s", "spill_m3s"]].sum(axis=1)
        storage[:, i] = frame.storage_m3
        turbine[:, i] = frame.turbine_m3s
    differences = np.diff(np.vstack((limits.initial_release_m3s, release)), axis=0)
    violations = dict(
        max_release_limit_violation_m3s=float(
            max(
                np.max(limits.min_release_m3s - release, initial=0),
                np.max(release - limits.max_release_m3s, initial=0),
            )
        ),
        max_storage_limit_violation_m3=float(
            max(
                np.max(limits.min_storage_m3 - storage, initial=0),
                np.max(storage - limits.max_storage_m3, initial=0),
            )
        ),
        max_release_ramp_violation_m3s=float(
            np.max(np.abs(differences) - limits.release_ramp_m3s_per_hour * dt / 3600, initial=0)
        ),
        max_availability_violation_m3s=float(
            np.max(turbine - limits.availability * [p.max_flow_m3s for p in cascade.plants], initial=0)
        ),
        environmental_shortfall_m3s=float(records.environmental_shortfall_m3s.max()),
    )
    if max(violations.values()) > 1e-5:
        raise OptimizationError(f"Independent operating-limit audit failed: {violations}")
    delivered = records.groupby("step").power_mw.sum().to_numpy()
    target = (
        offers.energy_mw
        + scenario.activation_up * offers.reserve_up_mw
        - scenario.activation_down * offers.reserve_down_mw
    )
    errors = delivered - target.to_numpy()
    variation_cost = float((np.abs(differences) * limits.variation_cost_eur_per_m3s).sum())
    return dict(
        **violations,
        max_delivery_error_mw=float(np.abs(errors).max(initial=0)),
        undelivered_energy_mwh=float(np.maximum(-errors, 0).sum() * dt / 3600),
        excess_energy_mwh=float(np.maximum(errors, 0).sum() * dt / 3600),
        release_variation_cost_eur=variation_cost,
    )


def optimize_offers(
    cascade,
    scenarios,
    terms,
    limits,
    dt_seconds=900,
    state=None,
    terminal_min=None,
    terminal_value=None,
    risk_weight=0.35,
    confidence=0.9,
    fixed_offers=None,
    allow_shortfall=False,
):
    """Firm energy and reserve quantities chosen before auction outcomes and activation.

    Capacity prices are assumed known and accepted; no bid-curve clearing or TSO qualification
    is claimed. Recourse may use only information revealed by each validated scenario-tree node.
    """
    finite(risk_weight, "risk_weight", 0)
    finite(confidence, "confidence", 0)
    if risk_weight >= 1 or confidence >= 1:
        raise ValueError("risk_weight and confidence must be below one")
    steps, prepared, node_paths = validate_scenarios(cascade, scenarios, state, dt_seconds)
    terms.validate(steps)
    limits.validate(cascade, steps)
    hours, lp = dt_seconds / 3600, LP()
    energy, up, down = {}, {}, {}
    maximum = sum(p.capacity_mw for p in cascade.plants)
    for t in range(steps):
        energy[t] = lp.var(("offer", t, "energy"), upper=min(maximum, terms.export_limit_mw[t]))
        up[t] = lp.var(("offer", t, "up"), upper=min(maximum, terms.max_up_mw[t]))
        down[t] = lp.var(("offer", t, "down"), upper=min(maximum, terms.max_down_mw[t]))
        lp.inequality({energy[t]: 1, up[t]: 1}, min(maximum, terms.export_limit_mw[t]))
        lp.inequality({energy[t]: -1, down[t]: 1}, 0)
    if fixed_offers is not None:
        names = ("energy_mw", "reserve_up_mw", "reserve_down_mw")
        if not set(names) <= set(fixed_offers) or len(fixed_offers) != steps:
            raise ValueError("Fixed offers require a complete ordered horizon with energy/up/down columns")
        if "step" in fixed_offers and fixed_offers.step.tolist() != list(range(steps)):
            raise ValueError("Fixed offer steps must be contiguous and ordered from zero")
        for mapping, name in zip((energy, up, down), names):
            for t, value in enumerate(fixed_offers[name]):
                finite(value, name, 0)
                lo, hi = lp.bounds[mapping[t]]
                if value > hi + 1e-7:
                    raise ValueError("Fixed offer exceeds connection, capacity or product limit")
                lp.bounds[mapping[t]] = (float(value), float(value))
    blocks, profits, constants, shared, deviations = {}, {}, {}, {}, []
    for scenario in scenarios:
        inflows, initial = prepared[scenario.name]
        before = len(lp.keys)
        block = water_block(
            lp, cascade, inflows, initial, dt_seconds, scenario.name, terminal_min, terminal_value
        )
        profit = {i: -lp.costs[i] for i in range(before, len(lp.keys)) if lp.costs[i]}
        profit.update(_add_limits(lp, block, cascade, limits, steps, dt_seconds))
        constants[scenario.name] = sum(
            sum(queue[steps:]) * (terminal_value or {}).get(link.target, 0)
            for queue, link in zip(initial.queues_m3, cascade.links)
        )
        for t in range(steps):
            profit[energy[t]] = hours * scenario.day_ahead_eur_mwh[t]
            profit[up[t]] = hours * (
                terms.up_capacity_eur_mw_h[t] + scenario.activation_up[t] * scenario.up_energy_eur_mwh[t]
            )
            profit[down[t]] = hours * (
                terms.down_capacity_eur_mw_h[t]
                - scenario.activation_down[t] * scenario.down_energy_eur_mwh[t]
            )
            balance = {block["q"][t, i]: p.mw_per_m3s for i, p in enumerate(cascade.plants)}
            balance.update(
                {energy[t]: -1, up[t]: -scenario.activation_up[t], down[t]: scenario.activation_down[t]}
            )
            if allow_shortfall:
                for sign in (1, -1):
                    deviation = lp.var((scenario.name, t, "deviation", sign))
                    balance[deviation] = sign
                    deviations.append(deviation)
            lp.equality(balance, 0)
            # A connection rating applies to actual delivery, including certification recourse.
            lp.inequality(
                {block["q"][t, i]: p.mw_per_m3s for i, p in enumerate(cascade.plants)},
                terms.export_limit_mw[t],
            )
            node = (t, node_paths[scenario.name][t])
            if node in shared:
                reference = shared[node]
                for kind in ("q", "b", "s", "deficit"):
                    for i in range(len(cascade.plants)):
                        lp.equality({block[kind][t, i]: 1, reference[kind][t, i]: -1}, 0)
            else:
                shared[node] = block
        blocks[scenario.name], profits[scenario.name] = block, profit
    # Minimize negative (mean / lower-tail-profit mixture). Expected coefficient remains
    # positive for every scenario, making the release-variation epigraph tight.
    lp.costs = [0.0] * len(lp.keys)
    for scenario in scenarios:
        for col, value in profits[scenario.name].items():
            lp.costs[col] -= (1 - risk_weight) * scenario.probability * value
    if risk_weight:
        eta_plus = lp.var(("risk", "eta_plus"), cost=-risk_weight)
        eta_minus = lp.var(("risk", "eta_minus"), cost=risk_weight)
        for scenario in scenarios:
            if scenario.role == "security":
                continue
            shortfall = lp.var(
                ("risk", scenario.name), cost=risk_weight * scenario.probability / (1 - confidence)
            )
            row = {col: -value for col, value in profits[scenario.name].items()}
            row.update({eta_plus: 1, eta_minus: -1, shortfall: -1})
            lp.inequality(row, constants[scenario.name])
    if allow_shortfall:
        # Lexicographic assessment: delivery error first, then economic dispatch. A large
        # arbitrary penalty could trade an undelivered reserve against terminal water value.
        economics = lp.costs.copy()
        lp.costs = [hours if i in deviations else 0.0 for i in range(len(lp.keys))]
        first = lp.solve()
        lp.inequality(dict.fromkeys(deviations, hours), max(float(first.fun), 0) + 1e-8)
        lp.costs = economics
    solved = lp.solve()
    offers = pd.DataFrame(
        {
            "step": range(steps),
            "energy_mw": [max(solved.x[energy[t]], 0.0) for t in range(steps)],
            "reserve_up_mw": [max(solved.x[up[t]], 0.0) for t in range(steps)],
            "reserve_down_mw": [max(solved.x[down[t]], 0.0) for t in range(steps)],
        }
    )
    schedules, final_states, outcomes = {}, {}, []
    for scenario in scenarios:
        inflows, initial = prepared[scenario.name]
        records, final, _, water_error = replay(
            cascade, inflows, np.zeros_like(inflows), initial, dt_seconds, blocks[scenario.name], solved.x
        )
        physical = _audit_dispatch(cascade, records, offers, scenario, limits, dt_seconds)
        capacity = float(
            hours
            * (
                offers.reserve_up_mw * terms.up_capacity_eur_mw_h
                + offers.reserve_down_mw * terms.down_capacity_eur_mw_h
            ).sum()
        )
        day_ahead = float(hours * (offers.energy_mw * scenario.day_ahead_eur_mwh).sum())
        activation = float(
            hours
            * (
                offers.reserve_up_mw * scenario.activation_up * scenario.up_energy_eur_mwh
                - offers.reserve_down_mw * scenario.activation_down * scenario.down_energy_eur_mwh
            ).sum()
        )
        carryover = terminal_worth(cascade, final, terminal_value)
        profit = day_ahead + capacity + activation + carryover - physical["release_variation_cost_eur"]
        algebra = (
            sum(value * solved.x[col] for col, value in profits[scenario.name].items())
            + constants[scenario.name]
        )
        if scenario.role == "forecast" and abs(profit - algebra) > max(1e-4, abs(profit) * 1e-8):
            raise OptimizationError("Independent scenario cashflow reconstruction failed")
        if not allow_shortfall and physical["max_delivery_error_mw"] > 1e-5:
            raise OptimizationError("Firm delivery audit failed")
        outcomes.append(
            dict(
                scenario=scenario.name,
                probability=scenario.probability,
                role=scenario.role,
                day_ahead_eur=day_ahead,
                capacity_eur=capacity,
                activation_eur=activation,
                terminal_water_eur=carryover,
                terminal_adjusted_profit_eur=profit,
                max_water_residual_m3=water_error,
                **physical,
            )
        )
        schedules[scenario.name], final_states[scenario.name] = records, final
    outcomes = pd.DataFrame(outcomes)
    mean = float((outcomes.probability * outcomes.terminal_adjusted_profit_eur).sum())
    forecast_outcomes = outcomes[outcomes.role == "forecast"]
    tail = lower_tail_mean(
        forecast_outcomes.terminal_adjusted_profit_eur, forecast_outcomes.probability, confidence
    )
    utility = (1 - risk_weight) * mean + risk_weight * tail
    expected_constant = sum(s.probability * constants[s.name] for s in scenarios)
    if abs(utility - (-solved.fun + (1 - risk_weight) * expected_constant)) > max(1e-4, abs(utility) * 1e-8):
        raise OptimizationError("Independent mean/CVaR objective audit failed")
    return OfferResult(
        offers,
        outcomes,
        schedules,
        final_states,
        dict(
            solved.audit,
            expected_profit_eur=mean,
            lower_tail_profit_eur=tail,
            utility_eur=utility,
            risk_weight=risk_weight,
            confidence=confidence,
            dt_seconds=dt_seconds,
            forecast_scenarios=int((outcomes.role == "forecast").sum()),
            security_scenarios=int((outcomes.role == "security").sum()),
            dispatch_information_nodes=len(shared),
            information="offers before auction; dispatch at validated observed-history nodes",
            environmental_constraints="hard; total turbine+bypass+spill release",
            qualification="research assessment only; not TSO prequalification or executable market bids",
            max_delivery_error_mw=float(outcomes.max_delivery_error_mw.max()),
            max_water_residual_m3=float(outcomes.max_water_residual_m3.max()),
            deliverable_on_tested_paths=bool(outcomes.max_delivery_error_mw.max() <= 1e-5),
            cashflow_scope="assumed accepted quantities; no imbalance or nondelivery settlement",
        ),
    )


def assess_offer(cascade, scenarios, terms, limits, offers, **kwargs):
    """Minimum-delivery-error stress assessment; not a probabilistic guarantee outside these paths."""
    return optimize_offers(
        cascade, scenarios, terms, limits, fixed_offers=offers, allow_shortfall=True, risk_weight=0, **kwargs
    )
