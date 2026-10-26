"""Fetch and cache GICS-style sector/industry data for equity holdings.

Uses yfinance's `.info` (sector/industry fields) - same reliable JSON-only code path
already used by live_prices.py for quotes (never `.history()`, which segfaults on
this machine's Python 3.14 + numpy combination). Sectors rarely change, so results
are cached in data/sector_map.csv rather than re-fetched on every sync.
"""
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_FILE = ROOT / "Portfolio_Holdings.xlsx"
SECTOR_MAP_FILE = ROOT / "data" / "sector_map.csv"
EQUITY_TYPES = {"Stocks", "ETF", "REIT", "InvIT"}

# Index/commodity ETFs (Nippon "Bees" family etc.) and REIT/InvIT trusts don't have a
# GICS sector via yfinance .info (sector/industry come back empty, quoteType shows as
# MUTUALFUND for trusts) - label them by what they track/hold instead of a blank cell.
ETF_SECTOR_OVERRIDES = {
    "GOLDBEES": "Gold ETF",
    "SILVERBEES": "Silver ETF",
    "LIQUIDBEES": "Liquid / Money Market ETF",
    "KRT-RR": "Real Estate (REIT)",
    "BIRET": "Real Estate (REIT)",
    "MINDSPACE": "Real Estate (REIT)",
    "INDIGRID": "Power Transmission (InvIT)",
}


def load_sector_map():
    if not SECTOR_MAP_FILE.exists():
        return {}
    df = pd.read_csv(SECTOR_MAP_FILE, dtype=str).fillna("")
    return dict(zip(df["Symbol"], df["Sector"]))


def fetch_sector(symbol):
    if symbol in ETF_SECTOR_OVERRIDES:
        return ETF_SECTOR_OVERRIDES[symbol]
    try:
        info = yf.Ticker(f"{symbol}.NS").info
        sector = info.get("sector") or info.get("industryDisp") or info.get("industry")
        return sector or ""
    except Exception:
        return ""


def build():
    holdings = pd.read_excel(HOLDINGS_FILE).fillna("")
    symbols = sorted(set(holdings.loc[holdings["Asset Type"].isin(EQUITY_TYPES), "Symbol"]) - {""})
    existing = load_sector_map()

    updated = dict(existing)
    fetched, failed = 0, []
    for symbol in symbols:
        if existing.get(symbol):
            continue  # already cached - sectors rarely change, don't re-fetch every run
        sector = fetch_sector(symbol)
        if sector:
            updated[symbol] = sector
            fetched += 1
        else:
            failed.append(symbol)

    pd.DataFrame(sorted(updated.items()), columns=["Symbol", "Sector"]).to_csv(SECTOR_MAP_FILE, index=False)
    print(f"Fetched {fetched} new sectors, {len(updated)} total cached, {len(failed)} failed: {failed}")


if __name__ == "__main__":
    build()
