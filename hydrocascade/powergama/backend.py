"""Pinned PowerGAMA DC dispatch, with one shared hydro formulation and independent replay.

Imports optional libraries only when this backend is selected. PowerGAMA native storage is
zero for mapped hydro generators: no independent MWh reservoir is allowed to double count water.
"""

from dataclasses import dataclass
from contextlib import contextmanager
import gc
from importlib import metadata
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import numpy as np
import pandas as pd

from . import POWERGAMA_COMMIT
from .network import Network
from ..lp import LP, OptimizationError
from ..model import finite
from ..scheduling import water_block, replay, terminal_worth, inputs
from ..simulation import CascadeState

try:
    import powergama
    from powergama.lp.extension import ModelExtension
    from powergama import constants as pg_constants
    import pyomo.environ as pyo
except ImportError as exc:
    raise ImportError(
        "Install the pinned optional backend: pip install -e '.[powergama]' (Python 3.11+)"
    ) from exc


def dependency_provenance():
    """Fail closed on unreviewed source/API changes, not just a matching development version."""
    dist = metadata.distribution("powergama")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    revision = direct.get("vcs_info", {}).get("commit_id")
    if revision != POWERGAMA_COMMIT:
        raise RuntimeError(
            f"PowerGAMA must be installed from pinned commit {POWERGAMA_COMMIT}; got {revision}"
        )
    return dict(
        powergama_version=dist.version,
        powergama_commit=revision,
        pyomo_version=metadata.version("pyomo"),
        highspy_version=metadata.version("highspy"),
    )


@contextmanager
def _scratch_directory():
    # The pinned upstream SQLite helpers use transactional contexts without explicitly
    # closing their short-lived connections. Collect those unreachable handles before
    # TemporaryDirectory cleanup, especially on Windows; never hide a cleanup failure.
    with TemporaryDirectory() as temporary:
        try:
            yield Path(temporary)
        finally:
            gc.collect()


def _grid(cascade, network, intervals, dt_seconds):
    """Use PowerGAMA's documented dataframe constructor with complete required/default columns."""
    grid = powergama.GridData()
    profiles = pd.DataFrame({"constant": np.ones(intervals)})
    generators, hydro_indices = [], {}
    for i, plant in enumerate(cascade.plants):
        hydro_indices[plant.id] = i
        generators.append(
            dict(
                type="hydro_cascade",
                desc=plant.id,
                node=network.hydro_buses[plant.id],
                pmax=plant.capacity_mw,
                pmin=0.0,
                fuelcost=0.0,
                inflow_fac=1.0,
                inflow_ref="constant",
                storage_cap=0.0,
                storage_ini=0.0,
            )
        )
    for unit in network.thermal:
        generators.append(
            dict(
                type="thermal",
                desc=unit.id,
                node=unit.bus,
                pmax=unit.capacity_mw,
                pmin=0.0,
                fuelcost=unit.marginal_cost_eur_mwh,
                inflow_fac=1.0,
                inflow_ref="constant",
            )
        )
    for i, bus in enumerate(network.buses):
        availability = np.asarray(network.renewable_available_mw)[:, i]
        maximum = float(availability.max(initial=0))
        profiles[f"renewable_{i}"] = availability / maximum if maximum else 0.0
        profiles[f"demand_{i}"] = np.asarray(network.demand_mw)[:, i]
        generators.append(
            dict(
                type="renewable",
                desc=f"renewable_{bus.id}",
                node=bus.id,
                pmax=maximum,
                pmin=0.0,
                fuelcost=0.0,
                inflow_fac=1.0,
                inflow_ref=f"renewable_{i}",
            )
        )
    tables = dict(
        node=pd.DataFrame(
            [dict(id=bus.id, area=bus.area, lat=bus.latitude, lon=bus.longitude) for bus in network.buses]
        ),
        branch=pd.DataFrame(
            [
                dict(
                    node_from=line.source,
                    node_to=line.target,
                    reactance=line.reactance_pu,
                    capacity=line.capacity_mw,
                )
                for line in network.lines
            ],
            columns=["node_from", "node_to", "reactance", "capacity"],
        ),
        dcbranch=pd.DataFrame(columns=["node_from", "node_to", "capacity"]),
        generator=pd.DataFrame(generators),
        consumer=pd.DataFrame(
            [
                dict(node=bus.id, demand_avg=1.0, demand_ref=f"demand_{i}")
                for i, bus in enumerate(network.buses)
            ]
        ),
        profiles=profiles,
        storval_time=pd.DataFrame({"constant": np.ones(intervals)}),
        storval_filling=pd.DataFrame({"constant": np.ones(101)}),
    )
    for category in ("node", "branch", "dcbranch", "generator", "consumer"):
        for name, default in grid.keys_powergama[category].items():
            if name not in tables[category]:
                tables[category][name] = default
            elif default is not None:
                tables[category][name] = tables[category][name].fillna(default)
    grid.from_dict(tables, timedelta=dt_seconds / 3600)
    return grid, hydro_indices


class HydroExtension(ModelExtension):
    """Add the existing sparse water block to one full-window PowerGAMA Pyomo model.

    Rolling/truncated windows and resume are rejected explicitly. Rebuilding at a new boundary
    would need to transfer both reservoir and transit state, rather than letting native storage do it.
    """

    supports_multi_period = True

    def __init__(
        self,
        cascade,
        inflows,
        state,
        dt_seconds,
        terminal_min,
        terminal_value,
        fixed_records=None,
        environmental_penalty_eur_m3=1000.0,
    ):
        self.cascade, self.inflows, self.state = cascade, inflows, state
        self.dt_seconds = dt_seconds
        self.interval_hours = dt_seconds / 3600
        self.intervals = len(inflows)
        self.lp = LP()
        self.controls = water_block(
            self.lp,
            cascade,
            inflows,
            state,
            dt_seconds,
            "hydro",
            terminal_min,
            terminal_value,
            shortfall_penalty=environmental_penalty_eur_m3,
        )
        if fixed_records is not None:
            keys = ["step", "plant_id"]
            required = keys + ["turbine_m3s", "bypass_m3s", "spill_m3s"]
            if not set(required) <= set(fixed_records) or fixed_records.duplicated(keys).any():
                raise ValueError(
                    "Fixed schedule requires unique step/plant keys and turbine/bypass/spill controls"
                )
            expected_keys = {(t, p.id) for t in range(self.intervals) for p in cascade.plants}
            if set(map(tuple, fixed_records[keys].to_numpy())) != expected_keys:
                raise ValueError("Fixed schedule does not cover exactly the requested horizon and plants")
            indexed = fixed_records.set_index(keys)
            for t in range(self.intervals):
                for i, plant in enumerate(cascade.plants):
                    for kind, column in (("q", "turbine_m3s"), ("b", "bypass_m3s"), ("s", "spill_m3s")):
                        value = float(indexed.loc[(t, plant.id), column])
                        finite(value, column, 0)
                        col = self.controls[kind][t, i]
                        lo, hi = self.lp.bounds[col]
                        if value < lo - 1e-7 or (hi is not None and value > hi + 1e-7):
                            raise ValueError("Fixed schedule exceeds physical turbine bounds")
                        self.lp.bounds[col] = (value, value)

    def prepare(self, lp, grid):
        self.generator_indices = {}
        for plant in self.cascade.plants:
            matches = grid.generator.index[grid.generator.desc == plant.id].tolist()
            if len(matches) != 1:
                raise ValueError("Hydro generator mapping must be unique")
            idx = matches[0]
            if grid.generator.loc[idx, "storage_cap"] != 0 or grid.generator.loc[idx, "pump_cap"] != 0:
                raise ValueError("Mapped hydro cannot have a second native energy storage or pump")
            self.generator_indices[plant.id] = idx
        if lp._cfg.window_hours != self.intervals or lp._cfg.window_commit != self.intervals:
            raise ValueError("Hydro adapter requires one full-window solve")

    def add_variables(self, model, grid, ctx):
        model.hydro_columns = pyo.RangeSet(0, len(self.lp.keys) - 1)
        model.hydro_water = pyo.Var(model.hydro_columns, bounds=lambda m, col: self.lp.bounds[col])

    def edit_objective_terms(self, model, terms, h):
        # PowerGAMA's objective is EUR/hour; water_block costs are EUR over an interval.
        terms["hydro_water"] = sum(
            self.lp.costs[col] * model.hydro_water[col] / self.interval_hours
            for col, key in enumerate(self.lp.keys)
            if key[1] == h
        )

    def add_constraints(self, model, grid, ctx):
        model.hydro_equalities = pyo.ConstraintList()
        model.hydro_inequalities = pyo.ConstraintList()
        for row, rhs in zip(self.lp.eq, self.lp.eq_rhs):
            model.hydro_equalities.add(
                sum(value * model.hydro_water[col] for col, value in row.items()) == rhs
            )
        for row, rhs in zip(self.lp.ub, self.lp.ub_rhs):
            model.hydro_inequalities.add(
                sum(value * model.hydro_water[col] for col, value in row.items()) <= rhs
            )
        model.hydro_generation = pyo.ConstraintList()
        for t in range(self.intervals):
            for i, plant in enumerate(self.cascade.plants):
                model.hydro_generation.add(
                    model.varGeneration[self.generator_indices[plant.id], t]
                    == plant.mw_per_m3s * model.hydro_water[self.controls["q"][t, i]]
                )
        model.hydro_loadshed_bounds = pyo.ConstraintList()
        for load in model.s_load:
            for h in model.s_h:
                model.hydro_loadshed_bounds.add(model.varLoadShed[load, h] <= model.p_demand[load, h])

    def on_solve_start(self, lp, timesteps, continue_from_last, results):
        if continue_from_last or list(timesteps) != list(range(self.intervals)):
            raise ValueError("Hydro adapter cannot resume or silently change its horizon")


@dataclass
class GridResult:
    hydro: pd.DataFrame
    buses: pd.DataFrame
    lines: pd.DataFrame
    generation: pd.DataFrame
    final_state: CascadeState
    audit: dict


def _constraint_audit(model):
    violation = 0.0
    for constraint in model.component_data_objects(pyo.Constraint, active=True):
        value = pyo.value(constraint.body)
        if constraint.lower is not None:
            violation = max(violation, pyo.value(constraint.lower) - value)
        if constraint.upper is not None:
            violation = max(violation, value - pyo.value(constraint.upper))
    for variable in model.component_data_objects(pyo.Var, active=True):
        value = pyo.value(variable)
        if variable.lb is not None:
            violation = max(violation, variable.lb - value)
        if variable.ub is not None:
            violation = max(violation, value - variable.ub)
    if not np.isfinite(violation) or violation > 1e-5:
        raise OptimizationError(f"PowerGAMA solution violates constraints: {violation}")
    return float(violation)


def solve_grid(
    cascade,
    network: Network,
    inflows,
    dt_seconds=3600,
    state=None,
    terminal_min=None,
    terminal_value=None,
    fixed_schedule=None,
    output=None,
):
    """Joint known-future DC OPF, or assessment of a fixed physical hydro schedule.

    A fixed schedule is never silently curtailed: electrical dumping/load shedding is reported.
    Optional output retains native inputs/results in addition to the auditable CSV exports.
    """
    provenance = dependency_provenance()
    inflows, _, state = inputs(cascade, inflows, np.zeros(len(inflows)), state, dt_seconds)
    network.validate(cascade, len(inflows))
    if fixed_schedule is not None and hasattr(fixed_schedule, "records"):
        fixed_schedule = fixed_schedule.records
    extension = HydroExtension(
        cascade, inflows, state, dt_seconds, terminal_min, terminal_value, fixed_records=fixed_schedule
    )
    grid, _ = _grid(cascade, network, len(inflows), dt_seconds)
    lp = powergama.LpProblem(
        grid,
        objective_mode="multi_period",
        horizon_hours=len(inflows),
        commit_hours=len(inflows),
        extensions=[extension],
    )
    with _scratch_directory() as temporary:
        native_dir = Path(output) if output is not None else Path(temporary)
        native_dir.mkdir(parents=True, exist_ok=True)
        results = powergama.Results(grid, native_dir / "powergama.sqlite", replace=True)
        started = perf_counter()
        lp.solve(results, solver="appsi_highs")
        elapsed = perf_counter() - started
        model = lp.model
        raw_violation = _constraint_audit(model)
        x = np.array([pyo.value(model.hydro_water[col]) for col in model.hydro_columns])
        # Upstream's SQLite extractor applies abs() to nodal duals, hiding negative prices.
        # Preserve the signed balance sensitivity instead; verify it against perturbed
        # demand objectives in integration tests, including a surplus/dump case.
        prices = np.array(
            [
                [lp.dual[model.cPowerbalance[bus.id, h]] / pg_constants.baseMVA for bus in network.buses]
                for h in range(len(inflows))
            ]
        )
        plant_prices = np.column_stack(
            [
                prices[:, [bus.id for bus in network.buses].index(network.hydro_buses[p.id])]
                for p in cascade.plants
            ]
        )
        hydro, final, revenue, water_error = replay(
            cascade, inflows, plant_prices, state, dt_seconds, extension.controls, x
        )
        generation_rows, bus_rows, line_rows = [], [], []
        max_balance, max_flow_error, max_capacity_error = 0.0, 0.0, 0.0
        for h in range(len(inflows)):
            for idx, row in grid.generator.iterrows():
                generation_rows.append(
                    dict(
                        step=h,
                        generator_id=row.desc,
                        bus=row.node,
                        type=row.type,
                        power_mw=pyo.value(model.varGeneration[idx, h]),
                        marginal_cost_eur_mwh=row.fuelcost,
                    )
                )
            for j, line in enumerate(network.lines):
                f = pyo.value(model.varAcBranchFlow[j, h])
                angle_from = pyo.value(model.varVoltageAngle[line.source, h])
                angle_to = pyo.value(model.varVoltageAngle[line.target, h])
                # PowerGAMA uses the opposite angle sign to the common export convention.
                dc_error = f - pg_constants.baseMVA * (angle_to - angle_from) / line.reactance_pu
                max_flow_error = max(max_flow_error, abs(dc_error))
                max_capacity_error = max(max_capacity_error, max(abs(f) - line.capacity_mw, 0))
                line_rows.append(
                    dict(
                        step=h,
                        line_id=line.id,
                        source=line.source,
                        target=line.target,
                        flow_mw=f,
                        capacity_mw=line.capacity_mw,
                        loading_fraction=abs(f) / line.capacity_mw if line.capacity_mw else 0.0,
                        dc_flow_residual_mw=dc_error,
                    )
                )
            for i, bus in enumerate(network.buses):
                generation = sum(
                    pyo.value(model.varGeneration[g, h])
                    for g in grid.generator.index
                    if grid.generator.loc[g, "node"] == bus.id
                )
                net_export = sum(
                    pyo.value(model.varAcBranchFlow[j, h])
                    * (1 if line.source == bus.id else -1 if line.target == bus.id else 0)
                    for j, line in enumerate(network.lines)
                )
                shed, dumped = pyo.value(model.varLoadShed[i, h]), pyo.value(model.varDumpLoad[i, h])
                balance = generation + shed - dumped - network.demand_mw[h, i] - net_export
                max_balance = max(max_balance, abs(balance))
                renewable_idx = len(cascade.plants) + len(network.thermal) + i
                renewable = pyo.value(model.varGeneration[renewable_idx, h])
                bus_rows.append(
                    dict(
                        step=h,
                        bus=bus.id,
                        demand_mw=network.demand_mw[h, i],
                        generation_mw=generation,
                        renewable_available_mw=network.renewable_available_mw[h, i],
                        renewable_used_mw=renewable,
                        renewable_curtailed_mw=max(network.renewable_available_mw[h, i] - renewable, 0),
                        price_eur_mwh=prices[h, i],
                        unserved_mw=shed,
                        dumped_mw=dumped,
                        voltage_angle_rad=pyo.value(model.varVoltageAngle[bus.id, h]),
                        balance_residual_mw=balance,
                    )
                )
        if max(max_balance, max_flow_error, max_capacity_error) > 1e-5:
            raise OptimizationError("Independent electrical-network audit failed")
        buses, lines, generation = (
            pd.DataFrame(bus_rows),
            pd.DataFrame(line_rows),
            pd.DataFrame(generation_rows),
        )
        hours = dt_seconds / 3600
        operating_cost = float((generation.power_mw * generation.marginal_cost_eur_mwh).sum() * hours)
        shortfall = float(hydro.environmental_shortfall_m3s.sum() * dt_seconds)
        carryover = terminal_worth(cascade, final, terminal_value)
        objective = (
            operating_cost
            + float(buses.unserved_mw.sum() + buses.dumped_mw.sum()) * hours * pg_constants.loadshedcost
        )
        objective += 1000 * shortfall - carryover
        # Initial water still in transit past the horizon is a constant omitted by the LP.
        constant_transit = sum(
            sum(queue[len(inflows) :]) * ((terminal_value or {}).get(link.target, 0))
            for queue, link in zip(state.queues_m3, cascade.links)
        )
        solver_objective = float(pyo.value(model.OBJ) * hours) - constant_transit
        if abs(objective - solver_objective) > max(1e-4, abs(objective) * 1e-8):
            raise OptimizationError("Independent economic objective does not match PowerGAMA")
        audit = dict(
            **provenance,
            mode="fixed_schedule_assessment" if fixed_schedule is not None else "joint_optimization",
            intervals=len(inflows),
            dt_seconds=dt_seconds,
            solve_seconds=elapsed,
            information="one full perfect-foresight window",
            network="lossless DC OPF; 100 MVA base",
            price_source="signed raw balance duals; native SQLite absolute-valued prices not used",
            mapped_native_storage_count=len(list(model.s_gen_storage)),
            operating_cost_eur=operating_cost,
            terminal_adjusted_cost_eur=objective,
            hydro_revenue_eur=revenue,
            terminal_water_value_eur=carryover,
            hydro_energy_mwh=float(hydro.energy_mwh.sum()),
            renewable_curtailed_mwh=float(buses.renewable_curtailed_mw.sum() * hours),
            unserved_energy_mwh=float(buses.unserved_mw.sum() * hours),
            dumped_energy_mwh=float(buses.dumped_mw.sum() * hours),
            environmental_shortfall_m3=shortfall,
            water_spill_m3=float(hydro.spill_m3s.sum() * dt_seconds),
            max_water_residual_m3=water_error,
            max_nodal_balance_residual_mw=max_balance,
            max_dc_flow_residual_mw=max_flow_error,
            max_capacity_violation_mw=max_capacity_error,
            max_raw_constraint_violation=raw_violation,
            loadshed_and_dump_penalty_eur_mwh=pg_constants.loadshedcost,
            environmental_penalty_eur_m3=1000.0,
        )
        result = GridResult(hydro, buses, lines, generation, final, audit)
        if output is not None:
            for name, frame in (
                ("hydro", hydro),
                ("buses", buses),
                ("lines", lines),
                ("generation", generation),
            ):
                frame.to_csv(native_dir / f"{name}.csv", index=False)
            (native_dir / "audit.json").write_text(
                json.dumps(audit, indent=2, allow_nan=False), encoding="utf-8"
            )
        return result
