# Development roadmap and claim boundaries

## Implemented in v0.2

- One shared SI-unit physical model with reservoir bounds, downstream spill/bypass and transit queues.
- Sparse deterministic scheduling, explicit terminal targets/values and finite-difference sensitivities.
- Finite scenario-tree decisions with nonanticipativity and held-out rolling-policy experiments.
- Small zonal market LP with thermal/renewables, congestion, local upward reserve, optional continuous
  thermal capacity additions and independent synthetic outage sampling.
- Modern public-data ingestion, observation scorer, reproducible evidence and cross-platform CI.

## Implemented optional hydro-grid extension

- Pinned PowerGAMA/Pyomo DC network with the existing physical cascade as its only water model.
- Fixed physical schedules assessed electrically, plus one full-window joint optimization.
- Six synthetic cases, corridor sensitivity, signed-price finite differences, independent meshed-flow
  benchmarks, reproducible source/output hashes and Linux/Windows integration CI.
- Detailed scope and evidence in [POWERGAMA.md](POWERGAMA.md) and [GRID_CASE_STUDY.md](GRID_CASE_STUDY.md).

## Next research milestones

The [reserve offer workbench](OPERATIONS.md) now implements quarter-hour observed-price ingestion,
issue-time forecast filtering/backtests, mean/CVaR offer quantities, zero-probability activation
security paths, hard river limits, outage/connection derating, independent delivery tests and
timestamped state handoff. Its [case study](OPERATIONS_CASE_STUDY.md) exposes failed stresses and
forecast undercoverage. These are engineering-assessment capabilities; the operational milestones
below still require company data, applicable market rules and field validation.

| Milestone | Required evidence before claiming completion |
|---|---|
| Real-cascade calibration | Independent plant data; held-out storage/generation/discharge errors; wet/dry years |
| Variable head and efficiency | Reservoir level-volume and tailwater curves; measured conversion performance |
| Unit commitment | Minimum turbine load, starts, operating zones, ramp data; MILP known-answer cases |
| Seasonal water-value model | Multi-year inflows and demand/prices; correlated scenarios; out-of-sample drought behavior |
| SDDP / larger problems | Valid state formulation, convergence/bounds, policy sampling and published benchmark |
| Operational bidding | Gate-closure timing, forecast archive, market rules, imbalance/activation evaluation |
| Real reserve products | Product-specific response/lot/block constraints, qualification and measured delivery; represented cascade activation paths now implemented |
| Market forecasting | Weather-consistent demand/renewables, fuel/outages, observed-price and flow backcast |
| Annual investment/adequacy | Full-year/multi-year chronology, annualized costs, outage duration/correlation, uncertainty intervals |
| Calibrated network/security modelling | Validated real topology/ratings, AC feasibility and contingency constraints; illustrative DC benchmarks already implemented |
| Rolling hydro-grid decisions | Native PowerGAMA window handoff and forecast backtests; the separate JSON operations planner now preserves reservoir/transit/release checkpoints |

These require data and sustained validation work. A UI or another solver package does not supply that
evidence. No release currently claims parity with BID3's market, seasonal hydro, adequacy or financing
workflows. The useful near-term target is a transparent, validated model for a specific decision and cascade.
