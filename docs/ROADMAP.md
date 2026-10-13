# Development roadmap and claim boundaries

## Implemented in v0.2

- One shared SI-unit physical model with reservoir bounds, downstream spill/bypass and transit queues.
- Sparse deterministic scheduling, explicit terminal targets/values and finite-difference sensitivities.
- Finite scenario-tree decisions with nonanticipativity and held-out rolling-policy experiments.
- Small zonal market LP with thermal/renewables, congestion, local upward reserve, optional continuous
  thermal capacity additions and independent synthetic outage sampling.
- Modern public-data ingestion, observation scorer, reproducible evidence and cross-platform CI.

## Next research milestones

| Milestone | Required evidence before claiming completion |
|---|---|
| Real-cascade calibration | Independent plant data; held-out storage/generation/discharge errors; wet/dry years |
| Variable head and efficiency | Reservoir level-volume and tailwater curves; measured conversion performance |
| Unit commitment | Minimum turbine load, starts, operating zones, ramp data; MILP known-answer cases |
| Seasonal water-value model | Multi-year inflows and demand/prices; correlated scenarios; out-of-sample drought behavior |
| SDDP / larger problems | Valid state formulation, convergence/bounds, policy sampling and published benchmark |
| Operational bidding | Gate-closure timing, forecast archive, market rules, imbalance/activation evaluation |
| Real reserve products | Response-time constraints, downstream activation effects, qualification and measured delivery |
| Market forecasting | Weather-consistent demand/renewables, fuel/outages, observed-price and flow backcast |
| Annual investment/adequacy | Full-year/multi-year chronology, annualized costs, outage duration/correlation, uncertainty intervals |
| Nodal/security modelling | DC power-flow/PTDF inputs, contingency constraints and independent network benchmark |

These require data and sustained validation work. A UI or another solver package does not supply that
evidence. No release currently claims parity with BID3's market, seasonal hydro, adequacy or financing
workflows. The useful near-term target is a transparent, validated model for a specific decision and cascade.
