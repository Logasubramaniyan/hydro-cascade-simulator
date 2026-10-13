# Measured USGS daily discharge sample

Provider: U.S. Geological Survey. Station `USGS-06090800`. Parameter `00060` (discharge),
statistic `00003` (daily mean). Period: 2023-01-01 to 2023-01-15, inclusive.

- `usgs_06090800_2023-01.csv`: sorted observations; converted SI discharge and original values/units.
- `usgs_06090800_2023-01.raw.json`: exact JSON payload content returned by the modern v1 API.
- `usgs_06090800_2023-01.provenance.json`: endpoint, retrieval time, range and raw-file SHA-256.

These are measured river observations, not observations of the invented portfolio cascade. Some
values have `ESTIMATED` qualifiers, which are preserved alongside the provider's approval status.
Do not call this sample naturalized reservoir inflow without a separate hydrological assessment.
It demonstrates ingestion and conversion, not plant-output validation.

Re-fetch with:

```bash
hydro-cascade fetch-usgs --site 06090800 --start 2023-01-01 --end 2023-01-15 \
  --output runs/usgs.csv
```

The provider may revise records; compare retrieval dates and raw hashes. CI uses the checked-in
sample and mocked requests, so normal tests do not require network access or credentials.

Sources: [USGS API migration documentation](https://api.waterdata.usgs.gov/docs/ogcapi/migration/),
[station page](https://waterdata.usgs.gov/monitoring-location/06090800/).

USGS data are attributed to the provider and generally public domain; the project's MIT code license
does not relabel those observations as project-created data.
