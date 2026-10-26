"""Fetch candidate equity dividends and append estimated lot-level income events.

The Yahoo chart JSON endpoint is used directly because yfinance's dividends property
segfaults in this environment. Results are candidates only: current quantity is used
as an estimate and must be checked against broker statements before confirmation.
"""
import datetime as dt
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_FILE = ROOT / "Portfolio_Holdings.xlsx"
LEDGER_FILE = ROOT / "data" / "dividends.csv"
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
HEADERS = {"User-Agent": "Mozilla/5.0"}

LEDGER_COLUMNS = ["Event ID", "Symbol", "Name", "Ex-Date", "Record Date", "Payment Date",
                  "Amount Per Share", "Quantity Eligible", "Total Amount", "Status", "Source",
                  "Verified", "Notes"]
EQUITY_TYPES = {"Stocks", "ETF", "REIT", "InvIT"}


def _date(value):
    try:
        return dt.datetime.strptime(str(value), "%d-%m-%Y").date()
    except ValueError:
        return None


def fetch_dividends(symbol, start_date, end_date):
    start = int(dt.datetime.combine(start_date, dt.time.min, dt.timezone.utc).timestamp())
    end = int(dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min, dt.timezone.utc).timestamp())
    response = requests.get(URL.format(symbol=symbol), params={
        "period1": start, "period2": end, "interval": "1d", "events": "div",
    }, headers=HEADERS, timeout=15)
    response.raise_for_status()
    result = (response.json().get("chart", {}).get("result") or [None])[0]
    return (result or {}).get("events", {}).get("dividends", {})


def build():
    today = dt.date.today()
    holdings = pd.read_excel(HOLDINGS_FILE).fillna("")
    existing = pd.read_csv(LEDGER_FILE, dtype=str).fillna("") if LEDGER_FILE.exists() else pd.DataFrame(columns=LEDGER_COLUMNS)
    existing_ids = set(existing["Event ID"].astype(str))
    additions = []
    failures = []
    purchase_dates = {}

    for _, holding in holdings.iterrows():
        if holding.get("Asset Type") not in EQUITY_TYPES:
            continue
        symbol = str(holding.get("Symbol", "")).strip()
        purchase_date = _date(holding.get("Inv. Date", ""))
        if not symbol or not purchase_date:
            continue
        purchase_dates[str(holding["Lot ID"])] = purchase_date
        try:
            events = fetch_dividends(symbol, purchase_date, today)
        except Exception as exc:
            failures.append(f"{symbol}: {type(exc).__name__}")
            continue
        quantity = float(holding.get("Quantity") or 0)
        for event in events.values():
            event_date = dt.datetime.fromtimestamp(int(event["date"]), dt.timezone.utc).date()
            amount = round(float(event["amount"]), 4)
            event_id = f"DIV-{holding['Lot ID']}-{event_date.isoformat()}-{amount}"
            if event_id in existing_ids:
                continue
            additions.append({
                "Event ID": event_id,
                "Symbol": symbol,
                "Name": holding.get("Name", ""),
                "Ex-Date": event_date.strftime("%d-%m-%Y"),
                "Record Date": "",
                "Payment Date": "",
                "Amount Per Share": amount,
                "Quantity Eligible": round(quantity, 4),
                "Total Amount": round(amount * quantity, 2),
                "Status": "Estimated",
                "Source": "Yahoo Finance chart events",
                "Verified": "no",
                "Notes": "Candidate event; verify eligible quantity, payment, and TDS against broker statement.",
            })
            existing_ids.add(event_id)

    if not existing.empty:
        def keep_existing(row):
            prefix = "DIV-"
            event_id = str(row.get("Event ID", ""))
            parts = event_id[len(prefix):].split("-") if event_id.startswith(prefix) else []
            year_index = next((index for index, part in enumerate(parts) if len(part) == 4 and part.isdigit()), -1)
            lot_id = "-".join(parts[:year_index]) if year_index > 0 else ""
            event_date = _date(row.get("Ex-Date", ""))
            is_generated = str(row.get("Status", "")).lower() == "estimated" and str(row.get("Verified", "")).lower() in {"", "no", "false", "0"}
            return not (is_generated and lot_id in purchase_dates and event_date and event_date < purchase_dates[lot_id])

        existing = existing[existing.apply(keep_existing, axis=1)].copy()
        existing_ids = set(existing["Event ID"].astype(str))

    if additions:
        additions_df = pd.DataFrame(additions, columns=LEDGER_COLUMNS)
        existing = additions_df if existing.empty else pd.concat([existing, additions_df], ignore_index=True)
    existing.reindex(columns=LEDGER_COLUMNS).to_csv(LEDGER_FILE, index=False)
    print(f"Added {len(additions)} estimated dividend events; ledger now has {len(existing)} events.")
    print(f"Total estimated gross dividends: {pd.to_numeric(existing['Total Amount'], errors='coerce').fillna(0).sum():,.2f}")
    if failures:
        print(f"Failed symbols ({len(failures)}): {', '.join(failures)}")


if __name__ == "__main__":
    build()
