from dataclasses import replace
from itertools import product

import numpy as np
import pytest

from hydrocascade import Cascade, Plant, Link
from hydrocascade.lp import OptimizationError
from hydrocascade.operations.model import OperatingLimits
from hydrocascade.pilot.assets import ReservoirCurve, Unit, UnitState
from hydrocascade.pilot.dispatch import optimize_units


def fixture(steps=4, initial=7200):
    c = Cascade((Plant("p", "Example", 14400, initial, 2, 100),))
    curve = ReservoirCurve("p", (0, 14400), (100, 100), 0)
    u = Unit("u", "p", (1, 2), (1, 1), startup_cost_eur=5)
    return c, [curve], [u], np.zeros((steps, 1))


def test_curve_integral_across_knots_and_loss_efficiency():
    curve = ReservoirCurve("p", (0, 100, 200), (10, 20, 40), 1)
    u = Unit("u", "p", (1, 2), (0.8, 1), hydraulic_loss_m_per_m3s2=0.5)
    assert curve.mean_level(0, 200) == pytest.approx(22.5)
    assert curve.mean_level(200, 0) == pytest.approx(22.5)
    assert curve.power(u, 1.5, 0, 200) == pytest.approx(0.00981 * (22.5 - 1 - 0.5 * 1.5**2) * 1.5 * 0.9)
    with pytest.raises(ValueError, match="range"):
        curve.mean_level(-1, 200)


@pytest.mark.parametrize("storage,level", [((0, 0), (1, 2)), ((0, 1), (2, 1)), ((0, 1), (1, float("nan")))])
def test_invalid_hydraulic_curves_fail(storage, level):
    with pytest.raises(ValueError):
        ReservoirCurve("p", storage, level, 0)


def test_minimum_load_start_cost_and_half_hour_energy_known_answer():
    c, curves, units, flow = fixture(2, initial=3600)
    r = optimize_units(c, flow, [10, 100], curves, units, dt_seconds=1800, terminal_min={"p": 0})
    assert r.unit_records.on.tolist() == [0, 1]
    assert r.records.turbine_m3s.tolist() == pytest.approx([0, 2])
    assert r.revenue_eur == pytest.approx(98.1)
    assert r.objective_eur == pytest.approx(93.1)
    assert r.audit["max_water_residual_m3"] < 1e-6
    assert r.audit["hydraulic_review_status"] == "pass"


def test_minimum_up_down_matches_exhaustive_binary_schedule():
    c, curves, units, flow = fixture(5, initial=14400)
    u = replace(units[0], flow_m3s=(1, 1.000001), min_up_steps=2, min_down_steps=2)
    prices = np.array([-50, 100, 100, -50, 80])
    r = optimize_units(c, flow, prices, curves, [u], terminal_min={"p": 0})
    candidates = []
    for bits in product((0, 1), repeat=5):
        previous, age, starts, stops, valid = 0, 2, 0, 0, True
        for t, on in enumerate(bits):
            if on != previous:
                if age < 2 or t + 2 > 5:
                    valid = False
                starts += on
                stops += 1 - on
                age = 0
            age += 1
            previous = on
        if valid:
            # The tiny flow range makes the expected optimum independent of flow degeneracy.
            candidates.append(0.981 * 0.25 * np.dot(bits, prices) - 5 * starts)
    assert r.objective_eur == pytest.approx(max(candidates), abs=0.001)
    assert r.audit["max_unit_violation"] < 1e-6


def test_inherited_minimum_up_time_and_initial_flow_boundary():
    c, curves, units, flow = fixture(3)
    state = {"u": UnitState(True, 1, 1)}
    from hydrocascade.simulation import CascadeState

    physical = CascadeState.initial(c)
    physical.last_turbine_m3s[0] = 1
    u = replace(units[0], min_up_steps=3)
    r = optimize_units(
        c, flow, [-100] * 3, curves, [u], unit_state=state, state=physical, terminal_min={"p": 0}
    )
    assert r.unit_records.on.tolist() == [1, 1, 0]
    assert r.final_unit_state["u"].on is False
    with pytest.raises(ValueError, match="initial.*flow"):
        optimize_units(c, flow, [1] * 3, curves, [u], unit_state=state)


def test_forced_outage_conflicting_with_initial_commitment_is_infeasible():
    c, curves, units, flow = fixture(2)
    from hydrocascade.simulation import CascadeState

    state = CascadeState.initial(c)
    state.last_turbine_m3s[0] = 1
    with pytest.raises(OptimizationError, match="MILP"):
        optimize_units(
            c,
            flow,
            [1, 1],
            curves,
            [replace(units[0], min_up_steps=3)],
            state=state,
            unit_state={"u": UnitState(True, 1, 1)},
            unit_availability={"u": [0, 1]},
        )


def test_unit_ramp_and_environmental_bypass_respect_shared_water_model():
    c, curves, units, flow = fixture(3)
    limits = replace(OperatingLimits.standard(c, 3), min_release_m3s=np.ones((3, 1)))
    u = replace(units[0], ramp_m3s_per_hour=4)
    r = optimize_units(c, flow, [-10, 10, 100], curves, [u], limits=limits, terminal_min={"p": 0})
    assert np.abs(np.diff(np.r_[0, r.unit_records.flow_m3s])).max() <= 1 + 1e-6
    assert r.records.iloc[0][["bypass_m3s", "spill_m3s"]].sum() == pytest.approx(1)
    assert r.records.environmental_shortfall_m3s.max() == 0


def test_variable_head_is_coupled_to_storage_and_approximation_can_hold():
    c, _, units, flow = fixture(2, initial=7200)
    curves = [ReservoirCurve("p", (0, 14400), (10, 100), 0)]
    r = optimize_units(
        c,
        flow,
        [100, 100],
        curves,
        units,
        terminal_min={"p": 0},
        approximation_tolerance_mw=0,
        storage_segments=1,
    )
    assert r.unit_records.replayed_power_mw.max() < 1.962
    assert r.audit["max_power_approximation_error_mw"] > 1e-6
    assert r.audit["hydraulic_review_status"] == "hold"


def test_delayed_downstream_water_is_preserved():
    c = Cascade(
        (Plant("a", "Upper", 3600, 3600, 1, 100), Plant("b", "Lower", 3600, 0, 1, 100)),
        (Link("a", "b", delay_steps=1),),
    )
    curves = [ReservoirCurve(p.id, (0, 3600), (100, 100), 0) for p in c.plants]
    units = [Unit(p.id, p.id, (0.5, 1), (1, 1)) for p in c.plants]
    r = optimize_units(
        c, np.zeros((2, 2)), [100, 200], curves, units, dt_seconds=3600, terminal_min={"a": 0, "b": 0}
    )
    lower = r.records[r.records.plant_id == "b"]
    assert lower.turbine_m3s.iloc[0] == 0
    assert r.audit["max_water_residual_m3"] < 1e-5


def test_incomplete_curves_unknown_units_and_invalid_solver_options_fail():
    c, curves, units, flow = fixture(2)
    with pytest.raises(ValueError, match="cover"):
        optimize_units(c, flow, [1, 1], [replace(curves[0], storage_m3=(0, 100))], units)
    with pytest.raises(ValueError):
        optimize_units(c, flow, [1, 1], curves, [replace(units[0], plant_id="unknown")])
    with pytest.raises(ValueError):
        optimize_units(c, flow, [1, 1], curves, units, time_limit_seconds=0)


def test_connection_peak_is_checked_even_if_interval_average_fits():
    c = Cascade((Plant("p", "Head change", 14400, 7200, 2, 100),))
    curve = ReservoirCurve("p", (0, 14400), (10, 100), 0)
    unit = Unit("u", "p", (1, 1.000001), (1, 1))
    r = optimize_units(
        c, [[0]], [100], [curve], [unit], dt_seconds=3600, terminal_min={"p": 0}, export_limit_mw=[0.45]
    )
    assert r.records.power_mw.iloc[0] < 0.45
    assert r.audit["actual_export_violation_mw"] > 0.08
    assert r.audit["hydraulic_review_status"] == "hold"


def test_aggregate_export_peak_checks_crossed_level_knots():
    from hydrocascade.pilot.dispatch import _peak_export
    from hydrocascade.simulation import CascadeState
    import pandas as pd

    c = Cascade((Plant("a", "A", 1800, 1800, 1, 100), Plant("b", "B", 1800, 0, 1, 100)))
    curves = {
        "a": ReservoirCurve("a", (0, 900, 1800), (10, 100, 100), 0),
        "b": ReservoirCurve("b", (0, 900, 1800), (10, 100, 100), 0),
    }
    units = [Unit(p.id, p.id, (0.5, 1), (1, 1)) for p in c.plants]
    records = pd.DataFrame(dict(step=[0, 0], plant_id=["a", "b"], storage_m3=[0, 1800]))
    controls = pd.DataFrame(dict(step=[0, 0], unit_id=["a", "b"], flow_m3s=[1, 1], on=[1, 1]))
    assert _peak_export(c, curves, units, CascadeState.initial(c), records, controls, 1)[0] == pytest.approx(
        1.962
    )


def test_feasible_incumbent_on_solver_timeout_is_not_accepted(monkeypatch):
    from types import SimpleNamespace
    from hydrocascade.pilot.solver import MILP
    import hydrocascade.pilot.solver as solver

    model = MILP()
    model.binary("test")
    monkeypatch.setattr(
        solver,
        "milp",
        lambda *a, **kw: SimpleNamespace(success=False, status=1, message="time limit", x=np.array([1.0])),
    )
    with pytest.raises(OptimizationError, match="no accepted schedule"):
        model.solve()
