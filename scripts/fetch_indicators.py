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
    "inflation_yoy": "https://www.multpl.com/inflation",
    "us_gdp_trillions": "https://www.multpl.com/us-gdp",
}

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
