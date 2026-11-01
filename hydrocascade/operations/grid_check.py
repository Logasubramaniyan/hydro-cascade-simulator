"""Optional downstream PowerGAMA check of a physical activation trajectory."""

import numpy as np


def check_demo_grid(cascade, scenario, records, state, output, terminal_min, terminal_value):
    from ..powergama.network import Bus, ACLine, ThermalUnit, Network
    from ..powergama.backend import solve_grid

    steps = len(scenario.day_ahead_eur_mwh)
    checks = []
    for name, rating in (("intact", 40.0), ("connection_derated", 10.0)):
        network = Network(
            (Bus("hydro"), Bus("load")),
            (ACLine("connection", "hydro", "load", 0.1, rating),),
            (ThermalUnit("backup", "load", 70, 120),),
            {p.id: "hydro" for p in cascade.plants},
            np.tile([5.0, 40.0], (steps, 1)),
            np.zeros((steps, 2)),
        )
        result = solve_grid(
            cascade,
            network,
            scenario.inflows_m3s,
            dt_seconds=900,
            state=state,
            terminal_min=terminal_min,
            terminal_value=terminal_value,
            fixed_schedule=records,
            output=output / name,
        )
        checks.append(
            dict(
                network_case=name,
                **result.audit,
                grid_can_absorb_schedule=result.audit["dumped_energy_mwh"] < 1e-6
                and result.audit["unserved_energy_mwh"] < 1e-6,
            )
        )
    return checks
