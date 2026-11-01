# Industry needs and implementation decisions

Research reviewed **31 October 2026**. This document traces public company/market needs and recent
publications to features implemented in this repository. It does not establish that a capability
is absent from every commercial vendor, or that the algorithms below are newly invented.

## Evidence and the specific software gaps addressed

| Evidence | Implication for this project | Implemented response | Boundary |
|---|---|---|---|
| Nord Pool's 22 October 2025 newsletter confirms the SDAC 15-minute MTU launch | Hourly-only demonstrations miss current scheduling granularity | Quarter-hour public-price adapter, 900-second planning, offset-aware timestamps and DST tests | No auction submission or clearing engine |
| Svenska kraftnät describes automated quarter-hour mFRR activation and more volatile early imbalance prices | Spare MW alone does not establish deliverable balancing energy | Explicit activation paths, signed activation settlement and separate sustained/reversal stress tests | Assumed accepted quantities; no actual imbalance settlement |
| Statkraft's June 2025 strategy prioritises Nordic flexible hydro, market operations and near-term value | A decision tool should show uncertainty and asset restrictions alongside gross revenue | Mean/lower-tail cashflows, maintenance derating, connection limits and common terminal accounting | No estimate of Statkraft's proprietary needs or savings |
| A July 2026 systematic review of 106 studies identifies scarce two-way coupling and real-world validation | More solver features must be accompanied by observable evidence | Observed-price backtests, physical replay, source hashes and an optional native network absorption check | Asset and activation data remain synthetic; the review abstract was accessible |
| Wang et al. (May 2026) connect probabilistic forecasts to risk-aware hydro decisions | Forecast uncertainty must survive the transition into scheduling | Pre-issue residual scenarios, mean/CVaR offers, empirical coverage/CRPS and a forecast-health report | Transparent baseline, not their RL model or a claimed replication |
| Schäffer et al. (2026) show that environmental reservoir constraints affect scheduling and value | Environmental constraints should affect the feasible schedule | Hard minimum/maximum release, total-release ramps, time-varying storage limits and independent audits | Linear constraints; no replication of their nonconvex/logical concession model |
| Naversen et al. (2020) show that reserve activation changes downstream water availability | Activation feasibility must include the entire cascade | Shared water block, transit queues, zero-probability security paths and execution checkpoints | Prior art for the concept; finite represented paths only |

Sources:

1. [Nord Pool: SDAC 15-minute MTU launched, 22 October 2025](https://www.nordpoolgroup.com/en/message-center-container/newsroom/newsletter/2025/10/nord-pool-connection/).
2. [Svenska kraftnät: automated mFRR and quarter-hour balancing](https://www.svk.se/aktorsportalen/bidra-med-reserver/om-olika-reserver/mfrr/).
3. [Statkraft strategy announcement, 18 June 2025](https://www.statkraft.com/newsroom/news-and-stories/2025/statkraft-strengthens-core-activities-and-competitiveness-following-strategic-review/).
4. [Short-term hydropower regulation through coupled hydrological and energy models: A systematic review, 2026, DOI 10.1016/j.rser.2026.116940](https://www.sciencedirect.com/science/article/pii/S136403212600239X).
5. [Wang, Wang and La: Uncertainty-Quantified Probabilistic Forecasting and Reinforcement Learning for Short-Term Hydropower Scheduling, first published 25 May 2026](https://ietresearch.onlinelibrary.wiley.com/doi/10.1049/cps2.70042).
6. [SINTEF publication record: Optimal operation of hydropower systems with environmental constraints on reservoir management, 2026, DOI 10.1016/j.renene.2026.125258](https://www.sintef.no/en/publications/publication/10317806/).
7. [Naversen, Farahmand and Helseth: Accounting for reserve capacity activation when scheduling a hydropower dominated system, 2020](https://www.sintef.no/en/publications/publication/1793080/).

## Practical contribution

The new workflow makes four different questions independently inspectable: whether the forecast
is behaving acceptably, whether the offer fits the represented water/operating conditions, whether
the electrical network can absorb the resulting physical schedule, and what assumptions drive the
financial result. A high expected value cannot substitute for those other checks.

The creative engineering contribution is the reproducible combination of pre-issue data lineage,
observable-history dispatch nodes, probability-weighted downside valuation, probability-free security
constraints, exact state handoff and explicit failed-stress evidence in one small open implementation.
The tests establish implementation behavior; they do not constitute field validation.

The full-horizon security paths in the published example deliberately examine 24-hour sustained
activation and repeated reversals. They are engineering stress assumptions. Svenska kraftnät's
[4 March 2025 test-program announcement](https://www.svk.se/en/about-us/news/news/svenska-kraftnat-concludes-1-mw-pilot-study-and-updates-test-program-for-prequalification-of-the-ancillary-service-mfrr/)
describes 15-minute scheduled-activation and 30-minute direct-activation endurance tests. The example's
longer stress does not validate response speed, telemetry, market lot sizes or those qualification tests.
Companies must supply product-specific activation paths and independently verify the current rules.

## Useful company pilot

A hydro operations or trading team can use this as an assessment harness: import its validated
plant/state data and candidate price/activation scenarios; compare an incumbent schedule with
activation-aware offers; inspect binding restrictions and failed stresses; and reproduce every
input/output before considering operational integration. The JSON interface accepts company cases
without changing the optimizer. The software sends no orders and controls no equipment.

The next evidence needed for an operational pilot is measured reservoir/storage/discharge/generation
data, turbine head/efficiency curves, applicable environmental permits, actual outage and connection
limits, forecast archives and balancing activations. A pilot should backtest multiple wet/dry and
price regimes against the company's incumbent, including nondelivery and imbalance costs under the
applicable settlement rules. See [the company pilot guide](COMPANY_PILOT.md).
