# Observed Swedish SE4 quarter-hour day-ahead prices

The CSV contains 3,744 observed auction-price intervals from 20 September through 28 October 2026
in UTC. It includes ten negative-price intervals. It is an observed price archive, not an archived
forecast, balancing-price series, plant observation or market-trading result.

Source: [Fraunhofer ISE Energy-Charts API](https://api.energy-charts.info/), carrying prices attributed
to **Bundesnetzagentur | SMARD.de**. The exact response licence is preserved in the raw JSON and
provenance: **CC BY 4.0**, [licence terms](https://creativecommons.org/licenses/by/4.0/).
Attribution: **Energy-Charts.info / Bundesnetzagentur | SMARD.de**. The CSV changes the raw
columnar timestamp/price representation into UTC interval-start rows; prices are not rescaled,
imputed or interpolated. Negative prices are retained.

The provenance file records endpoint, bidding zone, requested half-open UTC range, retrieval time,
units, cadence, row count and SHA-256 of both the exact response bytes and derived CSV. The raw
response is provided unchanged. The source's [OpenAPI specification](https://api.energy-charts.info/openapi.json)
distinguishes openly licensed price zones from zones restricted to private/internal use. The adapter
checks the actual response licence before saving anything; it does not assume all Nordic data is open.

To fetch into a **new** destination:

```bash
hydro-operations fetch-prices --zone SE4 \
  --start 2026-09-20T00:00Z --end 2026-10-29T00:00Z \
  --output runs/se4-refresh.csv
```

All timestamps require explicit offsets. The parser verifies complete 900-second spacing, unique
ordered rows, finite EUR/MWh values and the exact requested range. It rejects gaps and hourly series.
UTC avoids duplicate local clock labels around daylight saving; a separate test covers the 100
quarter-hours of the 25 October 2026 Europe/Berlin local day.

The operations study forecasts the 27 October UTC delivery day using only intervals ending by
26 October 10:00 UTC. A separate rolling-origin backtest scores 18-28 October delivery days. The
forecast-health decision at that issue uses only completed backtest days available before issue;
later outcomes are retained for retrospective evaluation and never fed into the earlier forecast.
