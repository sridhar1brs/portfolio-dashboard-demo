"""Small market-context quote service using Yahoo chart JSON directly."""
import socket
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import requests

# Force IPv4 resolution to prevent macOS IPv6 timeout delays on Yahoo Finance API
_orig_getaddrinfo = socket.getaddrinfo

def _getaddrinfo_v4(*args, **kwargs):
    res = _orig_getaddrinfo(*args, **kwargs)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 if v4 else res

socket.getaddrinfo = _getaddrinfo_v4

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0"}
CACHE_SECONDS = 300
_cache = {"at": 0, "data": None}

MARKETS = [
    ("Nifty 50", "^NSEI", "index"),
    ("Sensex", "^BSESN", "index"),
    ("Nifty Bank", "^NSEBANK", "index"),
    ("S&P 500", "^GSPC", "index"),
    ("NASDAQ", "^IXIC", "index"),
    ("USD / INR", "INR=X", "currency"),
    ("US 10Y Treasury", "^TNX", "rate"),
    ("India VIX", "^INDIAVIX", "volatility"),
    ("Gold", "GC=F", "commodity"),
    ("Crude Oil (WTI)", "CL=F", "commodity"),
]

# Standard finance thresholds for how far an index has fallen from its 52-week high:
# 5% = pullback, 10% = correction, 20% = bear market. Matches the chart meta's own
# fiftyTwoWeekHigh field, so no extra request is needed beyond the 2-day quote we fetch.
CORRECTION_LEVELS = [(20, "Bear market territory"), (10, "Correction"), (5, "Pullback")]


def _quote(name, symbol, kind):
    response = requests.get(
        CHART_URL.format(symbol=symbol),
        params={"range": "2d", "interval": "1d"},
        headers=HEADERS,
        timeout=10,
    )
    response.raise_for_status()
    result = (response.json().get("chart", {}).get("result") or [None])[0]
    if not result:
        raise ValueError("No quote data")
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")

    closes = [value for value in (result.get("indicators", {}).get("quote", [{}])[0].get("close", [])) if value is not None]
    if price is None and closes:
        price = closes[-1]
    if price is None:
        raise ValueError("Missing price")

    # Yahoo provides official session change in regularMarketChange / fulldayChange
    market_change = meta.get("regularMarketChange") if meta.get("regularMarketChange") is not None else meta.get("fulldayChange")
    market_change_pct = meta.get("regularMarketChangePercent") if meta.get("regularMarketChangePercent") is not None else meta.get("fulldayChangePercent")

    # Determine previous day's official close
    previous = meta.get("previousClose")
    if not previous and len(closes) >= 2:
        previous = closes[-2]
    if not previous:
        previous = meta.get("chartPreviousClose")

    if market_change is not None and market_change_pct is not None:
        change = float(market_change)
        change_pct = float(market_change_pct)
        if not previous:
            previous = float(price) - change
    else:
        change = float(price) - float(previous or price)
        change_pct = (change / float(previous) * 100) if previous else 0.0

    quote = {
        "name": name,
        "symbol": symbol,
        "kind": kind,
        "price": round(float(price), 4),
        "change": round(float(change), 4),
        "change_pct": round(float(change_pct), 2),
        "previous_close": round(float(previous), 4) if previous else None,
        "ok": True,
    }
    if kind == "index":
        high_52wk = meta.get("fiftyTwoWeekHigh")
        if high_52wk:
            drawdown_pct = (float(high_52wk) - float(price)) / float(high_52wk) * 100
            level_label = next((label for threshold, label in CORRECTION_LEVELS if drawdown_pct >= threshold), None)
            quote["high_52wk"] = round(float(high_52wk), 2)
            quote["drawdown_from_high_pct"] = round(drawdown_pct, 2)
            quote["correction_level"] = level_label
    return quote


def fetch_market_context(force_refresh=False):
    now = time.time()
    if not force_refresh and _cache["data"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]

    def _fetch_single(m):
        name, symbol, kind = m
        try:
            return {"quote": _quote(name, symbol, kind), "failure": None}
        except Exception as exc:
            return {"quote": None, "failure": {"name": name, "symbol": symbol, "error": type(exc).__name__}}

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(_fetch_single, MARKETS))

    quotes = [r["quote"] for r in results if r["quote"] is not None]
    failures = [r["failure"] for r in results if r["failure"] is not None]

    data = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "source": "Yahoo Finance Real-time Chart API",
        "quotes": quotes,
        "failures": failures,
    }
    _cache.update({"at": now, "data": data})
    return data

