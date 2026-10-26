"""Provider-neutral AI portfolio review, currently backed by OpenRouter's free-tier models.

Sends only aggregated, non-identifying portfolio metrics - never raw per-lot data,
account numbers, ISINs, or purchase dates. Advisory only: the model's output is a
structured opinion for you to evaluate, not financial advice or a trade instruction.
"""
import json
import os

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Verified against OpenRouter's live /api/v1/models catalog on 2026-09-04 (the two models
# guessed earlier - deepseek-r1:free, qwen-2.5-72b-instruct:free - had already been
# discontinued from the free tier, confirmed via live 404s). Ling 3.0 Flash Fin is a
# finance-focused model explicitly built for investment analysis - best fit for this app.
# Nemotron 3 Ultra is a large reasoning model, kept as the deeper-analysis alternative.
DEFAULT_MODEL = "inclusionai/ling-3.0-flash-fin:free"
FREE_MODELS = [
    "inclusionai/ling-3.0-flash-fin:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "z-ai/glm-5.2:free",
    "minimax/minimax-m3:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
]
USER_PROFILE = {
    "objective": "Wealth creation with some capital preservation",
    "time_horizon": "3-5 years",
    "liquidity_need_inr": 1000000,
    "maximum_tolerable_drawdown_pct": "15-20",
    "future_income_goal_inr_monthly": 100000,
    "tax_brackets": {"Primary Account": 30, "Secondary Account": 0},
}

SYSTEM_PROMPT = (
    "You are a portfolio analysis assistant. You receive aggregated, anonymized Indian "
    "equity/MF/bond portfolio metrics (no account numbers, no personal identifiers). "
    "Respond ONLY with a JSON object with these keys: "
    "\"strengths\" (array of short strings), \"risks\" (array of short strings), "
    "\"questions\" (array of objects with \"question\" and \"status\", where status is exactly one of "
    "\"Actionable now\", \"Needs more data\", or \"Needs your input\"), "
    "\"suggestions\" (array of objects with \"action\" and \"rationale\" short strings, "
    "identifying what type of investment - asset class, sector, or instrument - to add next "
    "for better diversification, stability, or returns, based on gaps in the given allocation, "
    "concentration, and sector data), "
    "\"summary\" (one paragraph). Be specific and reference the numbers given. Use the suitability profile "
    "only as context; do not infer personal recommendations or trade instructions. "
    "Do not give buy/sell instructions or guaranteed return claims. This is not financial advice."
)


def _strip_markdown_fence(content):
    """Some free models wrap JSON in ```json ... ``` fences despite instructions not to."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def build_snapshot(metrics, events, market):
    """Build the aggregated JSON payload sent to the model. No raw holdings table."""
    top_holdings = metrics.get("concentration", {}).get("top_holdings", [])[:5]
    return {
        "totals": metrics.get("totals"),
        "scores": metrics.get("scores"),
        "concentration": {
            "hhi": metrics.get("concentration", {}).get("hhi"),
            "top_holding_pct": metrics.get("concentration", {}).get("top_holding_pct"),
            "top_holdings": [{"name": h["name"], "pct": h["pct"]} for h in top_holdings],
        },
        "allocation": {
            "asset_type": metrics.get("allocation", {}).get("asset_type"),
            "sector": metrics.get("allocation", {}).get("sector"),
        },
        "dividends": {
            "tracked_total": events.get("dividends", {}).get("tracked_total"),
            "return_pct": events.get("dividends", {}).get("return_pct"),
            "event_count": events.get("dividends", {}).get("event_count"),
        },
        "holding_period": metrics.get("holding_period"),
        "debt_analysis": metrics.get("debt_analysis"),
            "suitability_profile": USER_PROFILE,
        "data_limits": [
            "Mutual-fund look-through equity/debt/sector exposure is not available.",
            "Debt duration, yield-to-maturity, and credit-quality data is not available.",
            "Turnover cannot be calculated without sales or transaction history.",
            "Suitability requires the user's stated goals, horizon, liquidity needs, and risk tolerance.",
        ],
        "market_context": [
            {"name": q["name"], "change_pct": q["change_pct"]} for q in market.get("quotes", [])
        ],
    }


def request_ai_review(snapshot, model=None):
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return {"ok": False, "error": "OPENROUTER_API_KEY is not set. Add it to a local .env file."}

    # Client-selected model takes priority; server env var and hardcoded default are fallbacks
    # for when the client doesn't send one (e.g. a direct API call).
    model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(snapshot)},
                ],
                "temperature": 0.3,
            },
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        review = json.loads(_strip_markdown_fence(content))
        return {"ok": True, "model": model, "review": review, "snapshot": snapshot}
    except requests.HTTPError:
        return {"ok": False, "error": f"OpenRouter request failed ({response.status_code}): {response.text[:300]}"}
    except (json.JSONDecodeError, KeyError, IndexError):
        return {"ok": False, "error": f"Model response was not valid structured JSON. Raw output: {content[:500]!r}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
