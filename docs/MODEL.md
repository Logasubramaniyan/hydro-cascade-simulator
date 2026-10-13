# Model specification

## Scope and units

The model is a fixed-head, constant-efficiency cascade on an acyclic river graph. Each turbine's
conversion factor is `alpha = 1000 * 9.81 * head_m * efficiency / 1e6`, in MW per (m³/s).
`power_mw = alpha * turbine_m3s`; interval energy is `power_mw * dt_seconds / 3600` MWh.
No rating curve, variable head, cavitation, minimum generation, startup cost or turbine-unit commitment
is inferred from a simple plant capacity.

Storage is in m³; inflows, turbine releases, bypass, spill and configured losses are in m³/s. The LP
internally scales storage to thousands of m³ to reduce coefficient spread. Residuals of LP equations
are in their respective scaled equation units; the independent replay reports physical m³ residuals.

## Water balance

For reservoir i and interval t:

`V[i,t+1] = V[i,t] + dt * local_inflow[i,t] + upstream_arrival_volume[i,t]`
`             - dt * (turbine[i,t] + bypass[i,t] + spill[i,t]) - loss_volume[i,t]`

All discharge categories are routed downstream. A link carries its fraction of total source discharge.
Outgoing fractions sum to at most 1; the unrouted fraction exits through an explicit system sink.
Reservoir storage plus queued water is conserved globally, accounting for local inflows, sink discharge
and losses. The initial pipeline defaults to empty and can be supplied explicitly in `CascadeState`.

`delay_steps = d` means a discharge during interval t arrives during interval t+d. Zero-delay links
are processed upstream first within the interval. This assumes interval-average flow and does not
represent intra-interval hydraulics. Delays must be integer multiples of the chosen timestep.

Simulation projects rule controls to available water and operating bounds and reports environmental
or ramp-down shortfalls. Explicit optimized controls are independently replayed with enforcement; a
control or end-storage disagreement raises an error. Simulation losses cannot consume water below
the operating floor; optimization treats configured losses as prescribed withdrawals and raises
infeasibility if they cannot be sustained. Zero losses are used in the portfolio examples.

## Physical and operating constraints

- Minimum storage <= V <= storage capacity; 0 <= turbine flow <= turbine flow capacity.
- Optional ramp limit is a change in turbine m³/s per dispatch step, including the initial condition.
- Total turbine + bypass + spill release plus environmental deficit meets the minimum release.
- The default environmental deficit penalty is EUR 1000 per m³ of missed release. This is an explicit
  illustrative penalty, not a legal or calibrated economic value. It must exceed relevant energy
  incentives if releases should have economic priority. Hard enforcement can be represented by checking
  zero deficits; a physically impossible requirement is never met by negative storage.
- Bypass and spill have no separate physical capacity or gate-level constraints in this version.

## Producer objective and terminal boundary

Maximize energy revenue plus final stored/transit water value minus environmental deficit penalties.
Prices are exogenous input prices. Negative prices are supported. Default deterministic scheduling
requires end storage >= each plant's configured initial storage, preventing artificial exhaustion.
Users may explicitly supply different terminal targets and nonnegative EUR/m³ values.

Delayed water that has not arrived at the horizon remains in queues and is credited at the destination's
terminal value. These values must include the intended downstream opportunity value to avoid treating
upper and lower reservoir water identically. Fixed terminal values in the experiments are illustrative
generation-equivalent values at EUR 65/MWh. They are not calibrated seasonal water-value curves.
For zero terminal values, dumping versus retaining otherwise worthless water can be a degenerate LP
choice; interpret neither as a fitted operational policy.

## Scenario tree and information timing

Each scenario has a positive probability, an inflow/price path, and a node path. Probabilities sum to one.
All scenarios at an identical node share turbine, bypass, spill and deficit decisions. A shared node
must have identical observed inputs and history; nodes with divergent parents cannot merge.

Current-interval inflow and price are observed before release decisions (hazard-decision formulation).
This timing is suitable for a conceptual operating policy, not a submitted day-ahead bid. Such bids
would require an additional market gate-closure stage and forecast uncertainty for the delivery interval.

The rolling demonstration builds three future trajectory regimes (dry, central, wet), assuming the
regime is revealed after the root. Only the shared root action is executed, then the forecast is rebuilt.
This is a finite scenario-tree approximation, not SDDP, and can be optimistic about future recourse.
The forecast uses current observations and training-only means; it never reads future evaluation values.

## Coupled market

The educational zonal LP minimizes thermal operating cost, local upward reserve holding cost,
unserved-energy and reserve-deficit penalties, environmental penalties and optional build costs,
less terminal hydro carryover value. Hydro uses the same water constraints and replay.
Zone balance: hydro + thermal + used renewable + net imports + unserved demand = demand.

Interconnectors carry signed flows limited by symmetric capacity; the network is a lossless transport
model, not DC load flow, PTDF, flow-based coupling or N-1 security. Thermal output + upward reserve
cannot exceed available capacity. Hydro output + reserve cannot exceed turbine capacity; end storage
must support the specified activation duration above its operating floor. Hydro reserve is also limited
by the optional per-step turbine ramp. No reserve sharing across zones or activation energy is simulated.
Reserve units are MW; holding costs are EUR/MW-hour. This capacity/energy safeguard is simplified:
it is not a calibrated product response-time or downstream activation-flow model.

Prices are energy-balance LP duals divided by interval hours: EUR/MWh. These are model marginal
prices without strategic bids, market rules, startup uplift or observed-price calibration.
Investment adds continuous thermal MW up to a supplied bound. Build costs are explicitly EUR/MW
for the modelled horizon; the code does not assume that a two-day run is an annual investment model.

The adequacy experiment draws independent Bernoulli thermal availability per interval and trial,
then solves the full chronological dispatch. It reports unserved MWh and loss-of-load hours within
that horizon. It uses perfect foresight within each sampled path, so it is optimistic for hydro.
It has no outage durations, seasonal calibration, correlated failures, multi-year weather archive or
confidence-certified annual LOLE. Do not use it as a reliability compliance assessment.
