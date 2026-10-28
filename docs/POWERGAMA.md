# PowerGAMA integration specification

The optional backend asks a specific question: how do transmission limits change the value and use
of a physical hydro cascade? It supports electrical assessment of a fixed physical schedule and a
joint, deterministic, known-future water/electricity optimization. It is a research implementation,
not a calibrated Swedish market model or a BID3 replacement.

## Installation and source boundary

Keep the core installation and grid environment separate. The core supports Python 3.10-3.12;
this optional backend requires Python 3.11+ because of the pinned upstream package. CI exercises
the integration on Python 3.12, Linux and Windows.

```bash
python -m venv .venv-grid
# Activate .venv-grid using your platform's normal activation command.
python -m pip install -c constraints-powergama.txt -e ".[dev,powergama]"
hydro-grid --output runs/grid-study --steps 48 --seed 42
```

PowerGAMA is installed from exact commit
[`dd681aba3ca32ce00bd9c38a3acbd624eb6dd6bb`](https://github.com/powergama/powergama/tree/dd681aba3ca32ce00bd9c38a3acbd624eb6dd6bb),
which reports version `1.6.0.dev0`. This is a development-source pin, not a claim that a released
PowerGAMA 1.5 installation supplies the same extension interface. Runtime provenance checks reject
an installation without that exact VCS commit. Direct dependency versions are recorded in
`constraints-powergama.txt`; transitive dependencies are not fully locked. Git and internet access
are needed at installation; the synthetic study itself is offline and uses the open HiGHS solver.
PowerGAMA is MIT licensed by SINTEF; see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Ownership of water and electrical state

`HydroExtension` inserts the existing `scheduling.water_block` equalities, inequalities, bounds and
objective terms into the native PowerGAMA Pyomo model through its `ModelExtension` interface.
There is one reservoir/transit formulation in cubic metres. Each mapped native hydro generator has
`storage_cap=0`, `storage_ini=0` and `pump_cap=0`. Its native availability is constant and its output
is constrained to the cascade's actual turbine discharge:

```text
hydro power MW = (1000 * 9.81 * efficiency * fixed head / 1e6) * turbine discharge m3/s
interval energy MWh = power MW * dt_seconds / 3600
storage change m3 = (inflow - turbine - bypass - spill) m3/s * dt_seconds
```

River links carry turbine, bypass and spill releases after integer delays. The original physical
simulator independently replays all optimized controls, including water still in transit at the
horizon. The same water can legitimately generate again downstream after it arrives. A second
native MWh reservoir would count availability twice and is explicitly rejected.

PowerGAMA supplies bus balance, phase-angle-based meshed DC flows, thermal generation, available
renewables, load shedding and dumping. This is a lossless DC approximation on a 100 MVA base.
For the pinned implementation, the exported angle convention satisfies
`flow_from_to = 100 * (angle_to - angle_from) / reactance_pu`. Tests verify a two-bus congestion
solution and a three-bus loop-flow solution independently of the adapter's audit code.

## Objectives, prices and fixed-schedule assessment

The joint objective in EUR is:

```text
thermal operating cost
  + 1000 EUR/MWh * (unserved energy + dumped energy)
  + 1000 EUR/m3 * environmental release shortfall
  - terminal reservoir-and-transit water value
```

The penalties are illustrative constants, recorded in every audit. They are not estimated market
prices, regulatory values or enforceable hard environmental guarantees. Load shedding is bounded
above by actual demand. Water-block costs are divided by interval hours when inserted into
PowerGAMA's hourly-rate objective; the final objective is converted back to EUR. A half-hour known
answer checks the conversion. Independent objective reconstruction includes initial water still
in transit beyond the horizon, a constant absent from the native optimization objective.

`terminal_min=None` retains the core's configured initial-storage targets. The published study
explicitly sets zero minimum targets and values remaining water at the common generation-equivalent
65 EUR/MWh coefficients, including downstream potential and transit water. This is a finite-horizon
approximation, not a fitted seasonal water-value policy. All policies use the same targets and values.

In fixed mode, turbine, bypass and spill controls are fixed exactly to a complete physical schedule.
The electrical solver may dispatch thermal/renewables and report unserved energy or dumped energy;
it cannot silently curtail hydro and continue to claim the original water trajectory. Negative
prices and high penalties can therefore appear when a fixed hydro schedule ignores congestion.
This diagnostic is not a realistic balancing-market or redispatch model.

CSV nodal prices use **signed raw power-balance duals**, divided by the upstream base MVA. At this
pin, the native [SQLite extractor](https://github.com/powergama/powergama/blob/dd681aba3ca32ce00bd9c38a3acbd624eb6dd6bb/src/powergama/lp/formulation/extract.py)
applies `abs()` to those prices. Native SQLite is retained for upstream inspection, but its price
series is not used for CSV prices, hydro revenue or economic claims here. Integration tests compare
signed prices with independent demand finite differences, including a -1000 EUR/MWh surplus case.
Duals can be nonunique in degenerate LPs; hydro revenue is a model settlement diagnostic rather
than a producer-profit or investment forecast.

## Python API

```python
from hydrocascade.powergama.study import build_case
from hydrocascade.powergama.backend import solve_grid
from hydrocascade.experiments import carryover_values
from hydrocascade.scheduling import optimize

cascade, network, inflows, prices = build_case("congested", steps=24, seed=42)
targets = {p.id: 0 for p in cascade.plants}
values = carryover_values(cascade)
schedule = optimize(cascade, inflows, prices, terminal_min=targets, terminal_value=values)
assessment = solve_grid(
    cascade,
    network,
    inflows,
    fixed_schedule=schedule,
    terminal_min=targets,
    terminal_value=values,
)
joint = solve_grid(cascade, network, inflows, terminal_min=targets, terminal_value=values)
print(joint.audit)
```

`Network`, `Bus`, `ACLine` and `ThermalUnit` are validated definitions in `powergama.network`.
Demand and renewable availability have shape `(intervals, buses)`. Hydro-to-bus mappings must cover
the cascade exactly. Inputs reject duplicate IDs, unknown endpoints, invalid reactance/capacity,
negative demand/availability and incomplete fixed controls. `solve_grid(..., output=path)` exports
`hydro.csv`, `buses.csv`, `lines.csv`, `generation.csv`, `audit.json` and native `powergama.sqlite`.
The study saves the complete cascade, network, inflows, prices and terminal assumptions per case.

## Verification and reproducibility

```bash
python -m pytest tests/test_powergama.py tests/test_grid_inputs.py -q \
  --cov=hydrocascade/powergama --cov-config=.coveragerc-powergama
python tools/check_grid_evidence.py
```

The suite exercises known marginal prices and loop flows, negative-price sensitivities, half-hour
units, delayed water, initial transit value, fixed-control assessment, environmental shortage,
reinforcement monotonicity, joint-versus-fixed objectives, Windows SQLite cleanup, source pins,
input rejection and a complete small study. Optional imports leave the core usable without PowerGAMA.

Every solve checks native constraint/bound violations, independent water conservation, independent
nodal/DC-flow/capacity residuals and independent objective reconstruction. The study verifies that
joint optimization weakly dominates both feasible fixed policies on the same terminal-adjusted
basis, and that relaxing corridor limits cannot increase the optimal objective.

To refresh the published evidence, use a new empty run/output directory:

```bash
hydro-grid --output runs/grid-new --steps 48 --seed 42
python tools/export_grid_evidence.py --source runs/grid-new --destination docs/grid-evidence-new
```

Review the new evidence and replace the published evidence deliberately. Both commands reject a
nonempty destination. The manifest hashes all package sources and all exported study artifacts
except native SQLite and the manifest itself. It records the seed, inputs, package versions and
upstream commit. Native databases remain in local runs and CI artifacts, avoiding opaque binary
Git diffs. Run times, LP-degenerate controls and plots can vary across versions/platforms; economic
invariants and residual tolerances are the portability contract, not byte-identical reruns.

## Current limits and extension points

Only one complete deterministic optimization window is supported. Truncated/rolling windows and
resume are rejected: they require explicit reservoir and transit-state handoff, not native storage
substitution. The core's existing scenario-tree and rolling-policy experiments remain separate;
they are not claimed as stochastic PowerGAMA dispatch.

There is no calibrated national topology, AC voltage/reactive-power assessment, N-1 security,
unit commitment, variable head, pumps, bid formation, reserve activation, balancing settlement,
seasonal SDDP, observed-price backcast or investment valuation in this adapter. No real grid or
plant parameters can be inferred from the illustrative Nordic coordinates. The appropriate next
steps are a validated network/data case, held-out reservoir/generation/discharge observations,
rolling state transfer and forecast backtests. Each needs its own data and verification evidence;
installing another solver does not establish commercial-model credibility.
