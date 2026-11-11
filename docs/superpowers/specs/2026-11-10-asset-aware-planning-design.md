# Asset-aware planning and pilot evaluation

The user delegated design and implementation decisions on 10 November 2026. The purpose is to
make a hydro company's read-only planning pilot more useful without representing synthetic assets
as calibrated plants. Existing core, stochastic reserve and PowerGAMA formulations remain available.

## Approach and choices

Extend the existing Python/SciPy package with a separate `hydrocascade.pilot` workflow. Replacing
the stochastic LP with one large nonlinear stochastic mixed-integer model would combine too many
unvalidated changes. A reporting-only layer would not resolve unit operating restrictions. Instead,
add a deterministic asset-aware MILP and independently assess its hydraulic approximation, then
provide strict market-time and historical settlement/observation interfaces.

## Requirements

- Piecewise storage-to-forebay levels, unit flow/efficiency curves, fixed tailwater and quadratic
  unit hydraulic loss; reject extrapolation and nonpositive operating head.
- Triangulated storage/flow generation surfaces coupled to the existing cubic-metre water block.
  Use mean interval storage. Unit binaries enforce minimum operating flow, start/stop costs,
  minimum up/down durations, initial commitments, availability and flow ramps. Preserve cascade
  delays, environmental constraints, river ramps and terminal water accounting.
- Independent replay checks water and units. Integrate power along each interval's storage path
  against the underlying curves, expose approximation error and check actual export capacity.
  A tolerance failure produces a hold; no approximation is a delivery certificate.
- Reject nonoptimal or unaudited MILP results. Record solver gap, bound, duration and dimensions.
  Carry unit state and physical state in exported handoff data. New transitions must complete
  minimum durations inside the horizon, avoiding hidden terminal commitment obligations.
- Local delivery days have UTC keys, unambiguous local labels and 92/96/100 quarter-hours across
  DST. User-configured product checks cover quantities, increments, blocks and gate closure;
  they report violations and never silently round or claim TSO approval.
- Historical evaluation aligns complete timestamped offers/meters/settlement inputs, keeps signed
  prices, reconstructs energy/capacity/activation/imbalance/explicit penalty cashflows, and labels
  the supplied settlement convention. Observe power/storage errors on independent held-out data.
- Fit one bounded efficiency multiplier using fully completed earlier intervals and score later
  observations separately. Geometry/discharge are supplied; no full-cascade calibration claim.
- Versioned JSON CLI cases include absolute delivery start and issue time. Unknown fields,
  time/cadence/key mismatches, nonfinite values, incomplete curves and nonempty outputs fail closed.
  Reproducible evidence includes exact inputs, source/artifact hashes and acceptance reasons.
- No new runtime dependencies; Python 3.10–3.12 and Linux/Windows CI. All synthetic evidence labelled.

## Remaining boundaries

No SCADA or bid submission, calibrated real-plant accuracy, dynamic tailwater/backwater, prohibited
zones, seasonal SDDP, stochastic unit commitment, AC/contingency security or complete TSO settlement
is claimed. The existing reserve LP is not silently upgraded to use the new unit model. Company
acceptance needs observed asset curves, forecast archives, operating permits and measured outcomes.

## Verification

Known-answer hydraulic integration and commitment cases; exhaustive small commitment comparison;
unit-state boundaries, forced outage infeasibility, negative prices, delayed water, approximation
holds, UTC/DST and closed-gate cases; signed settlement examples, strict joins, workflow/CLI tests;
all legacy tests and evidence, wheel/sdist and fresh install smoke checks, cross-platform CI.
