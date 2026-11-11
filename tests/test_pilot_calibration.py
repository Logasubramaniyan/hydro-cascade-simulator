from dataclasses import replace
import numpy as np
import pandas as pd
import pytest

from hydrocascade.pilot.assets import ReservoirCurve, Unit
from hydrocascade.pilot.calibration import fit_unit_efficiency


def observations():
    curve = ReservoirCurve("p", (0, 10000), (10, 30), 0)
    unit = Unit("u", "p", (1, 2), (0.8, 0.9))
    frame = pd.DataFrame(
        dict(
            timestamp=pd.date_range("2026-01-01T00:00Z", periods=12, freq="15min"),
            start_storage_m3=np.linspace(1000, 7000, 12),
            end_storage_m3=np.linspace(1200, 7200, 12),
            flow_m3s=np.linspace(1, 2, 12),
        )
    )
    frame["power_mw"] = [
        0.8 * curve.power(unit, r.flow_m3s, r.start_storage_m3, r.end_storage_m3) for r in frame.itertuples()
    ]
    return unit, curve, frame


def test_efficiency_fit_recovers_known_scale_and_scores_only_later_intervals():
    unit, curve, frame = observations()
    result = fit_unit_efficiency(unit, curve, frame, "2026-01-01T02:00Z")
    assert result.audit["efficiency_scale"] == pytest.approx(0.8)
    assert result.fitted_unit.efficiency == pytest.approx((0.64, 0.72))
    assert result.audit["training_intervals"] == 8
    assert result.audit["heldout_intervals"] == 4
    assert result.audit["heldout_mae_mw"] < 1e-10
    future = frame.copy()
    future.loc[8:, "power_mw"] *= 2
    changed = fit_unit_efficiency(unit, curve, future, "2026-01-01T02:00Z")
    assert changed.audit["efficiency_scale"] == result.audit["efficiency_scale"]
    assert changed.audit["heldout_mae_mw"] > 0


def test_efficiency_cannot_exceed_one_and_boundary_is_visible():
    unit, curve, frame = observations()
    frame["power_mw"] *= 5
    result = fit_unit_efficiency(unit, curve, frame, "2026-01-01T02:00Z")
    assert max(result.fitted_unit.efficiency) <= 1
    assert result.audit["at_parameter_bound"]


def test_insufficient_training_or_no_holdout_and_bad_measured_values_fail():
    unit, curve, frame = observations()
    with pytest.raises(ValueError, match="training"):
        fit_unit_efficiency(unit, curve, frame, "2026-01-01T00:30Z")
    with pytest.raises(ValueError, match="held-out"):
        fit_unit_efficiency(unit, curve, frame, "2026-01-02T00:00Z")
    frame.loc[0, "power_mw"] = np.nan
    with pytest.raises(ValueError):
        fit_unit_efficiency(unit, curve, frame, "2026-01-01T02:00Z")
    with pytest.raises(ValueError):
        fit_unit_efficiency(replace(unit, plant_id="other"), curve, frame, "2026-01-01T02:00Z")


def test_off_unit_does_not_hide_out_of_range_storage_observations():
    unit, curve, frame = observations()
    frame.loc[0, ["flow_m3s", "power_mw"]] = 0
    frame.loc[0, "start_storage_m3"] = 20000
    with pytest.raises(ValueError, match="range"):
        fit_unit_efficiency(unit, curve, frame, "2026-01-01T02:00Z")
