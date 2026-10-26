"""Load and summarize manually confirmed portfolio income and corporate actions."""
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DIVIDENDS_FILE = ROOT / "data" / "dividends.csv"
CORPORATE_ACTIONS_FILE = ROOT / "data" / "corporate_actions.csv"


def _read_csv(path, columns):
    if not path.exists():
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path, dtype=str).fillna("")


def load_dividends():
    columns = ["Event ID", "Symbol", "Name", "Ex-Date", "Record Date", "Payment Date",
               "Amount Per Share", "Quantity Eligible", "Total Amount", "Status", "Source",
               "Verified", "Notes"]
    return _read_csv(DIVIDENDS_FILE, columns)


def load_corporate_actions():
    columns = ["Event ID", "Symbol", "Name", "Action Type", "Announcement Date", "Ex-Date",
               "Record Date", "Ratio Old", "Ratio New", "Quantity Before", "Quantity After",
               "Status", "Source", "Verified", "Notes"]
    return _read_csv(CORPORATE_ACTIONS_FILE, columns)


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def summarize_events(invested_total=0, symbols=None):
    dividends = load_dividends()
    actions = load_corporate_actions()
    if symbols is not None:
        symbols = {str(symbol).strip() for symbol in symbols if str(symbol).strip()}
        dividends = dividends[dividends["Symbol"].isin(symbols)]
        actions = actions[actions["Symbol"].isin(symbols)]
    dividend_records = []
    for row in dividends.to_dict(orient="records"):
        amount = _number(row.get("Total Amount"))
        if not amount:
            amount = _number(row.get("Amount Per Share")) * _number(row.get("Quantity Eligible"))
        dividend_records.append({**row, "Total Amount": round(amount, 2)})
    action_records = actions.to_dict(orient="records")
    tracked_total = round(sum(row["Total Amount"] for row in dividend_records), 2)
    return {
        "dividends": {
            "events": dividend_records,
            "event_count": len(dividend_records),
            "tracked_total": tracked_total,
            "verified_count": sum(str(row.get("Verified", "")).lower() in {"yes", "true", "1"} for row in dividend_records),
            "return_pct": round(tracked_total / invested_total * 100, 3) if invested_total else 0,
        },
        "corporate_actions": {
            "events": action_records,
            "event_count": len(action_records),
            "verified_count": sum(str(row.get("Verified", "")).lower() in {"yes", "true", "1"} for row in action_records),
        },
        "notes": [
            "Events are manual-first and do not change holdings until explicitly reconciled.",
            "Historical income and actions are incomplete until broker statements are imported.",
        ],
    }
