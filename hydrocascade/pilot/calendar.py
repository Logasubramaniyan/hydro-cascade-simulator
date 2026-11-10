"""Explicit delivery intervals and configurable product screening, including local DST days."""

from dataclasses import dataclass
from datetime import date, timedelta
import numpy as np
import pandas as pd

from ..model import finite
from .assets import integer


def instant(value):
    if not isinstance(value, (str, pd.Timestamp)):
        raise ValueError("Timestamp must have an explicit UTC offset or timezone")
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Timestamp must have an explicit UTC offset or timezone")
    return stamp.tz_convert("UTC")


def validate_timestamps(values, expected=None, dt_seconds=900):
    finite(dt_seconds, "dt_seconds", 1e-9)
    stamps = pd.DatetimeIndex([instant(v) for v in values])
    if not len(stamps) or stamps.has_duplicates or not stamps.is_monotonic_increasing:
        raise ValueError("Timestamp keys must be nonempty, unique and ordered")
    if len(stamps) > 1 and not (np.diff(stamps.asi8) == round(dt_seconds * 1e9)).all():
        raise ValueError("Timestamp cadence does not match interval duration")
    if expected is not None and not stamps.equals(pd.DatetimeIndex(expected)):
        raise ValueError("Timestamp keys must exactly match the expected delivery window")
    return stamps


def delivery_day(day, timezone="Europe/Stockholm", dt_seconds=900):
    """Return the actual UTC intervals of a local civil delivery day (92/96/100 at 15 minutes)."""
    if dt_seconds not in (900, 1800, 3600):
        raise ValueError("Delivery-day intervals must be 900, 1800 or 3600 seconds")
    parsed = date.fromisoformat(day)
    start = pd.Timestamp(parsed).tz_localize(timezone)
    end = pd.Timestamp(parsed + timedelta(days=1)).tz_localize(timezone)
    times = pd.date_range(
        start.tz_convert("UTC"),
        end.tz_convert("UTC"),
        freq=pd.Timedelta(seconds=dt_seconds),
        inclusive="left",
    )
    return pd.DataFrame(
        dict(
            timestamp=times,
            end_utc=times + pd.Timedelta(seconds=dt_seconds),
            local_start=[t.tz_convert(timezone).isoformat() for t in times],
            period=np.arange(1, len(times) + 1),
        )
    )


@dataclass(frozen=True)
class ProductRules:
    name: str
    minimum_mw: float = 1
    increment_mw: float = 1
    block_steps: int = 1
    dt_seconds: int = 900
    block_anchor_utc: str = "1970-01-01T00:00:00Z"

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Product rules need an explicit name/source label")
        finite(self.minimum_mw, "minimum_mw", 0)
        finite(self.increment_mw, "increment_mw", 1e-9)
        integer(self.block_steps, "block_steps")
        integer(self.dt_seconds, "dt_seconds")
        instant(self.block_anchor_utc)


def check_product(offers, rules, issued_at, gate_closure, columns=("reserve_up_mw", "reserve_down_mw")):
    """Screen supplied rules; do not alter quantities or imply qualification/accepted orders."""
    if "timestamp" not in offers or not set(columns) <= set(offers):
        raise ValueError("Product screening requires timestamps and all quantity columns")
    times = validate_timestamps(offers.timestamp, dt_seconds=rules.dt_seconds)
    if len(offers) % rules.block_steps:
        raise ValueError("Delivery window must contain complete product blocks")
    issued, gate = instant(issued_at), instant(gate_closure)
    if gate > times[0]:
        raise ValueError("Gate closure cannot be after the delivery window begins")
    violations = []
    interval_ns = rules.dt_seconds * 1_000_000_000
    if times[0].value % interval_ns:
        violations.append("interval_alignment")
    if (times[0].value - instant(rules.block_anchor_utc).value) % (interval_ns * rules.block_steps):
        violations.append("block_alignment")
    if issued >= gate:
        violations.append("gate_closed")
    for column in columns:
        values = np.asarray(offers[column], float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError("Product quantities must be finite and nonnegative")
        positive = values > 1e-8
        if (values[positive] < rules.minimum_mw - 1e-7).any():
            violations.append("minimum_quantity")
        if (np.abs(values / rules.increment_mw - np.round(values / rules.increment_mw)) > 1e-7).any():
            violations.append("quantity_increment")
        blocks = values.reshape(-1, rules.block_steps)
        if (np.ptp(blocks, axis=1) > 1e-7).any():
            violations.append("block_quantity")
    return dict(
        passes=not violations,
        violations=sorted(set(violations)),
        product=rules.name,
        issued_at_utc=issued.isoformat(),
        gate_closure_utc=gate.isoformat(),
        scope="configured quantity/block/time screening; no response, telemetry or TSO qualification",
    )
