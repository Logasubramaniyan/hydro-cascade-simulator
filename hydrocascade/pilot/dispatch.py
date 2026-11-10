"""Deterministic unit commitment with storage-dependent hydraulic surfaces and independent replay."""

from dataclasses import dataclass
from types import SimpleNamespace
import numpy as np
import pandas as pd

from ..lp import OptimizationError
from ..model import finite
from ..operations.model import OperatingLimits
from ..operations.optimizer import _add_limits, _audit_dispatch
from ..scheduling import inputs, replay, terminal_worth, water_block
from .assets import UnitState, integer
from .solver import MILP


@dataclass
class UnitSchedule:
    records: pd.DataFrame
    unit_records: pd.DataFrame
    final_state: object
    final_unit_state: dict
    audit: dict
    revenue_eur: float
    objective_eur: float


def _surface(lp, unit, curve, volume_grid, flow_grid, on, mean_volume, mean_constant, capacity, key):
    """One selected triangle, not an unconstrained convex hull of a nonconcave power surface."""
    weights, selectors = [], []
    for a, b in zip(volume_grid[:-1], volume_grid[1:]):
        for q0, q1 in zip(flow_grid[:-1], flow_grid[1:]):
            for vertices in (((a, q0), (a, q1), (b, q1)), ((a, q0), (b, q0), (b, q1))):
                z = lp.binary((key, "triangle", len(selectors)))
                selectors.append(z)
                row = {z: -1}
                for s, q in vertices:
                    w = lp.var((key, "weight", len(weights)), upper=1)
                    row[w] = 1
                    weights.append((w, s, q, curve.power(unit, q, s, s)))
                lp.equality(row, 0)
    lp.equality(dict.fromkeys(selectors, 1) | {on: -1}, 0)
    # If off, all weights are zero and volume coupling is relaxed by physical capacity.
    row = mean_volume.copy()
    row.update({w: -s / 1000 for w, s, _, _ in weights})
    lp.inequality(row | {on: capacity / 1000}, capacity / 1000 - mean_constant)
    lp.inequality(
        {col: -v for col, v in row.items()} | {on: capacity / 1000}, capacity / 1000 + mean_constant
    )
    qcol, pcol = lp.var((key, "flow")), lp.var((key, "power"))
    lp.equality({w: -q for w, _, q, _ in weights} | {qcol: 1}, 0)
    lp.equality({w: -p for w, _, _, p in weights} | {pcol: 1}, 0)
    return qcol, pcol


def _commitment(lp, unit, initial, steps, dt, available):
    on, start, stop = {}, {}, {}
    for t in range(steps):
        on[t] = lp.binary((unit.id, t, "on"), unit.running_cost_eur_per_hour * dt / 3600)
        start[t] = lp.binary((unit.id, t, "start"), unit.startup_cost_eur)
        stop[t] = lp.binary((unit.id, t, "stop"), unit.shutdown_cost_eur)
        if not available[t]:
            lp.bounds[on[t]] = (0, 0)
        if t + unit.min_up_steps > steps:
            lp.bounds[start[t]] = (0, 0)
        if t + unit.min_down_steps > steps:
            lp.bounds[stop[t]] = (0, 0)
        minimum = unit.min_up_steps if initial.on else unit.min_down_steps
        if t < max(minimum - initial.age_steps, 0):
            lp.bounds[on[t]] = (
                (int(initial.on), int(initial.on)) if available[t] or not initial.on else (1, 0)
            )
        row = {on[t]: 1, start[t]: -1, stop[t]: 1}
        if t:
            row[on[t - 1]] = -1
        lp.equality(row, int(initial.on) if t == 0 else 0)
        lp.inequality({start[t]: 1, stop[t]: 1}, 1)
        lp.inequality({start[k]: 1 for k in range(max(0, t - unit.min_up_steps + 1), t + 1)} | {on[t]: -1}, 0)
        lp.inequality({stop[k]: 1 for k in range(max(0, t - unit.min_down_steps + 1), t + 1)} | {on[t]: 1}, 1)
    return on, start, stop


def _unit_audit(unit, frame, initial, dt):
    age, previous, last_flow, violation = initial.age_steps, initial.on, initial.flow_m3s, 0.0
    costs = 0.0
    for row in frame.itertuples():
        on = bool(row.on)
        started, stopped = int(on and not previous), int(previous and not on)
        if on != previous:
            duration = unit.min_up_steps if previous else unit.min_down_steps
            violation = max(violation, duration - age)
            age = 0
        minimum = unit.flow_m3s[0] if on else 0
        maximum = unit.flow_m3s[-1] * on
        violation = max(
            violation,
            minimum - row.flow_m3s,
            row.flow_m3s - maximum,
            abs(row.flow_m3s - last_flow) - unit.ramp_m3s_per_hour * dt / 3600,
            on - row.available,
            abs(row.start - started),
            abs(row.stop - stopped),
        )
        costs += started * unit.startup_cost_eur + stopped * unit.shutdown_cost_eur
        costs += on * unit.running_cost_eur_per_hour * dt / 3600
        age += 1
        previous, last_flow = on, row.flow_m3s
    if age < len(frame) and age < (unit.min_up_steps if previous else unit.min_down_steps):
        violation = max(violation, 1.0)
    return float(max(violation, 0)), UnitState(previous, age, last_flow), float(costs)


def _peak_export(cascade, curves, units, state, records, unit_records, steps):
    """Exact aggregate extrema for constant flows and piecewise-linear level/volume trajectories."""
    storage = records.set_index(["step", "plant_id"]).storage_m3
    flows = unit_records.set_index(["step", "unit_id"])
    peaks = []
    for t in range(steps):
        bounds, fractions = {}, {0.0, 1.0}
        for i, p in enumerate(cascade.plants):
            a = state.storage_m3[i] if t == 0 else storage.loc[(t - 1, p.id)]
            b = storage.loc[(t, p.id)]
            bounds[p.id] = (a, b)
            if abs(b - a) > 1e-10:
                fractions.update(
                    (v - a) / (b - a) for v in curves[p.id].storage_m3 if min(a, b) < v < max(a, b)
                )
        totals = []
        for fraction in fractions:
            power = 0.0
            for unit in units:
                a, b = bounds[unit.plant_id]
                volume = a + fraction * (b - a)
                row = flows.loc[(t, unit.id)]
                power += curves[unit.plant_id].power(unit, row.flow_m3s if row.on else 0, volume, volume)
            totals.append(power)
        peaks.append(max(totals))
    return np.asarray(peaks)


def optimize_units(
    cascade,
    inflows,
    prices,
    curves,
    units,
    dt_seconds=900,
    state=None,
    unit_state=None,
    limits=None,
    unit_availability=None,
    export_limit_mw=None,
    terminal_min=None,
    terminal_value=None,
    storage_segments=4,
    flow_segments=2,
    approximation_tolerance_mw=0.05,
    time_limit_seconds=60,
    relative_gap=1e-6,
):
    """Known-input asset dispatch; piecewise MILP accuracy is exposed by nonlinear hydraulic replay.

    Tailwater is fixed per reservoir; volume changes linearly within each interval, flows are constant.
    New run/rest obligations must finish inside the horizon. This is separate from the reserve LP.
    """
    inflows, prices, state = inputs(cascade, inflows, prices, state, dt_seconds)
    steps, n = inflows.shape
    for value, name in ((storage_segments, "storage_segments"), (flow_segments, "flow_segments")):
        integer(value, name)
        if value > 50:
            raise ValueError("Surface segments must be <= 50")
    finite(approximation_tolerance_mw, "approximation_tolerance_mw", 0)
    curve_map = {c.plant_id: c for c in curves}
    if len(curve_map) != len(curves) or set(curve_map) != set(cascade.index):
        raise ValueError("Curves must uniquely cover every plant")
    if (
        not units
        or len({u.id for u in units}) != len(units)
        or {u.plant_id for u in units} != set(cascade.index)
    ):
        raise ValueError("Unique units must cover every plant and reference only known plants")
    unit_state = (
        {u.id: UnitState(False, u.min_down_steps, 0) for u in units}
        if unit_state is None
        else dict(unit_state)
    )
    if set(unit_state) != {u.id for u in units}:
        raise ValueError("Initial state must cover every unit exactly")
    for i, p in enumerate(cascade.plants):
        curve = curve_map[p.id]
        if curve.storage_m3[0] > p.min_storage_m3 or curve.storage_m3[-1] < p.storage_capacity_m3:
            raise ValueError("Reservoir curve must cover physical storage bounds")
        if (
            abs(
                sum(unit_state[u.id].flow_m3s for u in units if u.plant_id == p.id)
                - state.last_turbine_m3s[i]
            )
            > 1e-7
        ):
            raise ValueError("Initial unit flow must match physical state's initial turbine flow")
    for u in units:
        if unit_state[u.id].on:
            u.efficiency_at(unit_state[u.id].flow_m3s)
        p = cascade.plants[cascade.index[u.plant_id]]
        curve_map[u.plant_id].power(u, u.flow_m3s[-1], p.min_storage_m3, p.min_storage_m3)
    unit_availability = {} if unit_availability is None else unit_availability
    if set(unit_availability) - {u.id for u in units}:
        raise ValueError("Unknown unit in availability")
    available = {}
    for u in units:
        values = np.asarray(unit_availability.get(u.id, np.ones(steps)), float)
        if values.shape != (steps,) or not np.isin(values, (0, 1)).all():
            raise ValueError("Unit availability must contain one binary value per interval")
        available[u.id] = values
    limits = OperatingLimits.standard(cascade, steps) if limits is None else limits
    limits.validate(cascade, steps)
    export = np.asarray(np.full(steps, 1e6) if export_limit_mw is None else export_limit_mw, float)
    if export.shape != (steps,) or not np.isfinite(export).all() or (export < 0).any():
        raise ValueError("Export limits must be finite nonnegative interval values")
    lp = MILP()
    block = water_block(lp, cascade, inflows, state, dt_seconds, "units", terminal_min, terminal_value)
    for col, value in _add_limits(lp, block, cascade, limits, steps, dt_seconds).items():
        lp.costs[col] -= value
    columns = {}
    for u in units:
        i, p = cascade.index[u.plant_id], cascade.plants[cascade.index[u.plant_id]]
        curve = curve_map[p.id]
        volumes = np.unique(
            np.r_[
                np.linspace(p.min_storage_m3, p.storage_capacity_m3, storage_segments + 1),
                [s for s in curve.storage_m3 if p.min_storage_m3 < s < p.storage_capacity_m3],
            ]
        )
        if len(volumes) < 2:
            raise ValueError("Asset-aware dispatch requires a nonzero storage range")
        flows = np.unique(np.r_[u.flow_m3s, np.linspace(u.flow_m3s[0], u.flow_m3s[-1], flow_segments + 1)])
        on, start, stop = _commitment(lp, u, unit_state[u.id], steps, dt_seconds, available[u.id])
        q, power = {}, {}
        for t in range(steps):
            mean = {block["v"][t, i]: 0.5}
            constant = state.storage_m3[i] / 2000 if t == 0 else 0
            if t:
                mean[block["v"][t - 1, i]] = 0.5
            q[t], power[t] = _surface(
                lp, u, curve, volumes, flows, on[t], mean, constant, p.storage_capacity_m3, (u.id, t)
            )
            lp.costs[power[t]] -= prices[t, i] * dt_seconds / 3600
            row = {q[t]: 1}
            previous = unit_state[u.id].flow_m3s if t == 0 else 0
            if t:
                row[q[t - 1]] = -1
            ramp = u.ramp_m3s_per_hour * dt_seconds / 3600
            lp.inequality(row, ramp + previous)
            lp.inequality({col: -v for col, v in row.items()}, ramp - previous)
        columns[u.id] = dict(on=on, start=start, stop=stop, flow=q, power=power)
    for t in range(steps):
        for i, p in enumerate(cascade.plants):
            lp.equality(
                {columns[u.id]["flow"][t]: 1 for u in units if u.plant_id == p.id} | {block["q"][t, i]: -1}, 0
            )
        lp.inequality({columns[u.id]["power"][t]: 1 for u in units}, export[t])
    solved = lp.solve(time_limit_seconds, relative_gap)
    records, final, _, water_error = replay(cascade, inflows, prices, state, dt_seconds, block, solved.x)
    rows, last_states, total_unit_cost, violation = [], {}, 0.0, 0.0
    for u in units:
        previous_storage = state.storage_m3[cascade.index[u.plant_id]]
        frame = records[records.plant_id == u.plant_id].sort_values("step")
        for row in frame.itertuples():
            t = row.step
            col = columns[u.id]
            q = max(float(solved.x[col["flow"][t]]), 0)
            on = int(round(solved.x[col["on"][t]]))
            actual = curve_map[u.plant_id].power(u, q if on else 0, previous_storage, row.storage_m3)
            rows.append(
                dict(
                    step=t,
                    unit_id=u.id,
                    plant_id=u.plant_id,
                    on=on,
                    start=int(round(solved.x[col["start"][t]])),
                    stop=int(round(solved.x[col["stop"][t]])),
                    available=available[u.id][t],
                    flow_m3s=q,
                    model_power_mw=max(float(solved.x[col["power"][t]]), 0),
                    replayed_power_mw=actual,
                )
            )
            previous_storage = row.storage_m3
        unit_frame = pd.DataFrame(rows).query("unit_id == @u.id")
        error, last_states[u.id], costs = _unit_audit(u, unit_frame, unit_state[u.id], dt_seconds)
        violation, total_unit_cost = max(violation, error), total_unit_cost + costs
    unit_records = pd.DataFrame(rows)
    if violation > 1e-5:
        raise OptimizationError("Independent unit commitment audit failed")
    grouped = unit_records.groupby(["step", "plant_id"])[
        ["model_power_mw", "replayed_power_mw", "flow_m3s"]
    ].sum()
    for row in records.itertuples():
        values = grouped.loc[(row.step, row.plant_id)]
        if abs(values.flow_m3s - row.turbine_m3s) > 1e-5:
            raise OptimizationError("Unit flow and independently replayed turbine flow disagree")
    records = records.drop(columns=["power_mw", "energy_mwh", "revenue_eur"]).merge(
        grouped.drop(columns="flow_m3s").reset_index(), on=["step", "plant_id"], validate="one_to_one"
    )
    records["power_mw"] = records.replayed_power_mw
    records["energy_mwh"] = records.power_mw * dt_seconds / 3600
    records["revenue_eur"] = records.energy_mwh * records.price_eur_mwh
    totals = records.groupby("step")[["model_power_mw", "power_mw"]].sum()
    offer = pd.DataFrame(dict(energy_mw=totals.model_power_mw, reserve_up_mw=0, reserve_down_mw=0))
    physical = _audit_dispatch(
        cascade,
        records,
        offer,
        SimpleNamespace(activation_up=np.zeros(steps), activation_down=np.zeros(steps)),
        limits,
        dt_seconds,
    )
    revenue = float((records.model_power_mw * records.price_eur_mwh).sum() * dt_seconds / 3600)
    carryover = terminal_worth(cascade, final, terminal_value)
    objective = revenue + carryover - total_unit_cost - physical["release_variation_cost_eur"]
    constant = sum(
        sum(queue[steps:]) * (terminal_value or {}).get(link.target, 0)
        for queue, link in zip(state.queues_m3, cascade.links)
    )
    if abs(objective - (-solved.fun + constant)) > max(1e-4, abs(objective) * 1e-8):
        raise OptimizationError("Independent unit objective reconstruction failed")
    error = float((unit_records.model_power_mw - unit_records.replayed_power_mw).abs().max())
    plant_error = float((records.model_power_mw - records.replayed_power_mw).abs().max())
    peak_export = _peak_export(cascade, curve_map, units, state, records, unit_records, steps)
    export_violation = float(np.maximum(peak_export - export, 0).max())
    accepted = (
        max(error, plant_error, physical["max_delivery_error_mw"]) <= approximation_tolerance_mw
        and export_violation <= 1e-5
    )
    audit = dict(
        solved.audit,
        **physical,
        max_water_residual_m3=water_error,
        max_unit_violation=violation,
        max_power_approximation_error_mw=max(error, plant_error, physical["max_delivery_error_mw"]),
        actual_export_violation_mw=export_violation,
        peak_export_mw=peak_export.tolist(),
        approximation_tolerance_mw=approximation_tolerance_mw,
        hydraulic_review_status="pass" if accepted else "hold",
        modeled_revenue_eur=revenue,
        hydraulic_replay_revenue_eur=float(records.revenue_eur.sum()),
        unit_cost_eur=total_unit_cost,
        terminal_water_eur=carryover,
        full_objective_eur=objective,
        information="deterministic supplied inputs; not an online control policy",
        hydraulics="triangulated mean-storage/flow surface; exact level integral on linear volume path; fixed tailwater",
        terminal_commitment="new run/rest obligations complete inside horizon; inherited state exported",
        qualification="read-only planning and validation prototype; not market or TSO approval",
    )
    return UnitSchedule(records, unit_records, final, last_states, audit, revenue, float(objective))
