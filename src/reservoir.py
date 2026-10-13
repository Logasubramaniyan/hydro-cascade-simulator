"""Legacy single-plant adapter backed by the shared physical engine (SI units)."""

from hydrocascade.model import Cascade, Plant
from hydrocascade.simulation import CascadeState, step


class ReservoirPlant:
    def __init__(self, cfg):
        plant = Plant(
            id="plant",
            name=cfg["name"],
            head_m=cfg["head"],
            efficiency=cfg["efficiency"],
            max_flow_m3s=cfg["max_flow"],
            storage_capacity_m3=cfg["storage_capacity"],
            initial_storage_m3=cfg["initial_storage"],
            min_release_m3s=cfg.get("min_flow", 20),
        )
        self.cascade = Cascade((plant,))
        self.state = CascadeState.initial(self.cascade)
        self.name, self.capacity = plant.name, plant.storage_capacity_m3
        self.history = {"release": [], "storage": [], "power_mw": [], "shortfall_m3s": []}

    @property
    def storage(self):
        return float(self.state.storage_m3[0])

    def step(self, inflow, dt_seconds=86400):
        plant = self.cascade.plants[0]
        self.state, frame, _ = step(
            self.cascade, self.state, [inflow], [min(inflow, plant.max_flow_m3s)], dt_seconds
        )
        row = frame.iloc[0]
        release = float(row.turbine_m3s + row.bypass_m3s + row.spill_m3s)
        for key, value in (
            ("release", release),
            ("storage", self.storage),
            ("power_mw", row.power_mw),
            ("shortfall_m3s", row.environmental_shortfall_m3s),
        ):
            self.history[key].append(float(value))
        return release
