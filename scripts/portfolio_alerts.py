"""Concentration and 52-week-drawdown alerts, persisted across syncs.

Alerts are deterministic (id = type|symbol-or-lot|band) so re-running this doesn't create
duplicates - the same underlying situation keeps the same id unless it worsens into a new
band, at which point a new alert is raised (mirrors "crossed 20% band" -> "crossed 30% band").
Dismissing ("investigate"/"ignore") an alert persists via data/alert_actions.csv and is only
cleared automatically if the alert re-triggers at a worse band later.
"""
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ACTIONS_FILE = ROOT / "data" / "alert_actions.csv"

CONCENTRATION_CAP_PCT = 10.0
DRAWDOWN_BANDS = [50, 40, 30, 20]  # checked highest-first; first match wins


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _load_actions():
    if not ACTIONS_FILE.exists():
        return pd.DataFrame(columns=["Alert ID", "Status", "Updated At"])
    return pd.read_csv(ACTIONS_FILE, dtype=str).fillna("")


def _save_actions(df):
    df.to_csv(ACTIONS_FILE, index=False)


def compute_alerts(rows, index_quotes=None):
    rows = list(rows)
    total_value = sum(_num(r.get("Latest Value")) for r in rows)
    actions = _load_actions().set_index("Alert ID")["Status"].to_dict() if not _load_actions().empty else {}
    alerts = []

    for quote in (index_quotes or []):
        level = quote.get("correction_level")
        if level:
            alert_id = f"INDEX_CORRECTION|{quote['name']}|{level}"
            alerts.append({
                "id": alert_id,
                "type": "INDEX_CORRECTION",
                "message": f"{quote['name']} {level.lower()}: -{quote['drawdown_from_high_pct']:.1f}% from 52-week high",
                "status": actions.get(alert_id, "open"),
            })

    for row in rows:
        name = row.get("Name", "(unnamed)")
        lot_id = row.get("Lot ID", name)
        latest_value = _num(row.get("Latest Value"))
        weight_pct = (latest_value / total_value * 100) if total_value else 0

        if weight_pct > CONCENTRATION_CAP_PCT:
            alert_id = f"CONCENTRATION|{lot_id}"
            alerts.append({
                "id": alert_id,
                "type": "CONCENTRATION",
                "message": f"{name} is {weight_pct:.1f}% of portfolio (cap {CONCENTRATION_CAP_PCT:.0f}%)",
                "status": actions.get(alert_id, "open"),
            })

        price = _num(row.get("Latest Price/NAV"))
        high_52wk = _num(row.get("52W High"))
        if price and high_52wk and price < high_52wk:
            drawdown_pct = (high_52wk - price) / high_52wk * 100
            band = next((b for b in DRAWDOWN_BANDS if drawdown_pct >= b), None)
            if band:
                alert_id = f"DRAWDOWN|{lot_id}|{band}"
                alerts.append({
                    "id": alert_id,
                    "type": "DRAWDOWN",
                    "message": f"{name} down {drawdown_pct:.1f}% from 52-week high (crossed {band}% band)",
                    "status": actions.get(alert_id, "open"),
                })

    active = [a for a in alerts if a["status"] != "ignored"]
    type_order = {"INDEX_CORRECTION": 0, "CONCENTRATION": 1, "DRAWDOWN": 2}
    return {
        "alerts": sorted(active, key=lambda a: (a["status"] != "open", type_order.get(a["type"], 9), a["id"])),
        "open_count": sum(1 for a in active if a["status"] == "open"),
        "total_count": len(active),
    }


def set_alert_status(alert_id, status):
    if status not in {"open", "investigated", "ignored"}:
        raise ValueError(f"Invalid status: {status}")
    df = _load_actions()
    df = df[df["Alert ID"] != alert_id]
    new_row = pd.DataFrame([{"Alert ID": alert_id, "Status": status, "Updated At": pd.Timestamp.now().isoformat()}])
    df = pd.concat([df, new_row], ignore_index=True) if not df.empty else new_row
    _save_actions(df)
