"""Hydraulic curves and individual generating units, with explicit SI-unit boundaries."""

from dataclasses import dataclass
import numpy as np

from ..model import finite


def integer(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def curve_points(x, y, name):
    x, y = np.asarray(x, float), np.asarray(y, float)
    if (
        x.ndim != 1
        or x.shape != y.shape
        or len(x) < 2
        or not np.isfinite(x).all()
        or not np.isfinite(y).all()
        or (np.diff(x) <= 0).any()
    ):
        raise ValueError(f"{name}: finite matching vectors with strictly increasing coordinates required")
    return tuple(x), tuple(y)


@dataclass(frozen=True)
class Unit:
    id: str
    plant_id: str
    flow_m3s: tuple
    efficiency: tuple
    min_up_steps: int = 1
    min_down_steps: int = 1
    startup_cost_eur: float = 0
    shutdown_cost_eur: float = 0
    running_cost_eur_per_hour: float = 0
    ramp_m3s_per_hour: float = 1e6
    hydraulic_loss_m_per_m3s2: float = 0

    def __post_init__(self):
        if (
            not isinstance(self.id, str)
            or not self.id
            or not isinstance(self.plant_id, str)
            or not self.plant_id
        ):
            raise ValueError("Unit and plant ids must be nonempty strings")
        x, y = curve_points(self.flow_m3s, self.efficiency, "Unit efficiency")
        if x[0] <= 0 or any(e <= 0 or e > 1 for e in y):
            raise ValueError("Unit minimum flow must be positive; efficiency must be in (0,1]")
        object.__setattr__(self, "flow_m3s", x)
        object.__setattr__(self, "efficiency", y)
        for name in ("min_up_steps", "min_down_steps"):
            integer(getattr(self, name), name)
        for name in (
            "startup_cost_eur",
            "shutdown_cost_eur",
            "running_cost_eur_per_hour",
            "ramp_m3s_per_hour",
            "hydraulic_loss_m_per_m3s2",
        ):
            finite(getattr(self, name), name, 0)

    def efficiency_at(self, flow):
        finite(flow, "unit flow", 0)
        if flow < self.flow_m3s[0] - 1e-7 or flow > self.flow_m3s[-1] + 1e-7:
            raise ValueError("Unit flow outside curve range")
        return float(np.interp(flow, self.flow_m3s, self.efficiency))


@dataclass(frozen=True)
class UnitState:
    on: bool
    age_steps: int
    flow_m3s: float

    def __post_init__(self):
        if not isinstance(self.on, bool):
            raise ValueError("Unit state on must be boolean")
        integer(self.age_steps, "age_steps", 0)
        finite(self.flow_m3s, "initial unit flow", 0)
        if not self.on and self.flow_m3s != 0:
            raise ValueError("An off unit must have zero initial flow")


@dataclass(frozen=True)
class ReservoirCurve:
    plant_id: str
    storage_m3: tuple
    forebay_m: tuple
    tailwater_m: float

    def __post_init__(self):
        x, y = curve_points(self.storage_m3, self.forebay_m, "Reservoir level")
        if not isinstance(self.plant_id, str) or not self.plant_id or x[0] < 0 or (np.diff(y) < 0).any():
            raise ValueError("Reservoir id, nonnegative volume and nondecreasing level are required")
        finite(self.tailwater_m, "tailwater_m")
        object.__setattr__(self, "storage_m3", x)
        object.__setattr__(self, "forebay_m", y)

    def mean_level(self, start_m3, end_m3):
        low, high = sorted((float(start_m3), float(end_m3)))
        if (
            not np.isfinite([low, high]).all()
            or low < self.storage_m3[0] - 1e-5
            or high > self.storage_m3[-1] + 1e-5
        ):
            raise ValueError("Storage outside reservoir curve range")
        points = np.r_[low, [s for s in self.storage_m3 if low < s < high], high]
        levels = np.interp(points, self.storage_m3, self.forebay_m)
        if high - low <= 1e-10:
            return float(levels[0])
        # Exact integral for a piecewise-linear level curve and linear volume trajectory.
        return float(np.sum(np.diff(points) * (levels[:-1] + levels[1:]) / 2) / (high - low))

    def power(self, unit, flow_m3s, start_m3, end_m3):
        finite(flow_m3s, "unit flow", 0)
        level = self.mean_level(start_m3, end_m3)
        if flow_m3s == 0:
            return 0.0
        head = level - self.tailwater_m - unit.hydraulic_loss_m_per_m3s2 * flow_m3s**2
        if head <= 0:
            raise ValueError("Operating head must be positive")
        return float(0.00981 * head * flow_m3s * unit.efficiency_at(flow_m3s))
