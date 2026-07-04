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
SGX_SECURITIES_URL = "https://api.sgx.com/securities/v1.1"

# Static mapping of action company name substrings to trading codes (e.g. OAJ.SI -> OAJ)
MAPPING = {
    "FORTRESS MINERALS": "OAJ",
    "STAMFORD LAND": "H07",
    "SINGAPORE SHIPPING": "S19",
    "PHILLIP SING INCOME": "OVQ",
    "PHILLIP SGX APAC": "BYI",
    "LION-OCBC SEC SG LOW CARBON": "LCR",
    "LION-OCBC SEC CHINA LEADERS": "YYY",
    "LION-OCBC SEC APAC FIN DIV": "YLD",
    "LION SHORT DUR BOND": "SBO",
    "XMH HOLDINGS": "BQF",
    "METRO HOLDINGS": "M01",
    "SINGAPORE POST": "S08",
    "TIANJIN PHARM": "T14",
    "TELKOM INDONESIA": "ITKD",
    "SPDR S&P 500": "S27",
    "SPDR DJIA": "D07",
    "AMOVA-STC ASIA": "CFA",
    "AMOVA-ICBCSG CN BD": "ZHY",
    "AMOVA SINGAPORE STI": "G3B",
    "AMOVA SGD IG CORP": "MBH",
    "ABF SPORE BOND": "A35",
    "ELITE UK REIT": "MXNU",
    "CSC HOLDINGS": "C06",
    "BANK CENTRAL ASIA": "IBKD",
    "ICBC CSOP": "CYC",
    "CONCORD NEW ENERGY": "SEG",
    "RECLAIMS GLOBAL": "NEX",
    "VALUETRONICS": "BN2",
    "BOUSTEAD SINGAPORE": "F9D",
    "SATS": "S58",
    "SINGTEL": "Z74",
    "FUXING CHINA": "AWK"
}


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


def clean_name(name: str | None) -> str:
    if not name:
        return ""
    name = name.upper()
    suffixes = [
        r"\bLIMITED\b", r"\bLTD\b", r"\bGROUP\b", r"\bHOLDINGS\b", 
        r"\bTRUST\b", r"\bREIT\b", r"\bCORP\b", r"\bCORPORATION\b",
        r"\bCO\b", r"\bINC\b", r"\bPLC\b"
    ]
    for suffix in suffixes:
        name = re.sub(suffix, "", name)
    return re.sub(r"[^A-Z0-9]", "", name)


def fetch_securities_map() -> dict[str, str]:
    """Fetch all listed SGX securities and build clean name map to trading codes (nc)."""
    try:
        data = fetch_json(SGX_SECURITIES_URL)
        prices = data.get("data", {}).get("prices", [])
        name_map = {}
        for p in prices:
            code = p.get("nc")
            if not code:
                continue
            name = p.get("n")
            issuer = p.get("issuer-name")
            if name:
                name_map[clean_name(name)] = code
            if issuer:
                name_map[clean_name(issuer)] = code
        return name_map
    except Exception as e:
        sys.stderr.write(f"Warning: failed to build securities map: {e}\n")
        return {}


def sgx_dividend_events() -> list[dict[str, Any]]:
    payload = fetch_json(SGX_CORPORATE_ACTIONS_URL)
    securities_map = fetch_securities_map()
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
        company_name = item.get("name") or ""
        
        # Determine the ticker symbol (code) using our combined mapping strategy
        symbol = None
        
        # 1. Try static mapping first
        for substring, target_code in MAPPING.items():
            if substring in company_name.upper():
                symbol = target_code
                break
                
        # 2. Try clean name fallback matching against the securities list
        if not symbol:
            action_clean = clean_name(company_name)
            symbol = securities_map.get(action_clean)
            
        # 3. Fallback to raw code fields if matching failed
        if not symbol:
            symbol = item.get("code") or item.get("ibmCode") or company_name

        events.append(
            {
                "id": f"sgx-{item.get('id')}",
                "symbol": f"{str(symbol).upper()}.SI" if not str(symbol).endswith(".SI") else str(symbol).upper(),
                "market": "SG",
                "companyName": company_name,
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
