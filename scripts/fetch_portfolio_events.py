#!/usr/bin/env python3
"""Generate EquityHub portfolio-events.json from free public sources.

Current free sources:
- SGX public corporate actions API for Singapore dividend events.
- Beansprout (growbeansprout.com) earnings calendar for SGX earnings dates.
- Alpha Vantage CSV for US upcoming earnings.
- Nasdaq calendar API for US upcoming dividends.
- Macro release schedules: FOMC (federalreserve.gov), CPI + jobs report (BLS),
  core PCE / Personal Income and Outlays (BEA).

The app filters this market-wide feed down to the user's current holdings.
"""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
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

def fetch_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)

def fetch_raw(url: str, headers: dict[str, str] | None = None) -> str:
    if headers is None:
        headers = {"User-Agent": "Mozilla/5.0"}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")

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
    try:
        data = fetch_json(SGX_SECURITIES_URL, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Referer": "https://www.sgx.com/",
        })
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
    try:
        payload = fetch_json(SGX_CORPORATE_ACTIONS_URL, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Referer": "https://www.sgx.com/",
        })
    except Exception as e:
        sys.stderr.write(f"Error fetching SGX events: {e}\n")
        return []
        
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
        
        # Determine the ticker symbol using combined mapping strategy
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

def sg_earnings_events() -> list[dict[str, Any]]:
    """Upcoming SGX earnings dates from Beansprout's earnings calendar.

    The server-rendered page embeds a SvelteKit data island with an
    `earningsCalendar` JS array covering past and upcoming announcements
    (beyond the rendered month), e.g.
    `{companyName:"Keppel DC REIT",exchangeTicker:"AJBU.SI",displayTicker:
    "AJBU",announcementDate:"2026-04-16",fiscalPeriod:"1Q"}`. Falls back to
    the per-entry data:text/calendar (ICS) hrefs if the blob format changes.
    """
    events: list[dict[str, Any]] = []
    today = datetime.now(timezone.utc).date().isoformat()

    try:
        html = fetch_text("https://growbeansprout.com/earnings-calendar")
    except Exception as e:
        sys.stderr.write(f"Error fetching Beansprout earnings calendar: {e}\n")
        return events

    seen: set[str] = set()

    def add(ticker: str, event_date: str, company: str, period: str | None) -> None:
        event_id = f"sg-earnings-{ticker}-{event_date}"
        if event_date < today or event_id in seen:
            return
        seen.add(event_id)
        events.append({
            "id": event_id,
            "symbol": f"{ticker}.SI",
            "market": "SG",
            "companyName": company,
            "type": "earnings",
            "eventDate": event_date,
            "fiscalPeriod": period,
            "exDate": None,
            "recordDate": None,
            "paymentDate": None,
            "amountPerShare": None,
            "currency": "SGD",
            "source": "Beansprout",
        })

    for match in re.finditer(
        r'companyName:"([^"]+)",exchangeTicker:"([A-Z0-9]+)\.SI",displayTicker:"[^"]*",'
        r'announcementDate:"(\d{4}-\d{2}-\d{2})",fiscalPeriod:(?:"([^"]*)"|null)',
        html,
    ):
        company, ticker, event_date, period = match.groups()
        add(ticker, event_date, company, period or None)

    if not events:
        for match in re.finditer(r'href="data:text/calendar;charset=utf-8,([^"]+)"', html):
            ics = urllib.parse.unquote(match.group(1))
            uid = re.search(r"UID:([A-Z0-9]+)-(\d{8})@", ics)
            summary = re.search(r"SUMMARY:(.+?)\s*Earnings Report", ics)
            if not uid or not summary:
                continue
            ticker, raw_date = uid.groups()
            event_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
            period = re.search(r"earnings report for (.+?) on ", ics)
            add(ticker, event_date, summary.group(1).strip(),
                period.group(1).strip() if period else None)

    return events

def us_earnings_events() -> list[dict[str, Any]]:
    """Fetch upcoming US stock earnings using Alpha Vantage's free demo endpoint."""
    events: list[dict[str, Any]] = []
    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()
    max_date_str = (today + timedelta(days=30)).isoformat()
    
    url = "https://www.alphavantage.co/query?function=EARNINGS_CALENDAR&horizon=3month&apikey=demo"
    sys.stderr.write(f"Fetching US earnings calendar from: {url}\n")
    try:
        csv_data = fetch_raw(url)
        reader = csv.DictReader(io.StringIO(csv_data))
        for row in reader:
            symbol = row.get("symbol", "").upper()
            report_date = row.get("reportDate", "")
            if not symbol or not report_date:
                continue
                
            # Filter for next 30 days
            if today_str <= report_date <= max_date_str:
                name = row.get("name", "")
                
                # Filter out ETFs
                name_upper = name.upper()
                if any(x in name_upper for x in ["ETF", "INDEX", "SPDR", "ISHARES"]):
                    continue
                if any(x in symbol for x in ["ETF", "SPY", "IVV", "VOO"]):
                    continue
                    
                events.append({
                    "id": f"us-earnings-{symbol}-{report_date}",
                    "symbol": symbol,
                    "market": "US",
                    "companyName": name,
                    "type": "earnings",
                    "eventDate": report_date,
                    "fiscalPeriod": None,
                    "exDate": None,
                    "recordDate": None,
                    "paymentDate": None,
                    "amountPerShare": None,
                    "currency": row.get("currency") or "USD",
                    "source": "Alpha Vantage",
                })
    except Exception as e:
        sys.stderr.write(f"Error fetching US earnings events: {e}\n")
        
    return events

def us_dividend_events() -> list[dict[str, Any]]:
    """Fetch upcoming US stock dividends from Nasdaq calendar API for the next 15 days."""
    events: list[dict[str, Any]] = []
    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()
    
    # Query Nasdaq day-by-day for the next 15 days
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }
    
    for i in range(15):
        date_str = (today + timedelta(days=i)).isoformat()
        url = f"https://api.nasdaq.com/api/calendar/dividends?date={date_str}"
        try:
            payload = fetch_json(url, headers=headers)
            calendar = payload.get("data", {}).get("calendar")
            if not calendar:
                continue
                
            rows = calendar.get("rows")
            if not rows:
                continue
                
            for row in rows:
                symbol = row.get("symbol", "").upper()
                ex_date_raw = row.get("dividend_Ex_Date")
                payment_date_raw = row.get("payment_Date")
                if not symbol or not ex_date_raw:
                    continue
                    
                # Clean and parse Nasdaq dates (Format: "M/DD/YYYY" e.g., "7/06/2026")
                def parse_nasdaq_date(d_str: str | None) -> str | None:
                    if not d_str:
                        return None
                    try:
                        return datetime.strptime(d_str, "%m/%d/%Y").date().isoformat()
                    except Exception:
                        return None
                        
                ex_date = parse_nasdaq_date(ex_date_raw)
                payment_date = parse_nasdaq_date(payment_date_raw)
                event_date = payment_date or ex_date
                if not event_date or event_date < today_str:
                    continue
                    
                company_name = row.get("companyName", "")
                
                # Filter out ETFs
                name_upper = company_name.upper()
                if any(x in name_upper for x in ["ETF", "INDEX", "SPDR", "ISHARES"]):
                    continue
                if any(x in symbol for x in ["ETF", "SPY", "IVV", "VOO"]):
                    continue
                    
                events.append({
                    "id": f"us-dividend-{symbol}-{event_date}",
                    "symbol": symbol,
                    "market": "US",
                    "companyName": company_name,
                    "type": "dividend",
                    "eventDate": event_date,
                    "fiscalPeriod": None,
                    "exDate": ex_date,
                    "recordDate": parse_nasdaq_date(row.get("record_Date")),
                    "paymentDate": payment_date,
                    "amountPerShare": float(row.get("dividend_Rate")) if row.get("dividend_Rate") is not None else None,
                    "currency": "USD",
                    "source": "Nasdaq Calendar",
                })
        except Exception as e:
            sys.stderr.write(f"Error fetching US dividends for {date_str}: {e}\n")
            
    return events

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
    sgx = sgx_dividend_events()
    us_earn = us_earnings_events()
    us_div = us_dividend_events()
    
    all_events = sgx + sg_earnings_events() + us_earn + us_div + macro_events()
    
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "events": sorted(all_events, key=lambda event: event["eventDate"]),
    }
    json.dump(output, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
