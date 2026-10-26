"""Fetch and cache market-cap-based Large/Mid/Small classification for equity holdings.

Uses yfinance's `.info` (marketCap field) - same reliable JSON-only code path already
used by live_prices.py/sector_data.py (never `.history()`, which segfaults on this
machine's Python 3.14 + numpy combination). Market cap changes daily, but the cap
*bucket* rarely flips, so results are cached in data/cap_map.csv rather than
re-fetched on every sync.

Classification uses absolute market-cap thresholds as an approximation of SEBI/AMFI's
official rank-based definition (top 100 by market cap = Large Cap, 101-250 = Mid Cap,
251+ = Small Cap, re-ranked twice a year across the full listed universe). We don't
have that full-universe ranking here, so this is a reasonable proxy, not the official
classification.
"""
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_FILE = ROOT / "Portfolio_Holdings.xlsx"
CAP_MAP_FILE = ROOT / "data" / "cap_map.csv"

LARGE_CAP_CR = 20_000
MID_CAP_CR = 5_000


def classify(market_cap_cr):
    if market_cap_cr >= LARGE_CAP_CR:
        return "Large Cap"
    if market_cap_cr >= MID_CAP_CR:
        return "Mid Cap"
    return "Small Cap"


def load_cap_map():
    if not CAP_MAP_FILE.exists():
        return {}
    df = pd.read_csv(CAP_MAP_FILE, dtype=str).fillna("")
    return dict(zip(df["Symbol"], zip(df["MarketCapCr"], df["Cap"])))


def fetch_cap(symbol):
    try:
        market_cap = yf.Ticker(f"{symbol}.NS").info.get("marketCap")
        if not market_cap:
            return None
        market_cap_cr = round(float(market_cap) / 1e7, 1)  # marketCap is in INR; 1 Cr = 1e7
        return market_cap_cr, classify(market_cap_cr)
    except Exception:
        return None


def build():
    holdings = pd.read_excel(HOLDINGS_FILE).fillna("")
    symbols = sorted(set(holdings.loc[holdings["Asset Type"] == "Stocks", "Symbol"]) - {""})
    existing = load_cap_map()

    updated = dict(existing)
    fetched, failed = 0, []
    for symbol in symbols:
        if existing.get(symbol):
            continue  # already cached - cap bucket rarely changes, don't re-fetch every run
        result = fetch_cap(symbol)
        if result:
            updated[symbol] = result
            fetched += 1
        else:
            failed.append(symbol)

    rows = sorted((symbol, cap_cr, cap) for symbol, (cap_cr, cap) in updated.items())
    pd.DataFrame(rows, columns=["Symbol", "MarketCapCr", "Cap"]).to_csv(CAP_MAP_FILE, index=False)
    print(f"Fetched {fetched} new market caps, {len(updated)} total cached, {len(failed)} failed: {failed}")


if __name__ == "__main__":
    build()
