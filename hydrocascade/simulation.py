"""Independent replay of controls, retaining spill and water in transit."""

from dataclasses import dataclass
import numpy as np
import pandas as pd
from .model import Cascade, finite


def array(values, shape, name, nonnegative=True):
    out = np.asarray(values, dtype=float)
    if out.shape != shape or not np.isfinite(out).all() or (nonnegative and (out < 0).any()):
        raise ValueError(
            f"{name} must be a finite array with shape {shape}"
            + (" and nonnegative entries" if nonnegative else "")
        )
    return out


@dataclass
class CascadeState:
    storage_m3: np.ndarray
    queues_m3: list[list[float]]
    last_turbine_m3s: np.ndarray

    @classmethod
    def initial(cls, cascade):
        return cls(
            np.array([p.initial_storage_m3 for p in cascade.plants], dtype=float),
            [[0.0] * link.delay_steps for link in cascade.links],
            np.zeros(len(cascade.plants)),
        )

    def copy(self):
        return CascadeState(
            self.storage_m3.copy(), [q.copy() for q in self.queues_m3], self.last_turbine_m3s.copy()
        )

    def validate(self, cascade):
        n = len(cascade.plants)
        array(self.storage_m3, (n,), "storage")
        array(self.last_turbine_m3s, (n,), "last turbine flow")
        if len(self.queues_m3) != len(cascade.links):
            raise ValueError("One transit queue is needed per link")
        for queue, link in zip(self.queues_m3, cascade.links):
            array(queue, (link.delay_steps,), "transit queue")
        for v, q, p in zip(self.storage_m3, self.last_turbine_m3s, cascade.plants):
            if not p.min_storage_m3 - 1e-7 <= v <= p.storage_capacity_m3 + 1e-7:
                raise ValueError("Storage outside configured operating bounds")
            if q > p.max_flow_m3s + 1e-7:
                raise ValueError("Previous turbine flow exceeds capacity")


def step(
    cascade: Cascade,
    state: CascadeState,
    inflow_m3s,
    turbine_m3s,
    dt_seconds=3600.0,
    bypass_m3s=None,
    spill_m3s=None,
    enforce_controls=False,
):
    """Advance an interval. Forced schedules are checked; rule controls are projected to feasibility.

    With explicit bypass/spill, no automatic environmental release is added; shortages are recorded.
    Zero-delay links arrive within the same interval. Transit queues contain volumes, not rates.
    """
    finite(dt_seconds, "dt_seconds", 1e-9)
    state.validate(cascade)
    n = len(cascade.plants)
    local = array(inflow_m3s, (n,), "inflow")
    requested = array(turbine_m3s, (n,), "turbine controls")
    bypass = None if bypass_m3s is None else array(bypass_m3s, (n,), "bypass")
    spill = None if spill_m3s is None else array(spill_m3s, (n,), "spill")
    next_state = state.copy()
    arriving = np.zeros(n)
    before = sum(state.storage_m3) + sum(map(sum, state.queues_m3))
    index = cascade.index
    for j, link in enumerate(cascade.links):
        if link.delay_steps:
            arriving[index[link.target]] += next_state.queues_m3[j].pop(0)
            next_state.queues_m3[j].append(0.0)
    rows = []
    sink = 0.0
    total_losses = 0.0
    for i in cascade.order:
        plant = cascade.plants[i]
        incoming = local[i] * dt_seconds + arriving[i]
        water = state.storage_m3[i] + incoming
        # Evaporation/abstraction is limited to water above the operating floor.
        loss = min(plant.loss_m3s * dt_seconds, max(water - plant.min_storage_m3, 0))
        water -= loss
        usable = max(water - plant.min_storage_m3, 0)
        low, high = 0.0, plant.max_flow_m3s
        if plant.ramp_m3s_per_step is not None:
            low = max(0.0, state.last_turbine_m3s[i] - plant.ramp_m3s_per_step)
            high = min(high, state.last_turbine_m3s[i] + plant.ramp_m3s_per_step)
        q = min(max(requested[i], low), high, usable / dt_seconds)
        b = min(
            max(plant.min_release_m3s - q, 0) if bypass is None else bypass[i],
            max(usable / dt_seconds - q, 0),
        )
        remaining = water - (q + b) * dt_seconds
        s = max(remaining - plant.storage_capacity_m3, 0) / dt_seconds
        if spill is not None:
            s = max(s, min(spill[i], max((remaining - plant.min_storage_m3) / dt_seconds, 0)))
        if enforce_controls:
            expected = (requested[i], 0 if bypass is None else bypass[i], 0 if spill is None else spill[i])
            if max(abs(x - y) for x, y in zip((q, b, s), expected)) > 1e-6:
                raise ValueError(f"Infeasible controls at {plant.id}")
        end = remaining - s * dt_seconds
        # Snap subtraction roundoff to physical bounds; residuals still expose the correction.
        if abs(end - plant.min_storage_m3) < 1e-7:
            end = plant.min_storage_m3
        if abs(end - plant.storage_capacity_m3) < 1e-7:
            end = plant.storage_capacity_m3
        out_volume = (q + b + s) * dt_seconds
        next_state.storage_m3[i] = end
        next_state.last_turbine_m3s[i] = q
        routed_fraction = 0.0
        for j, link in enumerate(cascade.links):
            if link.source == plant.id:
                routed = out_volume * link.fraction
                routed_fraction += link.fraction
                if link.delay_steps:
                    next_state.queues_m3[j][-1] += routed
                else:
                    arriving[index[link.target]] += routed
        sink += out_volume * (1 - routed_fraction)
        total_losses += loss
        residual = state.storage_m3[i] + incoming - loss - out_volume - end
        rows.append(
            dict(
                plant_id=plant.id,
                storage_start_m3=state.storage_m3[i],
                storage_m3=end,
                local_inflow_m3s=local[i],
                upstream_arrival_m3=arriving[i],
                turbine_m3s=q,
                bypass_m3s=b,
                spill_m3s=s,
                loss_m3=loss,
                power_mw=q * plant.mw_per_m3s,
                energy_mwh=q * plant.mw_per_m3s * dt_seconds / 3600,
                environmental_shortfall_m3s=max(plant.min_release_m3s - q - b - s, 0),
                ramp_shortfall_m3s=max(low - q, 0),
                water_residual_m3=residual,
            )
        )
    after = sum(next_state.storage_m3) + sum(map(sum, next_state.queues_m3))
    audit = dict(
        water_residual_m3=before + sum(local) * dt_seconds - total_losses - sink - after,
        sink_m3=sink,
        loss_m3=total_losses,
        transit_m3=sum(map(sum, next_state.queues_m3)),
    )
    return next_state, pd.DataFrame(rows).set_index("plant_id"), audit


def simulate(cascade, inflows, controls=None, dt_seconds=3600, state=None):
    """Run a feasible inflow-following baseline, or replay turbine controls."""
    inflows = np.asarray(inflows, dtype=float)
    if inflows.ndim != 2 or inflows.shape[0] < 1:
        raise ValueError("Inflows need shape (intervals, plants), with at least one interval")
    array(inflows, (len(inflows), len(cascade.plants)), "inflows")
    if controls is not None:
        controls = array(controls, inflows.shape, "controls")
    state = CascadeState.initial(cascade) if state is None else state.copy()
    records, audits = [], []
    for t, local in enumerate(inflows):
        # Follow total available arrivals; environmental release handled by step().
        arrivals = np.zeros(len(cascade.plants))
        for queue, link in zip(state.queues_m3, cascade.links):
            if queue:
                arrivals[cascade.index[link.target]] += queue[0] / dt_seconds
        requested = local + arrivals if controls is None else controls[t]
        if controls is None:
            # For zero-delay chains, allow the next plant to use routed water in the interval.
            for i in cascade.order:
                q = min(requested[i], cascade.plants[i].max_flow_m3s)
                for link in cascade.links:
                    if link.source == cascade.plants[i].id and link.delay_steps == 0:
                        requested[cascade.index[link.target]] += q * link.fraction
        state, frame, audit = step(cascade, state, local, requested, dt_seconds)
        records.append(frame.reset_index().assign(step=t))
        audits.append(dict(step=t, **audit))
    return pd.concat(records, ignore_index=True), pd.DataFrame(audits), state
