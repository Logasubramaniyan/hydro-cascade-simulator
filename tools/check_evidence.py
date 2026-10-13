"""Verify committed case-study inputs, source hashes and all declared output hashes."""

from pathlib import Path
import json
import sys
from hashlib import sha256


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    evidence = root / "docs" / "evidence"
    manifest = json.loads((evidence / "manifest.json").read_text())
    for relative, expected in manifest["code_sha256"].items():
        assert digest(root / relative) == expected, f"Stale evidence source: {relative}"
    for relative, expected in manifest["source_sha256"].items():
        assert digest(evidence / relative) == expected, f"Changed input: {relative}"
    for relative, expected in manifest["artifact_sha256"].items():
        assert digest(evidence / relative) == expected, f"Changed artifact: {relative}"
    data = root / "data" / "observed" / "usgs_06090800_2023-01.csv"
    provenance = json.loads(data.with_suffix(".provenance.json").read_text())
    assert digest(data.with_suffix(".raw.json")) == provenance["raw_sha256"]
    result = json.loads((evidence / "results.json").read_text())
    case = (root / "docs" / "CASE_STUDY.md").read_text(encoding="utf-8")
    for item in result["policy_summary"]:
        assert f"{item['mean_objective_eur']:,.2f}" in case
        assert item["trials"] == 8
    assert f"{result['optimizer']['full_objective_eur']:,.2f}" in case
    assert result["optimizer"]["max_water_residual_m3"] < 1e-6
    print("Evidence hashes and case-study headline values verified.")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, KeyError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
