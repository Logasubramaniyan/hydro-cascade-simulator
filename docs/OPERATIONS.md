# Reserve offer workbench specification

The workbench evaluates firm energy and reserve quantities for a fixed-head hydro cascade under
explicit price, inflow and activation scenarios. It uses the existing physical water equations and
SciPy/HiGHS; the native PowerGAMA check is optional. It is suitable for research and engineering
assessment, with input/output contracts that can accept a company's validated data.

## Run it

From a source checkout with the core development dependencies installed:

```bash
python -m pip install -c constraints-tested.txt -e ".[dev]"
hydro-operations study --output runs/operations --steps 96
hydro-operations plan --config examples/operations_case.json --output runs/my-plan
```

The study uses the checked-in public SE4 price archive and is offline. `--with-grid` also runs the
existing pinned PowerGAMA backend and requires its separate optional environment; see
[POWERGAMA.md](POWERGAMA.md). Installed wheels include the modules and CLI; to run a study outside
a source checkout, supply the archived price CSV and its raw/provenance sidecars with `--prices`.
Use `--all-records` to save every dispatch trajectory. The default study exports the worst/best
financial paths for each policy, while auditing every computed forecast/security path. All input
scenarios, outcomes and the dispatch-file map remain available to reproduce the rest.

## Decision timing and equations

`MarketScenario` contains local inflows, the uncertain day-ahead price curve, upward/downward
activation fractions, balancing-energy prices, probability and an optional dispatch-node path.
`MarketTerms` contains assumed accepted capacity prices in EUR/MW/h, upper quantity limits and
the time-dependent connection rating. `OperatingLimits` contains time-dependent storage/release
limits, turbine availability, total-release ramps, initial total release and a release-variation
cost proxy.

Energy `E[t]`, up reserve `U[t]` and down reserve `D[t]` are shared first-stage decisions, chosen
before auction outcomes. Following the auction, its complete day-ahead curve is known. At dispatch
interval `t`, current inflow, activation and balancing price are observed before recourse releases.
The required aggregate power is:

```text
sum(plant MW-per-m3/s * turbine m3/s) = E[t] + a_up[s,t]*U[t] - a_down[s,t]*D[t]
E[t] + U[t] <= connection limit and total installed MW
D[t] <= E[t]
```

One scalar scenario history is constructed from exactly the information revealed by that time.
Histories with identical observations share turbine/bypass/spill decisions. Supplied labels may
not split identical observed histories or merge different histories, preventing an arbitrary
scenario name from leaking the future activation path into current controls. The finite tree
still represents only the supplied paths; unique observed prefixes may reveal a modelled regime.

Each scenario receives the original `water_block`, including initial transit queues, delayed
releases, storage bounds and terminal reservoir/transit value. Native replay checks every release.
The operations layer adds hard environmental minima, maximum total discharge, storage curves,
availability derating and ramps on **turbine + bypass + spill**. Environmental deficit variables
are fixed to zero. An infeasible case fails explicitly; the solver cannot buy environmental compliance
using a soft penalty. All constraint audits report residuals in their named physical units.

The release-ramp input is in `(m3/s)/hour`, converted to an interval increment using `dt_seconds`.
It describes changes in average interval release, not subinterval river hydraulics. The variation
cost multiplies absolute interval-to-interval total-release changes; it is a configurable proxy,
not a calibrated turbine fatigue, start-cost or habitat-impact model.

## Settlement and downside valuation

Per-scenario value in EUR is:

```text
sum(dt_hours * [E * DA_price + U * up_capacity_price + D * down_capacity_price
               + a_up * U * up_energy_price - a_down * D * down_energy_price])
  + terminal reservoir/transit water value - release-variation cost
```

The down-activation sign represents buying back the avoided generation. A negative down-energy
price therefore increases the producer's cashflow in this simplified convention. Known-answer
tests establish the signs and quarter-/half-hour unit conversions. Capacity income assumes the
offered quantity is accepted at the supplied price. These are conditional cashflows, with no order
curve, market clearing, imbalance settlement, collateral or nondelivery remuneration model.

The objective maximizes `(1-w)*expected_value + w*lower_tail_value`, with `0 <= w < 1`. The lower
tail is the probability-weighted worst `1-confidence` fraction, using fractional probability atoms.
A standard linear epigraph represents it; independent reconstruction checks every scenario's
cashflow and the complete mean/CVaR objective. Risk aversion is a configurable preference, not an
inferred company risk policy. Terminal targets and values are common across compared strategies.

Scenarios with `role="forecast"` have positive probabilities summing to one. Scenarios with
`role="security"` have **zero** probability and add physical constraints without contributing to
expected value or the tail distribution. They must follow the same information rules. This allows
a company to specify adverse paths without pretending to know their probabilities.

## Price baseline and verification

The observed archive covers 39 UTC days in SE4. Its actual CC BY 4.0 licence, original-provider
attribution and exact raw/CSV hashes are retained. Only interval-end observations at or before
forecast issue enter the weekly-lag baseline and the empirical daily residual paths. Complete daily
residual blocks preserve within-day dependence. Three daily-mean-rank bins provide representative
paths with weights equal to their observed bin counts; synthetic inflow/activation paths are then
crossed with them. This is an illustrative scenario construction, not a calibrated joint stochastic
model of market prices, water and balancing activation.

Rolling-origin next-day tests report MAE, empirical interval coverage, interval width and ensemble
CRPS on held-out observed prices. The forecast-health report uses only fully completed score days
available before the corresponding issue. The 70% recent-coverage threshold is a review heuristic,
not a statistical guarantee. Intervals are empirical; there is no claim of conformal or
distribution-free coverage. Financial inputs never use the later held-out auction outcome.
`heldout_day_ahead_cash.csv` separately reprices the unchanged first-stage energy quantities at
the observed auction price; it is not total realized profit or a historical live-trading backtest.

## Stress and grid assessment

`assess_offer` fixes the offered quantities and lexicographically minimizes absolute delivery error
in MWh before optimizing the economic dispatch. All water and environmental constraints remain
hard. The reported maximum MW error belongs to this minimum-total-error solution; it is not the
solution of a separate minimax problem. No hypothetical revenue from a failed offer is counted as
achieved revenue in the strategy comparison.

A separately solved single stress path is an **optimistic trajectory-existence test**: it is a
necessary feasibility screen, not proof that an online controller can anticipate or follow an
unseen sequence. The stress-constrained policy instead shares observed-history controls across
all represented forecast and security paths. Neither establishes a guarantee outside that set.

The optional native PowerGAMA check fixes the exact hydro releases for a selected high-output
trajectory and tests an intact and derated electrical connection. Hydro is never silently curtailed.
Dumped or unserved energy exposes an absorption problem. This is a downstream electrical assessment,
not a stochastic, security-constrained hydro-grid co-optimization or an N-1 certificate.

## Company cases and execution boundaries

Use `examples/operations_case.json` as a schema-version-1 example. Every numeric array has explicit
units in its key; time arrays have shape `(intervals, plants)` or `(intervals,)`, and plant order
matches `cascade.plants`. The `data_kind` label is mandatory. Unknown top-level fields, invalid
probabilities, missing controls, invalid limits and mismatched checkpoint configuration are rejected.
Names cannot create output paths: dispatch filenames use numeric indices.

```bash
hydro-operations plan --config next-window.json \
  --checkpoint executed-prefix.json --as-of 2026-10-27T12:00Z \
  --output runs/replanned
```

`advance_dispatch` independently replays an executed prefix and saves reservoir storage, every
transit queue, last turbine flow, last **total** release, cadence, configuration hash and optional
execution timestamp. Resuming the JSON planner requires an explicit timestamp matching that saved
boundary. The caller must supply a next-window case aligned to that boundary. Replanning transfers
the previous total-release ramp boundary as well as water; it never resets delayed water to zero.
This does not add rolling-window support to the separate native PowerGAMA optimizer.

## Current operating boundary

The model exports continuous quantities and interval averages. Executable reserve bids also require
the applicable minimum/lot sizes, auction timing, activation/response profile, telemetry and TSO
prequalification. Unit commitment, minimum stable loads, prohibited zones, variable head/efficiency,
seasonal water values and real operating measurements are additional model/data requirements.
The published decision report is explicitly `research_only`, with evidence for its forecast,
stress and grid findings. See [COMPANY_PILOT.md](COMPANY_PILOT.md) for a practical acceptance process.
