import numpy as np
import pytest
from hydrocascade import Cascade, Plant
from hydrocascade.market import Thermal, Interconnector, clear_market, adequacy_sample


def empty_hydro(zone="a"):
    return Cascade((Plant("p", "No water", 0, 0, 10, 50, zone=zone),))


def test_merit_order_cost_and_marginal_price():
    result = clear_market(empty_hydro(), [[0]], ["a"], [Thermal("g", "a", 100, 40)], [], [[50]])
    assert result.thermal.power_mw.iloc[0] == 50
    assert result.zones.price_eur_mwh.iloc[0] == 40
    assert result.audit["objective"] == 2000
    assert result.audit["unserved_energy_mwh"] == 0


def test_congestion_separates_zonal_prices():
    result = clear_market(
        empty_hydro(),
        [[0]],
        ["a", "b"],
        [Thermal("cheap", "a", 100, 20), Thermal("costly", "b", 100, 80)],
        [Interconnector("a", "b", 10)],
        [[10, 50]],
    )
    assert result.flows.flow_mw.iloc[0] == 10
    assert result.zones.price_eur_mwh.tolist() == [20, 80]
    assert result.thermal.power_mw.tolist() == [20, 40]


def test_reserve_cannot_double_count_generation_capacity():
    result = clear_market(
        empty_hydro(), [[0]], ["a"], [Thermal("g", "a", 100, 40)], [], [[95]], reserve_mw=[[10]]
    )
    row = result.thermal.iloc[0]
    assert row.power_mw + row.reserve_mw <= 100 + 1e-6
    assert result.zones.unserved_mw.iloc[0] + result.zones.reserve_shortfall_mw.iloc[0] == pytest.approx(5)


def test_empty_reservoir_cannot_sell_hydro_reserves():
    result = clear_market(empty_hydro(), [[0]], ["a"], [], [], [[0]], reserve_mw=[[5]])
    assert result.hydro.reserve_mw.iloc[0] == 0
    assert result.zones.reserve_shortfall_mw.iloc[0] == 5


def test_renewable_curtailment_and_scarcity_energy():
    surplus = clear_market(empty_hydro(), [[0]], ["a"], [], [], [[10]], [[30]])
    assert surplus.zones.renewable_curtailed_mw.iloc[0] == 20
    scarcity = clear_market(empty_hydro(), [[0]], ["a"], [], [], [[10]], dt_seconds=1800)
    assert scarcity.audit["unserved_energy_mwh"] == 5
    assert scarcity.zones.price_eur_mwh.iloc[0] == 10000


def test_continuous_investment_covers_load_when_economic():
    result = clear_market(
        empty_hydro(),
        [[0]],
        ["a"],
        [Thermal("g", "a", 0, 40)],
        [],
        [[50]],
        investment={"g": {"max_build_mw": 100, "cost_eur_per_mw_for_horizon": 100}},
    )
    assert result.builds["g"] == 50
    assert result.audit["objective"] == 7000


def test_seeded_outage_trials_are_reproducible_and_energy_limited():
    args = (empty_hydro(), np.zeros((3, 1)), ["a"], [Thermal("g", "a", 50, 40)], [], [[50], [50], [50]])
    a = adequacy_sample(*args, outage_probabilities=[1], trials=3, seed=1)
    b = adequacy_sample(*args, outage_probabilities=[1], trials=3, seed=1)
    assert a.equals(b)
    assert a.unserved_energy_mwh.tolist() == [150] * 3
    assert a.loss_of_load_hours.tolist() == [3] * 3


def test_market_rejects_invalid_availability_and_zones():
    with pytest.raises(ValueError):
        clear_market(empty_hydro(), [[0]], ["a"], [Thermal("g", "a", 10, 20)], [], [[1]], availability=[[2]])
    with pytest.raises(ValueError):
        clear_market(empty_hydro(), [[0]], ["b"], [], [], [[1]])
    with pytest.raises(ValueError):
        clear_market(empty_hydro(), [[0]], ["a"], [], [], [[1]], investment={"missing": {}})
