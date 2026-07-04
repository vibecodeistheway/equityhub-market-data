#!/usr/bin/env python3
"""Generate EquityHub portfolio-events.json from free public sources.

Current free source:
- SGX public corporate actions API for Singapore dividend events.

The app filters this market-wide feed down to the user's current holdings.
Run from repo root:
    python3 scripts/fetch_portfolio_events.py > portfolio-events.json
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any


SGX_CORPORATE_ACTIONS_URL = "https://api.sgx.com/corporateactions/v1.0"


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Referer": "https://www.sgx.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def ms_to_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).date().isoformat()


def parse_rate(particulars: str | None) -> tuple[str | None, float | None]:
    if not particulars:
        return None, None
    match = re.search(r"Rate:\s*([A-Z]{3})\s*([0-9]+(?:\.[0-9]+)?)", particulars)
    if not match:
        return None, None
    return match.group(1), float(match.group(2))


def sgx_dividend_events() -> list[dict[str, Any]]:
    payload = fetch_json(SGX_CORPORATE_ACTIONS_URL)
    events: list[dict[str, Any]] = []

    today = datetime.now(timezone.utc).date().isoformat()
    for item in payload.get("data", []):
        if item.get("anncType") != "DIVIDEND":
            continue

        payment_date = ms_to_date(item.get("datePaid"))
        ex_date = ms_to_date(item.get("exDate"))
        event_date = payment_date or ex_date or ms_to_date(item.get("dateAnnc"))
        if not event_date or event_date < today:
            continue

        currency, amount = parse_rate(item.get("particulars"))
        symbol = item.get("code") or item.get("ibmCode") or item.get("name")

        events.append(
            {
                "id": f"sgx-{item.get('id')}",
                "symbol": str(symbol).upper(),
                "market": "SG",
                "companyName": item.get("name"),
                "type": "dividend",
                "eventDate": event_date,
                "fiscalPeriod": None,
                "exDate": ex_date,
                "recordDate": ms_to_date(item.get("recDate")),
                "paymentDate": payment_date,
                "amountPerShare": amount,
                "currency": currency or "SGD",
                "source": "SGX Corporate Actions",
            }
        )

    return events


def main() -> int:
    events = sgx_dividend_events()
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "events": sorted(events, key=lambda event: event["eventDate"]),
    }
    json.dump(output, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

