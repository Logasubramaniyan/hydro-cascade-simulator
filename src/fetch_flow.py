"""Compatibility wrapper; new code should use hydrocascade.data.fetch_usgs."""

from tempfile import TemporaryDirectory
from pathlib import Path
from hydrocascade.data import fetch_usgs


def fetch_usgs_flow(site_no, start, end):
    with TemporaryDirectory() as tmp:
        frame = fetch_usgs(site_no, start, end, Path(tmp) / "flow.csv")
    return frame.rename(columns={"timestamp": "date", "flow_m3s": "flow"})[["date", "flow"]]
