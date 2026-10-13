"""Fixed-head hydropower models with explicit SI units and auditable results."""

from .model import Cascade, Link, Plant
from .simulation import CascadeState, simulate, step
from .scheduling import Scenario, optimize, stochastic_schedule

__version__ = "0.2.0"
__all__ = [
    "Cascade",
    "Link",
    "Plant",
    "CascadeState",
    "Scenario",
    "simulate",
    "step",
    "optimize",
    "stochastic_schedule",
]
