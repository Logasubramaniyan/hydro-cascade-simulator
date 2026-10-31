"""Licence-checked, UTC quarter-hour public price ingestion with exact raw provenance."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ENDPOINT = "https://api.energy-charts.info/price"


def utc(value):
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError("An explicit timezone or UTC offset is required")
    return timestamp.tz_convert("UTC")


def parse_prices(payload, start, end):
    start, end = utc(start), utc(end)
    if end <= start or start.value % (900 * 10**9) or end.value % (900 * 10**9):
        raise ValueError("Use a nonempty, quarter-hour-aligned half-open UTC range")
    licence = str(payload.get("license_info", ""))
    # Never redistribute Nordic zones with private/internal-use-only licences.
    if "cc by 4.0" not in licence.lower() or "private" in licence.lower() or "prohibited" in licence.lower():
        raise ValueError("Public evidence requires an explicit CC BY 4.0 source licence")
    if str(payload.get("unit", "")).replace(" ", "") != "EUR/MWh" or payload.get("deprecated"):
        raise ValueError("Unexpected price units or deprecated endpoint")
    seconds, prices = np.asarray(payload["unix_seconds"], float), np.asarray(payload["price"], float)
    if (
        seconds.ndim != 1
        or prices.shape != seconds.shape
        or not len(seconds)
        or not (np.isfinite(seconds).all() and np.isfinite(prices).all())
        or (seconds % 900 != 0).any()
    ):
        raise ValueError("Price timestamps and values must be finite aligned vectors; no imputation")
    timestamps = pd.to_datetime(seconds, unit="s", utc=True)
    if timestamps.has_duplicates or not timestamps.is_monotonic_increasing:
        raise ValueError("Price timestamps must be ordered and unique")
    frame = pd.DataFrame({"timestamp": timestamps, "price_eur_mwh": prices})
    frame = frame[(frame.timestamp >= start) & (frame.timestamp < end)].reset_index(drop=True)
    expected = pd.date_range(start, end, freq="15min", inclusive="left")
    if not pd.DatetimeIndex(frame.timestamp).equals(expected):
        raise ValueError("Missing quarter-hours; hourly prices are not expanded into fabricated observations")
    return frame


def fetch_prices(zone, start, end, output, session=None):
    start, end = utc(start), utc(end)
    if (
        not zone
        or not isinstance(zone, str)
        or len(zone) > 20
        or end <= start
        or end - start > pd.Timedelta(days=62)
    ):
        raise ValueError("Choose a bidding zone and an increasing range of at most 62 days")
    output = Path(output)
    paths = (output, output.with_suffix(".raw.json"), output.with_suffix(".provenance.json"))
    if any(path.exists() for path in paths):
        raise ValueError("Price output/provenance must be new; preserve existing evidence")
    client = session or requests.Session()
    response = client.get(
        ENDPOINT, params={"bzn": zone, "start": start.isoformat(), "end": end.isoformat()}, timeout=(10, 60)
    )
    response.raise_for_status()
    payload = response.json()
    frame = parse_prices(payload, start, end)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = response.content
    paths[1].write_bytes(raw)
    frame.to_csv(output, index=False, lineterminator="\n")
    provenance = dict(
        provider="Fraunhofer ISE Energy-Charts; original price source stated in response licence",
        endpoint=ENDPOINT,
        zone=zone,
        start_utc=start.isoformat(),
        end_exclusive_utc=end.isoformat(),
        retrieved_at_utc=datetime.now(timezone.utc).isoformat(),
        licence=payload["license_info"],
        attribution="Energy-Charts.info / Bundesnetzagentur | SMARD.de",
        source_kind="observed day-ahead auction prices; not forecasts or balancing prices",
        timestamp_semantics="UTC interval start; 900-second duration; end exclusive",
        raw_sha256=sha256(raw).hexdigest(),
        csv_sha256=sha256(output.read_bytes()).hexdigest(),
        intervals=len(frame),
        negative_price_intervals=int((frame.price_eur_mwh < 0).sum()),
    )
    paths[2].write_text(json.dumps(provenance, indent=2), encoding="utf-8", newline="\n")
    return frame, provenance
