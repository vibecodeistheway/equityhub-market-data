#!/usr/bin/env python3
"""Fetches macro market indicators and writes indicators.json.

Sources:
  - Shiller PE, inflation (YoY), US GDP: multpl.com (HTML scrape)
  - CNN Fear & Greed: production.dataviz.cnn.io JSON API

Runs on a schedule via GitHub Actions; the EquityHub iOS app reads the
committed indicators.json from raw.githubusercontent.com.

Stdlib only — no pip dependencies.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MULTPL_PAGES = {
    "shiller_pe": "https://www.multpl.com/shiller-pe",
    "us_gdp_trillions": "https://www.multpl.com/us-gdp",
}

# multpl's inflation page lags the BLS release by weeks; BLS is authoritative
# and updates on release day (CPI-U, all items, not seasonally adjusted).
MULTPL_INFLATION_FALLBACK = "https://www.multpl.com/inflation"
BLS_CPI_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0"

FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


def fetch(url, headers=None):
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_multpl_value(url):
    """multpl.com renders the latest value as the first number after id="current"."""
    html = fetch(url)
    marker = html.find('id="current"')
    if marker == -1:
        raise ValueError(f"id=\"current\" marker not found on {url}")
    tail = html[marker : marker + 500]
    match = re.search(r"[0-9][0-9,]*\.?[0-9]*", tail)
    if not match:
        raise ValueError(f"no numeric value found after marker on {url}")
    return float(match.group(0).replace(",", ""))


def fetch_multpl_history(url, max_points):
    """History from a multpl table page (newest first), returned oldest first."""
    html = fetch(url)
    rows = re.findall(
        r"<td>([A-Z][a-z]{2} \d{1,2}, \d{4})</td>\s*<td>\s*&#x2002;\s*([\d.]+)",
        html,
    )
    if not rows:
        raise ValueError(f"no rows parsed from {url}")

    history = []
    for raw_date, raw_value in rows[:max_points]:
        parsed = datetime.strptime(raw_date, "%b %d, %Y")
        history.append({"d": parsed.strftime("%Y-%m-%d"), "v": float(raw_value)})
    history.reverse()
    return history


# 25 years of context for the in-app charts
HISTORY_SERIES = {
    "shiller_pe_history": ("https://www.multpl.com/shiller-pe/table/by-month", 25 * 12),
    "inflation_history": ("https://www.multpl.com/inflation/table/by-month", 25 * 12),
    "us_gdp_history": ("https://www.multpl.com/us-gdp/table/by-quarter", 25 * 4),
}


def fetch_cpi_yoy_bls():
    """Headline CPI YoY from the official BLS public API (no key needed)."""
    from datetime import date

    year = date.today().year
    url = f"{BLS_CPI_URL}?startyear={year - 1}&endyear={year}"
    data = json.loads(fetch(url))
    if data.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError(f"BLS API status: {data.get('status')}")

    entries = data["Results"]["series"][0]["data"]  # newest first
    latest = entries[0]
    prior = next(
        e
        for e in entries
        if e["period"] == latest["period"] and int(e["year"]) == int(latest["year"]) - 1
    )
    return round((float(latest["value"]) / float(prior["value"]) - 1) * 100, 2)


def fetch_fear_greed():
    raw = fetch(
        FEAR_GREED_URL,
        headers={
            "Accept": "application/json",
            "Referer": "https://www.cnn.com/markets/fear-and-greed",
            "Origin": "https://www.cnn.com",
        },
    )
    current = json.loads(raw)["fear_and_greed"]
    return {
        "score": current["score"],
        "rating": current["rating"],
        "previous_close": current["previous_close"],
        "previous_1_week": current["previous_1_week"],
        "previous_1_month": current["previous_1_month"],
        "previous_1_year": current["previous_1_year"],
    }


def main():
    output = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    errors = []

    for key, url in MULTPL_PAGES.items():
        try:
            output[key] = fetch_multpl_value(url)
        except Exception as exc:  # noqa: BLE001 - record and continue
            errors.append(f"{key}: {exc}")

    try:
        output["inflation_yoy"] = fetch_cpi_yoy_bls()
    except Exception as bls_exc:  # noqa: BLE001
        errors.append(f"inflation_yoy (BLS): {bls_exc}")
        try:
            output["inflation_yoy"] = fetch_multpl_value(MULTPL_INFLATION_FALLBACK)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"inflation_yoy (multpl fallback): {exc}")

    for key, (url, max_points) in HISTORY_SERIES.items():
        try:
            output[key] = fetch_multpl_history(url, max_points)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key}: {exc}")

    try:
        output["fear_greed"] = fetch_fear_greed()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"fear_greed: {exc}")

    if errors:
        output["errors"] = errors
        print("WARNING: some indicators failed:", *errors, sep="\n  ", file=sys.stderr)

    # Require at least the valuation core; otherwise fail the run so the
    # last good indicators.json stays in place.
    if "shiller_pe" not in output and "fear_greed" not in output:
        print("FATAL: all sources failed, keeping previous indicators.json", file=sys.stderr)
        sys.exit(1)

    with open("indicators.json", "w") as fh:
        json.dump(output, fh, indent=2)
        fh.write("\n")

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
