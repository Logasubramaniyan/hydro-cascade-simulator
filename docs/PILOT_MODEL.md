# Asset-aware planning and historical evaluation

This workflow is intended for a company's read-only planning/model-validation pilot. It implements
individual turbine decisions, changing forebay levels, efficiency curves, strict time alignment and
explicit historical cashflows. It is separate from the existing stochastic reserve LP and native
PowerGAMA model. Those models retain their documented fixed-head/continuous assumptions.

## Run a company case

After the core installation in the README:

```bash
hydro-pilot plan --config examples/asset_case.json --output runs/asset-plan
hydro-pilot verify --directory runs/asset-plan
```

Every output directory must be empty. The example uses synthetic assets and observed historical
prices as **known inputs**, despite having an issue timestamp; it is not a historical executable
forecast decision or achieved-profit result. A company must supply archived forecasts actually
available at its declared issue time. The timestamp alone does not prove information availability.

`schema_version=1`, `case_type=asset_dispatch` and a nonempty `data_kind` are mandatory. Required
fields are `cascade`, `curves`, `units`, `start_utc`, `issued_at_utc`, `inflows_m3s` and `prices_eur_mwh`.
The cascade must explicitly contain only `plants` and `links`; an empty links list means disconnected
reservoirs. Plant/link fields follow the core model. Unknown fields are rejected. Inputs are finite;
curves must cover all physical storage bounds; no hydraulic extrapolation is permitted.

Times require explicit offsets. The issue precedes the interval-aligned absolute start. Supported
workflow durations are 900/1800/3600 seconds. Inflows have shape `[interval, plant]`; prices are either
`[interval]` or `[interval, plant]`. The state and initial transit queues use the core cubic-metre
schema. `operating_limits` uses the existing `OperatingLimits` fields, including hard total-release
ramps and time-dependent storage/availability limits. Broad defaults are implementation conveniences,
not a substitute for the company's permits.

## Hydraulic and unit inputs

Each `ReservoirCurve` has `plant_id`, strictly increasing `storage_m3`, nondecreasing `forebay_m` and
fixed `tailwater_m`. Each `Unit` supplies:

- Unique `id` and a known `plant_id`.
- Increasing positive `flow_m3s` knots and matching efficiencies in `(0,1]`. The first flow is the
  minimum generating flow; an off unit has zero flow.
- `min_up_steps`, `min_down_steps`, startup/shutdown EUR, running EUR/hour and flow ramp m3/s/hour.
- Optional `hydraulic_loss_m_per_m3s2`, giving a loss coefficient times squared unit flow.

`unit_state` maps every unit to `on` (boolean), `age_steps` (elapsed complete intervals in that state)
and `flow_m3s`. Initial unit flows must sum to each plant's physical `last_turbine_m3s`. Defaults mean
all units have been off long enough to start. Binary `unit_availability` arrays represent planned
outages; a conflict with an inherited minimum run obligation is infeasible, never silently relaxed.

## Mathematical formulation

The shared water block supplies reservoir mass balance, delays/diversions, plant turbine limits,
bypass/spill, terminal targets and values of reservoir/transit water. Environmental shortfall is
fixed to zero. Individual unit flows sum exactly to plant turbine discharge.

For each unit, interval and triangle, binary `z` selects one triangle of a storage/flow grid. Three
nonnegative vertex weights sum to `z`; all triangle selectors sum to unit on/off `y`. Their weighted
flow and power equal unit flow and power. Weighted storage equals mean interval storage when on;
physical-volume big-M inequalities relax it when off. This prevents interpolation across unrelated
operating regions or a free choice of head. Grid power at volume `v` and flow `q` is

`P(v,q) = 0.00981 * [forebay(v) - tailwater - loss_coefficient*q²] * q * efficiency(q)` MW.

The mean storage is `(start_volume + end_volume)/2`. Forebay and efficiency curves are piecewise
linear; `storage_segments` and `flow_segments` add regular grid points to supplied knots. Default
values are 4 and 2, with a maximum of 50 each. More segments increase the MILP significantly.

Binary starts/stops satisfy `y[t]-y[t-1] = start[t]-stop[t]`, with no simultaneous start/stop. Rolling
start/stop sums enforce minimum run/rest durations, initial residual obligations are fixed, and new
transitions are prohibited when their minimum duration would extend beyond the horizon. This is a
conservative boundary convention; it can distort decisions near a short horizon and is documented
instead of hiding unpriced future commitments. Initial obligations may extend through the window;
their actual age is carried forward. Flow ramps also apply across startup/shutdown and the initial
boundary. Aggregate plant and unit availability constraints both apply.

The objective maximizes supplied-price energy revenue plus terminal reservoir/transit value minus
startup, shutdown, running and total-release-variation costs. Carryover values remain supplied
approximations, not fitted seasonal values. The solver is the existing SciPy/HiGHS MILP interface,
with default 60-second limit and relative gap `1e-6` (maximum accepted configuration `0.01`). A time
limit incumbent is rejected. Audits record configured-solution optimality gap, dual bound, nodes,
dimensions, time, row/bound/integrality checks and independent objective reconstruction.

## Independent hydraulic acceptance

The core simulator replays all water controls independently. Individual unit status, transitions,
minimum durations, flow bounds, availability, ramps and costs are audited separately.

For power, volume is assumed to change linearly within an interval and unit flow is held constant.
The replay integrates the forebay curve exactly over that volume path, splitting at every level
knot. Efficiency and hydraulic loss use the executed constant flow. This differs from interpolating
a triangulated mean-storage surface. Per-unit, per-plant and aggregate mean-power errors remain
visible. `hydraulic_tolerance_mw` defaults to 0.05 MW and is an engineering screening assumption.

The export audit also evaluates **instantaneous aggregate** power at interval endpoints and every
crossed forebay knot across all plants. With this interval physics, these points contain every
aggregate extremum. An average within the connection rating can still fail its peak check. A power
approximation or peak-export failure sets `hydraulic_review_status=hold`. Refining the interpolation
cannot automatically repair every physical connection failure. Neither pass nor solver optimality
establishes measured asset accuracy, response performance or operational approval.

## Outputs and handoff

`plant_dispatch.csv` distinguishes interpolated `model_power_mw` from independently replayed
`power_mw`, energy and revenue. `unit_dispatch.csv` records flow, status, starts/stops, availability
and both power estimates. `energy_schedule.csv` contains interpolated aggregate interval quantities;
it is a study schedule, with no assertion that market lot rules are satisfied. `audit.json` and
`decision_report.json` preserve failures. The decision status always remains `research_only`.

`handoff.json` includes exact cascade and curve/unit fingerprints, cadence, absolute next start,
reservoir water, transit queues, previous turbine/total release and each unit's state/age/flow.
An exported handoff is explicitly labelled `state_basis=projected_schedule`; it is **not evidence of
execution**. Resume with `handoff_path`, resolved relative to the new case JSON. Start time must match
the handoff boundary, and changed assets/cadence are rejected. Operators replacing projected state
with measured execution must supply an independently reviewed `observed_execution` state, including
transit/previous releases and unit history. The label is provenance, not automatic verification.

## Market time and product screening

```python
from hydrocascade.pilot.calendar import delivery_day

day = delivery_day("2026-10-25", timezone="Europe/Stockholm")
assert len(day) == 100
```

Local delivery days retain UTC interval keys and offset-bearing local labels. Spring/autumn clock
changes give 92/100 quarter-hours; ordinary days give 96. Supplied `ProductRules` screen minimum
quantities, increments, constant-quantity blocks, UTC clock alignment and an explicit block anchor
(default UTC epoch). Screening requires complete blocks and an issue strictly before the supplied
gate closure. `product_screen` in a dispatch case screens energy quantities; the Python interface
defaults to reserve columns for assessing existing reserve offers. Failures are reported; quantities
are never rounded. These are caller-configured rules, not a hardcoded current TSO product definition.
Response, telemetry, gate rules, bidding formats, prices/volumes accepted by clearing and qualification
must be verified separately.

## Historical financial and observation evaluation

```bash
hydro-pilot evaluate --config your-evaluation.json --output runs/evaluation
```

Use `case_type=historical_evaluation`, `data_kind`, optional `dt_seconds`, and CSV paths `offers_csv`,
`meter_csv`, `market_csv`, relative to the configuration. Every frame needs identical ordered,
unique, explicit-offset timestamps at the same cadence; missing or extra intervals fail instead of
disappearing in a join. Offers contain accepted `energy_mw`, `reserve_up_mw`, `reserve_down_mw`; meters
contain `actual_mw`. Non-pumping downward reserve cannot exceed the energy baseline.

Market columns are `day_ahead_eur_mwh`, `up_capacity_eur_mw_h`, `down_capacity_eur_mw_h`, `activation_up`,
`activation_down`, `up_energy_eur_mwh`, `down_energy_eur_mwh`, `imbalance_sell_eur_mwh`,
`imbalance_buy_eur_mwh`, `delivery_penalty_eur_mwh`. All prices may be negative except the explicit
penalty rate. Nonnegative activation fractions sum to at most one. For interval duration `h` hours:

- Target MW = energy + activated up reserve - activated down reserve.
- Day-ahead cash = energy MW times DA EUR/MWh times `h`.
- Capacity cash = accepted reserve MW times supplied EUR/MW/hour times `h`.
- Activation cash = requested up MWh times up price minus requested down MWh times down price.
- Residual MWh = (actual - target) MW times `h`. Positive residual uses the supplied sell price;
  negative residual uses buy price with its signed volume.
- Explicit penalty = absolute residual MWh times the supplied delivery-error rate.

These are explicit **requested-activation plus residual-imbalance assumptions**, not a complete
Nordic/TSO settlement implementation. Do not mix incompatible activation/imbalance conventions or
double-count fees. Net cash excludes unspecified operating costs/taxes/charges and terminal water.

Optional `predicted_csv`, `observed_csv`, `mae_thresholds` and `minimum_coverage` apply plant/metric
acceptance checks to independent observations. Supported metrics are storage, power and turbine
flow. Missing observations reduce coverage, remain missing and can fail thresholds. Use equivalent
interval averages and end-of-interval storage. Synthetic measurements exercise the interface only.

## Efficiency fitting with a held-out period

```bash
hydro-pilot calibrate --config your-calibration.json --output runs/calibration
```

Use `case_type=unit_calibration`, `data_kind`, `unit`, `curve`, `observations_csv`, `training_end_utc`
and optional interval duration/minimum training intervals (default eight). Metering is for one
individual unit, with `timestamp`, `start_storage_m3`, `end_storage_m3`, `flow_m3s`, `power_mw`.
Fit one bounded least-squares multiplier on its supplied efficiency curve. Only intervals ending
at/before cutoff enter the fit. Later intervals score MAE, bias and RMSE; crossing-cutoff intervals
are explicitly excluded. At least one later interval is mandatory. The multiplier is bounded below
by 0.1 and above by unit efficiency <=1; a bound hit is visible. Fitting is not automatically promoted
to planning inputs. Unit flow, level-volume geometry, tailwater and losses must be independently
supplied. A scale cannot identify errors in all of them simultaneously or calibrate a full cascade.

## Reproducibility and remaining scope

Each command saves its exact original JSON, resolved state/inputs, copied evaluation observations,
source and artifact SHA-256, dependency versions and an explicit data label. `hydro-pilot verify`
checks those recorded fingerprints against the current checkout. Hashes detect drift; they are not
cryptographic signatures or proof that an observation came from a company.

Dynamic tailwater/backwater, forbidden turbine zones, seasonal water values, stochastic commitment,
AC/N-1 security, SCADA state estimation and executable bids remain outside this implementation.
See the [case study](ASSET_CASE_STUDY.md) and [company acceptance guide](COMPANY_PILOT.md).

Sources: [SciPy 1.13 MILP interface and status/gap definitions](https://docs.scipy.org/doc/scipy-1.13.1/reference/generated/scipy.optimize.milp.html),
[Nord Pool's October 2026 DST operational notice](https://www.nordpoolgroup.com/en/trading/Operational-Message-List/2026/10/nord-pool-would-like-to-remind-its-members-about-the-upcoming-daylight-saving-time-dst-clock-change-on-sunday-october-25th.-20261023111100/).
The latter confirms 100 quarter-hour periods for the autumn change; supplied quantity/block rules
in this project's benchmark are intentionally illustrative.
