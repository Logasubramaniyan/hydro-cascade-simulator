import json
import subprocess
import sys
from pathlib import Path
from hydrocascade.cli import main


def test_offline_demo_and_manifest(tmp_path):
    output = tmp_path / "demo"
    main(["demo", "--output", str(output), "--steps", "6", "--horizon", "3", "--trials", "1"])
    assert (output / "dispatch.svg").exists()
    assert (output / "policy_trials.csv").exists()
    findings = json.loads((output / "results.json").read_text())
    assert findings["dataset"] == "synthetic"
    assert findings["optimizer"]["max_water_residual_m3"] < 1e-6
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["code_sha256"]
    assert all("\\" not in path for path in manifest["code_sha256"])
    assert all("\\" not in path for path in manifest["artifact_sha256"])
    assert "dispatch.svg" in manifest["artifact_sha256"]
    main(
        [
            "schedule",
            "--config",
            str(output / "cascade.json"),
            "--inputs",
            str(output / "inputs.csv"),
            "--output",
            str(tmp_path / "scheduled"),
        ]
    )
    assert (tmp_path / "scheduled" / "schedule.csv").exists()


def test_module_help_from_unrelated_working_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    import os

    env = dict(os.environ, PYTHONPATH=str(root))
    result = subprocess.run(
        [sys.executable, "-m", "hydrocascade", "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "schedule" in result.stdout


def test_legacy_adapter_uses_shared_physics():
    from src.reservoir import ReservoirPlant

    plant = ReservoirPlant(
        dict(name="dry", head=50, efficiency=0.9, max_flow=100, storage_capacity=1000, initial_storage=0)
    )
    assert plant.step(0) == 0
    assert plant.storage == 0
    assert plant.history["power_mw"] == [0]
