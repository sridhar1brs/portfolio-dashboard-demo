"""Deterministic portfolio health metrics for the dashboard."""
from collections import defaultdict
from datetime import date, datetime
import math


SCORE_WEIGHTS = {
    "diversification": 0.25,
    "concentration": 0.25,
    "data_health": 0.20,
    "liquidity": 0.15,
    "income_visibility": 0.15,
}


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(value):
    raw = str(value or "").strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _xnpv(rate, cash_flows, base_date):
    if rate <= -1:
        return math.inf
    return sum(
        amount / (1 + rate) ** ((flow_date - base_date).days / 365.25)
        for flow_date, amount in cash_flows
    )


def _xirr(cash_flows):
    """Solve annualized return for irregular dated cash flows without numpy_financial."""
    if not cash_flows or not any(amount < 0 for _, amount in cash_flows) or not any(amount > 0 for _, amount in cash_flows):
        return None
    ordered = sorted(cash_flows, key=lambda flow: flow[0])
    base_date = ordered[0][0]
    lower, upper = -0.9999, 10.0
    lower_value = _xnpv(lower, ordered, base_date)
    upper_value = _xnpv(upper, ordered, base_date)
    while lower_value * upper_value > 0 and upper < 1_000_000:
        upper *= 2
        upper_value = _xnpv(upper, ordered, base_date)
    if lower_value * upper_value > 0:
        return None
    for _ in range(200):
        midpoint = (lower + upper) / 2
        value = _xnpv(midpoint, ordered, base_date)
        if abs(value) < 0.01:
            return midpoint
        if lower_value * value <= 0:
            upper, upper_value = midpoint, value
        else:
            lower, lower_value = midpoint, value
    return (lower + upper) / 2


def _return_metrics(rows, income_summary=None, as_of=None):
    as_of = as_of or date.today()
    dated_rows = []
    for row in rows:
        invested = _num(row.get("Inv. Amount"))
        invested_date = _parse_date(row.get("Inv. Date"))
        if invested > 0 and invested_date:
            dated_rows.append((invested_date, invested))
    latest = sum(_num(row.get("Latest Value")) for row in rows)
    if not dated_rows or latest <= 0:
        return {
            "cagr_pct": None, "xirr_pct": None, "return_start_date": None,
            "return_cash_flow_count": 0, "return_lot_count": 0, "return_income_event_count": 0,
        }

    first_date = min(flow_date for flow_date, _ in dated_rows)
    years = max((as_of - first_date).days / 365.25, 1 / 365.25)
    invested_total = sum(amount for _, amount in dated_rows)
    cagr = (latest / invested_total) ** (1 / years) - 1 if invested_total else None
    cash_flows = [(flow_date, -amount) for flow_date, amount in dated_rows]
    income_event_count = 0
    dividends = (income_summary or {}).get("events", [])
    for event in dividends:
        event_date = _parse_date(event.get("Payment Date") or event.get("Ex-Date"))
        amount = _num(event.get("Total Amount"))
        if event_date and amount:
            cash_flows.append((event_date, amount))
            income_event_count += 1
    cash_flows.append((as_of, latest))
    xirr = _xirr(cash_flows)
    return {
        "cagr_pct": round(cagr * 100, 2) if cagr is not None else None,
        "xirr_pct": round(xirr * 100, 2) if xirr is not None else None,
        "return_start_date": first_date.strftime("%d-%m-%Y"),
        "return_cash_flow_count": len(cash_flows),
        "return_lot_count": len(dated_rows),
        "return_income_event_count": income_event_count,
    }


def _group_totals(rows, column):
    totals = defaultdict(float)
    for row in rows:
        key = str(row.get(column) or "(unset)").strip() or "(unset)"
        totals[key] += _num(row.get("Latest Value"))
    return dict(sorted(totals.items(), key=lambda item: item[1], reverse=True))


def _hhi_percent(values, total):
    if not total:
        return 0.0
    return sum((value / total) ** 2 for value in values.values()) * 10000


def _score_concentration(hhi, top_holding_pct):
    hhi_score = max(0.0, min(100.0, 100.0 - (hhi - 1000.0) / 20.0))
    top_score = max(0.0, min(100.0, 100.0 - max(0.0, top_holding_pct - 5.0) * 2.0))
    return round((hhi_score * 0.6) + (top_score * 0.4))


def _score_diversification(asset_types, advisors, accounts):
    breadth = min(100.0, (len(asset_types) / 6.0) * 60.0 + (len(advisors) / 5.0) * 20.0 + (len(accounts) / 5.0) * 20.0)
    return round(breadth)


def _score_data_health(rows):
    if not rows:
        return 0
    checks = []
    for row in rows:
        checks.extend([
            bool(str(row.get("Name") or "").strip()),
            bool(str(row.get("Latest Value") or "").strip()),
            bool(str(row.get("Latest Price/NAV") or "").strip()),
            bool(str(row.get("Symbol") or row.get("AMFI Scheme Code") or "").strip()),
        ])
    return round(sum(checks) / len(checks) * 100)


def _score_liquidity(rows):
    if not rows:
        return 0
    illiquid_value = sum(
        _num(row.get("Latest Value"))
        for row in rows
        if str(row.get("Asset Type") or "") in {"Bonds", "InvIT", "REIT"}
    )
    total = sum(_num(row.get("Latest Value")) for row in rows)
    return round(max(0.0, 100.0 - (illiquid_value / total * 100.0 if total else 100.0)))


def _score_income_visibility(income_summary, invested):
    if not income_summary or not invested:
        return 0
    tracked = _num(income_summary.get("tracked_total"))
    events = int(_num(income_summary.get("event_count")))
    verified = int(_num(income_summary.get("verified_count")))
    if not events or not tracked:
        return 0
    yield_score = min(60.0, tracked / invested * 100.0 * 120.0)
    coverage_score = min(40.0, verified / events * 40.0) if verified else 20.0
    return round(yield_score + coverage_score)


def _average_holding_period(rows):
    today = datetime.now()
    weighted_days = 0.0
    weight = 0.0
    dated_lots = 0
    for row in rows:
        raw_date = str(row.get("Inv. Date") or "")
        try:
            invested_date = datetime.strptime(raw_date, "%d-%m-%Y")
        except ValueError:
            continue
        value = _num(row.get("Latest Value"))
        if value <= 0:
            continue
        dated_lots += 1
        weighted_days += max(0, (today - invested_date).days) * value
        weight += value
    return round(weighted_days / weight) if weight else None, dated_lots


def _debt_analysis(rows):
    bond_rows = [row for row in rows if str(row.get("Asset Type") or "") == "Bonds"]
    debt_fund_rows = [
        row for row in rows
        if str(row.get("Sector") or "") in {"Debt - Short Term", "Debt - Medium Term", "Liquid Fund"}
    ]
    maturities = []
    today = datetime.now()
    for row in bond_rows:
        raw_date = str(row.get("Maturity Date") or "")
        try:
            days = (datetime.strptime(raw_date, "%d-%m-%Y") - today).days
        except ValueError:
            days = None
        maturities.append({
            "name": row.get("Name"),
            "maturity_date": raw_date or None,
            "days_remaining": days,
            "rate_pct": _num(row.get("Inv. Rate %")),
            "latest_value": round(_num(row.get("Latest Value")), 2),
        })
    return {
        "bond_value": round(sum(_num(row.get("Latest Value")) for row in bond_rows), 2),
        "bond_coupon_rates": sorted({_num(row.get("Inv. Rate %")) for row in bond_rows if row.get("Inv. Rate %")}),
        "maturities": maturities,
        "debt_fund_value": round(sum(_num(row.get("Latest Value")) for row in debt_fund_rows), 2),
        "debt_fund_categories": sorted({str(row.get("Sector")) for row in debt_fund_rows if row.get("Sector")}),
        "duration": "Unavailable without fund factsheets",
        "ytm": "Unavailable without fund factsheets",
        "credit_quality": "Unavailable without fund factsheets",
        "issuer_mix": "Unavailable without security-level debt data",
    }


def _day_gain(row):
    if str(row.get("Asset Type") or "") == "Bonds":
        return _num(row.get("Latest Value")) * _num(row.get("Inv. Rate %")) / 36500
    return _num(row.get("Day's Gain"))


def calculate_metrics(rows, income_summary=None):
    rows = list(rows)
    latest = sum(_num(row.get("Latest Value")) for row in rows)
    invested = sum(_num(row.get("Inv. Amount")) for row in rows)
    overall_gain = latest - invested
    day_gain = sum(_day_gain(row) for row in rows)
    asset_types = _group_totals(rows, "Asset Type")
    advisors = _group_totals(rows, "Advisor")
    accounts = _group_totals(rows, "Broker")
    custom_tags = _group_totals(rows, "Tag1")
    sectors = _group_totals(rows, "Sector")
    holding_values = defaultdict(float)
    for row in rows:
        holding_values[str(row.get("Name") or "(unnamed)")] += _num(row.get("Latest Value"))
    top_holdings = sorted(holding_values.items(), key=lambda item: item[1], reverse=True)[:5]
    top_holding_pct = (top_holdings[0][1] / latest * 100.0) if top_holdings and latest else 0.0
    hhi = _hhi_percent(holding_values, latest)
    data_issues = []
    for row in rows:
        missing = []
        if not str(row.get("Symbol") or row.get("AMFI Scheme Code") or "").strip():
            missing.append("identifier")
        if not str(row.get("Latest Price/NAV") or "").strip():
            missing.append("price")
        if missing:
            data_issues.append({"name": row.get("Name", "(unnamed)"), "issues": missing})

    average_holding_days, dated_lots = _average_holding_period(rows)
    debt_analysis = _debt_analysis(rows)
    return_metrics = _return_metrics(rows, income_summary)
    scores = {
        "diversification": _score_diversification(asset_types, advisors, accounts),
        "concentration": _score_concentration(hhi, top_holding_pct),
        "data_health": _score_data_health(rows),
        "liquidity": _score_liquidity(rows),
        "income_visibility": _score_income_visibility(income_summary, invested),
    }
    overall_score = round(sum(scores[key] * SCORE_WEIGHTS[key] for key in scores))
    return {
        "totals": {
            "lots": len(rows),
            "invested": round(invested, 2),
            "latest_value": round(latest, 2),
            "overall_gain": round(overall_gain, 2),
            "day_gain": round(day_gain, 2),
        },
        "scores": scores,
        "overall_score": overall_score,
        "weights": SCORE_WEIGHTS,
        "concentration": {
            "hhi": round(hhi, 1),
            "top_holding_pct": round(top_holding_pct, 2),
            "top_holdings": [{"name": name, "value": round(value, 2), "pct": round(value / latest * 100, 2) if latest else 0} for name, value in top_holdings],
        },
        "allocation": {
            "asset_type": asset_types,
            "advisor": advisors,
            "account": accounts,
            "custom_tag": custom_tags,
            "sector": sectors,
        },
        "data_quality": {
            "issues": data_issues,
            "issue_count": len(data_issues),
            "income_events_tracked": int(_num((income_summary or {}).get("event_count"))),
            "income_note": "Income visibility is based on the manual dividend ledger; historical coverage may be incomplete.",
        },
        "holding_period": {
            "average_days": average_holding_days,
            "average_years": round(average_holding_days / 365.25, 2) if average_holding_days is not None else None,
            "dated_lots": dated_lots,
            "turnover": "Unavailable without sales history",
        },
        "return_metrics": return_metrics,
        "debt_analysis": debt_analysis,
    }
