# EquityHub Market Data

Automated macro-indicator feed for the EquityHub iOS app's Market tab.

A scheduled GitHub Actions workflow ([update.yml](.github/workflows/update.yml))
runs every 6 hours, fetches the indicators below, and commits
[`indicators.json`](indicators.json). The app reads it from:

```
https://raw.githubusercontent.com/vibecodeistheway/equityhub-market-data/main/indicators.json
```

| Field | Source |
|---|---|
| `shiller_pe` | [multpl.com/shiller-pe](https://www.multpl.com/shiller-pe) |
| `inflation_yoy` | BLS public API (official CPI-U YoY) — [multpl.com/inflation](https://www.multpl.com/inflation) fallback |
| `us_gdp_trillions` | [multpl.com/us-gdp](https://www.multpl.com/us-gdp) |
| `fear_greed` | CNN Fear & Greed Index |
| `shiller_pe_history` | multpl by-month table (last 25y, monthly, oldest first) |

If a source fails, the previous value stays in place (the script only fails the
run when everything is unreachable), and failures are listed under `errors`.

The S&P 500 / Wilshire 5000 quotes are *not* in this feed — the app fetches
those live from Yahoo Finance.
