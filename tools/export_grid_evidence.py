"""Export a complete study to Git-reviewable evidence, retaining native SQLite only in runs/."""

import argparse
from pathlib import Path
import shutil


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", default="docs/grid-evidence")
    args = parser.parse_args(argv)
    source, destination = Path(args.source), Path(args.destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Destination must be empty; use a fresh evidence directory")
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        if path.is_file() and path.suffix != ".sqlite":
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    print(f"Exported reviewable evidence to {destination.resolve()}")


if __name__ == "__main__":
    main()
