"""Copy the verified demo's figures and runnable inputs to their documented locations."""

from pathlib import Path
import shutil


def main():
    root = Path(__file__).resolve().parents[1]
    evidence = root / "docs" / "evidence"
    assets = root / "docs" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    for name in ("dispatch.svg", "policies.svg"):
        shutil.copyfile(evidence / name, assets / name)
    shutil.copyfile(evidence / "cascade.json", root / "examples" / "cascade.json")
    shutil.copyfile(evidence / "cascade.json", root / "config" / "plants.json")
    shutil.copyfile(evidence / "inputs.csv", root / "examples" / "hourly.csv")
    print("Portfolio figures and example inputs copied from verified evidence.")


if __name__ == "__main__":
    main()
