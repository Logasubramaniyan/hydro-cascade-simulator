# Hydropower flexibility under grid congestion

This reproducible 48-hour experiment shows how a physical cascade and transmission constraints
interact. It compares three scheduling policies using the same synthetic water, demand and wind
inputs. The code and evidence are intended to demonstrate modelling and verification skills;
these are not calibrated Swedish market outcomes or estimates of commercial savings.

![Six-case hydro-grid comparison](grid-evidence/grid_study.svg)

## Experimental design

Three fixed-head reservoirs are connected by two river links, each with a one-hour travel delay.
The upper plant connects to `north_a`; the middle and lower plants connect to `north_b`. A meshed
three-bus lossless DC network connects these buses to `south`. An internal line has reactance
0.08 pu and rating 150 MW; the two southbound lines have reactances 0.20/0.10 pu and ratings
20/30 MW. Loop flows obey the network equations; the two corridor ratings cannot simply be
summed into an independently controllable transfer limit.

Northern thermal capacity is 60 MW at 55 EUR/MWh, southern capacity 200 MW at 120 EUR/MWh.
Northern demand is 20 and 15 MW; southern demand varies between 95 and 165 MW. Available wind
at `north_a` varies between 25 and 105 MW. Seed 42 generates the cascade's inflows and exogenous
price series. Every exact input is saved in [per-case evidence](grid-evidence/).

| Policy | Hydro decisions | Electrical decisions |
|---|---|---|
| Inflow following | Original simulator's feasible release rule | Native dispatch with hydro controls fixed |
| Price based | Optimize against an exogenous price series without network feedback | Native dispatch with hydro controls fixed |
| Joint | Optimize water and generation together over all 48 hours | Native meshed DC dispatch in the same model |

All policies receive identical input paths and terminal accounting. Fixed-policy assessment can
dump hydro energy at an illustrative 1000 EUR/MWh penalty when its schedule cannot be absorbed.
That preserves the original physical release instead of pretending hydro was curtailed without
changing reservoir state. Joint optimization chooses a feasible release trajectory directly.

All runs relax minimum terminal targets to zero and value remaining reservoir/transit water at
common generation-equivalent 65 EUR/MWh coefficients. Consequently the comparison metric is
thermal operating cost plus shortfall/dump penalties minus terminal water value. A lower adjusted
cost need not mean a lower current thermal bill: preserving future water has value in this model.
This fixed carryover value is an assumption, not an estimated seasonal opportunity cost.

## Results

Terminal-adjusted system costs in EUR, taken from [summary.csv](grid-evidence/summary.csv):

| Case | Inflow following | Price based | Joint |
|---|---:|---:|---:|
| Unconstrained network | 151,827.81 | 108,753.52 | 87,768.65 |
| Congested network | 437,352.79 | 1,094,641.84 | 415,582.31 |
| High wind, 1.8 times available output | 436,187.47 | 1,093,715.40 | 415,582.31 |
| Dry, 0.45 times inflow | 486,897.41 | 823,991.13 | 466,583.08 |
| Wet, 1.6 times inflow | 729,218.26 | 1,225,788.80 | 384,850.07 |
| Reinforced southbound lines, 2.5 times ratings | 164,353.72 | 161,336.37 | 139,153.49 |

The unconstrained case sets all line ratings to 1000 MW. Other cases retain the congested ratings
except the specified changes. The same seed and underlying paths are used, isolating each change.

In the congested case, price-based scheduling produces 620.15 MWh of dumped electrical energy.
Joint scheduling eliminates that dumping and curtails 1,160.19 MWh of available wind, compared
with 1,658.38 MWh under inflow following. This is a demonstration of a schedule ignoring network
feedback; the large price-policy penalty gap is not a forecast of achievable market revenue.
Signed prices in this fixed-schedule assessment range from -1000 to 120 EUR/MWh. Hydro revenue
can therefore be negative and is reported with its correct sign, not an absolute price.

High wind leaves the joint objective unchanged here because additional northern wind cannot
displace southern thermal output through the constrained network; joint wind curtailment rises
to 3,656.19 MWh. This result depends on this synthetic topology, load and marginal-cost pattern.

The joint congested case incurs 489,921.98 EUR of current thermal operating cost and an adjusted
cost of 415,582.31 EUR. In the reinforced case those values are 212,811.19 and 139,153.49 EUR.
Joint scheduling is assessed on the complete adjusted objective; it is not asserted to minimize
the thermal component alone relative to every fixed schedule.

## Corridor sensitivity

The [sensitivity output](grid-evidence/corridor_sensitivity.csv) changes only the two southbound
ratings. Joint terminal-adjusted costs decline as the feasible electrical set expands:

| Corridor rating multiplier | Joint adjusted cost, EUR |
|---:|---:|
| 0.5 | 537,182.31 |
| 1.0 | 415,582.31 |
| 1.5 | 297,136.08 |
| 2.0 | 196,006.96 |
| 2.5 | 139,153.49 |
| 4.0 | 87,768.65 |

These are operating-study outcomes over two days. No capital cost, annual chronology, discounting,
outage uncertainty or annualized welfare assessment is included, so this is not a reinforcement
NPV or investment recommendation.

## Verification and provenance

The committed [manifest](grid-evidence/manifest.json) records exact package source hashes,
input/output hashes, seed, environment and the reviewed PowerGAMA source commit. Each policy has
hydro/bus/line/generator CSVs and an audit JSON. Native SQLite is retained in local/CI run artifacts,
not committed. Its upstream price table uses absolute-valued duals at this pin; all price/revenue
claims above use the adapter's signed CSV exports. See [the integration specification](POWERGAMA.md).

Across the 18 principal solves, the maximum water conservation residual is below 2e-9 m3, nodal
balance residual below 2e-13 MW and DC-flow residual below 3e-14 MW. There is no reported line
capacity violation, unserved energy or environmental shortfall. CI checks tolerances of 1e-5
and verifies joint dominance over the two fixed policies and monotonic corridor sensitivity.
Analytical tests independently establish known prices, loop flows and half-hour conversion.

```bash
hydro-grid --output runs/grid-reproduction --steps 48 --seed 42
python tools/check_grid_evidence.py
```

The first command creates a fresh experiment; the second verifies the committed evidence against
its recorded source and artifact hashes. This does not assert byte-identical solutions across
platforms: degenerate LP controls, run times and plot output may differ while objectives and
feasibility agree. The manifest's recorded environment describes the published run.

The existing measured USGS river-flow sample is separate from this experiment and does not
calibrate its inflows or plant parameters. Real-cascade validation would require independent
storage, generation and discharge observations plus held-out periods. Forecast backtesting,
seasonal water values, operational market rules and security constraints remain research work.
