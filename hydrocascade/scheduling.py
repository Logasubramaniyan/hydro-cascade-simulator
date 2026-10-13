"""Deterministic and finite scenario-tree optimization for a price-taking producer."""

from dataclasses import dataclass
import numpy as np
import pandas as pd
from .lp import LP
from .model import finite
from .simulation import CascadeState, array, step


@dataclass
class Schedule:
    records: pd.DataFrame
    audit: dict
    final_state: CascadeState
    revenue_eur: float
    objective_eur: float


@dataclass(frozen=True)
class Scenario:
    name: str
    probability: float
    inflows: np.ndarray
    prices: np.ndarray
    nodes: tuple[str, ...]


def terminal_worth(cascade, state, values):
    values = values or {}
    amount = sum(state.storage_m3[i] * values.get(p.id, 0) for i, p in enumerate(cascade.plants))
    amount += sum(
        sum(queue) * values.get(link.target, 0) for queue, link in zip(state.queues_m3, cascade.links)
    )
    return float(amount)


def water_block(
    lp,
    cascade,
    inflows,
    state,
    dt,
    prefix,
    terminal_min=None,
    terminal_value=None,
    probability=1.0,
    shortfall_penalty=1000.0,
):
    """Linear water constraints; storage is internally scaled to 1000 m3.

    Plant losses are prescribed withdrawals and must be physically feasible in optimization.
    Positive-cost environmental slack diagnoses infeasibility without inventing water.
    """
    t_count, n = inflows.shape
    k = dt / 1000
    terminal_min = (
        {p.id: p.initial_storage_m3 for p in cascade.plants} if terminal_min is None else terminal_min
    )
    terminal_value = {} if terminal_value is None else terminal_value
    ids = set(cascade.index)
    if not set(terminal_min) <= ids or not set(terminal_value) <= ids:
        raise ValueError("Unknown plant in terminal assumptions")
    for p in cascade.plants:
        target = terminal_min.get(p.id, p.min_storage_m3)
        finite(target, "terminal_min", p.min_storage_m3)
        if target > p.storage_capacity_m3:
            raise ValueError("Terminal target exceeds capacity")
        finite(terminal_value.get(p.id, 0), "terminal water value", 0)
    q, b, s, v, deficit = {}, {}, {}, {}, {}
    for t in range(t_count):
        for i, p in enumerate(cascade.plants):
            q[t, i] = lp.var((prefix, t, i, "q"), upper=p.max_flow_m3s)
            b[t, i] = lp.var((prefix, t, i, "b"))
            s[t, i] = lp.var((prefix, t, i, "s"))
            lower = terminal_min.get(p.id, p.min_storage_m3) if t == t_count - 1 else p.min_storage_m3
            v[t, i] = lp.var(
                (prefix, t, i, "v"),
                lower=lower / 1000,
                upper=p.storage_capacity_m3 / 1000,
                cost=-probability * 1000 * terminal_value.get(p.id, 0) if t == t_count - 1 else 0,
            )
            deficit[t, i] = lp.var(
                (prefix, t, i, "deficit"), upper=p.min_release_m3s, cost=probability * shortfall_penalty * dt
            )
    for t in range(t_count):
        for i, p in enumerate(cascade.plants):
            coeff = {v[t, i]: 1, q[t, i]: k, b[t, i]: k, s[t, i]: k}
            rhs = inflows[t, i] * k - p.loss_m3s * k
            if t:
                coeff[v[t - 1, i]] = -1
            else:
                rhs += state.storage_m3[i] / 1000
            for j, link in enumerate(cascade.links):
                if link.target == p.id:
                    earlier = t - link.delay_steps
                    if earlier >= 0:
                        source = cascade.index[link.source]
                        for control in (q, b, s):
                            col = control[earlier, source]
                            coeff[col] = coeff.get(col, 0) - k * link.fraction
                    elif t < len(state.queues_m3[j]):
                        rhs += state.queues_m3[j][t] / 1000
            lp.equality(coeff, rhs)
            lp.inequality({q[t, i]: -1, b[t, i]: -1, s[t, i]: -1, deficit[t, i]: -1}, -p.min_release_m3s)
            if p.ramp_m3s_per_step is not None:
                if t:
                    lp.inequality({q[t, i]: 1, q[t - 1, i]: -1}, p.ramp_m3s_per_step)
                    lp.inequality({q[t, i]: -1, q[t - 1, i]: 1}, p.ramp_m3s_per_step)
                else:
                    lp.inequality({q[t, i]: 1}, state.last_turbine_m3s[i] + p.ramp_m3s_per_step)
                    lp.inequality({q[t, i]: -1}, p.ramp_m3s_per_step - state.last_turbine_m3s[i])
    # Credit end-of-horizon water travelling to a reservoir, so delay cannot erase carryover value.
    for link in cascade.links:
        source = cascade.index[link.source]
        value = terminal_value.get(link.target, 0)
        for t in range(max(0, t_count - link.delay_steps), t_count):
            for control in (q, b, s):
                lp.costs[control[t, source]] -= probability * value * dt * link.fraction
    return dict(q=q, b=b, s=s, v=v, deficit=deficit)


def replay(cascade, inflows, prices, state, dt, variables, x):
    records, max_residual = [], 0.0
    revenue = 0.0
    for t, inflow in enumerate(inflows):
        controls = {
            kind: [max(x[variables[kind][t, i]], 0) for i in range(len(cascade.plants))]
            for kind in ("q", "b", "s")
        }
        state, frame, audit = step(
            cascade, state, inflow, controls["q"], dt, controls["b"], controls["s"], enforce_controls=True
        )
        for i, p in enumerate(cascade.plants):
            if abs(state.storage_m3[i] - 1000 * x[variables["v"][t, i]]) > 1e-4:
                raise RuntimeError("Optimizer and physical replay disagree")
            frame.loc[p.id, "price_eur_mwh"] = prices[t, i]
            frame.loc[p.id, "revenue_eur"] = frame.loc[p.id, "energy_mwh"] * prices[t, i]
        revenue += frame.revenue_eur.sum()
        max_residual = max(max_residual, abs(audit["water_residual_m3"]))
        records.append(frame.reset_index().assign(step=t, transit_m3=audit["transit_m3"]))
    return pd.concat(records, ignore_index=True), state, float(revenue), max_residual


def inputs(cascade, inflows, prices, state, dt):
    finite(dt, "dt_seconds", 1e-9)
    inflows = np.asarray(inflows, dtype=float)
    if inflows.ndim != 2 or len(inflows) == 0:
        raise ValueError("Inflows require at least one interval")
    inflows = array(inflows, (len(inflows), len(cascade.plants)), "inflows")
    prices = np.asarray(prices, dtype=float)
    if prices.shape == (len(inflows),):
        prices = np.repeat(prices[:, None], len(cascade.plants), axis=1)
    prices = array(prices, inflows.shape, "prices", nonnegative=False)
    state = CascadeState.initial(cascade) if state is None else state.copy()
    state.validate(cascade)
    return inflows, prices, state


def optimize(
    cascade,
    inflows,
    prices,
    dt_seconds=3600,
    state=None,
    terminal_min=None,
    terminal_value=None,
    shortfall_penalty=1000.0,
):
    """Known-future benchmark. Defaults to preserving each plant's configured initial storage."""
    finite(shortfall_penalty, "shortfall_penalty", 1e-9)
    inflows, prices, state = inputs(cascade, inflows, prices, state, dt_seconds)
    lp = LP()
    variables = water_block(
        lp,
        cascade,
        inflows,
        state,
        dt_seconds,
        "deterministic",
        terminal_min,
        terminal_value,
        shortfall_penalty=shortfall_penalty,
    )
    for (t, i), col in variables["q"].items():
        lp.costs[col] -= prices[t, i] * cascade.plants[i].mw_per_m3s * dt_seconds / 3600
    solved = lp.solve()
    records, final, revenue, water_error = replay(
        cascade, inflows, prices, state, dt_seconds, variables, solved.x
    )
    audit = dict(
        solved.audit,
        max_water_residual_m3=water_error,
        information="perfect foresight",
        terminal_min_m3=terminal_min,
        terminal_value_eur_m3=terminal_value,
        environmental_shortfall_m3=float(records.environmental_shortfall_m3s.sum() * dt_seconds),
    )
    objective = (
        revenue
        + terminal_worth(cascade, final, terminal_value)
        - shortfall_penalty * audit["environmental_shortfall_m3"]
    )
    audit["full_objective_eur"] = objective
    return Schedule(records, audit, final, revenue, float(objective))


def stochastic_schedule(
    cascade,
    scenarios,
    dt_seconds=3600,
    state=None,
    terminal_min=None,
    terminal_value=None,
    shortfall_penalty=1000.0,
):
    """Finite scenario tree. Equal node labels share q/b/spill/deficit decisions.

    Inflow and price at a node are observed before its decision (hazard-decision timing).
    Labels must encode history: merged nodes with different parents are rejected.
    """
    finite(shortfall_penalty, "shortfall_penalty", 1e-9)
    if not scenarios or len({s.name for s in scenarios}) != len(scenarios):
        raise ValueError("Scenarios need unique names")
    if any(not np.isfinite(s.probability) or s.probability <= 0 for s in scenarios):
        raise ValueError("Scenario probabilities must be positive")
    if not np.isclose(sum(s.probability for s in scenarios), 1):
        raise ValueError("Scenario probabilities must sum to one")
    lp = LP()
    blocks, prepared, node_data = {}, {}, {}
    horizon = None
    for scenario in scenarios:
        inflows, prices, initial = inputs(cascade, scenario.inflows, scenario.prices, state, dt_seconds)
        if horizon is None:
            horizon = len(inflows)
        if len(inflows) != horizon or len(scenario.nodes) != horizon or any(not n for n in scenario.nodes):
            raise ValueError("Scenario horizons and node paths must match")
        variables = water_block(
            lp,
            cascade,
            inflows,
            initial,
            dt_seconds,
            scenario.name,
            terminal_min,
            terminal_value,
            scenario.probability,
            shortfall_penalty,
        )
        blocks[scenario.name] = variables
        prepared[scenario.name] = (inflows, prices, initial)
        for (t, i), col in variables["q"].items():
            lp.costs[col] -= (
                scenario.probability * prices[t, i] * cascade.plants[i].mw_per_m3s * dt_seconds / 3600
            )
        for t, node in enumerate(scenario.nodes):
            key = (t, node)
            history = scenario.nodes[:t]
            if key in node_data:
                reference, parent, ref_inflow, ref_price = node_data[key]
                if (
                    history != parent
                    or not np.array_equal(ref_inflow, inflows[t])
                    or not np.array_equal(ref_price, prices[t])
                ):
                    raise ValueError("Shared nodes must have identical observed inputs and history")
                for i in range(len(cascade.plants)):
                    for kind in ("q", "b", "s", "deficit"):
                        lp.equality({variables[kind][t, i]: 1, reference[kind][t, i]: -1}, 0)
            else:
                node_data[key] = (variables, history, inflows[t], prices[t])
    solved = lp.solve()
    schedules = {}
    expected_objective = 0.0
    for scenario in scenarios:
        inflows, prices, initial = prepared[scenario.name]
        records, final, revenue, error = replay(
            cascade, inflows, prices, initial, dt_seconds, blocks[scenario.name], solved.x
        )
        shortfall = float(records.environmental_shortfall_m3s.sum() * dt_seconds)
        objective = revenue + terminal_worth(cascade, final, terminal_value) - shortfall_penalty * shortfall
        expected_objective += scenario.probability * objective
        schedules[scenario.name] = Schedule(
            records,
            dict(solved.audit, max_water_residual_m3=error, environmental_shortfall_m3=shortfall),
            final,
            revenue,
            objective,
        )
    return schedules, dict(
        solved.audit,
        expected_objective_eur=expected_objective,
        scenario_count=len(scenarios),
        information="nonanticipative scenario tree",
    )
