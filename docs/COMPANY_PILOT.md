# Company pilot and acceptance guide

The immediate application is a transparent assessment harness for a hydro planning, trading or
model-validation team. It can examine proposed energy/reserve quantities against represented water,
environmental, outage and connection constraints, expose insufficient activation energy, and record
the data and assumptions behind the result. The published benchmark uses observed prices and
synthetic assets. It has not been operated at an energy company.

## A practical first engagement

Choose one small cascade and one actual decision, such as how much upward reserve an existing
day-ahead schedule could support for a specified activation duration. Start with read-only exports
from the company's incumbent planning system, run this model alongside it, and have the asset and
market specialists review the disagreements. The JSON planner and CSV audit outputs allow that
comparison without connecting to control systems or sending market orders.

Use [the example case](../examples/operations_case.json) and [schema description](OPERATIONS.md).
Company scenarios can replace every synthetic plant, state, inflow, activation and price assumption.
Keep company inputs in local run directories; public Git evidence should contain only authorised data.

## Data required and what each item resolves

| Input | Decision or validation supported |
|---|---|
| Storage/level, turbine generation, discharge, bypass/spill and timestamps | Physical replay against independently observed state transitions |
| Travel-time estimates and initial river/transit state | Arrival timing and downstream reserve-energy availability |
| Head/efficiency curves, unit availability and minimum operating restrictions | Whether the current fixed-head continuous approximation is adequate |
| Applicable reservoir/release permits and rate limits | Actual operating envelope; expert-approved interpretation of licence requirements |
| Outage plan, connection constraints and validated network case | Planned derating, feasible export and external grid absorption |
| Forecast issue archives with availability timestamps | Genuine historical information sets and leakage-free evaluation |
| Accepted energy/capacity quantities, actual activations and settlement | Real financial and nondelivery evaluation rather than assumed acceptance |

The current storage/release/ramp limits are hard linear constraints. More complex concessions or
unit restrictions may need a different formulation before the company case is represented faithfully.
Do not infer a real plant model from the sample's coordinates, capacities or conversion coefficients.

## Acceptance evidence to agree before a pilot

Set the thresholds with the asset owner and trading/model-risk teams, using the decision's physical
and financial scales. The repository's numerical tolerances are implementation checks, not acceptable
forecast or reservoir errors for a real asset.

- **Physics:** held-out storage/generation/discharge errors, conservation residuals, arrival timing,
  environmental violations and fit across wet/dry/outage periods.
- **Information:** forecasts and state inputs demonstrably available before each decision; no use of
  a later auction price or future activation in earlier controls.
- **Delivery:** the incumbent and candidate assessed on the same activation trajectories, response
  assumptions, terminal accounting and network conditions; quantities that fail delivery remain visible.
- **Economics:** observed settlements where available; equivalent accounting for terminal water,
  imbalance, nondelivery and opportunity costs; confidence intervals over multiple held-out periods.
- **Forecasts:** error, empirical coverage/sharpness and decision sensitivity under regime changes;
  comparison against the company's actual baseline.
- **Execution handoff:** checkpoint cadence, cascade hash, timestamp, previous releases and transit
  state checked at every boundary. Next-window input times must be aligned by the caller.
- **Market applicability:** product minimum/lot sizes, bid blocks, gate closure, qualification,
  response speed and telemetry verified using the applicable current rules.

## Existing interfaces and remaining integration work

`hydro-operations plan` accepts a versioned JSON case and exports offers, scenario cashflows,
dispatch trajectories, independent audits and resolved input hashes. Timestamped checkpoints can
resume it with the executed state. The optional native grid check fixes hydro exactly and reports
dumped/unserved energy; it does not assume the water schedule survives a grid curtailment unchanged.

Real SCADA/state-estimation adapters, asset-specific head/unit models, archived forecasts, full
market settlement and TSO-compatible bid submission remain integration work. None is simulated by
a success flag. The published decision report is `research_only`; its forecast-health, stress and
grid findings explain why further evidence is needed. A company can use the harness to structure
that evidence and identify which restriction limits flexibility before funding a larger implementation.
