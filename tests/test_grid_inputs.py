"""Input schema and optionality can be tested without importing PowerGAMA."""

from dataclasses import replace
import numpy as np
import pytest
from hydrocascade.powergama.network import ACLine, Bus, ThermalUnit, Network
from hydrocascade.powergama.study import build_case


def test_network_roundtrip_and_case_defaults():
    cascade, network, inflows, _ = build_case()
    restored = Network.from_dict(network.to_dict())
    restored.validate(cascade, len(inflows))
    assert np.array_equal(network.demand_mw, restored.demand_mw)
    assert network.lines == restored.lines


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: Bus(""),
        lambda: Bus("a", latitude=91),
        lambda: ACLine("x", "a", "a", 0.1, 1),
        lambda: ACLine("x", "a", "b", 0, 1),
        lambda: ThermalUnit("x", "a", -1, 1),
    ],
)
def test_invalid_physical_network_parameters(constructor):
    with pytest.raises(ValueError):
        constructor()


def test_network_rejects_unmapped_hydro_shapes_and_unknown_nodes():
    c, n, flow, _ = build_case(steps=3)
    bad_networks = (
        replace(n, hydro_buses={}),
        replace(n, demand_mw=np.zeros((1, 3))),
        replace(n, renewable_available_mw=np.full((3, 3), -1)),
        replace(n, lines=(ACLine("bad", "missing", "south", 0.1, 1),)),
        replace(n, buses=(n.buses[0], n.buses[0])),
        replace(n, thermal=(ThermalUnit("g", "missing", 1, 1),)),
        replace(n, thermal=(ThermalUnit("upper", "south", 1, 1),)),
    )
    for bad in bad_networks:
        with pytest.raises(ValueError):
            bad.validate(c, len(flow))


def test_cases_are_seeded_and_changed_factors_are_explicit():
    _, n, inflow, prices = build_case("congested", 12, 17)
    _, wet, wet_flow, wet_prices = build_case("wet", 12, 17)
    assert np.allclose(inflow * 1.6, wet_flow)
    assert np.array_equal(prices, wet_prices)
    assert np.array_equal(n.demand_mw, wet.demand_mw)
    with pytest.raises(ValueError):
        build_case("unknown")
    with pytest.raises(ValueError):
        build_case(corridor_scale=-1)
