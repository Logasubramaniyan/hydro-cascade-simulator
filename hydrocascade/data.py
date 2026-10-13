"""Strict time-series IO and USGS v1 daily-discharge adapter with provenance."""

from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlparse
import numpy as np
import pandas as pd
import requests
from .model import Cascade

USGS_DAILY = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/daily/items"
CFS_TO_M3S = 0.028316846592


def load_cascade(path):
    return Cascade.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def load_timeseries(path, cascade, dt_seconds):
    """Require ISO timestamps and explicit inflow_<id>_m3s and price_eur_mwh columns."""
    frame = pd.read_csv(path)
    required = ["timestamp", "price_eur_mwh"] + [f"inflow_{p.id}_m3s" for p in cascade.plants]
    if not set(required) <= set(frame):
        raise ValueError(f"Required columns: {required}")
    if len(frame) == 0 or not frame.timestamp.astype(str).str.match(r"^\d{4}-\d{2}-\d{2}(T|$)").all():
        raise ValueError("Use ISO 8601 timestamps")
    timestamps = pd.to_datetime(frame.timestamp, utc=True, errors="raise")
    if timestamps.duplicated().any() or not timestamps.is_monotonic_increasing:
        raise ValueError("Timestamps must be unique and sorted")
    if len(timestamps) > 1 and not np.allclose(timestamps.diff().dropna().dt.total_seconds(), dt_seconds):
        raise ValueError("Timestamp spacing does not match dt_seconds; gaps are not silently filled")
    numbers = frame[required[1:]].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(numbers.to_numpy()).all() or (numbers.iloc[:, 1:].to_numpy() < 0).any():
        raise ValueError("Prices must be finite; inflows must be finite and nonnegative")
    frame[required[1:]] = numbers
    frame["timestamp"] = timestamps
    return frame


def parse_usgs(features, site):
    """Retain quality flags; reject duplicate series/dates and unknown units."""
    rows = []
    expected_site = site if site.startswith("USGS-") else "USGS-" + site
    for feature in features:
        p = feature["properties"]
        if (
            p.get("monitoring_location_id") != expected_site
            or p.get("parameter_code") != "00060"
            or p.get("statistic_id") != "00003"
        ):
            raise ValueError("Unexpected USGS station, parameter or statistic")
        units = p.get("unit_of_measure")
        if units not in ("ft^3/s", "m^3/s"):
            raise ValueError(f"Unsupported USGS discharge units: {units}")
        value = float(p["value"]) if p.get("value") is not None else np.nan
        if not np.isfinite(value) or value < 0:
            raise ValueError("Missing or invalid USGS discharge; no implicit imputation")
        rows.append(
            dict(
                timestamp=p["time"],
                flow_m3s=value * (CFS_TO_M3S if units == "ft^3/s" else 1),
                source_value=value,
                source_unit=units,
                station=expected_site,
                approval_status=p.get("approval_status", "unknown"),
                qualifier=json.dumps(p.get("qualifier")),
                time_series_id=p.get("time_series_id"),
            )
        )
    if not rows:
        raise ValueError("USGS returned no daily discharge data")
    frame = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    if frame.timestamp.duplicated().any() or frame.time_series_id.nunique() != 1:
        raise ValueError("Ambiguous or duplicate USGS time series")
    pd.to_datetime(frame.timestamp, format="%Y-%m-%d", errors="raise")
    return frame


def fetch_usgs(site, start, end, output, api_key=None, session=None):
    """Fetch paginated public data, save exact payload and metadata. API keys are never persisted."""
    if not site.removeprefix("USGS-").isdigit():
        raise ValueError("USGS station number must contain digits")
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    if end_date < start_date:
        raise ValueError("End date precedes start date")
    client = session or requests.Session()
    params = dict(
        monitoring_location_id=site if site.startswith("USGS-") else "USGS-" + site,
        parameter_code="00060",
        statistic_id="00003",
        time=f"{start}/{end}",
        f="json",
        limit=10000,
    )
    headers = {"X-Api-Key": api_key} if api_key else {}
    url, payloads, features, visited = USGS_DAILY, [], [], set()
    while url:
        if url in visited or len(visited) >= 1000:
            raise ValueError("Cyclic or excessive USGS pagination")
        if urlparse(url).scheme != "https" or urlparse(url).netloc != "api.waterdata.usgs.gov":
            raise ValueError("Untrusted pagination host")
        visited.add(url)
        response = client.get(url, params=params, headers=headers, timeout=(10, 45))
        response.raise_for_status()
        page = response.json()
        if page.get("type") != "FeatureCollection":
            raise ValueError("Unexpected USGS response schema")
        payloads.append(page)
        features.extend(page.get("features", []))
        url = next((link["href"] for link in page.get("links", []) if link.get("rel") == "next"), None)
        params = None
    frame = parse_usgs(features, site)
    expected_dates = pd.date_range(start, end, freq="D").strftime("%Y-%m-%d").tolist()
    if frame.timestamp.tolist() != expected_dates:
        raise ValueError("USGS data have gaps or dates outside the requested daily range")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payloads, indent=2, sort_keys=True).encode("utf-8")
    output.with_suffix(".raw.json").write_bytes(raw)
    frame.to_csv(output, index=False)
    metadata = dict(
        provider="USGS",
        endpoint=USGS_DAILY,
        station=params or frame.station.iloc[0],
        start=start,
        end=end,
        retrieved_utc=datetime.now(timezone.utc).isoformat(),
        raw_sha256=sha256(raw).hexdigest(),
        rows=len(frame),
        output_unit="m3/s",
        source="measured river discharge; may be regulated; not naturalized reservoir inflow",
        quality="Approval and estimation flags retained; neither implies plant validation",
    )
    output.with_suffix(".provenance.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return frame


def file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()
