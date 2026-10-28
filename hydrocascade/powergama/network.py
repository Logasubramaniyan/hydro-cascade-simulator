"""Backend-neutral, validated network inputs. These are illustrative, not calibrated grids."""

from dataclasses import dataclass
import numpy as np
from ..model import Cascade, finite
from ..simulation import array


@dataclass(frozen=True)
class Bus:
    id: str
    area: str = "illustrative"
    latitude: float = 60.0
    longitude: float = 15.0

    def __post_init__(self):
        if not self.id or not self.area:
            raise ValueError("Bus id and area are required")
        finite(self.latitude, "latitude")
        finite(self.longitude, "longitude")
        if abs(self.latitude) > 90 or abs(self.longitude) > 180:
            raise ValueError("Coordinates outside latitude/longitude bounds")


@dataclass(frozen=True)
class ACLine:
    id: str
    source: str
    target: str
    reactance_pu: float
    capacity_mw: float

    def __post_init__(self):
        if not self.id or self.source == self.target:
            raise ValueError("Line needs an id and different endpoints")
        finite(self.reactance_pu, "reactance_pu", 1e-12)
        finite(self.capacity_mw, "capacity_mw", 0)


@dataclass(frozen=True)
class ThermalUnit:
    id: str
    bus: str
    capacity_mw: float
    marginal_cost_eur_mwh: float

    def __post_init__(self):
        if not self.id:
            raise ValueError("Thermal generator id is required")
        finite(self.capacity_mw, "thermal capacity", 0)
        finite(self.marginal_cost_eur_mwh, "thermal marginal cost", 0)


@dataclass(frozen=True)
class Network:
    buses: tuple[Bus, ...]
    lines: tuple[ACLine, ...]
    thermal: tuple[ThermalUnit, ...]
    hydro_buses: dict[str, str]
    demand_mw: np.ndarray
    renewable_available_mw: np.ndarray

    def validate(self, cascade: Cascade, intervals: int):
        ids = {bus.id for bus in self.buses}
        if not ids or len(ids) != len(self.buses):
            raise ValueError("Network needs nonempty, unique buses")
        if len({line.id for line in self.lines}) != len(self.lines):
            raise ValueError("Line ids must be unique")
        if len({unit.id for unit in self.thermal}) != len(self.thermal):
            raise ValueError("Thermal ids must be unique")
        generator_ids = (
            list(cascade.index)
            + [unit.id for unit in self.thermal]
            + [f"renewable_{bus.id}" for bus in self.buses]
        )
        if len(generator_ids) != len(set(generator_ids)):
            raise ValueError("Hydro, thermal and renewable generator ids must not collide")
        for line in self.lines:
            if line.source not in ids or line.target not in ids:
                raise ValueError("Line references unknown bus")
        if any(unit.bus not in ids for unit in self.thermal):
            raise ValueError("Thermal generator references unknown bus")
        if set(self.hydro_buses) != set(cascade.index) or not set(self.hydro_buses.values()) <= ids:
            raise ValueError("Map every hydro plant to exactly one known bus")
        array(self.demand_mw, (intervals, len(self.buses)), "demand")
        array(self.renewable_available_mw, (intervals, len(self.buses)), "renewable availability")

    def to_dict(self):
        from dataclasses import asdict

        return dict(
            buses=[asdict(bus) for bus in self.buses],
            lines=[asdict(line) for line in self.lines],
            thermal=[asdict(unit) for unit in self.thermal],
            hydro_buses=self.hydro_buses,
            demand_mw=np.asarray(self.demand_mw).tolist(),
            renewable_available_mw=np.asarray(self.renewable_available_mw).tolist(),
        )

    @classmethod
    def from_dict(cls, data):
        return cls(
            tuple(Bus(**bus) for bus in data["buses"]),
            tuple(ACLine(**line) for line in data["lines"]),
            tuple(ThermalUnit(**unit) for unit in data["thermal"]),
            data["hydro_buses"],
            np.asarray(data["demand_mw"], dtype=float),
            np.asarray(data["renewable_available_mw"], dtype=float),
        )
