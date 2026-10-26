"""Durable local activity and correction ledgers for portfolio holdings."""
import csv
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
TRANSACTIONS_FILE = ROOT / "data" / "transactions.csv"
CORRECTIONS_FILE = ROOT / "data" / "holding_corrections.csv"
DELETIONS_FILE = ROOT / "data" / "holding_deletions.csv"

TRANSACTION_FIELDS = [
    "Transaction ID", "Type", "Lot ID", "Advisor", "Broker", "Asset Type", "Name", "Symbol",
    "Exchange", "Sector", "Cap", "Currency", "FX Rate", "Quantity", "Price", "Fees",
    "Transaction Date", "Notes",
]
CORRECTION_FIELDS = ["Correction ID", "Lot ID", "Field", "Old Value", "New Value", "Reason", "Timestamp", "Source"]
DELETION_FIELDS = ["Deletion ID", "Lot ID", "Name", "Reason", "Timestamp", "Source"]


def _read(path, fields):
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _append(path, fields, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _read(path, fields)
    rows.append({field: str(row.get(field, "") or "") for field in fields})
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _number(value, label, positive=True):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number")
    if positive and result <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return result


def _number_text(value):
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.10f}".rstrip("0").rstrip(".")


def _date(value):
    try:
        return datetime.strptime(str(value), "%d-%m-%Y").strftime("%d-%m-%Y")
    except (TypeError, ValueError):
        raise ValueError("Transaction date must use DD-MM-YYYY")


def list_transactions():
    return _read(TRANSACTIONS_FILE, TRANSACTION_FIELDS)


def _rewrite(path, fields, rows):
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({field: str(row.get(field, "") or "") for field in fields} for row in rows)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def enrich_buy_transactions(symbol, details):
    """Persist quote metadata for manual buys so rebuilds keep the enrichment."""
    rows = list_transactions()
    changed = False
    for row in rows:
        if row.get("Type") != "BUY" or row.get("Symbol", "").upper() != symbol.upper():
            continue
        for field in ("Name", "Exchange", "Sector", "Cap", "Asset Type", "Currency"):
            if not row.get(field) and details.get(field):
                row[field] = details[field]
                changed = True
    if changed:
        _rewrite(TRANSACTIONS_FILE, TRANSACTION_FIELDS, rows)
    return changed


def list_corrections():
    return _read(CORRECTIONS_FILE, CORRECTION_FIELDS)


def list_deletions():
    return _read(DELETIONS_FILE, DELETION_FIELDS)


def add_transaction(payload, holdings):
    transaction_type = str(payload.get("Type", "")).upper()
    if transaction_type not in {"BUY", "SELL"}:
        raise ValueError("Type must be BUY or SELL")
    quantity = _number(payload.get("Quantity"), "Quantity")
    price = _number(payload.get("Price"), "Price")
    fees = _number(payload.get("Fees", 0) or 0, "Fees", positive=False)
    transaction_date = _date(payload.get("Transaction Date"))
    if transaction_type == "BUY" and not str(payload.get("Symbol", "")).strip():
        raise ValueError("Symbol is required for a Buy")
    lot_id = str(payload.get("Lot ID", "")).strip()
    if transaction_type == "SELL":
        if not lot_id:
            raise ValueError("Select a source lot for a sale")
        matches = holdings[holdings["Lot ID"].astype(str) == lot_id]
        if matches.empty:
            raise ValueError("Selected lot was not found")
        available = float(matches.iloc[0].get("Quantity") or 0)
        if quantity > available + 1e-9:
            raise ValueError(f"Sale quantity exceeds available lot quantity ({available:g})")
    else:
        # Buy forms may carry the currently selected lot ID for the selector, but
        # every buy must become a new independent lot.
        lot_id = f"MANUAL-{uuid.uuid4().hex[:12].upper()}"
    row = {field: payload.get(field, "") for field in TRANSACTION_FIELDS}
    row.update({"Transaction ID": f"TXN-{uuid.uuid4().hex[:12].upper()}", "Type": transaction_type,
                "Lot ID": lot_id, "Quantity": _number_text(quantity), "Price": _number_text(price), "Fees": _number_text(fees),
                "Transaction Date": transaction_date})
    _append(TRANSACTIONS_FILE, TRANSACTION_FIELDS, row)
    return row


def add_correction(payload, holdings):
    lot_id = str(payload.get("Lot ID", "")).strip()
    field = str(payload.get("Field", "")).strip()
    reason = str(payload.get("Reason", "")).strip()
    if not lot_id or not field or not reason:
        raise ValueError("Lot ID, field, and correction reason are required")
    if field not in holdings.columns:
        raise ValueError("That holding field cannot be corrected")
    matches = holdings[holdings["Lot ID"].astype(str) == lot_id]
    if matches.empty:
        raise ValueError("Selected lot was not found")
    old_value = str(matches.iloc[0].get(field, "") or "")
    row = {
        "Correction ID": f"COR-{uuid.uuid4().hex[:12].upper()}", "Lot ID": lot_id, "Field": field,
        "Old Value": old_value, "New Value": str(payload.get("New Value", "") or ""),
        "Reason": reason, "Timestamp": datetime.now().isoformat(timespec="seconds"), "Source": "dashboard",
    }
    _append(CORRECTIONS_FILE, CORRECTION_FIELDS, row)
    return row


def add_deletion(payload, holdings):
    lot_id = str(payload.get("Lot ID", "")).strip()
    reason = str(payload.get("Reason", "")).strip()
    if not lot_id or not reason:
        raise ValueError("Select a lot and enter a deletion reason")
    matches = holdings[holdings["Lot ID"].astype(str) == lot_id]
    if matches.empty:
        raise ValueError("Selected lot was not found")
    if lot_id in {row["Lot ID"] for row in list_deletions()}:
        raise ValueError("This holding is already deleted")
    row = {
        "Deletion ID": f"DEL-{uuid.uuid4().hex[:12].upper()}", "Lot ID": lot_id,
        "Name": str(matches.iloc[0].get("Name", "") or ""), "Reason": reason,
        "Timestamp": datetime.now().isoformat(timespec="seconds"), "Source": "dashboard",
    }
    _append(DELETIONS_FILE, DELETION_FIELDS, row)
    return row


def apply_activity(merged_df):
    """Apply corrections and manual buy/sell activity to the generated holdings view."""
    deleted_lots = {row["Lot ID"] for row in list_deletions() if row.get("Lot ID")}
    corrections = list_corrections()
    for correction in corrections:
        mask = merged_df["Lot ID"].astype(str) == correction["Lot ID"]
        if mask.any() and correction["Field"] in merged_df.columns:
            merged_df.loc[mask, correction["Field"]] = correction["New Value"]

    transactions = list_transactions()
    if not transactions:
        return merged_df
    for transaction in transactions:
        quantity = float(transaction.get("Quantity") or 0)
        if transaction.get("Type") == "SELL":
            mask = merged_df["Lot ID"].astype(str) == transaction.get("Lot ID", "")
            if not mask.any():
                continue
            current = float(merged_df.loc[mask, "Quantity"].iloc[0] or 0)
            merged_df.loc[mask, "Quantity"] = max(0, current - quantity)
            continue
        if transaction.get("Type") != "BUY":
            continue
        transaction_lot_id = transaction.get("Lot ID", "")
        if (merged_df["Lot ID"].astype(str) == transaction_lot_id).any():
            transaction_lot_id = f"{transaction_lot_id}-BUY-{transaction.get('Transaction ID', '')[-6:]}"
        if (merged_df["Lot ID"].astype(str) == transaction_lot_id).any():
            continue
        row = {column: "" for column in merged_df.columns}
        for field in ("Lot ID", "Advisor", "Broker", "Asset Type", "Name", "Symbol", "Exchange", "Sector", "Cap", "Currency", "FX Rate"):
            row[field] = transaction.get(field, "")
        row["Lot ID"] = transaction_lot_id
        fx_rate = float(transaction.get("FX Rate") or 1)
        buy_price = float(transaction.get("Price") or 0)
        row["Quantity"] = _number_text(quantity)
        row["Inv. Price"] = _number_text(buy_price * fx_rate)
        row["Cost FX Rate"] = _number_text(fx_rate)
        row["Inv. Date"] = transaction.get("Transaction Date", "")
        row["Inv. Amount"] = _number_text(quantity * buy_price * fx_rate)
        row["Latest Price/NAV"] = _number_text(buy_price)
        row["Latest Value"] = _number_text(quantity * buy_price * fx_rate)
        row["As Of Date"] = datetime.now().strftime("%d-%m-%Y")
        row["Notes"] = transaction.get("Notes", "")
        merged_df = pd.concat([merged_df, pd.DataFrame([row])], ignore_index=True)
    numeric = ["Quantity", "Inv. Amount", "Latest Value"]
    for column in numeric:
        merged_df[column] = pd.to_numeric(merged_df[column], errors="coerce").fillna(0)
    merged_df["Overall Gain"] = merged_df["Latest Value"] - merged_df["Inv. Amount"]
    merged_df["Overall Gain %"] = merged_df.apply(lambda row: row["Overall Gain"] / row["Inv. Amount"] * 100 if row["Inv. Amount"] else "", axis=1)
    if deleted_lots:
        merged_df = merged_df[~merged_df["Lot ID"].astype(str).isin(deleted_lots)].copy()
    merged_df = merged_df[merged_df["Quantity"] > 0].copy()
    return merged_df