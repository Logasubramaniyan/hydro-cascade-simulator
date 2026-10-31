# Contributing

Create a Python 3.10-3.12 environment and install `pip install -c constraints-tested.txt -e ".[dev]"`.

Before opening a pull request:

```bash
python -m ruff check .
python -m ruff format --check .
python -m pytest -m "not powergama" --cov=hydrocascade --cov-config=.coveragerc-core
python -m build
```

Changes to equations need an independent analytical case or conservation/feasibility test. Test changes
to decision timing with a future-information perturbation, not only with code-path assertions.
Do not loosen numerical tolerances without explaining the scale and evidence.

Use SI units in public APIs and field names. Label each dataset and result as measured, synthetic,
derived, or calibrated. Preserve sources, retrieval dates and quality flags. Do not commit credentials,
private plant data, compiled files or arbitrary local run directories.

Document limitations when adding market or plant functionality. A feature name alone is not evidence of
operational suitability. Keep the physical replay independent of optimization matrix construction.

For defects, include the smallest input that reproduces the issue, expected physical behavior,
environment versions, and any solver status/residuals. For improvements, state the decision the feature
should support and how it will be validated.

For the optional PowerGAMA adapter, use a separate Python 3.11-3.12 environment and install
`pip install -c constraints-powergama.txt -e ".[dev,powergama]"`. Run:

```bash
python -m pytest tests/test_powergama.py tests/test_grid_inputs.py -q \
  --cov=hydrocascade/powergama --cov-config=.coveragerc-powergama
python -m hydrocascade.powergama.study --output runs/grid-check --steps 12 --seed 42
python tools/check_grid_evidence.py
```

Run coverage suites sequentially, or set distinct `COVERAGE_FILE` paths. When changing study code,
reproduce the full 48-hour study in a fresh directory, export its evidence with
`tools/export_grid_evidence.py`, update the case-study claims, and verify the source/artifact hashes.
Keep native SQLite files in run directories or CI artifacts. See [the integration notes](docs/POWERGAMA.md).
