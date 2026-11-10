# Asset-aware planning implementation plan

> **For agentic workers:** Use superpowers:executing-plans task by task. The user has delegated choices and requested continuous implementation.

**Goal:** Supply a tested asset-aware dispatch and historical pilot-evaluation workflow.

**Architecture:** New `hydrocascade.pilot` modules reuse the existing physical water block and replay.
The deterministic MILP's approximation is independently checked before accepting its output for review.
Calendar, product and settlement utilities share strict timestamp validation with the JSON workflow.

**Tech stack:** Existing Python, NumPy, Pandas and SciPy/HiGHS MILP; no added runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-11-10-asset-aware-planning-design.md`

## Global constraints

- Python 3.10–3.12, Linux/Windows; existing core and native grid behaviour preserved.
- Synthetic assets labelled. No production, calibrated-plant, TSO or commercial savings claims.
- Physical and financial units explicit; no extrapolation, hidden rounding or silent missing-data joins.

## Review focus

- Inherited minimum run/rest obligations and new transitions near horizon ends.
- Convex mixing across unrelated hydraulic operating regions or invalid off-state storage coupling.
- Nonlinear replay violating an export limit while MILP output appears feasible.
- Duplicate, naive or DST-ambiguous timestamps and settlement data lost by a join.
- Wrong signs or interval scaling in negative-price and downward-activation settlement.

## Task 1: Hydraulic assets and unit dispatch

Files: new `hydrocascade/pilot/assets.py`, `dispatch.py`, `solver.py`, `__init__.py`;
tests `tests/test_pilot_dispatch.py`.

Interfaces: `ReservoirCurve`, `Unit`, `UnitState`; `optimize_units(cascade, inflows, prices,
curves, units, ..., dt_seconds=900)` returns records, unit records, state and audit.

- [ ] Write known-answer hydraulic, commitment and boundary tests; run and observe failure.
- [ ] Implement validated curves, integral head evaluation and triangulated MILP with unit constraints.
- [ ] Check independent replay, objective, units, nonlinear power/export and approximation tolerance.
- [ ] Run dispatch tests, including exhaustive binary-state comparison and delayed water.

## Task 2: Market time and historical evaluation

Files: new `hydrocascade/pilot/calendar.py`, `evaluation.py`; tests `tests/test_pilot_evaluation.py`.

Interfaces: `delivery_day`, `validate_timestamps`, `ProductRules`, `check_product`,
`settle_dispatch`, `score_asset_observations`. All external frames keyed by explicit-offset timestamps.

- [ ] Write DST, gate closure, lot/block and known signed-cashflow/strict-join tests; observe failure.
- [ ] Implement time rules and explicit requested-activation plus residual-imbalance cashflow convention.
- [ ] Score independent observed power/storage with minimum-coverage and explicit acceptance thresholds.
- [ ] Run evaluation tests and combined dispatch/evaluation suite.

## Task 3: Company workflow, evidence and publication

Files: new `hydrocascade/pilot/workflow.py`, example JSON, pilot docs and evidence verifier;
modify CLI metadata, README, roadmap, changelog and CI. Tests `tests/test_pilot_workflow.py`.

Interfaces: `run_case(config, output)`, `main(argv=None)`; `hydro-pilot plan` and `hydro-pilot evaluate`.

- [ ] Test strict schema, absolute execution times, CLI, complete exports and tamper detection; observe failure.
- [ ] Implement planning/evaluation commands, handoff, input/source hashes and operator-review report.
- [ ] Publish a reproducible synthetic asset case with observed public day-ahead prices and explicit failures.
- [ ] Reproduce operations evidence after additive package changes, retaining all prior numerical claims.
- [ ] Run full core/optional suites, evidence checks, lint/format, builds and installed-wheel CLI smoke.
- [ ] Independent review; fix material findings with regression tests. Commit/push to a codex branch,
  create a reviewable PR and inspect GitHub Actions results.
