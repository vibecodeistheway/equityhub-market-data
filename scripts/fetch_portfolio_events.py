#!/usr/bin/env python3
"""Generate EquityHub portfolio-events.json from free public sources.

Current free sources:
- SGX public corporate actions API for Singapore dividend events.
- Macro release schedules: FOMC (federalreserve.gov), CPI + jobs report (BLS),
  core PCE / Personal Income and Outlays (BEA).

The app filters this market-wide feed down to the user's current holdings;
macro events are shown regardless of holdings.
Run from repo root:
    python3 scripts/fetch_portfolio_events.py > portfolio-events.json
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Any


SGX_CORPORATE_ACTIONS_URL = "https://api.sgx.com/corporateactions/v1.0"
FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_CPI_SCHEDULE_URL = "https://www.bls.gov/schedule/news_release/cpi.htm"
BLS_EMPSIT_SCHEDULE_URL = "https://www.bls.gov/schedule/news_release/empsit.htm"
BEA_SCHEDULE_URL = "https://apps.bea.gov/rss/rss.xml"
BEA_SCHEDULE_HTML_URL = "https://www.bea.gov/news/schedule"

# Announcement (second) days of scheduled FOMC meetings, updated yearly from
# federalreserve.gov. Used when the calendar page scrape fails.
FOMC_FALLBACK_DATES = [
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
]

MONTHS = {
    name: index
    for index, name in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
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


def fetch_text(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc
    request = urllib.request.Request(
        url,
        headers={
            # BLS's CDN 403s bare clients and some Chrome UA strings; a Safari
            # UA with full Accept headers and a same-site referer passes.
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version=17.4 Safari/605.1.15"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": f"https://{host}/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
        # BLS's CDN fingerprints the TLS stack and 403s Python's urllib while
        # accepting curl with the same headers.
        result = subprocess.run(
            [
                "curl", "-sS", "--fail", "--compressed", "--max-time", "30",
                *[
                    arg
                    for k, v in request.header_items()
                    if k.lower() not in ("host", "connection")
                    for arg in ("-H", f"{k}: {v}")
                ],
                url,
            ],
            capture_output=True,
            check=True,
        )
        return result.stdout.decode("utf-8", errors="replace")


def macro_event(kind: str, title: str, event_date: str, source: str) -> dict[str, Any]:
    return {
        "id": f"macro-{kind}-{event_date}",
        "symbol": kind,
        "market": "US",
        "companyName": title,
        "type": "macro",
        "eventDate": event_date,
        "fiscalPeriod": None,
        "exDate": None,
        "recordDate": None,
        "paymentDate": None,
        "amountPerShare": None,
        "currency": None,
        "source": source,
    }


def fomc_dates() -> list[str]:
    """Announcement days parsed from the Fed's calendar page.

    Each meeting renders a month name and a day pattern like "27-28" or
    "16-17*"; the second day is the statement/decision day.
    """
    html = fetch_text(FOMC_CALENDAR_URL)
    dates: set[str] = set()

    for year_match in re.finditer(r"(20\d{2})\s+FOMC\s+Meetings(.*?)(?=20\d{2}\s+FOMC\s+Meetings|$)", html, re.S | re.I):
        year = int(year_match.group(1))
        block = year_match.group(2)
        for meeting in re.finditer(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)"
            r"(?:/[A-Za-z]+)?\s*</div>.*?(\d{1,2})-(\d{1,2})",
            block,
            re.S,
        ):
            month_name, _, second_day = meeting.groups()
            month = MONTHS[month_name.lower()]
            # A meeting spanning month end ("Oct/Nov 31-1") announces in the
            # following month.
            day = int(second_day)
            if day < int(meeting.group(2)):
                month = month % 12 + 1
            try:
                dates.add(date(year, month, day).isoformat())
            except ValueError:
                continue

    return sorted(dates)


def bls_release_dates(url: str) -> list[str]:
    """Release dates from a BLS schedule page (rows like "Jun 05, 2026")."""
    html = fetch_text(url)
    dates: set[str] = set()
    for match in re.finditer(r"([A-Z][a-z]{2})\.?\s+(\d{1,2}),\s+(20\d{2})", html):
        month_abbr, day, year = match.groups()
        month = MONTHS.get(
            {
                "jan": "january", "feb": "february", "mar": "march", "apr": "april",
                "may": "may", "jun": "june", "jul": "july", "aug": "august",
                "sep": "september", "oct": "october", "nov": "november", "dec": "december",
            }.get(month_abbr.lower(), ""),
            0,
        )
        if not month:
            continue
        try:
            dates.add(date(int(year), month, int(day)).isoformat())
        except ValueError:
            continue
    return sorted(dates)


def bea_pce_dates() -> list[str]:
    """Personal Income and Outlays (core PCE) release dates from bea.gov.

    Each schedule row renders a year-less release date cell
    (`<div class="release-date">July 31</div>`) followed by the release title
    ("Personal Income and Outlays, June 2026"). The year comes from the title;
    December data releasing in January belongs to the following year.
    """
    html = fetch_text(BEA_SCHEDULE_HTML_URL)
    dates: set[str] = set()
    for match in re.finditer(
        r'release-date">'
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2})<(.{0,1500}?)Personal Income and Outlays,\s*"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(20\d{2})",
        html,
        re.S | re.I,
    ):
        release_month_name, day, _, data_month_name, data_year = match.groups()
        release_month = MONTHS[release_month_name.lower()]
        data_month = MONTHS[data_month_name.lower()]
        year = int(data_year) + (1 if release_month < data_month else 0)
        try:
            dates.add(date(year, release_month, int(day)).isoformat())
        except ValueError:
            continue
    return sorted(dates)


def macro_events() -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date().isoformat()
    events: list[dict[str, Any]] = []

    try:
        dates = fomc_dates() or FOMC_FALLBACK_DATES
    except Exception as error:  # noqa: BLE001 - one broken source must not kill the feed
        print(f"FOMC scrape failed: {error}", file=sys.stderr)
        dates = FOMC_FALLBACK_DATES
    events += [
        macro_event("FOMC", "FOMC Rate Decision", d, "Federal Reserve")
        for d in dates
        if d >= today
    ]

    for kind, title, source, fetch in [
        ("CPI", "US CPI Release", "BLS", lambda: bls_release_dates(BLS_CPI_SCHEDULE_URL)),
        ("NFP", "US Jobs Report (Nonfarm Payrolls)", "BLS", lambda: bls_release_dates(BLS_EMPSIT_SCHEDULE_URL)),
        ("PCE", "US Core PCE Release", "BEA", bea_pce_dates),
    ]:
        try:
            events += [
                macro_event(kind, title, d, source)
                for d in fetch()
                if d >= today
            ]
        except Exception as error:  # noqa: BLE001
            print(f"{kind} scrape failed: {error}", file=sys.stderr)

    return events


def main() -> int:
    events = sgx_dividend_events() + macro_events()
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "events": sorted(events, key=lambda event: event["eventDate"]),
    }
    json.dump(output, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

