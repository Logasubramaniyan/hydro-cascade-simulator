"""Observation scoring, with explicit alignment, coverage and missingness checks."""

import numpy as np
import pandas as pd


def score_observations(predicted, observed, metrics=("storage_m3", "power_mw", "turbine_m3s")):
    """Join by timestamp and plant_id; measured data must be supplied independently.

    This function scores a given model; it does not fit parameters or claim calibration.
    """
    keys = ["timestamp", "plant_id"]
    frames = []
    for frame in (predicted.copy(), observed.copy()):
        if not set(keys + list(metrics)) <= set(frame):
            raise ValueError("Missing observation keys or metrics")
        frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True, errors="raise")
        if frame[keys].isna().any().any() or frame.duplicated(keys).any():
            raise ValueError("Observation keys must be non-null and unique")
        for metric in metrics:
            frame[metric] = pd.to_numeric(frame[metric], errors="raise")
            if np.isinf(frame[metric]).any():
                raise ValueError("Infinite observations are invalid")
        frames.append(frame)
    joined = frames[0].merge(
        frames[1], on=keys, how="outer", suffixes=("_pred", "_obs"), indicator=True, validate="one_to_one"
    )
    results = []
    for plant_id, group in joined.groupby("plant_id"):
        for metric in metrics:
            available = group[[metric + "_pred", metric + "_obs"]].dropna()
            if len(available) == 0:
                results.append(
                    dict(
                        plant_id=plant_id,
                        metric=metric,
                        matched=0,
                        coverage=0.0,
                        mae=None,
                        rmse=None,
                        bias=None,
                        nse=None,
                    )
                )
                continue
            errors = available[metric + "_pred"] - available[metric + "_obs"]
            observations = available[metric + "_obs"]
            denominator = ((observations - observations.mean()) ** 2).sum()
            results.append(
                dict(
                    plant_id=plant_id,
                    metric=metric,
                    matched=len(available),
                    coverage=len(available) / len(group),
                    mae=float(errors.abs().mean()),
                    rmse=float(np.sqrt((errors**2).mean())),
                    bias=float(errors.mean()),
                    nse=float(1 - (errors**2).sum() / denominator) if denominator > 0 else None,
                )
            )
    return pd.DataFrame(results)
