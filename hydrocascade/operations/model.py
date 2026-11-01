"""Explicit time-resolved engineering inputs, independent of any TSO's qualification rules."""

from dataclasses import dataclass
from hashlib import sha256
import numpy as np

from ..simulation import array


@dataclass(frozen=True)
class MarketScenario:
    name: str
    probability: float
    inflows_m3s: np.ndarray
    day_ahead_eur_mwh: np.ndarray
    activation_up: np.ndarray
    activation_down: np.ndarray
    up_energy_eur_mwh: np.ndarray
    down_energy_eur_mwh: np.ndarray
    nodes: tuple[str, ...] = ()
    role: str = "forecast"

    def __post_init__(self):
        for name in (
            "inflows_m3s",
            "day_ahead_eur_mwh",
            "activation_up",
            "activation_down",
            "up_energy_eur_mwh",
            "down_energy_eur_mwh",
        ):
            object.__setattr__(self, name, np.asarray(getattr(self, name), dtype=float))
        object.__setattr__(self, "nodes", tuple(self.nodes))


@dataclass(frozen=True)
class MarketTerms:
    up_capacity_eur_mw_h: np.ndarray
    down_capacity_eur_mw_h: np.ndarray
    max_up_mw: np.ndarray
    max_down_mw: np.ndarray
    export_limit_mw: np.ndarray

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, np.asarray(getattr(self, name), dtype=float))

    def validate(self, steps):
        for name in self.__dataclass_fields__:
            array(getattr(self, name), (steps,), name, nonnegative="eur" not in name)


@dataclass(frozen=True)
class OperatingLimits:
    min_release_m3s: np.ndarray
    max_release_m3s: np.ndarray
    min_storage_m3: np.ndarray
    max_storage_m3: np.ndarray
    availability: np.ndarray
    release_ramp_m3s_per_hour: np.ndarray
    initial_release_m3s: np.ndarray
    variation_cost_eur_per_m3s: np.ndarray

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, np.asarray(getattr(self, name), dtype=float))

    @classmethod
    def standard(cls, cascade, steps):
        """Broad finite defaults; callers must supply site-specific licences and outage plans."""
        n = len(cascade.plants)
        return cls(
            np.tile([p.min_release_m3s for p in cascade.plants], (steps, 1)),
            np.full((steps, n), 1e6),
            np.tile([p.min_storage_m3 for p in cascade.plants], (steps, 1)),
            np.tile([p.storage_capacity_m3 for p in cascade.plants], (steps, 1)),
            np.ones((steps, n)),
            np.full(n, 1e6),
            np.zeros(n),
            np.zeros(n),
        )

    def validate(self, cascade, steps):
        n = len(cascade.plants)
        for name in self.__dataclass_fields__:
            shape = (
                (steps, n)
                if name
                in ("min_release_m3s", "max_release_m3s", "min_storage_m3", "max_storage_m3", "availability")
                else (n,)
            )
            array(getattr(self, name), shape, name)
        if (self.availability > 1).any():
            raise ValueError("Availability must be in [0,1]")
        if (self.min_release_m3s > self.max_release_m3s).any():
            raise ValueError("Minimum release exceeds maximum")
        if (self.min_storage_m3 > self.max_storage_m3).any():
            raise ValueError("Minimum storage exceeds maximum")
        for i, plant in enumerate(cascade.plants):
            if (self.min_storage_m3[:, i] < plant.min_storage_m3).any() or (
                self.max_storage_m3[:, i] > plant.storage_capacity_m3
            ).any():
                raise ValueError("Time-varying storage limits exceed physical bounds")


def validate_scenarios(cascade, scenarios, state, dt_seconds):
    from ..scheduling import inputs

    if (
        not scenarios
        or len({s.name for s in scenarios}) != len(scenarios)
        or any(not s.name for s in scenarios)
    ):
        raise ValueError("Scenarios need unique nonempty names")
    probabilities = np.array([s.probability for s in scenarios])
    if (
        not np.isfinite(probabilities).all()
        or (probabilities < 0).any()
        or not np.isclose(probabilities.sum(), 1, rtol=0, atol=1e-10)
    ):
        raise ValueError("Forecast probabilities must sum to one; security scenarios have zero probability")
    if any(
        s.role not in ("forecast", "security") or (s.probability == 0) != (s.role == "security")
        for s in scenarios
    ):
        raise ValueError("Forecast probabilities must be positive; only security scenarios have zero weight")
    steps = len(scenarios[0].day_ahead_eur_mwh)
    shared, prepared, node_paths, observed_labels = {}, {}, {}, {}
    for scenario in scenarios:
        inflows, _, initial = inputs(
            cascade, scenario.inflows_m3s, scenario.day_ahead_eur_mwh, state, dt_seconds
        )
        if len(inflows) != steps or (
            scenario.nodes and (len(scenario.nodes) != steps or any(not node for node in scenario.nodes))
        ):
            raise ValueError("Scenario horizons and nonempty node paths must agree")
        for name in (
            "day_ahead_eur_mwh",
            "activation_up",
            "activation_down",
            "up_energy_eur_mwh",
            "down_energy_eur_mwh",
        ):
            array(getattr(scenario, name), (steps,), name, nonnegative=name.startswith("activation"))
        if (scenario.activation_up + scenario.activation_down > 1 + 1e-12).any():
            raise ValueError("Activation fractions must sum to at most one per interval")
        history_digest, path = b"", []
        for t in range(steps):
            # Whole DA curve is revealed after the auction; current activation/inflow and
            # balancing energy prices are observed before this interval's recourse action.
            observation = np.r_[
                scenario.day_ahead_eur_mwh,
                inflows[t],
                scenario.activation_up[t],
                scenario.activation_down[t],
                scenario.up_energy_eur_mwh[t],
                scenario.down_energy_eur_mwh[t],
            ]
            history_digest = sha256(history_digest + observation.astype("<f8").tobytes()).digest()
            canonical = history_digest.hex()
            node = scenario.nodes[t] if scenario.nodes else canonical
            key = (t, node)
            if key in shared:
                history, reference = shared[key]
                if tuple(path) != history or not np.array_equal(observation, reference):
                    raise ValueError(
                        "Shared dispatch nodes must have identical revealed information and history"
                    )
            else:
                shared[key] = (tuple(path), observation)
            observed_key = (t, canonical)
            if observed_key in observed_labels and observed_labels[observed_key] != node:
                raise ValueError("Identical observed histories cannot use different dispatch nodes")
            observed_labels[observed_key] = node
            path.append(node)
        prepared[scenario.name] = (inflows, initial)
        node_paths[scenario.name] = tuple(path)
    return steps, prepared, node_paths
