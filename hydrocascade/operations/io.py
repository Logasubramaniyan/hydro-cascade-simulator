"""Company-supplied JSON case inputs and machine-readable decision-support exports."""

from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import numpy as np

from ..model import Cascade
from ..simulation import CascadeState
from .model import MarketScenario, MarketTerms, OperatingLimits
from .optimizer import optimize_offers


def encode(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Cannot encode {type(value)}")


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, allow_nan=False, default=encode), encoding="utf-8", newline="\n"
    )


def case_dict(
    cascade,
    scenarios,
    terms,
    limits,
    state,
    dt_seconds=900,
    terminal_min=None,
    terminal_value=None,
    risk_weight=0.35,
):
    return dict(
        schema_version=1,
        data_kind="synthetic asset, inflow, activation and balancing-price assumptions",
        cascade=cascade.to_dict(),
        scenarios=[asdict(s) for s in scenarios],
        terms=asdict(terms),
        operating_limits=asdict(limits),
        state=asdict(state),
        dt_seconds=dt_seconds,
        terminal_min_m3=terminal_min,
        terminal_value_eur_m3=terminal_value,
        risk_weight=risk_weight,
        confidence=0.9,
    )


def read_case(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    allowed = {
        "schema_version",
        "data_kind",
        "cascade",
        "scenarios",
        "terms",
        "operating_limits",
        "state",
        "dt_seconds",
        "terminal_min_m3",
        "terminal_value_eur_m3",
        "risk_weight",
        "confidence",
    }
    if data.get("schema_version") != 1 or not data.get("data_kind") or set(data) - allowed:
        raise ValueError("Case needs schema_version=1, a data_kind label and only documented input fields")
    cascade = Cascade.from_dict(data["cascade"])
    state = CascadeState.initial(cascade)
    if data.get("state") is not None:
        source = data["state"]
        state = CascadeState(
            np.asarray(source["storage_m3"], float),
            source["queues_m3"],
            np.asarray(source["last_turbine_m3s"], float),
        )
        state.validate(cascade)
    return (
        data,
        cascade,
        [MarketScenario(**s) for s in data["scenarios"]],
        MarketTerms(**data["terms"]),
        OperatingLimits(**data["operating_limits"]),
        state,
    )


def write_result(result, output, selected=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    result.offers.to_csv(output / "offers.csv", index=False, lineterminator="\n")
    result.outcomes.to_csv(output / "outcomes.csv", index=False, lineterminator="\n")
    write_json(output / "audit.json", result.audit)
    selected = set(result.schedules) if selected is None else set(selected)
    # Filenames use indices rather than user-provided scenario names; no path traversal.
    files = {}
    for i, (name, frame) in enumerate(result.schedules.items()):
        if name in selected:
            filename = f"dispatch_{i:03d}.csv"
            frame.to_csv(output / filename, index=False, lineterminator="\n")
            files[name] = filename
    write_json(
        output / "dispatch_files.json",
        dict(
            exported=files,
            computed_scenarios=len(result.schedules),
            all_scenarios_exported=len(files) == len(result.schedules),
        ),
    )


def run_plan(config, output, checkpoint=None, as_of=None):
    data, cascade, scenarios, terms, limits, state = read_case(config)
    execution = None
    if as_of is not None and checkpoint is None:
        raise ValueError("--as-of requires a checkpoint")
    if checkpoint is not None:
        from .state import DispatchCheckpoint

        if as_of is None:
            raise ValueError("Resuming requires an explicit --as-of execution boundary")
        loaded = DispatchCheckpoint.load(
            checkpoint, cascade, data.get("dt_seconds", 900), expected_as_of=as_of
        )
        state = loaded.state
        limits = replace(limits, initial_release_m3s=loaded.last_release_m3s)
        data["state"], data["operating_limits"] = asdict(state), asdict(limits)
        execution = dict(
            checkpoint_sha256=sha256(Path(checkpoint).read_bytes()).hexdigest(), as_of_utc=loaded.as_of_utc
        )
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Plan output must be empty")
    result = optimize_offers(
        cascade,
        scenarios,
        terms,
        limits,
        state=state,
        dt_seconds=data.get("dt_seconds", 900),
        terminal_min=data.get("terminal_min_m3"),
        terminal_value=data.get("terminal_value_eur_m3"),
        risk_weight=data.get("risk_weight", 0.35),
        confidence=data.get("confidence", 0.9),
    )
    result.audit["execution_boundary"] = execution
    result.audit["resolved_inputs_sha256"] = sha256(
        json.dumps(data, sort_keys=True, default=encode).encode()
    ).hexdigest()
    write_result(result, output)
    write_json(output / "inputs.json", data)
    return result
