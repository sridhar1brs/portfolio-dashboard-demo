"""Fetch trailing cash distributions for selected REIT, InvIT, and real-estate ETF holdings."""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = ROOT / "data" / "distribution_history.csv"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0"}
DISTRIBUTION_SYMBOLS = {"BIRET", "MINDSPACE", "INDIGRID", "KRT-RR"}


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_distribution_events(symbol):
    response = requests.get(
        CHART_URL.format(symbol=f"{symbol}.NS"),
        params={"range": "1y", "interval": "1d", "events": "div"},
        headers=HEADERS,
        timeout=12,
    )
    response.raise_for_status()
    result = (response.json().get("chart", {}).get("result") or [None])[0]
    dividends = (result or {}).get("events", {}).get("dividends", {})
    retrieved = datetime.now(timezone.utc).isoformat()
    return [
        {
            "Symbol": symbol,
            "ExDate": datetime.fromtimestamp(event["date"], timezone.utc).date().isoformat(),
            "AmountPerUnit": round(_number(event.get("amount")), 6),
            "Source": "Yahoo Finance chart JSON",
            "RetrievedAt": retrieved,
        }
        for event in dividends.values()
        if _number(event.get("amount"))
    ]


def fetch_distribution_income(rows):
    rows = [row for row in rows if str(row.get("Symbol") or "") in DISTRIBUTION_SYMBOLS]
    events, unavailable = [], set()
    for symbol in sorted({str(row.get("Symbol")) for row in rows}):
        try:
            symbol_events = fetch_distribution_events(symbol)
            if not symbol_events:
                unavailable.add(symbol)
            events.extend(symbol_events)
        except Exception:
            unavailable.add(symbol)
    pd.DataFrame(events, columns=["Symbol", "ExDate", "AmountPerUnit", "Source", "RetrievedAt"]).to_csv(CACHE_FILE, index=False)
    by_symbol = {}
    for row in rows:
        symbol = str(row.get("Symbol"))
        quantity = _number(row.get("Quantity"))
        current_value = _number(row.get("Latest Value"))
        invested = _number(row.get("Inv. Amount"))
        ttm_per_unit = sum(event["AmountPerUnit"] for event in events if event["Symbol"] == symbol)
        income = ttm_per_unit * quantity
        by_symbol.setdefault(symbol, {
            "symbol": symbol,
            "name": row.get("Name") or symbol,
            "quantity": 0,
            "current_value": 0,
            "invested": 0,
            "ttm_per_unit": ttm_per_unit,
            "ttm_income": 0,
        })
        item = by_symbol[symbol]
        item["quantity"] += quantity
        item["current_value"] += current_value
        item["invested"] += invested
        item["ttm_income"] += income
    results = []
    for symbol, item in sorted(by_symbol.items()):
        item["monthly_run_rate"] = item["ttm_income"] / 12
        item["ttm_yield_pct"] = item["ttm_income"] / item["current_value"] * 100 if item["current_value"] else None
        item["overall_gain"] = item["current_value"] - item["invested"]
        # Approximate total return: trailing income plus observed price movement, on invested capital.
        item["total_return_pct"] = (
            (item["overall_gain"] + item["ttm_income"]) / item["invested"] * 100 if item["invested"] else None
        )
        item["status"] = "Data unavailable" if symbol in unavailable else "Trailing estimate"
        results.append(item)
    totals = {
        "invested": sum(item["invested"] for item in results),
        "current_value": sum(item["current_value"] for item in results),
        "ttm_income": sum(item["ttm_income"] for item in results),
        "monthly_run_rate": sum(item["monthly_run_rate"] for item in results),
    }
    totals["overall_gain"] = totals["current_value"] - totals["invested"]
    totals["total_return_pct"] = (
        (totals["overall_gain"] + totals["ttm_income"]) / totals["invested"] * 100 if totals["invested"] else None
    )
    totals["ttm_yield_pct"] = totals["ttm_income"] / totals["current_value"] * 100 if totals["current_value"] else None
    return {
        "items": results, "totals": totals,
        "source": "Yahoo Finance chart JSON", "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
