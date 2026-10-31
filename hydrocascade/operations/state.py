"""Portable dispatch checkpoints include water in transit and previous total river release."""

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import numpy as np
import pandas as pd

from ..model import finite
from ..simulation import CascadeState, array, step
from .market_data import utc


def cascade_hash(cascade):
    return sha256(json.dumps(cascade.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class DispatchCheckpoint:
    state: CascadeState
    last_release_m3s: np.ndarray
    dt_seconds: float
    cascade_sha256: str
    as_of_utc: str | None = None

    def save(self, path):
        data = dict(
            schema_version=1,
            cascade_sha256=self.cascade_sha256,
            dt_seconds=self.dt_seconds,
            storage_m3=self.state.storage_m3.tolist(),
            queues_m3=self.state.queues_m3,
            last_turbine_m3s=self.state.last_turbine_m3s.tolist(),
            last_release_m3s=self.last_release_m3s.tolist(),
            as_of_utc=self.as_of_utc,
        )
        Path(path).write_text(json.dumps(data, indent=2, allow_nan=False), encoding="utf-8", newline="\n")

    @classmethod
    def load(cls, path, cascade, dt_seconds, expected_as_of=None):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        finite(dt_seconds, "dt_seconds", 1e-9)
        if (
            data.get("schema_version") != 1
            or data.get("cascade_sha256") != cascade_hash(cascade)
            or data["dt_seconds"] != dt_seconds
        ):
            raise ValueError("Checkpoint schema, cascade or interval mismatch")
        state = CascadeState(
            np.asarray(data["storage_m3"], float),
            data["queues_m3"],
            np.asarray(data["last_turbine_m3s"], float),
        )
        state.validate(cascade)
        release = array(data["last_release_m3s"], (len(cascade.plants),), "last total release")
        if (release < state.last_turbine_m3s - 1e-8).any():
            raise ValueError("Checkpoint total release cannot be below turbine flow")
        as_of = utc(data["as_of_utc"]).isoformat() if data.get("as_of_utc") else None
        if expected_as_of is not None and (as_of is None or utc(expected_as_of).isoformat() != as_of):
            raise ValueError("Checkpoint execution time mismatch")
        return cls(state, release, float(dt_seconds), data["cascade_sha256"], as_of)


def advance_dispatch(cascade, state, inflows, records, steps, dt_seconds=900, start_utc=None):
    """Replay an executed prefix; carry both delayed water and the ecological ramp boundary."""
    if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1 or steps > len(inflows):
        raise ValueError("Executed prefix must contain a valid positive number of intervals")
    finite(dt_seconds, "dt_seconds", 1e-9)
    n = len(cascade.plants)
    inflows = array(inflows, (len(inflows), n), "inflows")
    state = state.copy()
    required = {"step", "plant_id", "turbine_m3s", "bypass_m3s", "spill_m3s"}
    if not required <= set(records) or records.duplicated(["step", "plant_id"]).any():
        raise ValueError("Executed records need unique step/plant keys and all release controls")
    indexed = records.set_index(["step", "plant_id"])
    expected = {(t, p.id) for t in range(steps) for p in cascade.plants}
    if not expected <= set(indexed.index):
        raise ValueError("Executed prefix is missing plant controls")
    for t in range(steps):
        controls = [
            [indexed.loc[(t, p.id), col] for p in cascade.plants]
            for col in ("turbine_m3s", "bypass_m3s", "spill_m3s")
        ]
        state, _, _ = step(
            cascade,
            state,
            inflows[t],
            controls[0],
            dt_seconds,
            controls[1],
            controls[2],
            enforce_controls=True,
        )
    as_of = (
        (utc(start_utc) + pd.Timedelta(seconds=dt_seconds * steps)).isoformat()
        if start_utc is not None
        else None
    )
    return DispatchCheckpoint(
        state, np.sum(controls, axis=0), float(dt_seconds), cascade_hash(cascade), as_of
    )
