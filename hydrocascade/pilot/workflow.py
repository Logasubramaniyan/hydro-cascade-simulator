"""Versioned, timestamped company-case workflows with reproducible audit exports."""

import argparse
from dataclasses import asdict, replace
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import platform
import sys

import numpy as np
import pandas as pd

from ..model import Cascade
from ..operations.io import write_json
from ..operations.model import OperatingLimits
from ..operations.state import cascade_hash
from ..simulation import CascadeState, array
from .assets import ReservoirCurve, Unit, UnitState
from .calibration import fit_unit_efficiency
from .calendar import ProductRules, check_product, instant
from .dispatch import optimize_units
from .evaluation import score_asset_observations, settle_dispatch

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def sources():
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted((ROOT / "hydrocascade").rglob("*.py"))}


def read_config(path, kind, allowed, required):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    common = {"schema_version", "case_type", "data_kind", "dt_seconds"}
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or data.get("case_type") != kind
        or not isinstance(data.get("data_kind"), str)
        or not data["data_kind"].strip()
        or set(data) - (allowed | common)
        or not (required | common - {"dt_seconds"}) <= set(data)
    ):
        raise ValueError(
            "Case requires documented schema/version/type, data_kind and fields; unknown fields rejected"
        )
    dt = data.get("dt_seconds", 900)
    if isinstance(dt, bool) or dt not in (900, 1800, 3600):
        raise ValueError("Case dt_seconds must be 900, 1800 or 3600")
    return data


def output_directory(output):
    output = Path(output)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output must be an empty directory")
    return output


def finish(output, config, config_hash, source_hashes, input_hashes=None):
    if digest(config) != config_hash or sources() != source_hashes:
        raise RuntimeError("Inputs or package sources changed during execution")
    (output / "original_config.json").write_bytes(Path(config).read_bytes())
    manifest = dict(
        schema_version=1,
        input_sha256=config_hash,
        input_files_sha256=input_hashes or {},
        code_sha256=source_hashes,
        artifact_sha256={
            p.relative_to(output).as_posix(): digest(p)
            for p in sorted(output.rglob("*"))
            if p.is_file() and p.name != "manifest.json"
        },
        python=sys.version,
        platform=platform.platform(),
        dependencies={name: metadata.version(name) for name in ("numpy", "scipy", "pandas")},
    )
    write_json(output / "manifest.json", manifest)


def asset_hash(data):
    return sha256(
        json.dumps(
            {k: data[k] for k in ("cascade", "curves", "units")}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _state(data, cascade):
    if data is None:
        return CascadeState.initial(cascade)
    if set(data) != {"storage_m3", "queues_m3", "last_turbine_m3s"}:
        raise ValueError("Physical state needs only storage, transit queues and last turbine flow")
    state = CascadeState(
        np.asarray(data["storage_m3"], float), data["queues_m3"], np.asarray(data["last_turbine_m3s"], float)
    )
    state.validate(cascade)
    return state


def run_case(config, output):
    allowed = {
        "start_utc",
        "issued_at_utc",
        "cascade",
        "curves",
        "units",
        "inflows_m3s",
        "prices_eur_mwh",
        "state",
        "unit_state",
        "unit_availability",
        "operating_limits",
        "export_limit_mw",
        "terminal_min_m3",
        "terminal_value_eur_m3",
        "storage_segments",
        "flow_segments",
        "hydraulic_tolerance_mw",
        "time_limit_seconds",
        "relative_gap",
        "handoff_path",
        "product_screen",
    }
    required = {"start_utc", "issued_at_utc", "cascade", "curves", "units", "inflows_m3s", "prices_eur_mwh"}
    data = read_config(config, "asset_dispatch", allowed, required)
    config, output = Path(config), output_directory(output)
    input_hash, code_hashes = digest(config), sources()
    start, issued = instant(data["start_utc"]), instant(data["issued_at_utc"])
    dt = data.get("dt_seconds", 900)
    if issued >= start or start.value % (dt * 1_000_000_000):
        raise ValueError("Issue must precede an interval-aligned absolute delivery start")
    if not isinstance(data["cascade"], dict) or set(data["cascade"]) != {"plants", "links"}:
        raise ValueError(
            "Pilot cascade requires exactly plants and links, including an explicit empty links list"
        )
    cascade = Cascade.from_dict(data["cascade"])
    curves = [ReservoirCurve(**x) for x in data["curves"]]
    units = [Unit(**x) for x in data["units"]]
    state = _state(data.get("state"), cascade)
    unit_state = {k: UnitState(**v) for k, v in data["unit_state"].items()} if "unit_state" in data else None
    steps = len(data["inflows_m3s"])
    limits = (
        OperatingLimits(**data["operating_limits"])
        if "operating_limits" in data
        else OperatingLimits.standard(cascade, steps)
    )
    handoff_bytes, handoff_hash = None, None
    if "handoff_path" in data:
        path = config.parent / data["handoff_path"]
        handoff_bytes = path.read_bytes()
        previous = json.loads(handoff_bytes)
        handoff_hash = sha256(handoff_bytes).hexdigest()
        if (
            previous.get("schema_version") != 1
            or previous.get("cascade_sha256") != cascade_hash(cascade)
            or previous.get("asset_sha256") != asset_hash(data)
            or previous.get("dt_seconds") != dt
        ):
            raise ValueError("Handoff cascade, asset curves/units, cadence or schema mismatch")
        if instant(previous["as_of_utc"]) != start:
            raise ValueError("Handoff boundary must equal the case's absolute delivery start")
        if previous.get("state_basis") not in ("projected_schedule", "observed_execution"):
            raise ValueError("Handoff must explicitly label projected schedule or observed execution state")
        state = _state(previous["state"], cascade)
        unit_state = {k: UnitState(**v) for k, v in previous["unit_state"].items()}
        release = array(previous["last_release_m3s"], (len(cascade.plants),), "handoff last release")
        if (release < state.last_turbine_m3s - 1e-7).any():
            raise ValueError("Handoff release cannot be below turbine flow")
        limits = replace(limits, initial_release_m3s=release)
    result = optimize_units(
        cascade,
        data["inflows_m3s"],
        data["prices_eur_mwh"],
        curves,
        units,
        dt_seconds=dt,
        state=state,
        unit_state=unit_state,
        limits=limits,
        unit_availability=data.get("unit_availability"),
        export_limit_mw=data.get("export_limit_mw"),
        terminal_min=data.get("terminal_min_m3"),
        terminal_value=data.get("terminal_value_eur_m3"),
        storage_segments=data.get("storage_segments", 4),
        flow_segments=data.get("flow_segments", 2),
        approximation_tolerance_mw=data.get("hydraulic_tolerance_mw", 0.05),
        time_limit_seconds=data.get("time_limit_seconds", 60),
        relative_gap=data.get("relative_gap", 1e-6),
    )
    times = pd.date_range(start, periods=steps, freq=pd.Timedelta(seconds=dt))
    for frame in (result.records, result.unit_records):
        frame.insert(0, "timestamp", [times[t].isoformat() for t in frame.step])
    energy = result.records.groupby("timestamp", sort=True).model_power_mw.sum().reset_index(name="energy_mw")
    product = None
    if "product_screen" in data:
        screen = data["product_screen"]
        if set(screen) != {"rules", "gate_closure_utc"}:
            raise ValueError("product_screen requires only rules and gate_closure_utc")
        rules = ProductRules(**screen["rules"])
        if rules.dt_seconds != dt:
            raise ValueError("Product interval must match dispatch cadence")
        product = check_product(energy, rules, issued, screen["gate_closure_utc"], columns=("energy_mw",))
    output.mkdir(parents=True, exist_ok=True)
    result.records.to_csv(output / "plant_dispatch.csv", index=False, lineterminator="\n")
    result.unit_records.to_csv(output / "unit_dispatch.csv", index=False, lineterminator="\n")
    energy.to_csv(output / "energy_schedule.csv", index=False, lineterminator="\n")
    write_json(output / "inputs.json", data)
    write_json(
        output / "resolved_state.json",
        dict(
            state=asdict(state),
            unit_state={k: asdict(v) for k, v in unit_state.items()} if unit_state else None,
            initial_release_m3s=limits.initial_release_m3s,
            handoff_sha256=handoff_hash,
        ),
    )
    if handoff_bytes is not None:
        (output / "input_handoff.json").write_bytes(handoff_bytes)
    last = result.records[result.records.step == steps - 1].set_index("plant_id")
    write_json(
        output / "handoff.json",
        dict(
            schema_version=1,
            state_basis="projected_schedule",
            cascade_sha256=cascade_hash(cascade),
            asset_sha256=asset_hash(data),
            dt_seconds=dt,
            as_of_utc=(start + pd.Timedelta(seconds=steps * dt)).isoformat(),
            state=asdict(result.final_state),
            unit_state={k: asdict(v) for k, v in result.final_unit_state.items()},
            last_release_m3s=[
                float(last.loc[p.id, ["turbine_m3s", "bypass_m3s", "spill_m3s"]].sum())
                for p in cascade.plants
            ],
        ),
    )
    write_json(output / "audit.json", result.audit)
    reasons = [
        "real-asset calibration and forecast validation require independent company observations",
        "deterministic approximation; no operational bid submission or TSO qualification",
    ]
    if result.audit["hydraulic_review_status"] != "pass":
        reasons.append("hydraulic approximation or independently replayed export capacity fails acceptance")
    if product is not None and not product["passes"]:
        reasons.append("configured product/time screening failed")
    write_json(
        output / "decision_report.json",
        dict(
            status="research_only",
            data_kind=data["data_kind"],
            hydraulic_review=result.audit["hydraulic_review_status"],
            product_screen=product,
            start_utc=start.isoformat(),
            issued_at_utc=issued.isoformat(),
            reasons=reasons,
            purpose="asset/planning/model-validation review; no dispatch or order execution",
        ),
    )
    finish(output, config, input_hash, code_hashes)
    return result


def run_evaluation(config, output):
    files = {"offers_csv", "meter_csv", "market_csv", "predicted_csv", "observed_csv"}
    allowed = files | {"mae_thresholds", "minimum_coverage"}
    data = read_config(config, "historical_evaluation", allowed, {"offers_csv", "meter_csv", "market_csv"})
    config, output = Path(config), output_directory(output)
    input_hash, code_hashes = digest(config), sources()
    if ("predicted_csv" in data) != ("observed_csv" in data) or ("observed_csv" in data) != (
        "mae_thresholds" in data
    ):
        raise ValueError(
            "Observation evaluation needs predicted_csv, observed_csv and explicit mae_thresholds together"
        )
    raw, frames = {}, {}
    for key in sorted(files & set(data)):
        path = config.parent / data[key]
        raw[key] = path.read_bytes()
        from io import BytesIO

        frames[key] = pd.read_csv(BytesIO(raw[key]))
    result = settle_dispatch(
        frames["offers_csv"], frames["meter_csv"], frames["market_csv"], data.get("dt_seconds", 900)
    )
    observations = None
    if "observed_csv" in data:
        observations = score_asset_observations(
            frames["predicted_csv"],
            frames["observed_csv"],
            data["mae_thresholds"],
            data.get("minimum_coverage", 0.95),
        )
    output.mkdir(parents=True, exist_ok=True)
    (output / "inputs").mkdir()
    hashes = {}
    for key, content in raw.items():
        name = f"inputs/{key}.csv"
        (output / name).write_bytes(content)
        hashes[name] = sha256(content).hexdigest()
    result.intervals.to_csv(output / "settlement.csv", index=False, lineterminator="\n")
    write_json(output / "summary.json", result.summary)
    write_json(output / "inputs.json", data)
    if observations is not None:
        observations["scores"].to_csv(output / "observation_scores.csv", index=False, lineterminator="\n")
    write_json(
        output / "decision_report.json",
        dict(
            status="research_only",
            data_kind=data["data_kind"],
            observation_screen=(
                None if observations is None else {k: v for k, v in observations.items() if k != "scores"}
            ),
            scope=result.summary["convention"],
            reasons=[
                "provided metering/settlement convention must be verified by the company",
                "financial cashflows exclude unspecified costs; evaluation is not a profit-uplift claim",
            ],
        ),
    )
    finish(output, config, input_hash, code_hashes, hashes)
    return result


def run_calibration(config, output):
    allowed = {"unit", "curve", "observations_csv", "training_end_utc", "minimum_training_intervals"}
    data = read_config(
        config, "unit_calibration", allowed, {"unit", "curve", "observations_csv", "training_end_utc"}
    )
    config, output = Path(config), output_directory(output)
    input_hash, code_hashes = digest(config), sources()
    raw = (config.parent / data["observations_csv"]).read_bytes()
    from io import BytesIO

    observations = pd.read_csv(BytesIO(raw))
    result = fit_unit_efficiency(
        Unit(**data["unit"]),
        ReservoirCurve(**data["curve"]),
        observations,
        data["training_end_utc"],
        data.get("dt_seconds", 900),
        data.get("minimum_training_intervals", 8),
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "observations.csv").write_bytes(raw)
    write_json(output / "inputs.json", data)
    write_json(output / "fitted_unit.json", asdict(result.fitted_unit))
    write_json(output / "audit.json", result.audit)
    result.predictions.to_csv(output / "predictions.csv", index=False, lineterminator="\n")
    write_json(
        output / "decision_report.json",
        dict(
            status="research_only",
            data_kind=data["data_kind"],
            scope=result.audit["scope"],
            automatic_parameter_promotion=False,
            reasons=[
                "company must validate geometry, metering, held-out errors and efficiency identifiability",
                "fitted multiplier alone does not calibrate an asset or demonstrate operational suitability",
            ],
        ),
    )
    finish(output, config, input_hash, code_hashes, {"observations.csv": sha256(raw).hexdigest()})
    return result


def verify_run(directory, root=ROOT):
    directory, root = Path(directory).resolve(), Path(root).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != 1
        or not manifest.get("artifact_sha256")
        or not manifest.get("code_sha256")
    ):
        raise ValueError("Run manifest schema or required hashes are missing")
    if manifest["artifact_sha256"].get("original_config.json") != manifest["input_sha256"]:
        raise ValueError("Original configuration hash is missing or inconsistent")
    for base, collection in ((directory, manifest["artifact_sha256"]), (root, manifest["code_sha256"])):
        for name, expected in collection.items():
            path = (base / name).resolve()
            if "\\" in name or not path.is_relative_to(base) or digest(path) != expected:
                raise ValueError(f"Run source/artifact hash mismatch: {name}")
    for name, expected in manifest.get("input_files_sha256", {}).items():
        if manifest["artifact_sha256"].get(name) != expected:
            raise ValueError("Copied evaluation input hash mismatch")
    return dict(
        verified=True, artifacts=len(manifest["artifact_sha256"]), sources=len(manifest["code_sha256"])
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only asset-aware hydro planning and pilot evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "evaluate", "calibrate"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument("--output", required=True)
    command = sub.add_parser("verify")
    command.add_argument("--directory", required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        print(json.dumps(verify_run(args.directory), indent=2))
    elif args.command == "plan":
        result = run_case(args.config, args.output)
        print(json.dumps(dict(result.audit, decision_status="research_only"), indent=2))
    elif args.command == "calibrate":
        print(json.dumps(run_calibration(args.config, args.output).audit, indent=2))
    else:
        print(json.dumps(run_evaluation(args.config, args.output).summary, indent=2))


if __name__ == "__main__":
    main()
