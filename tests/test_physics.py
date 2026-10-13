from dataclasses import replace
import numpy as np
import pytest
from hydrocascade import Cascade, Link, Plant, CascadeState, simulate, step


def plant(id="p", **kwargs):
    return Plant(
        id,
        id,
        **(
            dict(storage_capacity_m3=1_000_000, initial_storage_m3=500_000, max_flow_m3s=100, head_m=50)
            | kwargs
        ),
    )


def test_empty_reservoir_records_shortfall_without_generation():
    cascade = Cascade((plant(initial_storage_m3=0, min_release_m3s=20),))
    state, rows, audit = step(cascade, CascadeState.initial(cascade), [0], [100])
    assert state.storage_m3[0] == 0
    assert rows.power_mw.iloc[0] == 0
    assert rows.environmental_shortfall_m3s.iloc[0] == 20
    assert audit["water_residual_m3"] == 0


def test_daily_rate_conversion_and_energy():
    cascade = Cascade((plant(storage_capacity_m3=10_000_000, initial_storage_m3=0),))
    state, rows, _ = step(cascade, CascadeState.initial(cascade), [10], [2], 86400)
    assert state.storage_m3[0] == 8 * 86400
    assert rows.energy_mwh.iloc[0] == pytest.approx(rows.power_mw.iloc[0] * 24)


def test_stored_water_can_generate_without_inflow():
    cascade = Cascade((plant(max_flow_m3s=5, efficiency=0.5),))
    state, rows, _ = step(cascade, CascadeState.initial(cascade), [0], [100])
    assert rows.turbine_m3s.iloc[0] == 5
    assert rows.power_mw.iloc[0] == pytest.approx(1000 * 9.81 * 50 * 5 * 0.5 / 1e6)
    assert state.storage_m3[0] == 500_000 - 5 * 3600


def test_spill_routes_downstream_and_preserves_water():
    cascade = Cascade(
        (
            plant("upper", storage_capacity_m3=100, initial_storage_m3=100),
            plant("lower", initial_storage_m3=0),
        ),
        (Link("upper", "lower"),),
    )
    state, rows, audit = step(cascade, CascadeState.initial(cascade), [10, 0], [0, 0], 10)
    assert rows.loc["upper", "spill_m3s"] == 10
    assert state.storage_m3[1] == 100
    assert audit["water_residual_m3"] == 0


def test_delay_keeps_water_in_transit_until_arrival():
    cascade = Cascade(
        (plant("a", initial_storage_m3=100), plant("b", initial_storage_m3=0)),
        (Link("a", "b", delay_steps=2),),
    )
    state = CascadeState.initial(cascade)
    state, _, audit = step(cascade, state, [0, 0], [5, 0], 10)
    assert state.storage_m3.tolist() == [50, 0]
    assert audit["transit_m3"] == 50
    state, _, _ = step(cascade, state, [0, 0], [0, 0], 10)
    assert state.storage_m3[1] == 0
    state, _, audit = step(cascade, state, [0, 0], [0, 0], 10)
    assert state.storage_m3[1] == 50
    assert audit["transit_m3"] == 0
    assert audit["water_residual_m3"] == 0


def test_diversion_unrouted_water_is_counted_at_sink():
    cascade = Cascade((plant("a"), plant("b", initial_storage_m3=0)), (Link("a", "b", fraction=0.7),))
    state, _, audit = step(cascade, CascadeState.initial(cascade), [0, 0], [10, 0], 10)
    assert state.storage_m3[1] == 70
    assert audit["sink_m3"] == pytest.approx(30)
    assert audit["water_residual_m3"] == 0


def test_randomized_multireservoir_conservation_with_losses_and_delays():
    cascade = Cascade(
        (plant("a", loss_m3s=0.5), plant("b"), plant("c")),
        (Link("a", "b", 2, 0.6), Link("a", "c", 0, 0.3), Link("b", "c", 1)),
    )
    rng = np.random.default_rng(123)
    records, audit, state = simulate(
        cascade, rng.uniform(0, 200, (200, 3)), rng.uniform(0, 200, (200, 3)), dt_seconds=3600
    )
    assert audit.water_residual_m3.abs().max() < 1e-6
    assert records.water_residual_m3.abs().max() < 1e-6
    assert records.storage_m3.between(0, 1_000_000).all()
    assert records.turbine_m3s.le(100).all()
    state.validate(cascade)


def test_environmental_bypass_and_ramp_limits():
    cascade = Cascade((plant(min_release_m3s=10, ramp_m3s_per_step=3),))
    _, rows, _ = step(cascade, CascadeState.initial(cascade), [20], [20])
    assert rows.turbine_m3s.iloc[0] == 3
    assert rows.bypass_m3s.iloc[0] == 7
    assert rows.environmental_shortfall_m3s.iloc[0] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"efficiency": 0},
        {"efficiency": 1.1},
        {"head_m": -1},
        {"initial_storage_m3": -1},
        {"initial_storage_m3": 2e6},
        {"max_flow_m3s": float("nan")},
        {"ramp_m3s_per_step": -1},
    ],
)
def test_reject_invalid_plant_parameters(kwargs):
    with pytest.raises(ValueError):
        plant(**kwargs)


def test_invalid_topology_is_rejected():
    p = (plant("a"), plant("b"))
    for links in (
        (Link("a", "b"), Link("b", "a")),
        (Link("a", "missing"),),
        (Link("a", "b", fraction=0.8), Link("a", "b", fraction=0.5)),
    ):
        with pytest.raises(ValueError):
            Cascade(p, links)
    with pytest.raises(ValueError):
        Link("a", "b", delay_steps=1.5)
    with pytest.raises(ValueError):
        Cascade((p[0], p[0]))
    with pytest.raises(ValueError):
        Cascade(())


def test_state_and_controls_validate_shape_finiteness_and_bounds():
    cascade = Cascade((plant(),))
    state = CascadeState.initial(cascade)
    for inflow, control, dt in (
        ([-1], [0], 3600),
        ([0], [float("nan")], 3600),
        ([0], [0], 0),
        ([0, 1], [0], 3600),
    ):
        with pytest.raises(ValueError):
            step(cascade, state, inflow, control, dt)
    with pytest.raises(ValueError):
        step(cascade, state, [0], [500], enforce_controls=True)
    state.storage_m3[0] = 2e6
    with pytest.raises(ValueError):
        state.validate(cascade)


def test_model_json_roundtrip():
    cascade = Cascade((plant("a"), plant("b")), (Link("a", "b", 1),))
    assert Cascade.from_dict(cascade.to_dict()) == cascade
    assert replace(cascade.plants[0], efficiency=0.5).capacity_mw < cascade.plants[0].capacity_mw
