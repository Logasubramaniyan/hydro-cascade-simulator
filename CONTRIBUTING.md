# Contributing

Create a Python 3.10-3.12 environment and install `pip install -c constraints-tested.txt -e ".[dev]"`.

Before opening a pull request:

```bash
python -m ruff check .
python -m ruff format --check .
python -m pytest --cov=hydrocascade --cov-fail-under=85
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
