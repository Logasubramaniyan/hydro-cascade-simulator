# Changelog

## 0.2.0

Rebuild the demonstration as an auditable modelling package. Fix dimensional water accounting,
negative storage/generation without water, discarded spill, unused configuration and disconnected tests.
Add cascade delays/diversions, turbine controls, terminal-aware optimization, scenario-tree decisions,
rolling experiments and an educational coupled market. Replace the incomplete Conda workflow with
Linux/Windows testing and package builds. Add modern USGS ingestion, data provenance, observation
scoring, known-answer cases, case-study figures and explicit modelling limitations.

The original hand-written flow sample and plots are replaced by labelled, reproducible examples.
Legacy `src` entry points delegate to the shared package; the public API now names physical units.
This is a breaking model change, not numerical continuity with v0.1 outputs.
