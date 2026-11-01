# Validation and evidence

## Evidence hierarchy

1. Independent analytical examples establish selected equations and optimal decisions.
2. Randomized conservation tests cover storage, spill, diversions, losses and delayed water.
3. Solver residuals and independent physical replay establish feasibility of each exported schedule.
4. Held-out synthetic experiments compare decision policies under explicitly stated information timing.
5. A measured USGS sample verifies data transport, unit conversion, sorting and quality preservation.
6. Operational validity requires independent plant observations and calibration. It has not been established.

The public discharge sample is measured river flow, potentially affected by upstream regulation. It is
not a measured inflow series for the invented demonstration plants. Its estimated-value flags remain
visible even when USGS marks the records Approved. Public-domain USGS data retain provider attribution.

## Analytical checks

- Empty reservoir with no inflow: zero generation and explicit unmet environmental release.
- 10 m³/s inflow, 2 m³/s discharge over 24 h: storage increases by 691,200 m³.
- One-MW conversion factor, 3600 m³ initially stored, hourly prices 10/100 EUR/MWh:
  with zero terminal target, the optimizer generates 1 MWh in the second hour for EUR 100.
- Two equally likely future prices 100/0, current price 50, one MWh available:
  shared first-stage decisions have expected value EUR 50; separate clairvoyant solves would yield EUR 75.
- Congested link at 10 MW between marginal costs 20 and 80: zone prices separate at 20/80 EUR/MWh.
- Energy plus reserve share plant capacity; an empty hydro reservoir cannot provide energy-backed reserve.

Tests perturb future evaluation inflows and prices to verify an unchanged first rolling action.
The perfect-foresight benchmark is compared on the same terminal value/penalty basis and must dominate
the feasible policies. No guaranteed benefit from stochastic scheduling is claimed.

## Reproduce

```bash
python -m pytest -q -m "not powergama" --cov=hydrocascade --cov-config=.coveragerc-core
python -m hydrocascade demo --output runs/demo --seed 42 --trials 8 --steps 48 --horizon 12
```

Generated results include exact inputs, per-plant schedules, policy trials, summaries, market outputs,
adequacy trials, sensitivity estimates and an environment/hash manifest. Seeds and methods are part of
the manifest. Wall time, timestamps and platform are intentionally not bitwise invariant; floating-point
solutions and alternative LP optima may vary across solvers. Use tolerances and compare objectives and
feasibility rather than assuming identical release schedules for degenerate problems.

## Score a real cascade

Supply independently measured columns `timestamp`, `plant_id`, `storage_m3`, `power_mw`, `turbine_m3s`.
Predictions must use the same interval convention and storage point (start/end). Measurements need
consistent UTC timestamps, turbine versus total river-discharge definitions, and documented units.
Keep separate calibration and evaluation periods, including wet, dry and seasonal-transition conditions.

```bash
hydro-cascade validate --predictions runs/schedule/schedule.csv \
  --observations your-observations.csv --output runs/validation.csv
```

The scorer returns coverage, MAE, RMSE, bias and Nash-Sutcliffe efficiency by plant and metric. No
matches and zero-variance observations are explicitly undefined; missing data are not converted to zero.
Record parameter revisions and compare to simple baselines. Revenue accuracy needs actual forecast
information and comparable terminal states, not historical prices supplied with perfect foresight.

No real-plant error percentage, revenue uplift or BID3 parity is asserted by this repository.

## Reserve offers and observed-price evidence

The [operations study](OPERATIONS_CASE_STUDY.md) adds an openly licensed observed SE4 auction-price
archive with raw/CSV hashes, complete quarter-hour validation and a 100-interval DST-day test.
Its forecaster only reads interval ends before issue. A future-price perturbation test verifies
that later observations cannot alter the earlier forecast. Rolling-origin MAE, interval coverage,
width and empirical CRPS expose the baseline's failures; a forecast-health hold uses only score
days completed before that issue.

Known answers establish signed reserve/activation settlement, fractional weighted CVaR, water-limited
sustained activation, delayed downstream reserve energy and hard total-release ramping through
bypass/spill. Additional tests cover maintenance, flood/storage curves, zero-probability security
paths, histories that cannot leak future activation, timestamped checkpoint handoff and complete
JSON/CLI workflows. An optional test replays an offer trajectory into real pinned PowerGAMA.

The operations evidence verifier checks source/output hashes, market licence/provenance, timing,
scenario cashflows, physical tolerances, financial dominance and the documented headline values.
Its stress assessments are represented-path feasibility tests; they do not constitute TSO
qualification or prove online control feasibility on unseen activation sequences. Actual plant
and activation observations remain necessary for a company pilot.
