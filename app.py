"""Executive Portfolio Command Center.

Reads Portfolio_Holdings.xlsx or data/holdings.csv, serves the frontend single-page dashboard,
and provides live price refreshes, import/export, and portfolio metrics.

Run: python3 app.py   ->  http://127.0.0.1:5151
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
ROOT = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass
DATA_FILE = ROOT / "data" / "holdings.csv"
EXPENSES_FILE = ROOT / "data" / "expenses.csv"
EXCEL_DATA_FILE = ROOT / "Portfolio_Holdings.xlsx"
SCRIPTS_DIR = ROOT / "scripts"
BUILD_SCRIPT = SCRIPTS_DIR / "build_portfolio_csv.py"

sys.path.insert(0, str(SCRIPTS_DIR))
from live_prices import fetch_equity_prices, fetch_mf_navs, fetch_usd_inr  # noqa: E402
from portfolio_metrics import calculate_metrics, _day_gain  # noqa: E402
from portfolio_events import summarize_events  # noqa: E402
from market_data import fetch_market_context  # noqa: E402
from ai_portfolio_agent import build_snapshot, request_ai_review, FREE_MODELS, DEFAULT_MODEL  # noqa: E402
from ai_committee import run_investment_committee  # noqa: E402
from portfolio_alerts import compute_alerts, set_alert_status  # noqa: E402
from distribution_data import fetch_distribution_income  # noqa: E402
from holding_activity import add_correction, add_deletion, add_transaction, enrich_buy_transactions, list_corrections, list_deletions, list_transactions  # noqa: E402
from export_portfolio_workbook import export_workbook  # noqa: E402

EQUITY_TYPES = {"Stocks", "US Stocks", "ETF", "REIT", "InvIT"}

app = Flask(__name__, static_folder=str(ROOT), static_url_path="")


def load_holdings():
    df = pd.read_csv(DATA_FILE, dtype=str).fillna("")
    df = df.where(pd.notnull(df), "")
    return df


def apply_request_filters(df):
    for key, column in {"advisor": "Advisor", "account": "Broker", "asset_type": "Asset Type", "custom_tag": "Tag1"}.items():
        values = [value for value in request.args.getlist(key) if value]
        if values:
            df = df[df[column].isin(values)]
    return df


@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.route("/style.css")
def serve_style_css():
    return send_from_directory(ROOT, "style.css", mimetype="text/css")


@app.route("/app.js")
def serve_app_js():
    return send_from_directory(ROOT, "app.js", mimetype="application/javascript")


@app.route("/seed_data.js")
def serve_seed_data_js():
    return send_from_directory(ROOT, "seed_data.js", mimetype="application/javascript")


@app.route("/initial_holdings.json")
def serve_initial_holdings():
    return send_from_directory(ROOT, "initial_holdings.json", mimetype="application/json")


@app.route("/api/holdings")
def api_holdings():
    if not DATA_FILE.exists():
        return jsonify({"error": f"{DATA_FILE.name} not found - run the re-import first"}), 404
    df = load_holdings()
    return jsonify(df.to_dict(orient="records"))


@app.route("/api/holding-activity", methods=["GET", "POST"])
def api_holding_activity():
    if request.method == "GET":
        return jsonify({"transactions": list_transactions(), "corrections": list_corrections(), "deletions": list_deletions()})
    try:
        transaction = add_transaction(request.get_json(silent=True) or {}, load_holdings())
        result = subprocess.run([sys.executable, str(BUILD_SCRIPT)], capture_output=True, text=True)
        if result.returncode:
            return jsonify({"ok": False, "error": result.stderr or result.stdout}), 500
        return jsonify({"ok": True, "transaction": transaction})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/quote-preview")
def api_quote_preview():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"ok": False, "error": "Symbol is required"}), 400
    quotes = fetch_equity_prices([symbol])
    quote = quotes.get(symbol)
    if not quote:
        return jsonify({"ok": False, "error": f"No quote found for {symbol}"}), 404
    return jsonify({
        "ok": True,
        "symbol": symbol,
        "name": quote.get("name", ""),
        "exchange": quote.get("exchange", ""),
        "sector": quote.get("sector", ""),
        "currency": quote.get("currency", ""),
        "asset_type": "ETF" if quote.get("quote_type") == "ETF" or " ETF" in str(quote.get("name", "")).upper() else "Stocks",
        "price": quote.get("price"),
    })


@app.route("/api/holding-corrections", methods=["POST"])
def api_holding_corrections():
    try:
        payload = request.get_json(silent=True) or {}
        holdings = load_holdings()
        changes = payload.get("Changes") or {}
        corrections = []
        if changes:
            for field, value in changes.items():
                corrections.append(add_correction({
                    "Lot ID": payload.get("Lot ID"), "Field": field,
                    "New Value": value, "Reason": payload.get("Reason"),
                }, holdings))
        else:
            corrections.append(add_correction(payload, holdings))
        result = subprocess.run([sys.executable, str(BUILD_SCRIPT)], capture_output=True, text=True)
        if result.returncode:
            return jsonify({"ok": False, "error": result.stderr or result.stdout}), 500
        return jsonify({"ok": True, "corrections": corrections})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/holding-deletions", methods=["POST"])
def api_holding_deletions():
    try:
        deletion = add_deletion(request.get_json(silent=True) or {}, load_holdings())
        result = subprocess.run([sys.executable, str(BUILD_SCRIPT)], capture_output=True, text=True)
        if result.returncode:
            return jsonify({"ok": False, "error": result.stderr or result.stdout}), 500
        return jsonify({"ok": True, "deletion": deletion})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


def load_expenses():
    if not EXPENSES_FILE.exists():
        return []
    try:
        df = pd.read_csv(EXPENSES_FILE, dtype=str).fillna("")
        records = df.to_dict(orient="records")
        for r in records:
            try:
                r["amount"] = float(r.get("amount", 0) or 0)
            except (ValueError, TypeError):
                r["amount"] = 0.0
        return sorted(records, key=lambda x: str(x.get("date", "")), reverse=True)
    except Exception:
        return []


def save_expenses(records):
    EXPENSES_FILE.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(records)
    if df.empty:
        df = pd.DataFrame(columns=["id", "date", "description", "category", "amount", "created_at"])
    df.to_csv(EXPENSES_FILE, index=False)


@app.route("/api/expenses", methods=["GET"])
def api_get_expenses():
    records = load_expenses()
    total = sum(r["amount"] for r in records)
    return jsonify({"ok": True, "expenses": records, "total": round(total, 2)})


@app.route("/api/expenses", methods=["POST"])
def api_add_expense():
    data = request.get_json(silent=True) or {}
    description = str(data.get("description", "")).strip()
    date_val = str(data.get("date", "")).strip() or datetime.now().strftime("%Y-%m-%d")
    category = str(data.get("category", "General")).strip() or "General"
    try:
        amount = float(data.get("amount", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid amount"}), 400

    if not description:
        return jsonify({"ok": False, "error": "Description is required"}), 400
    if amount <= 0:
        return jsonify({"ok": False, "error": "Amount must be greater than 0"}), 400

    records = load_expenses()
    new_id = f"exp_{int(datetime.now().timestamp() * 1000)}"
    new_record = {
        "id": new_id,
        "date": date_val,
        "description": description,
        "category": category,
        "amount": round(amount, 2),
        "created_at": datetime.now().isoformat(),
    }
    records.insert(0, new_record)
    save_expenses(records)
    total = sum(r["amount"] for r in records)
    return jsonify({"ok": True, "expense": new_record, "total": round(total, 2)})


@app.route("/api/expenses/<expense_id>", methods=["DELETE"])
def api_delete_expense(expense_id):
    records = load_expenses()
    initial_len = len(records)
    records = [r for r in records if str(r.get("id")) != str(expense_id)]
    if len(records) == initial_len:
        return jsonify({"ok": False, "error": "Expense not found"}), 404
    save_expenses(records)
    total = sum(r["amount"] for r in records)
    return jsonify({"ok": True, "deleted_id": expense_id, "total": round(total, 2)})


@app.route("/api/expenses/<expense_id>", methods=["PUT"])
def api_update_expense(expense_id):
    data = request.get_json(silent=True) or {}
    description = str(data.get("description", "")).strip()
    date_val = str(data.get("date", "")).strip() or datetime.now().strftime("%Y-%m-%d")
    category = str(data.get("category", "General")).strip() or "General"
    try:
        amount = float(data.get("amount", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid amount"}), 400

    if not description:
        return jsonify({"ok": False, "error": "Description is required"}), 400
    if amount <= 0:
        return jsonify({"ok": False, "error": "Amount must be greater than 0"}), 400

    records = load_expenses()
    found = False
    updated_record = None
    for r in records:
        if str(r.get("id")) == str(expense_id):
            r["date"] = date_val
            r["description"] = description
            r["category"] = category
            r["amount"] = round(amount, 2)
            updated_record = r
            found = True
            break

    if not found:
        return jsonify({"ok": False, "error": "Expense not found"}), 404

    save_expenses(records)
    total = sum(r["amount"] for r in records)
    return jsonify({"ok": True, "expense": updated_record, "total": round(total, 2)})


@app.route("/api/portfolio-metrics")
def api_portfolio_metrics():
    if not DATA_FILE.exists():
        return jsonify({"error": f"{DATA_FILE.name} not found - run the re-import first"}), 404
    df = apply_request_filters(load_holdings())
    rows = df.to_dict(orient="records")
    invested_total = round(pd.to_numeric(df["Inv. Amount"], errors="coerce").fillna(0).sum(), 2)
    symbols = df["Symbol"].tolist() if "Symbol" in df else []
    events = summarize_events(invested_total, symbols=symbols)["dividends"]
    return jsonify(calculate_metrics(rows, events))


@app.route("/api/portfolio-events")
def api_portfolio_events():
    df = apply_request_filters(load_holdings())
    invested_total = round(pd.to_numeric(df["Inv. Amount"], errors="coerce").fillna(0).sum(), 2)
    return jsonify(summarize_events(invested_total, symbols=df["Symbol"].tolist()))


@app.route("/api/distribution-income")
def api_distribution_income():
    return jsonify(fetch_distribution_income(apply_request_filters(load_holdings()).to_dict(orient="records")))


@app.route("/api/market-context")
def api_market_context():
    force_refresh = request.args.get("refresh") in ("1", "true", "True")
    df = apply_request_filters(load_holdings())
    attribution = []
    for asset_type, group in df.groupby("Asset Type"):
        invested = round(float(pd.to_numeric(group["Inv. Amount"], errors="coerce").fillna(0).sum()), 2)
        latest_value = round(float(pd.to_numeric(group["Latest Value"], errors="coerce").fillna(0).sum()), 2)
        day_gain = round(float(sum(_day_gain(row) for row in group.to_dict(orient="records"))), 2)
        overall_gain = round(latest_value - invested, 2)
        attribution.append({
            "asset_type": asset_type,
            "day_gain": day_gain,
            "latest_value": latest_value,
            "invested": invested,
            "return_pct": round(day_gain / invested * 100, 4) if invested else 0,
            "overall_gain": overall_gain,
            "overall_return_pct": round(overall_gain / invested * 100, 4) if invested else 0,
        })
    return jsonify({"market": fetch_market_context(force_refresh=force_refresh), "attribution": sorted(attribution, key=lambda item: item["invested"], reverse=True)})


@app.route("/api/ai-review", methods=["POST"])
def api_ai_review():
    requested_model = (request.get_json(silent=True) or {}).get("model")
    df = apply_request_filters(load_holdings())
    metrics = calculate_metrics(df.to_dict(orient="records"))
    invested_total = round(float(pd.to_numeric(df["Inv. Amount"], errors="coerce").fillna(0).sum()), 2)
    events = summarize_events(invested_total, symbols=df["Symbol"].tolist())
    market = fetch_market_context()
    snapshot = build_snapshot(metrics, events, market)
    result = request_ai_review(snapshot, model=requested_model)
    return jsonify(result), (200 if result.get("ok") else 502)


@app.route("/api/ai-committee", methods=["POST"])
def api_ai_committee():
    requested_model = (request.get_json(silent=True) or {}).get("model")
    df = apply_request_filters(load_holdings())
    metrics = calculate_metrics(df.to_dict(orient="records"))
    invested_total = round(float(pd.to_numeric(df["Inv. Amount"], errors="coerce").fillna(0).sum()), 2)
    events = summarize_events(invested_total, symbols=df["Symbol"].tolist())
    market = fetch_market_context()
    snapshot = build_snapshot(metrics, events, market)
    result = run_investment_committee(snapshot, requested_model=requested_model)
    return jsonify(result), (200 if result.get("ok") else 502)


@app.route("/api/ai-models")
def api_ai_models():
    return jsonify({"models": FREE_MODELS, "default": DEFAULT_MODEL})


@app.route("/api/portfolio-alerts")
def api_portfolio_alerts():
    df = apply_request_filters(load_holdings())
    index_quotes = fetch_market_context().get("quotes", [])
    return jsonify(compute_alerts(df.to_dict(orient="records"), index_quotes=index_quotes))


@app.route("/api/portfolio-alerts/<path:alert_id>", methods=["POST"])
def api_portfolio_alert_action(alert_id):
    status = (request.get_json(silent=True) or {}).get("status")
    try:
        set_alert_status(alert_id, status)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@app.route("/api/reimport", methods=["POST"])
def api_reimport():
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        capture_output=True, text=True,
    )
    return jsonify({
        "ok": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    })


# ── Upload Holdings & Minimal Template Engine ────────────────────────────────
COLUMN_ALIASES = {
    "Symbol": ["symbol", "ticker", "code", "scrip", "stock", "identifier"],
    "Name": ["name", "security", "company", "description", "instrument", "scheme name", "fund name"],
    "Quantity": ["quantity", "qty", "units", "shares", "volume", "held"],
    "Inv. Price": ["inv. price", "inv price", "buy price", "cost", "avg price", "average price", "price", "purchase price", "cost price"],
    "Asset Type": ["asset type", "asset", "type", "category", "class", "asset class"],
    "Inv. Date": ["inv. date", "inv date", "buy date", "date", "purchase date", "transaction date"],
    "Broker": ["broker", "demat", "account", "platform"],
    "Exchange": ["exchange", "market", "exch"],
    "Sector": ["sector", "industry"],
    "Currency": ["currency", "curr", "ccy"],
}

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
STANDARD_COLUMNS = [
    "Lot ID", "Owner", "Advisor", "Broker", "Asset Type", "Tag1", "Name", "Symbol",
    "AMFI Scheme Code", "Exchange", "Sector", "Cap", "Currency", "FX Rate", "Cost FX Rate",
    "Latest Price/NAV", "Change", "Quantity", "Inv. Price", "Inv. Date", "Inv. Amount",
    "Latest Value", "Day's Gain", "Day's Gain %", "Overall Gain", "Overall Gain %",
    "Realised Gains", "Inv. Rate %", "Maturity Date", "Maturity Value", "As Of Date", "Notes", "52W High"
]

TEMPLATE_HEADERS = ["Symbol", "Name", "Asset Type", "Quantity", "Buy Price", "Buy Date", "Broker"]
TEMPLATE_SAMPLE_ROWS = [
    ["RELIANCE", "Reliance Industries Limited", "Stocks", "10", "1285.50", "2024-01-15", "Zerodha"],
    ["TCS", "Tata Consultancy Services", "Stocks", "5", "3920.00", "2023-06-10", "Zerodha"],
    ["AAPL", "Apple Inc.", "US Stocks", "5", "232.00", "2023-03-15", "Interactive Brokers"],
    ["122639", "Parag Parikh Flexi Cap Fund Direct Growth", "Mutual Fund", "1500", "84.50", "2022-08-01", "Groww"],
    ["GOLDBEES", "Nippon India ETF Gold BeES", "ETF", "800", "72.40", "2022-10-10", "Zerodha"],
    ["718GS2033", "Government of India 7.18% GS 2033", "Bonds", "1000", "99.80", "2023-08-15", "Zerodha"],
]


def sync_holdings_to_offline_files(df):
    """Synchronize DataFrame records to initial_holdings.json and seed_data.js."""
    records = df.to_dict(orient="records")
    clean_records = []
    for r in records:
        clean_r = {}
        for k, v in r.items():
            if pd.isna(v) or (isinstance(v, float) and (v != v)):
                clean_r[k] = ""
            else:
                clean_r[k] = str(v)
        clean_records.append(clean_r)

    import json
    try:
        with open(ROOT / "initial_holdings.json", "w", encoding="utf-8") as f:
            json.dump(clean_records, f, indent=2, default=str)
    except Exception as e:
        print("[WARN] Failed to write initial_holdings.json:", e)

    seed_file = ROOT / "seed_data.js"
    if seed_file.exists():
        try:
            text = seed_file.read_text(encoding="utf-8")
            prefix = "window.MERIDIAN_SEED_DATA = "
            if text.startswith(prefix):
                json_text = text[len(prefix):].rstrip().rstrip(";")
                seed_obj = json.loads(json_text)
                seed_obj["holdings"] = clean_records
                seed_file.write_text(prefix + json.dumps(seed_obj, indent=2, default=str) + ";\n", encoding="utf-8")
        except Exception as e:
            print("[WARN] Failed to update seed_data.js:", e)


@app.route("/api/upload-holdings", methods=["POST"])
def api_upload_holdings():
    """Accept a minimal CSV or Excel file, validate, auto-calculate all metrics, and update holdings."""
    import io, shutil
    from datetime import datetime as dt

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file part in request"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "No file selected"}), 400

    suffix = Path(f.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return jsonify({"ok": False, "error": f"Unsupported file type '{suffix}'. Use .csv or .xlsx"}), 400

    try:
        raw = f.read()
        if suffix == ".csv":
            df_up = pd.read_csv(io.BytesIO(raw), dtype=str).fillna("")
        else:
            df_up = pd.read_excel(io.BytesIO(raw), dtype=str).fillna("")
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not parse file: {exc}"}), 422

    # Map column aliases to canonical names
    orig_cols = [str(c).strip() for c in df_up.columns]
    normalized_cols = []
    for c in orig_cols:
        matched = False
        c_lower = c.lower()
        for canon, aliases in COLUMN_ALIASES.items():
            if c_lower == canon.lower() or c_lower in aliases:
                normalized_cols.append(canon)
                matched = True
                break
        if not matched:
            normalized_cols.append(c)
    df_up.columns = normalized_cols
    df_up = df_up.map(lambda x: x.strip() if isinstance(x, str) else x)

    # Auto-resolve Name and Symbol if only one is present
    has_symbol = "Symbol" in df_up.columns
    has_name = "Name" in df_up.columns
    if not has_symbol and has_name:
        df_up["Symbol"] = df_up["Name"]
    elif not has_name and has_symbol:
        df_up["Name"] = df_up["Symbol"]
    elif not has_symbol and not has_name:
        return jsonify({
            "ok": False,
            "error": "Missing Symbol or Name column.",
            "hint": "Provide at least: Symbol, Quantity, Buy Price. Download our sample template from the Import dialog.",
        }), 422

    if "Quantity" not in df_up.columns:
        return jsonify({
            "ok": False,
            "error": "Missing Quantity column.",
            "hint": "Specify the number of shares/units held in the 'Quantity' column.",
        }), 422

    # Filter out empty rows
    df_up = df_up[(df_up["Symbol"].str.strip().astype(bool)) | (df_up["Name"].str.strip().astype(bool))].copy()
    if len(df_up) == 0:
        return jsonify({"ok": False, "error": "File contains no data rows."}), 422

    # Load sector & cap maps for auto-enrichment
    sector_map = {}
    cap_map = {}
    sector_csv = ROOT / "data" / "sector_map.csv"
    cap_csv = ROOT / "data" / "cap_map.csv"
    if sector_csv.exists():
        try:
            sdf = pd.read_csv(sector_csv, dtype=str).fillna("")
            if "Symbol" in sdf.columns and "Sector" in sdf.columns:
                sector_map = dict(zip(sdf["Symbol"].str.upper(), sdf["Sector"]))
        except Exception:
            pass
    if cap_csv.exists():
        try:
            cdf = pd.read_csv(cap_csv, dtype=str).fillna("")
            if "Symbol" in cdf.columns and "Cap" in cdf.columns:
                cap_map = dict(zip(cdf["Symbol"].str.upper(), cdf["Cap"]))
        except Exception:
            pass

    today_str = dt.now().strftime("%d-%m-%Y")
    known_us_tech = {"AAPL", "MSFT", "GOOGL", "GOOG", "NVDA", "AMZN", "META", "TSLA", "NFLX", "AMD", "INTC"}

    # Process and auto-calculate each row
    for idx, row in df_up.iterrows():
        sym = str(row.get("Symbol", "") or "").strip().upper()
        name = str(row.get("Name", "") or "").strip()
        if not name:
            df_up.at[idx, "Name"] = sym
        if not sym:
            sym = name.upper()
            df_up.at[idx, "Symbol"] = sym

        # Auto-infer Asset Type if missing or generic
        asset_type = str(row.get("Asset Type", "") or "").strip()
        if not asset_type or asset_type.lower() in {"", "equity", "default"}:
            if sym.isdigit() and len(sym) >= 5:
                asset_type = "Mutual Fund"
                df_up.at[idx, "AMFI Scheme Code"] = sym
            elif sym in known_us_tech or str(row.get("Currency", "")).upper() == "USD":
                asset_type = "US Stocks"
            elif "BEES" in sym or "ETF" in sym:
                asset_type = "ETF"
            elif "BOND" in sym or "GS" in sym or "NCD" in sym:
                asset_type = "Bonds"
            elif sym in {"EMBASSY", "BIRET", "MINDSPACE", "PGINVIT"}:
                asset_type = "REIT"
            else:
                asset_type = "Stocks"
        df_up.at[idx, "Asset Type"] = asset_type

        # Auto-infer Exchange and Currency
        if "Exchange" not in df_up.columns or not str(row.get("Exchange", "")).strip():
            if asset_type == "US Stocks":
                df_up.at[idx, "Exchange"] = "NASDAQ"
            elif asset_type in {"Stocks", "ETF", "REIT", "InvIT", "Bonds"}:
                df_up.at[idx, "Exchange"] = "NSE"
            else:
                df_up.at[idx, "Exchange"] = ""

        if "Currency" not in df_up.columns or not str(row.get("Currency", "")).strip():
            df_up.at[idx, "Currency"] = "USD" if asset_type == "US Stocks" else "INR"

        # Auto-generate Lot ID
        lot_id = str(row.get("Lot ID", "") or "").strip()
        if not lot_id:
            df_up.at[idx, "Lot ID"] = f"LOT-{idx + 1:03d}"

        # Parse Quantity and Price
        try:
            qty = float(str(row.get("Quantity", "0") or "0").replace(",", ""))
        except Exception:
            qty = 0.0
        try:
            iprice = float(str(row.get("Inv. Price", "0") or "0").replace(",", ""))
        except Exception:
            iprice = 0.0

        # Auto-calculate Inv. Amount
        inv_amount = round(qty * iprice, 2)
        df_up.at[idx, "Inv. Amount"] = str(inv_amount)

        # Latest Price / NAV defaults to Inv. Price if not explicitly supplied
        latest_p_str = str(row.get("Latest Price/NAV", "") or "").strip()
        if not latest_p_str or latest_p_str == "0":
            latest_price = iprice
            df_up.at[idx, "Latest Price/NAV"] = str(iprice)
        else:
            try:
                latest_price = float(latest_p_str.replace(",", ""))
            except Exception:
                latest_price = iprice

        latest_val = round(qty * latest_price, 2)
        df_up.at[idx, "Latest Value"] = str(latest_val)

        # Gains calculations
        overall_gain = round(latest_val - inv_amount, 2)
        gain_pct = round((overall_gain / inv_amount) * 100, 2) if inv_amount > 0 else 0.0
        df_up.at[idx, "Overall Gain"] = str(overall_gain)
        df_up.at[idx, "Overall Gain %"] = str(gain_pct)

        # Sector & Market Cap auto-enrichment
        if "Sector" not in df_up.columns or not str(row.get("Sector", "")).strip():
            df_up.at[idx, "Sector"] = sector_map.get(sym, "General")
        if "Cap" not in df_up.columns or not str(row.get("Cap", "")).strip():
            df_up.at[idx, "Cap"] = cap_map.get(sym, "Large Cap" if asset_type == "Stocks" else "")

        # Default tagging metadata
        if "Owner" not in df_up.columns or not str(row.get("Owner", "")).strip():
            df_up.at[idx, "Owner"] = "Primary Account"
        if "Advisor" not in df_up.columns or not str(row.get("Advisor", "")).strip():
            df_up.at[idx, "Advisor"] = "Self-Directed"
        if "Broker" not in df_up.columns or not str(row.get("Broker", "")).strip():
            df_up.at[idx, "Broker"] = "Zerodha"
        if "Tag1" not in df_up.columns or not str(row.get("Tag1", "")).strip():
            df_up.at[idx, "Tag1"] = "Core"
        if "Inv. Date" not in df_up.columns or not str(row.get("Inv. Date", "")).strip():
            df_up.at[idx, "Inv. Date"] = today_str
        if "As Of Date" not in df_up.columns or not str(row.get("As Of Date", "")).strip():
            df_up.at[idx, "As Of Date"] = today_str

    # Ensure all standard columns exist in the DataFrame
    for col in STANDARD_COLUMNS:
        if col not in df_up.columns:
            df_up[col] = ""

    all_cols = STANDARD_COLUMNS + [c for c in df_up.columns if c not in STANDARD_COLUMNS]
    df_up = df_up[all_cols]

    # ── Backup existing holdings ─────────────────────────────────────────────
    DATA_DIR = DATA_FILE.parent
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    warnings = []
    if DATA_FILE.exists():
        backup_path = DATA_DIR / f"holdings_backup_{dt.now().strftime('%Y%m%d_%H%M%S')}.csv"
        shutil.copy2(DATA_FILE, backup_path)
        warnings.append(f"Previous holdings backed up to {backup_path.name}")

    # ── Write new holdings & sync offline files ──────────────────────────────
    df_up.to_csv(DATA_FILE, index=False)
    sync_holdings_to_offline_files(df_up)

    return jsonify({
        "ok": True,
        "rows": len(df_up),
        "columns": df_up.columns.tolist(),
        "warnings": warnings,
        "message": f"Successfully imported {len(df_up)} holdings! All amounts, gains, and metadata have been calculated.",
    })


@app.route("/api/download-template", methods=["GET"])
def api_download_template():
    """Serve a clean, minimal 7-column holdings CSV template."""
    import io as _io
    import csv as _csv
    from flask import Response

    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerows(TEMPLATE_SAMPLE_ROWS)
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=holdings_template.csv"},
    )




@app.route("/api/export-workbook", methods=["POST"])
def api_export_workbook():
    try:
        output = export_workbook()
        return jsonify({"ok": True, "file": output.name})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/export-dynamic-excel", methods=["GET", "POST"])
def api_export_dynamic_excel():
    try:
        out_file = ROOT / "Portfolio_Holdings.xlsx"
        if (ROOT / "scripts" / "export_portfolio_workbook.py").exists():
            import subprocess
            subprocess.run([sys.executable, str(ROOT / "scripts" / "export_portfolio_workbook.py")], check=True)
        if request.method == "GET" and request.args.get("download") == "1":
            return send_file(out_file, as_attachment=True, download_name="Portfolio_Holdings.xlsx")
        return jsonify({"ok": True, "file": out_file.name, "path": str(out_file)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/sync-prices", methods=["POST"])
def api_sync_prices():
    if not DATA_FILE.exists():
        return jsonify({"ok": False, "error": f"{DATA_FILE.name} not found"}), 404

    df = pd.read_csv(DATA_FILE, dtype=str).fillna("")
    num_cols = ["FX Rate", "Latest Price/NAV", "Change", "Quantity", "Inv. Amount", "Latest Value",
                "Day's Gain", "Day's Gain %", "Overall Gain", "Overall Gain %", "52W High"]
    for col in num_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df[num_cols] = df[num_cols].astype("float64")

    equity_mask = df["Asset Type"].isin(EQUITY_TYPES) & df["Symbol"].notna() & (df["Symbol"] != "")
    mf_mask = (df["Asset Type"] == "Mutual Fund") & df["AMFI Scheme Code"].notna() & (df["AMFI Scheme Code"] != "")

    symbols = df.loc[equity_mask, "Symbol"].tolist()
    amfi_codes = df.loc[mf_mask, "AMFI Scheme Code"].astype(int).astype(str).tolist()

    fx_rate = fetch_usd_inr() or 95.7925
    prices = fetch_equity_prices(symbols)
    navs = fetch_mf_navs(amfi_codes)

    today = datetime.now().strftime("%d-%m-%Y")
    synced, failed = [], []

    def apply_new_price(idx, new_price, prev_price, high_52wk=None):
        """prev_price = official previous close/NAV (true 1-day baseline), NOT
        whatever stale value happened to be sitting in Latest Price before this
        sync - using our own old value here previously caused "Day's Gain" to
        show the change since last import (sometimes weeks) instead of 1 day.
        """
        currency = str(df.at[idx, "Currency"] or "INR")
        if currency == "USD":
            df.at[idx, "FX Rate"] = fx_rate
            new_price *= fx_rate
            prev_price *= fx_rate
            if not df.at[idx, "Inv. Amount"]:
                inv_price = float(df.at[idx, "Inv. Price"]) if pd.notna(df.at[idx, "Inv. Price"]) else 0.0
                qty = float(df.at[idx, "Quantity"]) if pd.notna(df.at[idx, "Quantity"]) else 0.0
                df.at[idx, "Inv. Amount"] = round(inv_price * qty * fx_rate, 2)
        quantity = float(df.at[idx, "Quantity"]) if pd.notna(df.at[idx, "Quantity"]) else 0.0
        inv_amount = float(df.at[idx, "Inv. Amount"]) if pd.notna(df.at[idx, "Inv. Amount"]) else 0.0

        new_latest_value = round(quantity * new_price, 2)
        prev_value = round(quantity * prev_price, 2)
        days_gain = round(new_latest_value - prev_value, 2)
        overall_gain = round(new_latest_value - inv_amount, 2)

        df.at[idx, "Latest Price/NAV"] = round(new_price, 4)
        df.at[idx, "Change"] = round(new_price - prev_price, 4)
        df.at[idx, "Latest Value"] = new_latest_value
        df.at[idx, "Day's Gain"] = days_gain
        df.at[idx, "Day's Gain %"] = round(days_gain / prev_value * 100, 2) if prev_value else ""
        df.at[idx, "Overall Gain"] = overall_gain
        df.at[idx, "Overall Gain %"] = round(overall_gain / inv_amount * 100, 2) if inv_amount else ""
        df.at[idx, "As Of Date"] = today
        if high_52wk:
            df.at[idx, "52W High"] = high_52wk

    def apply_quote_metadata(idx, symbol, quote):
        asset_type = str(df.at[idx, "Asset Type"] or "")
        quote_type = str(quote.get("quote_type") or "").upper()
        metadata = {
            "Name": quote.get("name"),
            "Exchange": quote.get("exchange"),
            "Sector": quote.get("sector"),
            "Currency": quote.get("currency"),
        }
        quote_name = str(quote.get("name") or "").upper()
        if not asset_type or (asset_type == "Stocks" and (quote_type == "ETF" or " ETF" in quote_name)):
            metadata["Asset Type"] = "ETF" if quote_type == "ETF" or " ETF" in quote_name else "Stocks"
        for field, value in metadata.items():
            if value and (not df.at[idx, field] or field in {"Name", "Exchange", "Sector", "Currency"}):
                df.at[idx, field] = value
        enrich_buy_transactions(symbol, metadata)

    for idx in df[equity_mask].index:
        symbol = df.at[idx, "Symbol"]
        if symbol in prices:
            apply_quote_metadata(idx, symbol, prices[symbol])
            apply_new_price(idx, prices[symbol]["price"], prices[symbol]["prev_close"], prices[symbol].get("high_52wk"))
            synced.append(df.at[idx, "Name"])
        else:
            failed.append(f"{df.at[idx, 'Name']} ({symbol})")

    for idx in df[mf_mask].index:
        code = str(int(df.at[idx, "AMFI Scheme Code"]))
        if code in navs:
            apply_new_price(idx, navs[code]["nav"], navs[code]["prev_nav"])
            synced.append(df.at[idx, "Name"])
        else:
            failed.append(f"{df.at[idx, 'Name']} ({code})")

    df.to_csv(DATA_FILE, index=False)
    df.to_excel(EXCEL_DATA_FILE, index=False, sheet_name="Holdings")

    # Clean records and save to initial_holdings.json & seed_data.js
    records = df.to_dict(orient="records")
    clean_records = []
    for r in records:
        clean_r = {}
        for k, v in r.items():
            if pd.isna(v):
                clean_r[k] = ""
            elif isinstance(v, float) and (v != v):
                clean_r[k] = ""
            else:
                clean_r[k] = v
        clean_records.append(clean_r)

    import json
    try:
        with open(ROOT / "initial_holdings.json", "w", encoding="utf-8") as f:
            json.dump(clean_records, f, indent=2, default=str)
    except Exception as e:
        print("[WARN] Failed to write initial_holdings.json:", e)

    seed_file = ROOT / "seed_data.js"
    if seed_file.exists():
        try:
            text = seed_file.read_text(encoding="utf-8")
            prefix = "window.MERIDIAN_SEED_DATA = "
            if text.startswith(prefix):
                json_text = text[len(prefix):].rstrip().rstrip(";")
                seed_obj = json.loads(json_text)
                seed_obj["holdings"] = clean_records
                seed_file.write_text(prefix + json.dumps(seed_obj, indent=2, default=str) + ";\n", encoding="utf-8")
        except Exception as e:
            print("[WARN] Failed to update seed_data.js:", e)

    us_symbols_set = {"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "NFLX"}
    nse_count = len([s for s in symbols if not str(s).endswith(".US") and s not in us_symbols_set])
    us_count = len([s for s in symbols if str(s).endswith(".US") or s in us_symbols_set])
    mf_count = len(amfi_codes)
    bonds_count = len(df[df["Asset Type"] == "Bonds"])

    return jsonify({
        "ok": True,
        "synced_count": len(synced),
        "failed_count": len(failed),
        "failed": failed,
        "as_of": today,
        "sources": {
            "Nse": {"count": nse_count, "synced": len([s for s in symbols if not str(s).endswith(".US") and s not in us_symbols_set and s in prices]), "status": "Synced"},
            "Us": {"count": us_count, "synced": len([s for s in symbols if (str(s).endswith(".US") or s in us_symbols_set) and s in prices]), "status": "Synced"},
            "Mf": {"count": mf_count, "synced": len([c for c in amfi_codes if c in navs]), "status": "Synced"},
            "Bonds": {"count": bonds_count, "synced": bonds_count, "status": "Accrued"},
            "Forex": {"rate": fx_rate, "status": "Live"}
        },
        "holdings": clean_records,
    })


if __name__ == "__main__":
    import os
    import socket

    def find_free_port(start_port):
        p = start_port
        while p < start_port + 20:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(('127.0.0.1', p)) != 0:
                    return p
            p += 1
        return start_port

    requested_port = int(os.environ.get("PORT", 5152))
    port = find_free_port(requested_port)

    if port != requested_port:
        print(f"\n[INFO] Port {requested_port} is currently in use by another process.")
        print(f"[INFO] Automatically running on available port: http://127.0.0.1:{port}\n")
    else:
        print(f"Portfolio dashboard: http://127.0.0.1:{port}")

    app.run(host="127.0.0.1", port=port, debug=False)
