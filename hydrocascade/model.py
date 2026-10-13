"""Validated immutable model inputs. Flow: m3/s; storage: m3; head: m."""

from dataclasses import asdict, dataclass
import math


def finite(value, name, minimum=None):
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be finite" + (f" and >= {minimum}" if minimum is not None else ""))


@dataclass(frozen=True)
class Plant:
    id: str
    name: str
    storage_capacity_m3: float
    initial_storage_m3: float
    max_flow_m3s: float
    head_m: float
    efficiency: float = 0.9
    min_storage_m3: float = 0.0
    min_release_m3s: float = 0.0
    ramp_m3s_per_step: float | None = None
    loss_m3s: float = 0.0
    zone: str = "system"

    def __post_init__(self):
        if not self.id or not self.name or not self.zone:
            raise ValueError("Plant id, name and zone cannot be empty")
        for field in (
            "storage_capacity_m3",
            "initial_storage_m3",
            "max_flow_m3s",
            "head_m",
            "min_storage_m3",
            "min_release_m3s",
            "loss_m3s",
        ):
            finite(getattr(self, field), field, 0)
        finite(self.efficiency, "efficiency", 0)
        if not 0 < self.efficiency <= 1 or self.head_m <= 0:
            raise ValueError("Efficiency must be in (0,1] and head must be positive")
        if not self.min_storage_m3 <= self.initial_storage_m3 <= self.storage_capacity_m3:
            raise ValueError("Initial storage must be between minimum and capacity")
        if self.ramp_m3s_per_step is not None:
            finite(self.ramp_m3s_per_step, "ramp_m3s_per_step", 0)

    @property
    def mw_per_m3s(self):
        return 1000 * 9.81 * self.head_m * self.efficiency / 1e6

    @property
    def capacity_mw(self):
        return self.mw_per_m3s * self.max_flow_m3s


@dataclass(frozen=True)
class Link:
    source: str
    target: str
    delay_steps: int = 0
    fraction: float = 1.0

    def __post_init__(self):
        if (
            isinstance(self.delay_steps, bool)
            or not isinstance(self.delay_steps, int)
            or self.delay_steps < 0
        ):
            raise ValueError("delay_steps must be a nonnegative integer")
        finite(self.fraction, "fraction", 0)
        if self.fraction > 1 or self.source == self.target:
            raise ValueError("Invalid link fraction or self-loop")


@dataclass(frozen=True)
class Cascade:
    plants: tuple[Plant, ...]
    links: tuple[Link, ...] = ()

    def __post_init__(self):
        if not self.plants or len({p.id for p in self.plants}) != len(self.plants):
            raise ValueError("A cascade needs plants with unique ids")
        index = self.index
        if len({(link.source, link.target) for link in self.links}) != len(self.links):
            raise ValueError("Duplicate links are not supported")
        fractions = dict.fromkeys(index, 0.0)
        for link in self.links:
            if link.source not in index or link.target not in index:
                raise ValueError("Link references an unknown plant")
            fractions[link.source] += link.fraction
        if any(f > 1 + 1e-12 for f in fractions.values()):
            raise ValueError("Outgoing fractions must sum to at most one")
        self.order  # Reject cycles, including delayed cycles: this is a river DAG.

    @property
    def index(self):
        return {p.id: i for i, p in enumerate(self.plants)}

    @property
    def order(self):
        predecessors = {p.id: set() for p in self.plants}
        for link in self.links:
            predecessors[link.target].add(link.source)
        order = []
        while len(order) < len(self.plants):
            ready = [p.id for p in self.plants if p.id not in order and predecessors[p.id] <= set(order)]
            if not ready:
                raise ValueError("Cascade links must form an acyclic graph")
            order.extend(ready)
        return tuple(self.index[p] for p in order)

    def to_dict(self):
        return {"plants": [asdict(p) for p in self.plants], "links": [asdict(link) for link in self.links]}

    @classmethod
    def from_dict(cls, data):
        return cls(
            tuple(Plant(**p) for p in data["plants"]), tuple(Link(**link) for link in data.get("links", []))
        )
