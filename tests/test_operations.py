from dataclasses import replace
import numpy as np
import pandas as pd
import pytest

from hydrocascade import Cascade, CascadeState, Link, Plant
from hydrocascade.lp import OptimizationError
from hydrocascade.operations import (
    MarketScenario,
    MarketTerms,
    OperatingLimits,
    optimize_offers,
    assess_offer,
    lower_tail_mean,
)
from hydrocascade.operations.state import advance_dispatch, DispatchCheckpoint


def unit(storage=900, capacity=900, **kwargs):
    return Cascade(
        (Plant("p", "analytic one-MW plant", capacity, storage, 1, 1e6 / (1000 * 9.81 * 0.9), **kwargs),)
    )


def scenario(steps=1, name="s", probability=1.0, prices=None, activation=None, inflows=None, nodes=None):
    return MarketScenario(
        name,
        probability,
        np.zeros((steps, 1)) if inflows is None else inflows,
        np.full(steps, 100) if prices is None else prices,
        np.zeros(steps) if activation is None else activation,
        np.zeros(steps),
        np.zeros(steps),
        np.zeros(steps),
        tuple(nodes or ()),
    )


def terms(steps=1, up=0, down=0, price=0, export=1):
    return MarketTerms(
        np.full(steps, price),
        np.full(steps, price),
        np.full(steps, up),
        np.full(steps, down),
        np.full(steps, export),
    )


def solve(c, scenarios, market=None, limits=None, **kwargs):
    steps = len(scenarios[0].day_ahead_eur_mwh)
    return optimize_offers(
        c,
        scenarios,
        market or terms(steps),
        limits or OperatingLimits.standard(c, steps),
        terminal_min={p.id: 0 for p in c.plants},
        **kwargs,
    )


def test_quarter_hour_cashflow_and_water_known_answer():
    c = unit()
    result = solve(c, [scenario()])
    assert result.offers.energy_mw.iloc[0] == pytest.approx(1)
    assert result.outcomes.day_ahead_eur.iloc[0] == pytest.approx(25)
    assert result.schedules["s"].energy_mwh.sum() == pytest.approx(0.25)
    assert result.final_states["s"].storage_m3[0] == pytest.approx(0)
    assert result.audit["max_water_residual_m3"] < 1e-7


def test_half_hour_conversion_and_terminal_value():
    c = unit(1800, 1800)
    r = solve(c, [scenario(prices=[40])], dt_seconds=1800)
    assert r.outcomes.day_ahead_eur.iloc[0] == pytest.approx(20)
    saved = solve(c, [scenario(prices=[40])], dt_seconds=1800, terminal_value={"p": 100 / 3600})
    assert saved.offers.energy_mw.iloc[0] == 0
    assert saved.audit["expected_profit_eur"] == pytest.approx(50)


def test_weighted_tail_fractional_atoms_and_input_rejection():
    assert lower_tail_mean([-100, 0, 100], [0.1, 0.2, 0.7], 0.8) == pytest.approx(-50)
    assert lower_tail_mean([-100, 0, 100], [0.1, 0.2, 0.7], 0) == pytest.approx(60)
    for values, probabilities, confidence in (
        ([], [], 0.9),
        ([1], [0], 0.9),
        ([1], [1], 1),
        ([np.nan], [1], 0.9),
    ):
        with pytest.raises(ValueError):
            lower_tail_mean(values, probabilities, confidence)


def test_cvar_changes_the_commitment_under_adverse_price_tail():
    c = unit()
    paths = [
        scenario(name="good", probability=0.8, prices=[100]),
        scenario(name="bad", probability=0.2, prices=[-100]),
    ]
    neutral = solve(c, paths, risk_weight=0, confidence=0.8)
    cautious = solve(c, paths, risk_weight=0.8, confidence=0.8)
    assert neutral.offers.energy_mw.iloc[0] == pytest.approx(1)
    assert neutral.audit["expected_profit_eur"] == pytest.approx(15)
    assert neutral.audit["lower_tail_profit_eur"] == pytest.approx(-25)
    assert cautious.offers.energy_mw.iloc[0] == pytest.approx(0)
    assert cautious.audit["lower_tail_profit_eur"] == pytest.approx(0)


def test_sustained_activation_exposes_headroom_only_overbooking():
    c, steps = unit(), 8
    paths = [scenario(steps, prices=np.zeros(steps), activation=np.ones(steps))]
    market = terms(steps, up=1, price=20)
    r = solve(c, paths, market)
    assert r.offers.reserve_up_mw.sum() == pytest.approx(1)
    assert r.audit["deliverable_on_tested_paths"]
    naive = pd.DataFrame(
        {"step": range(steps), "energy_mw": 0.0, "reserve_up_mw": 1.0, "reserve_down_mw": 0.0}
    )
    assessed = assess_offer(
        c, paths, market, OperatingLimits.standard(c, steps), naive, terminal_min={"p": 0}
    )
    assert not assessed.audit["deliverable_on_tested_paths"]
    assert assessed.outcomes.undelivered_energy_mwh.iloc[0] == pytest.approx(1.75, abs=1e-6)
    assert assessed.audit["max_water_residual_m3"] < 1e-7


def test_environmental_ramp_includes_bypass_and_spill_when_turbine_unavailable():
    c = unit(0, 0)
    paths = [scenario(inflows=[[2]])]
    limits = replace(
        OperatingLimits.standard(c, 1), availability=np.zeros((1, 1)), release_ramp_m3s_per_hour=np.array([1])
    )
    with pytest.raises(OptimizationError):
        solve(c, paths, limits=limits)
    r = solve(c, paths, limits=replace(limits, release_ramp_m3s_per_hour=np.array([8])))
    frame = r.schedules["s"]
    assert frame.turbine_m3s.iloc[0] == 0
    assert frame.bypass_m3s.iloc[0] + frame.spill_m3s.iloc[0] == pytest.approx(2)


def test_environmental_minimum_has_no_soft_penalty_escape():
    c = unit(0, 0, min_release_m3s=1)
    with pytest.raises(OptimizationError):
        solve(c, [scenario()])
    c = unit(min_release_m3s=1)
    limits = replace(OperatingLimits.standard(c, 1), availability=np.zeros((1, 1)))
    r = solve(c, [scenario()], limits=limits)
    assert r.schedules["s"].bypass_m3s.iloc[0] == pytest.approx(1)


def test_maintenance_and_time_varying_flood_limits():
    c = unit()
    limits = replace(
        OperatingLimits.standard(c, 1), availability=np.full((1, 1), 0.3), max_storage_m3=np.full((1, 1), 450)
    )
    r = solve(c, [scenario()], limits=limits)
    assert r.offers.energy_mw.iloc[0] == pytest.approx(0.3)
    assert r.final_states["s"].storage_m3[0] <= 450 + 1e-7


def test_down_activation_cashflow_sign_and_no_fictitious_generation():
    c = unit()
    s = replace(scenario(prices=[20]), activation_down=np.ones(1), down_energy_eur_mwh=np.full(1, -5))
    fixed = pd.DataFrame({"energy_mw": [1.0], "reserve_up_mw": [0.0], "reserve_down_mw": [1.0]})
    r = solve(c, [s], terms(down=1), fixed_offers=fixed)
    assert r.outcomes.activation_eur.iloc[0] == pytest.approx(1.25)
    assert r.audit["expected_profit_eur"] == pytest.approx(6.25)
    assert r.schedules["s"].power_mw.iloc[0] == 0


def test_nonanticipative_dispatch_and_no_recombining_or_different_observations():
    c = unit(1800, 1800)
    a = scenario(2, name="a", probability=0.5, activation=[0, 1], nodes=("root", "a"))
    b = scenario(2, name="b", probability=0.5, activation=[0, 0], nodes=("root", "b"))
    r = solve(c, [a, b], terms(2, up=1, price=10))
    for col in ("turbine_m3s", "bypass_m3s", "spill_m3s"):
        assert r.schedules["a"][col].iloc[0] == pytest.approx(r.schedules["b"][col].iloc[0])
    with pytest.raises(ValueError, match="revealed"):
        solve(c, [a, replace(b, activation_up=np.ones(2))])
    a = scenario(2, name="a", probability=0.5, nodes=("a", "merge"))
    b = scenario(2, name="b", probability=0.5, inflows=[[1], [0]], nodes=("b", "merge"))
    with pytest.raises(ValueError, match="history"):
        solve(c, [a, b])


def test_equal_observed_histories_cannot_be_split_to_leak_future_information():
    c = unit()
    a = scenario(name="a", probability=0.5, nodes=("a",))
    b = scenario(name="b", probability=0.5, nodes=("b",))
    with pytest.raises(ValueError, match="Identical observed"):
        solve(c, [a, b])


def test_zero_probability_security_paths_reduce_offers_without_distorting_tail_weights():
    c = unit()
    expected = scenario(2, prices=[0, 0])
    stress = replace(expected, name="stress", probability=0.0, role="security", activation_up=np.ones(2))
    unconstrained = solve(c, [expected], terms(2, up=1, price=10))
    secured = solve(c, [expected, stress], terms(2, up=1, price=10))
    assert unconstrained.offers.reserve_up_mw.sum() == pytest.approx(2)
    assert secured.offers.reserve_up_mw.sum() == pytest.approx(1)
    assert secured.audit["expected_profit_eur"] == pytest.approx(2.5)
    assert secured.audit["lower_tail_profit_eur"] == pytest.approx(2.5)
    assert secured.audit["security_scenarios"] == 1


def delayed_case():
    first = unit().plants[0]
    lower = replace(first, id="lower", name="lower", initial_storage_m3=0)
    c = Cascade((first, lower), (Link("p", "lower", 1),))
    s = replace(scenario(2, prices=[0, 0], activation=[1, 1]), inflows_m3s=np.zeros((2, 2)))
    fixed = pd.DataFrame({"energy_mw": [0, 0], "reserve_up_mw": [1, 1], "reserve_down_mw": [0, 0]})
    r = solve(c, [s], terms(2, up=1, export=2), fixed_offers=fixed)
    return c, s, r


def test_delayed_downstream_water_is_available_only_after_arrival():
    c, s, r = delayed_case()
    frame = r.schedules["s"]
    lower = frame[frame.plant_id == "lower"]
    assert lower.power_mw.tolist() == pytest.approx([0, 1])
    assert frame.energy_mwh.sum() == pytest.approx(0.5)
    assert r.audit["max_water_residual_m3"] < 1e-7


def test_checkpoints_keep_transit_and_total_release_across_execution_boundary(tmp_path):
    c, s, r = delayed_case()
    initial = CascadeState.initial(c)
    first = advance_dispatch(c, initial, s.inflows_m3s, r.schedules["s"], 1)
    assert first.state.queues_m3 == [[900]]
    path = tmp_path / "checkpoint.json"
    first.save(path)
    loaded = DispatchCheckpoint.load(path, c, 900)
    suffix = r.schedules["s"].query("step == 1").assign(step=0)
    second = advance_dispatch(c, loaded.state, s.inflows_m3s[1:], suffix, 1)
    assert second.state.storage_m3 == pytest.approx(r.final_states["s"].storage_m3)
    assert second.state.queues_m3 == r.final_states["s"].queues_m3
    assert first.last_release_m3s == pytest.approx([1, 0])
    for changed_c, dt in ((c, 3600), (unit(), 900)):
        with pytest.raises(ValueError, match="mismatch"):
            DispatchCheckpoint.load(path, changed_c, dt)


def test_initial_transit_terminal_value_is_included_in_risk_accounting():
    c, _, _ = delayed_case()
    c = replace(c, links=(Link("p", "lower", 3),))
    state = CascadeState.initial(c)
    state.storage_m3[:] = 0
    state.queues_m3[0] = [0, 0, 900]
    s = replace(scenario(), inflows_m3s=np.zeros((1, 2)), day_ahead_eur_mwh=np.zeros(1))
    r = solve(c, [s], terms(export=2), state=state, terminal_value={"lower": 0.1})
    assert r.audit["expected_profit_eur"] == pytest.approx(90)
    assert r.audit["lower_tail_profit_eur"] == pytest.approx(90)


def test_release_variation_cost_reconstructed_independently():
    c = unit()
    limits = replace(OperatingLimits.standard(c, 1), variation_cost_eur_per_m3s=np.array([3]))
    r = solve(c, [scenario()], limits=limits)
    assert r.audit["expected_profit_eur"] == pytest.approx(22)
    assert r.outcomes.release_variation_cost_eur.iloc[0] == pytest.approx(3)


@pytest.mark.parametrize("change", ["probability", "activation", "availability", "release", "risk", "fixed"])
def test_bad_inputs_rejected(change):
    c, paths, market = unit(), [scenario()], terms()
    limits, kwargs = OperatingLimits.standard(c, 1), {}
    if change == "probability":
        paths = [replace(paths[0], probability=0)]
    elif change == "activation":
        paths = [replace(paths[0], activation_up=[2])]
    elif change == "availability":
        limits = replace(limits, availability=[[1.1]])
    elif change == "release":
        limits = replace(limits, min_release_m3s=[[2]], max_release_m3s=[[1]])
    elif change == "risk":
        kwargs = {"risk_weight": 1}
    else:
        kwargs = {"fixed_offers": pd.DataFrame()}
    with pytest.raises(ValueError):
        solve(c, paths, market, limits, **kwargs)
