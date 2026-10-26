"""Fetch live equity prices (yfinance) and mutual fund NAVs (mfapi.in / AMFI).

Uses Ticker.info rather than yf.download()/.history() - on this machine's
Python 3.14 + numpy 2.5 combination, download()/history() segfault (likely a
numpy ABI incompatibility in yfinance's price-history parsing path), while
.info uses a simpler JSON-only request path that works reliably.

Bonds are intentionally excluded - no reliable free live-price source for
Indian bonds (illiquid/OTC), so they stay on their static snapshot value.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import yfinance as yf

MFAPI_LATEST_URL = "https://api.mfapi.in/mf/{code}/latest"
MFAPI_HISTORY_URL = "https://api.mfapi.in/mf/{code}"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

BSE_QUOTE_URL = "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?Debtflag=&scripcode={code}&seriesid="
BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json, text/plain, */*",
}
# NSE symbol -> BSE scrip code, for tickers where yfinance's regularMarketPrice has been
# observed to come back implausible (see the 52-week-range check below) - REITs/InvITs
# are thinly traded and Yahoo's Indian REIT/InvIT data feed appears unreliable for them.
# NSE's own quote API blocks sandboxed/datacenter IPs (Access Denied), so BSE is the
# fallback instead. Verified against BSE's stock page on 2026-09-02.
BSE_SCRIP_CODES = {
    "INDIGRID": "540565",
    "MINDSPACE": "543217",
    "BIRET": "543261",
}


def fetch_from_bse(symbol):
    """Fallback quote for a symbol in BSE_SCRIP_CODES. Returns {"price", "prev_close"} or None."""
    code = BSE_SCRIP_CODES.get(symbol)
    if not code:
        return None
    try:
        resp = requests.get(BSE_QUOTE_URL.format(code=code), headers=BSE_HEADERS, timeout=10)
        resp.raise_for_status()
        header = resp.json()["Header"]
        price = float(header["LTP"])
        prev_close = float(header["PrevClose"])
        return {
            "price": round(price, 4),
            "prev_close": round(prev_close, 4),
            "name": header.get("Cmpname") or symbol,
            "exchange": "BSE",
            "currency": "INR",
            "quote_type": "EQUITY",
            "sector": "Real Estate"
        }
    except Exception:
        return None


def fetch_single_equity(symbol):
    """Worker task to fetch an individual equity quote from Yahoo Finance with BSE fallback."""
    high_52wk = None
    try:
        us_symbols = {"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "NFLX", "AMD", "INTC"}
        yahoo_symbol = symbol if symbol in us_symbols or str(symbol).endswith(".US") else f"{symbol}.NS"
        info = yf.Ticker(yahoo_symbol).info
        price = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
        prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
        low_52wk = info.get("fiftyTwoWeekLow")
        high_52wk = info.get("fiftyTwoWeekHigh")
        if price and low_52wk and high_52wk and not (low_52wk * 0.98 <= price <= high_52wk * 1.02):
            price = None  # implausible vs its own 52-week range - don't trust yfinance here
        if price:
            return symbol, {
                "price": round(float(price), 4),
                "prev_close": round(float(prev_close), 4) if prev_close else round(float(price), 4),
                "high_52wk": round(float(high_52wk), 4) if high_52wk else None,
                "name": info.get("longName") or info.get("shortName") or "",
                "exchange": info.get("fullExchangeName") or info.get("exchange") or "",
                "sector": info.get("sector") or "",
                "market_cap": info.get("marketCap") or "",
                "quote_type": info.get("quoteType") or "",
                "currency": info.get("currency") or "",
            }
    except Exception:
        pass
    bse_quote = fetch_from_bse(symbol)
    if bse_quote:
        bse_quote["high_52wk"] = round(float(high_52wk), 4) if high_52wk else None
        return symbol, bse_quote
    return symbol, None


def fetch_equity_prices(symbols, max_workers=16):
    """Concurrent Multi-Source Orchestrator for equities using ThreadPoolExecutor.
    symbols: list of NSE tickers (no .NS suffix).
    Returns {symbol: {"price": float, "prev_close": float, ...}}.
    """
    unique_symbols = sorted(set(symbols))
    prices = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_single_equity, s): s for s in unique_symbols}
        for future in as_completed(futures):
            sym, quote = future.result()
            if quote:
                prices[sym] = quote
    return prices


def fetch_usd_inr():
    """Return the current USD/INR quote for converting US holdings into INR."""
    try:
        response = requests.get(
            YAHOO_CHART_URL.format(symbol="INR=X"),
            params={"range": "5d", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        response.raise_for_status()
        result = (response.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return None
        meta = result.get("meta", {})
        return float(meta.get("regularMarketPrice") or meta.get("chartPreviousClose"))
    except Exception:
        return None


def fetch_single_mf(code):
    """Worker task to fetch an individual mutual fund NAV from AMFI API."""
    try:
        resp = requests.get(MFAPI_HISTORY_URL.format(code=code), timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        history = payload["data"]
        nav = round(float(history[0]["nav"]), 4)
        prev_nav = round(float(history[1]["nav"]), 4) if len(history) > 1 else nav
        return code, {"nav": nav, "prev_nav": prev_nav}
    except Exception:
        return code, None


def fetch_mf_navs(scheme_codes, max_workers=10):
    """Concurrent Multi-Source Orchestrator for AMFI Mutual Funds using ThreadPoolExecutor.
    scheme_codes: list of AMFI scheme codes (as strings).
    Returns {code: {"nav": float, "prev_nav": float}}.
    """
    unique_codes = sorted(set(scheme_codes))
    navs = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_single_mf, c): c for c in unique_codes}
        for future in as_completed(futures):
            c, result = future.result()
            if result:
                navs[c] = result
    return navs


