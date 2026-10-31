"""Optional integration tests use the actual pinned PowerGAMA/Pyomo/HiGHS stack."""

from dataclasses import replace
import json
import numpy as np
import pytest

pytest.importorskip("powergama")

from hydrocascade import Cascade, CascadeState, Link, Plant, optimize
from hydrocascade.powergama import POWERGAMA_COMMIT
from hydrocascade.powergama.backend import HydroExtension, dependency_provenance, solve_grid
from hydrocascade.powergama.network import ACLine, Bus, Network, ThermalUnit
from hydrocascade.powergama.study import build_case, run_study
from hydrocascade.experiments import carryover_values

pytestmark = pytest.mark.powergama


def zero_hydro():
    return Cascade((Plant("p", "no water", 0, 0, 0, 50),))


def unit_hydro(storage=3600, capacity=3600, flow=1):
    return Cascade((Plant("p", "one MW per m3s", capacity, storage, flow, 1e6 / (1000 * 9.81 * 0.9)),))


def two_bus(intervals=1, transfer=10, demand=None, renewable=None):
    return Network(
        (Bus("a"), Bus("b")),
        (ACLine("ab", "a", "b", 0.1, transfer),),
        (ThermalUnit("cheap", "a", 100, 20), ThermalUnit("expensive", "b", 100, 80)),
        {"p": "a"},
        np.tile([10.0, 50.0], (intervals, 1)) if demand is None else np.array(demand, dtype=float),
        np.zeros((intervals, 2)) if renewable is None else np.array(renewable, dtype=float),
    )


def test_actual_pinned_source_is_used():
    assert dependency_provenance()["powergama_commit"] == POWERGAMA_COMMIT


def test_merit_order_and_congestion_have_known_prices():
    result = solve_grid(zero_hydro(), two_bus(), [[0]])
    assert result.lines.flow_mw.tolist() == pytest.approx([10])
    assert result.buses.price_eur_mwh.tolist() == pytest.approx([20, 80])
    assert result.audit["operating_cost_eur"] == pytest.approx(3600)
    assert result.audit["mapped_native_storage_count"] == 0
    assert result.audit["max_dc_flow_residual_mw"] < 1e-7


def test_meshed_grid_respects_dc_loop_flows_not_only_transport_limits():
    network = Network(
        (Bus("a"), Bus("b"), Bus("c")),
        (
            ACLine("ab", "a", "b", 0.1, 100),
            ACLine("bc", "b", "c", 0.1, 100),
            ACLine("ac", "a", "c", 0.1, 100),
        ),
        (ThermalUnit("cheap", "a", 100, 20), ThermalUnit("backup", "c", 100, 80)),
        {"p": "a"},
        np.array([[0.0, 0.0, 90.0]]),
        np.zeros((1, 3)),
    )
    result = solve_grid(zero_hydro(), network, [[0]])
    assert result.lines.set_index("line_id").flow_mw.to_dict() == pytest.approx(
        {"ab": 30, "bc": 30, "ac": 60}
    )
    assert result.audit["max_nodal_balance_residual_mw"] < 1e-7


def test_water_is_spent_in_most_expensive_hour_of_joint_dispatch():
    c = unit_hydro()
    net = two_bus(2, transfer=200, demand=[[0, 1], [0, 102]])
    result = solve_grid(c, net, [[0], [0]], terminal_min={"p": 0})
    assert result.hydro.power_mw.tolist() == pytest.approx([0, 1])
    assert result.audit["hydro_energy_mwh"] == pytest.approx(1)
    assert result.audit["operating_cost_eur"] == pytest.approx(2100)


def test_half_hour_energy_price_and_water_units():
    c = unit_hydro(storage=1800, capacity=1800)
    net = two_bus(1, transfer=200, demand=[[0, 102]])
    result = solve_grid(c, net, [[0]], dt_seconds=1800, terminal_min={"p": 0})
    assert result.audit["hydro_energy_mwh"] == pytest.approx(0.5)
    assert result.audit["operating_cost_eur"] == pytest.approx(1040)
    assert result.buses.price_eur_mwh.iloc[-1] == pytest.approx(80)
    assert result.final_state.storage_m3[0] == 0


def test_delayed_water_is_not_duplicated_as_independent_energy_storage():
    c = Cascade(
        (
            Plant("upper", "upper", 3600, 3600, 1, 1e6 / (1000 * 9.81 * 0.9)),
            Plant("lower", "lower", 3600, 0, 1, 1e6 / (1000 * 9.81 * 0.9)),
        ),
        (Link("upper", "lower", 1),),
    )
    n = two_bus(2, 100, demand=[[0, 100], [0, 100]])
    n = replace(n, hydro_buses={"upper": "a", "lower": "a"})
    r = solve_grid(c, n, [[0, 0], [0, 0]], terminal_min={"upper": 0, "lower": 0})
    assert r.hydro.query("plant_id == 'lower'").power_mw.tolist() == pytest.approx([0, 1])
    assert r.audit["hydro_energy_mwh"] == pytest.approx(2)
    assert r.audit["mapped_native_storage_count"] == 0
    assert r.audit["max_water_residual_m3"] < 1e-6


def test_fixed_schedule_is_preserved_and_excess_is_reported_as_dump():
    c = unit_hydro()
    schedule = optimize(c, [[0]], [100], terminal_min={"p": 0})
    net = two_bus(1, transfer=0, demand=[[0, 0]])
    r = solve_grid(c, net, [[0]], terminal_min={"p": 0}, fixed_schedule=schedule)
    assert r.hydro.power_mw.tolist() == pytest.approx(schedule.records.power_mw.tolist())
    assert r.audit["dumped_energy_mwh"] == pytest.approx(1)
    assert r.audit["mode"] == "fixed_schedule_assessment"
    assert r.buses.price_eur_mwh.iloc[0] == pytest.approx(-1000)
    joint = solve_grid(c, net, [[0]], terminal_min={"p": 0}, terminal_value={"p": 0.01})
    assert joint.audit["dumped_energy_mwh"] == 0


@pytest.mark.parametrize("surplus", [False, True])
def test_signed_prices_match_independent_demand_finite_differences(surplus):
    c = unit_hydro() if surplus else zero_hydro()
    net = two_bus(1, transfer=0 if surplus else 10, demand=[[0.2, 0]] if surplus else [[10, 50]])
    fixed = optimize(c, [[0]], [100], terminal_min={"p": 0}) if surplus else None
    base = solve_grid(c, net, [[0]], fixed_schedule=fixed, terminal_min={"p": 0})
    demand = net.demand_mw.copy()
    demand[0, 0] += 1e-4
    perturbed = solve_grid(
        c, replace(net, demand_mw=demand), [[0]], fixed_schedule=fixed, terminal_min={"p": 0}
    )
    sensitivity = (
        perturbed.audit["terminal_adjusted_cost_eur"] - base.audit["terminal_adjusted_cost_eur"]
    ) / 1e-4
    assert sensitivity == pytest.approx(base.buses.price_eur_mwh.iloc[0], abs=1e-4)


def test_empty_hydro_keeps_environmental_shortfall_visible():
    c = Cascade((Plant("p", "dry", 0, 0, 1, 50, min_release_m3s=2),))
    r = solve_grid(c, two_bus(), [[0]])
    assert r.audit["environmental_shortfall_m3"] == 7200
    assert r.audit["hydro_energy_mwh"] == 0


def test_repeated_sqlite_exports_do_not_leave_windows_file_locks(tmp_path):
    for _ in range(2):
        r = solve_grid(zero_hydro(), two_bus(), [[0]], output=tmp_path)
        assert r.audit["unserved_energy_mwh"] == 0
    assert (tmp_path / "powergama.sqlite").exists()
    assert json.loads((tmp_path / "audit.json").read_text())["powergama_commit"] == POWERGAMA_COMMIT


def test_joint_objective_dominates_fixed_schedule_on_common_terminal_basis():
    c, n, inflows, prices = build_case("congested", 12, 42)
    targets = {p.id: 0 for p in c.plants}
    values = carryover_values(c)
    fixed = optimize(c, inflows, prices, terminal_min=targets, terminal_value=values)
    assessed = solve_grid(c, n, inflows, fixed_schedule=fixed, terminal_min=targets, terminal_value=values)
    joint = solve_grid(c, n, inflows, terminal_min=targets, terminal_value=values)
    assert joint.audit["terminal_adjusted_cost_eur"] <= assessed.audit["terminal_adjusted_cost_eur"] + 1e-5
    assert joint.audit["max_capacity_violation_mw"] < 1e-6


def test_reinforcement_does_not_increase_optimal_terminal_adjusted_cost():
    c, n, inflows, _ = build_case("congested", 12, 2)
    targets = {p.id: 0 for p in c.plants}
    values = carryover_values(c)
    base = solve_grid(c, n, inflows, terminal_min=targets, terminal_value=values)
    stronger = replace(n, lines=tuple(replace(line, capacity_mw=line.capacity_mw * 2) for line in n.lines))
    improved = solve_grid(c, stronger, inflows, terminal_min=targets, terminal_value=values)
    assert improved.audit["terminal_adjusted_cost_eur"] <= base.audit["terminal_adjusted_cost_eur"] + 1e-5


def test_initial_water_in_transit_has_consistent_terminal_value():
    c = Cascade(
        (Plant("p", "p", 3600, 0, 1, 50), Plant("lower", "lower", 3600, 0, 1, 50)), (Link("p", "lower", 3),)
    )
    state = CascadeState.initial(c)
    state.queues_m3[0] = [0, 0, 3600]
    net = replace(two_bus(), hydro_buses={"p": "a", "lower": "b"})
    result = solve_grid(c, net, [[0, 0]], state=state, terminal_value={"lower": 100 / 3600})
    assert result.audit["terminal_water_value_eur"] == pytest.approx(100)
    assert result.audit["terminal_adjusted_cost_eur"] == pytest.approx(3500)


def test_fixed_schedule_schema_and_bounds_fail_loudly():
    import pandas as pd

    c = unit_hydro()
    for fixed in (
        pd.DataFrame(),
        pd.DataFrame(
            {"step": [0], "plant_id": ["p"], "turbine_m3s": [2], "bypass_m3s": [0], "spill_m3s": [0]}
        ),
    ):
        with pytest.raises(ValueError):
            HydroExtension(c, np.array([[0]]), CascadeState.initial(c), 3600, {"p": 0}, {}, fixed)


def test_full_window_and_no_second_storage_guards():
    from hydrocascade.powergama.backend import _grid
    from types import SimpleNamespace

    c = unit_hydro()
    ext = HydroExtension(c, np.array([[0]]), CascadeState.initial(c), 3600, {"p": 0}, {})
    grid, _ = _grid(c, two_bus(), 1, 3600)
    with pytest.raises(ValueError, match="full-window"):
        ext.prepare(SimpleNamespace(_cfg=SimpleNamespace(window_hours=2, window_commit=1)), grid)
    grid.generator.loc[0, "storage_cap"] = 3600
    with pytest.raises(ValueError, match="second native"):
        ext.prepare(SimpleNamespace(_cfg=SimpleNamespace(window_hours=1, window_commit=1)), grid)
    with pytest.raises(ValueError, match="resume"):
        ext.on_solve_start(None, [0], True, None)


def test_complete_small_study_and_portable_manifest(tmp_path):
    summary = run_study(tmp_path / "study", steps=3)
    assert len(summary) == 18
    manifest = json.loads((tmp_path / "study" / "manifest.json").read_text())
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "hydrocascade"
    assert set(manifest["code_sha256"]) == {
        p.relative_to(package.parent).as_posix() for p in package.rglob("*.py")
    }
    assert all("\\" not in key for key in manifest["code_sha256"])
    assert all("\\" not in key for key in manifest["artifact_sha256"])
    assert manifest["powergama_commit"] == POWERGAMA_COMMIT
    with pytest.raises(ValueError, match="empty"):
        run_study(tmp_path / "study", steps=3)
