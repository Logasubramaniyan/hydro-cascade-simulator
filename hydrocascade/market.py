"""Small zonal cost-minimizing market with energy/reserve co-optimization.

This is an educational transport-network LP, not a nodal or unit-commitment market replica.
"""

from dataclasses import dataclass
import numpy as np
import pandas as pd
from .model import finite
from .simulation import array, CascadeState
from .scheduling import water_block, replay
from .lp import LP


@dataclass(frozen=True)
class Thermal:
    id: str
    zone: str
    capacity_mw: float
    marginal_cost_eur_mwh: float
    reserve_cost_eur_mw_h: float = 1.0

    def __post_init__(self):
        if not self.id or not self.zone:
            raise ValueError("Generator id and zone are required")
        for field in ("capacity_mw", "marginal_cost_eur_mwh", "reserve_cost_eur_mw_h"):
            finite(getattr(self, field), field, 0)


@dataclass(frozen=True)
class Interconnector:
    source: str
    target: str
    capacity_mw: float

    def __post_init__(self):
        finite(self.capacity_mw, "line capacity", 0)
        if self.source == self.target:
            raise ValueError("Interconnector endpoints must differ")


@dataclass
class MarketResult:
    hydro: pd.DataFrame
    zones: pd.DataFrame
    thermal: pd.DataFrame
    flows: pd.DataFrame
    builds: dict
    audit: dict


def clear_market(
    cascade,
    inflows,
    zones,
    thermal,
    lines,
    demand_mw,
    renewables_mw=None,
    reserve_mw=None,
    availability=None,
    dt_seconds=3600,
    state=None,
    terminal_min=None,
    terminal_value=None,
    voll_eur_mwh=10000,
    reserve_activation_seconds=900,
    investment=None,
):
    """Solve chronological dispatch; prices are LP balance-constraint duals.

    investment: {generator_id: {max_build_mw, cost_eur_per_mw_for_horizon}}.
    All renewable capacity profiles and reserve requirements are exogenous.
    Reserve is local upward capacity; activation energy is safeguarded but not dispatched.
    """
    finite(dt_seconds, "dt_seconds", 1e-9)
    finite(voll_eur_mwh, "VOLL", 1e-9)
    finite(reserve_activation_seconds, "reserve_activation_seconds", 1e-9)
    if reserve_activation_seconds > dt_seconds:
        raise ValueError("Reserve activation duration must fit in the dispatch interval")
    zones = tuple(zones)
    if not zones or len(set(zones)) != len(zones):
        raise ValueError("Zones must be nonempty and unique")
    if len({g.id for g in thermal}) != len(thermal):
        raise ValueError("Thermal generator ids must be unique")
    if any(p.zone not in zones for p in cascade.plants) or any(g.zone not in zones for g in thermal):
        raise ValueError("All generators need a known zone")
    if any(line.source not in zones or line.target not in zones for line in lines):
        raise ValueError("Unknown interconnector zone")
    inflows = np.asarray(inflows, dtype=float)
    if inflows.ndim != 2 or len(inflows) == 0:
        raise ValueError("Inflows need at least one interval")
    t_count, z_count = len(inflows), len(zones)
    inflows = array(inflows, (t_count, len(cascade.plants)), "inflows")
    demand = array(demand_mw, (t_count, z_count), "demand")
    renewables = (
        np.zeros_like(demand) if renewables_mw is None else array(renewables_mw, demand.shape, "renewables")
    )
    reserves = np.zeros_like(demand) if reserve_mw is None else array(reserve_mw, demand.shape, "reserve")
    available = (
        np.ones((t_count, len(thermal)))
        if availability is None
        else array(availability, (t_count, len(thermal)), "availability")
    )
    if (available > 1).any():
        raise ValueError("Availability must be between zero and one")
    state = CascadeState.initial(cascade) if state is None else state.copy()
    state.validate(cascade)
    investment = {} if investment is None else investment
    if not set(investment) <= {g.id for g in thermal}:
        raise ValueError("Investment references an unknown generator")
    lp = LP()
    water = water_block(lp, cascade, inflows, state, dt_seconds, "market", terminal_min, terminal_value)
    hours = dt_seconds / 3600
    build = {}
    for g in thermal:
        if g.id in investment:
            spec = investment[g.id]
            finite(spec["max_build_mw"], "max_build_mw", 0)
            finite(spec["cost_eur_per_mw_for_horizon"], "investment cost", 0)
            build[g.id] = lp.var(
                ("build", g.id), upper=spec["max_build_mw"], cost=spec["cost_eur_per_mw_for_horizon"]
            )
    generation, tr, hr, flow, used, shed, reserve_deficit, balance = {}, {}, {}, {}, {}, {}, {}, {}
    for t in range(t_count):
        for j, g in enumerate(thermal):
            generation[t, j] = lp.var(("thermal", t, j), cost=g.marginal_cost_eur_mwh * hours)
            tr[t, j] = lp.var(("thermal_reserve", t, j), cost=g.reserve_cost_eur_mw_h * hours)
            coeff = {generation[t, j]: 1, tr[t, j]: 1}
            if g.id in build:
                coeff[build[g.id]] = -available[t, j]
            lp.inequality(coeff, g.capacity_mw * available[t, j])
        for i, p in enumerate(cascade.plants):
            hr[t, i] = lp.var(("hydro_reserve", t, i), cost=hours)
            lp.inequality({water["q"][t, i]: p.mw_per_m3s, hr[t, i]: 1}, p.capacity_mw)
            lp.inequality(
                {hr[t, i]: reserve_activation_seconds / (1000 * p.mw_per_m3s), water["v"][t, i]: -1},
                -p.min_storage_m3 / 1000,
            )
            if p.ramp_m3s_per_step is not None:
                lp.inequality({hr[t, i]: 1}, p.ramp_m3s_per_step * p.mw_per_m3s)
        for j, line in enumerate(lines):
            flow[t, j] = lp.var(("flow", t, j), lower=-line.capacity_mw, upper=line.capacity_mw)
        for z, zone in enumerate(zones):
            used[t, z] = lp.var(("renewables", t, z), upper=renewables[t, z])
            shed[t, z] = lp.var(("shed", t, z), upper=demand[t, z], cost=voll_eur_mwh * hours)
            reserve_deficit[t, z] = lp.var(
                ("reserve_deficit", t, z), upper=reserves[t, z], cost=voll_eur_mwh * hours
            )
            coefficients = {used[t, z]: 1, shed[t, z]: 1}
            reserve_coeff = {reserve_deficit[t, z]: -1}
            for i, p in enumerate(cascade.plants):
                if p.zone == zone:
                    coefficients[water["q"][t, i]] = p.mw_per_m3s
                    reserve_coeff[hr[t, i]] = -1
            for j, g in enumerate(thermal):
                if g.zone == zone:
                    coefficients[generation[t, j]] = 1
                    reserve_coeff[tr[t, j]] = -1
            for j, line in enumerate(lines):
                if line.source == zone:
                    coefficients[flow[t, j]] = -1
                elif line.target == zone:
                    coefficients[flow[t, j]] = 1
            balance[t, z] = lp.equality(coefficients, demand[t, z])
            lp.inequality(reserve_coeff, -reserves[t, z])
    solved = lp.solve()
    prices = np.zeros_like(inflows)
    zone_records, thermal_records, line_records = [], [], []
    for t in range(t_count):
        for z, zone in enumerate(zones):
            price = float(solved.eqlin.marginals[balance[t, z]] / hours)
            for i, p in enumerate(cascade.plants):
                if p.zone == zone:
                    prices[t, i] = price
            reserve_held = sum(solved.x[hr[t, i]] for i, p in enumerate(cascade.plants) if p.zone == zone)
            reserve_held += sum(solved.x[tr[t, j]] for j, g in enumerate(thermal) if g.zone == zone)
            zone_records.append(
                dict(
                    step=t,
                    zone=zone,
                    demand_mw=demand[t, z],
                    price_eur_mwh=price,
                    renewable_used_mw=solved.x[used[t, z]],
                    renewable_curtailed_mw=renewables[t, z] - solved.x[used[t, z]],
                    unserved_mw=solved.x[shed[t, z]],
                    reserve_held_mw=reserve_held,
                    reserve_shortfall_mw=solved.x[reserve_deficit[t, z]],
                )
            )
        for j, g in enumerate(thermal):
            thermal_records.append(
                dict(
                    step=t,
                    generator_id=g.id,
                    power_mw=solved.x[generation[t, j]],
                    reserve_mw=solved.x[tr[t, j]],
                )
            )
        for j, line in enumerate(lines):
            line_records.append(
                dict(step=t, source=line.source, target=line.target, flow_mw=solved.x[flow[t, j]])
            )
    hydro, _, _, error = replay(cascade, inflows, prices, state, dt_seconds, water, solved.x)
    hydro["reserve_mw"] = [
        solved.x[hr[int(row.step), cascade.index[row.plant_id]]] for row in hydro.itertuples()
    ]
    zone_frame = pd.DataFrame(zone_records)
    audit = dict(
        solved.audit,
        max_water_residual_m3=error,
        unserved_energy_mwh=float(zone_frame.unserved_mw.sum() * hours),
        scarcity_intervals=int(zone_frame.groupby("step").unserved_mw.sum().gt(1e-7).sum()),
        network="lossless zonal transport",
        prices="LP marginal prices, no uplift",
        reserve="local upward holding with storage safeguard; no activation simulation",
    )
    return MarketResult(
        hydro,
        zone_frame,
        pd.DataFrame(thermal_records),
        pd.DataFrame(line_records),
        {key: float(solved.x[col]) for key, col in build.items()},
        audit,
    )


def adequacy_sample(
    cascade,
    inflows,
    zones,
    thermal,
    lines,
    demand_mw,
    renewables_mw=None,
    outage_probabilities=None,
    trials=100,
    seed=2026,
    dt_seconds=3600,
):
    """Illustrative independent interval outages. Not calibrated LOLE or a reliability standard."""
    if not isinstance(trials, int) or isinstance(trials, bool) or trials < 1:
        raise ValueError("trials must be a positive integer")
    probs = (
        np.zeros(len(thermal))
        if outage_probabilities is None
        else array(outage_probabilities, (len(thermal),), "outage probabilities")
    )
    if (probs > 1).any():
        raise ValueError("Outage probabilities must be <= 1")
    rng = np.random.default_rng(seed)
    rows = []
    for trial in range(trials):
        available = (rng.random((len(inflows), len(thermal))) >= probs).astype(float)
        result = clear_market(
            cascade,
            inflows,
            zones,
            thermal,
            lines,
            demand_mw,
            renewables_mw,
            availability=available,
            dt_seconds=dt_seconds,
        )
        rows.append(
            dict(
                trial=trial,
                unserved_energy_mwh=result.audit["unserved_energy_mwh"],
                loss_of_load_hours=result.audit["scarcity_intervals"] * dt_seconds / 3600,
            )
        )
    return pd.DataFrame(rows)
