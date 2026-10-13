import numpy as np
import pytest
from hydrocascade import Cascade, CascadeState, Plant, Link, Scenario, optimize, stochastic_schedule
from hydrocascade.experiments import demo_cascade, synthetic_series, rolling_policy, evaluate_policies
from hydrocascade.lp import OptimizationError


def unit_cascade(**kwargs):
    # Exactly 1 MW per m3/s, so 3600 m3 produces 1 MWh in a one-hour interval.
    return Cascade(
        (
            Plant(
                "p",
                "Analytic",
                **(
                    dict(
                        storage_capacity_m3=3600,
                        initial_storage_m3=3600,
                        max_flow_m3s=1,
                        head_m=1e6 / (1000 * 9.81 * 0.9),
                    )
                    | kwargs
                ),
            ),
        )
    )


def test_analytical_two_price_dispatch():
    result = optimize(unit_cascade(), np.zeros((2, 1)), [10, 100], terminal_min={"p": 0})
    assert result.records.turbine_m3s.tolist() == pytest.approx([0, 1])
    assert result.revenue_eur == pytest.approx(100)
    assert result.final_state.storage_m3[0] == 0
    assert result.audit["max_water_residual_m3"] < 1e-7


def test_default_terminal_storage_prevents_end_of_horizon_drain():
    result = optimize(unit_cascade(), np.zeros((2, 1)), [10, 100])
    assert result.revenue_eur == 0
    assert result.final_state.storage_m3[0] == 3600


def test_terminal_water_value_can_prefer_saving():
    result = optimize(unit_cascade(), [[0]], [10], terminal_min={"p": 0}, terminal_value={"p": 100 / 3600})
    assert result.revenue_eur == 0
    assert result.objective_eur == pytest.approx(100)


def test_negative_prices_do_not_force_turbine_generation():
    result = optimize(
        unit_cascade(initial_storage_m3=0), [[1]], [-100], terminal_min={"p": 0}, terminal_value={"p": 1e-4}
    )
    assert result.records.power_mw.iloc[0] == 0
    assert result.final_state.storage_m3[0] == 3600


def test_environmental_infeasibility_is_visible_and_mass_feasible():
    result = optimize(unit_cascade(initial_storage_m3=0, min_release_m3s=2), [[0]], [100])
    assert result.records.environmental_shortfall_m3s.iloc[0] == 2
    assert result.audit["environmental_shortfall_m3"] == 7200
    assert result.records.power_mw.iloc[0] == 0


def test_impossible_terminal_condition_raises_solver_error():
    with pytest.raises(OptimizationError):
        optimize(unit_cascade(initial_storage_m3=0), [[0]], [0], terminal_min={"p": 3600})
    with pytest.raises(ValueError):
        optimize(unit_cascade(), [[0]], [0], terminal_min={"p": 4000})


def test_delayed_cascade_replays_and_retains_water():
    cascade = demo_cascade()
    inflow, price = synthetic_series(24, 17)
    result = optimize(cascade, inflow, price)
    assert result.audit["max_water_residual_m3"] < 1e-6
    assert result.records.turbine_m3s.ge(-1e-7).all()
    assert sum(map(sum, result.final_state.queues_m3)) > 0


def test_initial_pipeline_arrival_is_available_to_optimizer():
    cascade = Cascade(
        unit_cascade(initial_storage_m3=0).plants + (Plant("b", "b", 3600, 0, 1, 1e6 / (1000 * 9.81 * 0.9)),),
        (Link("p", "b", 2),),
    )
    state = CascadeState.initial(cascade)
    state.queues_m3[0] = [3600, 0]
    result = optimize(cascade, [[0, 0]], [100], state=state)
    assert result.records[result.records.plant_id == "b"].energy_mwh.iloc[0] == pytest.approx(1)


def test_ramp_limits_replay():
    result = optimize(unit_cascade(ramp_m3s_per_step=0.25), [[0], [0]], [10, 100], terminal_min={"p": 0})
    assert result.records.turbine_m3s.tolist() == pytest.approx([0.25, 0.5])


def test_stochastic_shared_decision_avoids_future_knowledge():
    c = unit_cascade()
    scenarios = [
        Scenario("high", 0.5, np.zeros((2, 1)), np.array([50, 100]), ("root", "high")),
        Scenario("low", 0.5, np.zeros((2, 1)), np.array([50, 0]), ("root", "low")),
    ]
    schedules, audit = stochastic_schedule(c, scenarios, terminal_min={"p": 0})
    assert schedules["high"].records.turbine_m3s.iloc[0] == pytest.approx(
        schedules["low"].records.turbine_m3s.iloc[0]
    )
    assert audit["expected_objective_eur"] == pytest.approx(50)
    # Separate perfect-foresight schedules would yield expected revenue 75.
    assert audit["expected_objective_eur"] < 75


@pytest.mark.parametrize("bad", ["probability", "root_price", "merged_history", "horizon"])
def test_invalid_scenario_trees_are_rejected(bad):
    a = Scenario("a", 0.5, np.zeros((3, 1)), np.array([50, 20, 10]), ("root", "a", "last"))
    b = Scenario("b", 0.5, np.zeros((3, 1)), np.array([50, 20, 10]), ("root", "b", "b-last"))
    if bad == "probability":
        b = Scenario("b", 0.2, b.inflows, b.prices, b.nodes)
    elif bad == "root_price":
        b = Scenario("b", 0.5, b.inflows, np.array([60, 20, 10]), b.nodes)
    elif bad == "merged_history":
        b = Scenario("b", 0.5, b.inflows, b.prices, ("root", "b", "last"))
    else:
        b = Scenario("b", 0.5, b.inflows[:2], b.prices[:2], b.nodes[:2])
    with pytest.raises(ValueError):
        stochastic_schedule(unit_cascade(), [a, b])


def test_rolling_policy_first_action_does_not_access_future_realizations():
    c = demo_cascade()
    train_f, train_p = synthetic_series(100, 3)
    flow, price = synthetic_series(4, 4)
    altered = flow.copy()
    altered[1:] *= 10
    r1, _, _ = rolling_policy(c, flow, price, train_f, train_p, horizon=4)
    r2, _, _ = rolling_policy(c, altered, price * np.array([1, 10, 10, 10]), train_f, train_p, horizon=4)
    assert r1[r1.step == 0].turbine_m3s.tolist() == pytest.approx(r2[r2.step == 0].turbine_m3s.tolist())


def test_oracle_dominates_feasible_policy_on_same_terminal_basis():
    results = evaluate_policies(trials=2, steps=12, horizon=4, seed=8)
    wide = results.pivot(index="trial", columns="policy", values="objective_eur")
    for policy in ("inflow_following", "rolling_deterministic", "rolling_stochastic"):
        assert (wide.perfect_foresight + 1e-5 >= wide[policy]).all()
    assert results.max_water_residual_m3.max() < 1e-6


def test_optimizer_rejects_invalid_shapes_values_and_terminal_ids():
    for flow, price in (([[float("nan")]], [1]), ([[0]], [float("nan")]), ([[0]], [1, 2]), ([], [])):
        with pytest.raises(ValueError):
            optimize(unit_cascade(), flow, price)
    with pytest.raises(ValueError):
        optimize(unit_cascade(), [[0]], [1], terminal_value={"missing": 1})


def test_terminal_objective_includes_initial_water_still_in_transit():
    cascade = Cascade(
        unit_cascade(initial_storage_m3=0).plants + (Plant("b", "b", 3600, 0, 1, 1e6 / (1000 * 9.81 * 0.9)),),
        (Link("p", "b", 3),),
    )
    state = CascadeState.initial(cascade)
    state.queues_m3[0] = [0, 0, 3600]
    result = optimize(cascade, [[0, 0]], [0], state=state, terminal_value={"b": 100 / 3600})
    assert result.objective_eur == pytest.approx(100)
    assert sum(result.final_state.queues_m3[0]) == 3600
