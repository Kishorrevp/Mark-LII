"""JARVIS bundled plugin — live currency conversion.

Uses the free, key-less open.er-api.com daily rates (no account, no API key).
Declares the "network" permission, so JARVIS asks once before its first use.
"""
import json
import urllib.request

PLUGIN = {
    "name": "currency_converter",
    "version": "1.0.0",
    "description": (
        "Convert money between currencies with today's exchange rate (e.g. 100 USD to INR). "
        "Uses a free public rates service — needs the internet."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "amount": {"type": "NUMBER", "description": "Amount of money"},
            "from_currency": {"type": "STRING", "description": "ISO code, e.g. USD"},
            "to_currency": {"type": "STRING", "description": "ISO code, e.g. INR"},
        },
        "required": ["amount", "from_currency", "to_currency"],
    },
    "risk": "low",
    "permissions": ["network"],
    "capabilities": ["currency", "exchange rate", "forex", "money conversion", "convert currency"],
}

_NAMES = {"dollar": "USD", "dollars": "USD", "rupee": "INR", "rupees": "INR", "euro": "EUR", "euros": "EUR",
          "pound": "GBP", "pounds": "GBP", "yen": "JPY", "dirham": "AED", "dirhams": "AED", "ringgit": "MYR",
          "singapore dollar": "SGD"}


def _code(c):
    c = str(c or "").strip()
    return _NAMES.get(c.lower(), c.upper())[:3]


def run(parameters, player=None, session_memory=None):
    try:
        amt = float(parameters.get("amount"))
        a, b = _code(parameters.get("from_currency")), _code(parameters.get("to_currency"))
        with urllib.request.urlopen(f"https://open.er-api.com/v6/latest/{a}", timeout=10) as r:
            data = json.loads(r.read(200_000).decode("utf-8"))
        if data.get("result") != "success" or b not in (data.get("rates") or {}):
            return f"I couldn't get a {a} to {b} rate right now."
        rate = float(data["rates"][b])
        day = str(data.get("time_last_update_utc", ""))[:16]
        return f"{amt:,.2f} {a} = {amt * rate:,.2f} {b} (rate {rate:.4f}, updated {day})"
    except Exception as e:
        return f"I couldn't reach the exchange-rate service ({type(e).__name__})."


def verify(parameters, result_text):
    return (" = " in result_text, "rate fetched" if " = " in result_text else "no rate")
