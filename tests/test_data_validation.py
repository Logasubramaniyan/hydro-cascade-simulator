import json
import numpy as np
import pandas as pd
import pytest
from hydrocascade.data import parse_usgs, fetch_usgs, load_timeseries, CFS_TO_M3S, load_cascade
from hydrocascade.experiments import demo_cascade
from hydrocascade.validation import score_observations


def feature(time="2023-01-01", value="100", unit="ft^3/s", **kwargs):
    return {
        "properties": dict(
            monitoring_location_id="USGS-06090800",
            parameter_code="00060",
            statistic_id="00003",
            time=time,
            value=value,
            unit_of_measure=unit,
            time_series_id="abc",
            approval_status="Approved",
            qualifier=["ESTIMATED"],
        )
        | kwargs
    }


def test_usgs_conversion_quality_and_sorting():
    frame = parse_usgs([feature("2023-01-02"), feature("2023-01-01")], "06090800")
    assert frame.timestamp.tolist() == ["2023-01-01", "2023-01-02"]
    assert frame.flow_m3s.iloc[0] == pytest.approx(100 * CFS_TO_M3S)
    assert frame.qualifier.iloc[0] == '["ESTIMATED"]'
    assert frame.approval_status.iloc[0] == "Approved"


@pytest.mark.parametrize(
    "feature_list",
    [
        [],
        [feature(value=None)],
        [feature(value="-999999")],
        [feature(unit="unknown")],
        [feature(), feature()],
        [feature(parameter_code="00065")],
    ],
)
def test_usgs_rejects_missing_ambiguous_and_incompatible_data(feature_list):
    with pytest.raises(ValueError):
        parse_usgs(feature_list, "06090800")


class FakeResponse:
    def __init__(self, payload, fails=False):
        self.payload = payload
        self.fails = fails

    def raise_for_status(self):
        if self.fails:
            raise RuntimeError("HTTP error")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def test_paginated_fetch_writes_provenance_and_does_not_save_key(tmp_path):
    url = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/daily/items?offset=1"
    session = FakeSession(
        [
            FakeResponse(
                {
                    "type": "FeatureCollection",
                    "features": [feature()],
                    "links": [{"rel": "next", "href": url}],
                }
            ),
            FakeResponse({"type": "FeatureCollection", "features": [feature("2023-01-02")]}),
        ]
    )
    path = tmp_path / "flow.csv"
    frame = fetch_usgs("06090800", "2023-01-01", "2023-01-02", path, "test-only-token", session)
    assert len(frame) == 2
    assert len(session.calls) == 2
    assert session.calls[0][1]["timeout"] == (10, 45)
    assert session.calls[0][1]["headers"]["X-Api-Key"] == "test-only-token"
    for output in tmp_path.iterdir():
        assert "test-only-token" not in output.read_text()
    assert json.loads(path.with_suffix(".provenance.json").read_text())["rows"] == 2


def test_fetch_rejects_gaps_untrusted_pagination_and_http_errors(tmp_path):
    session = FakeSession([FakeResponse({"type": "FeatureCollection", "features": [feature()]})])
    with pytest.raises(ValueError, match="gaps"):
        fetch_usgs("06090800", "2023-01-01", "2023-01-02", tmp_path / "flow.csv", session=session)
    session = FakeSession(
        [
            FakeResponse(
                {
                    "type": "FeatureCollection",
                    "features": [feature()],
                    "links": [{"rel": "next", "href": "https://example.com/data"}],
                }
            )
        ]
    )
    with pytest.raises(ValueError, match="pagination host"):
        fetch_usgs("06090800", "2023-01-01", "2023-01-01", tmp_path / "flow.csv", session=session)
    with pytest.raises(RuntimeError, match="HTTP error"):
        fetch_usgs(
            "06090800",
            "2023-01-01",
            "2023-01-01",
            tmp_path / "flow.csv",
            session=FakeSession([FakeResponse({}, True)]),
        )


def test_csv_rejects_ambiguous_dates_gaps_and_invalid_values(tmp_path):
    cascade = demo_cascade()
    columns = {f"inflow_{p.id}_m3s": [1, 2] for p in cascade.plants}
    frame = pd.DataFrame(
        dict(timestamp=["2025-01-01T00:00:00Z", "2025-01-01T01:00:00Z"], price_eur_mwh=[10, -20], **columns)
    )
    path = tmp_path / "inputs.csv"
    frame.to_csv(path, index=False)
    assert len(load_timeseries(path, cascade, 3600)) == 2
    for timestamps in (
        ["01-01-2025", "02-01-2025"],
        [frame.timestamp.iloc[0]] * 2,
        [frame.timestamp.iloc[0], "2025-01-01T03:00:00Z"],
    ):
        bad = frame.copy()
        bad["timestamp"] = timestamps
        bad.to_csv(path, index=False)
        with pytest.raises(ValueError):
            load_timeseries(path, cascade, 3600)
    frame.loc[0, "inflow_upper_m3s"] = -1
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError):
        load_timeseries(path, cascade, 3600)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(cascade.to_dict()))
    assert load_cascade(cfg) == cascade


def observations():
    return pd.DataFrame(
        dict(
            timestamp=["2025-01-01", "2025-01-02", "2025-01-03"],
            plant_id=["p"] * 3,
            storage_m3=[1, 2, 3],
            power_mw=[1, 2, 3],
            turbine_m3s=[1, 2, 3],
        )
    )


def test_observation_scoring_known_errors_and_missingness():
    obs = observations()
    pred = obs.copy()
    pred["power_mw"] += 1
    scores = score_observations(pred, obs).set_index("metric")
    assert scores.loc["power_mw", "mae"] == 1
    assert scores.loc["power_mw", "rmse"] == 1
    assert scores.loc["power_mw", "bias"] == 1
    assert scores.loc["power_mw", "nse"] == pytest.approx(-0.5)
    pred.loc[0, "storage_m3"] = np.nan
    scores = score_observations(pred, obs).set_index("metric")
    assert scores.loc["storage_m3", "coverage"] == pytest.approx(2 / 3)
    with pytest.raises(ValueError):
        score_observations(pd.concat([pred, pred.iloc[:1]]), obs)


def test_zero_variance_and_no_matches_are_not_fabricated_accuracy():
    obs = observations()
    obs["power_mw"] = 1
    pred = obs.copy()
    pred["storage_m3"] = np.nan
    scores = score_observations(pred, obs).set_index("metric")
    assert np.isnan(scores.loc["power_mw", "nse"])
    assert scores.loc["storage_m3", "matched"] == 0


def test_measured_sample_conversion_and_raw_hash_are_reproducible():
    from pathlib import Path
    from hydrocascade.data import file_hash

    path = Path(__file__).resolve().parents[1] / "data" / "observed" / "usgs_06090800_2023-01.csv"
    raw = json.loads(path.with_suffix(".raw.json").read_text())
    frame = parse_usgs([feature for page in raw for feature in page["features"]], "06090800")
    saved = pd.read_csv(path)
    assert frame.timestamp.tolist() == saved.timestamp.tolist()
    assert np.allclose(frame.flow_m3s, saved.flow_m3s)
    assert (
        file_hash(path.with_suffix(".raw.json"))
        == json.loads(path.with_suffix(".provenance.json").read_text())["raw_sha256"]
    )
