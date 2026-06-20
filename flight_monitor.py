#!/usr/bin/env python3
"""FareMonkey – Flight price monitor using the Amadeus Flight Offers Search API."""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

AMADEUS_CLIENT_ID = os.environ.get("AMADEUS_CLIENT_ID", "")
AMADEUS_CLIENT_SECRET = os.environ.get("AMADEUS_CLIENT_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CURRENCY = os.environ.get("CURRENCY", "USD")
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")
ACTIVE_START = int(os.environ.get("ACTIVE_START", "7"))
ACTIVE_END = int(os.environ.get("ACTIVE_END", "22"))
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "3"))
MONTHLY_CALL_CAP = int(os.environ.get("MONTHLY_CALL_CAP", "1900"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "1000"))

AMADEUS_BASE = "https://api.amadeus.com"
STATE_FILE = Path(__file__).parent / "state.json"
ROUTES_FILE = Path(__file__).parent / "routes.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_json(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def current_local_time() -> datetime:
    """Return the current time in the configured TIMEZONE."""
    import zoneinfo

    tz = zoneinfo.ZoneInfo(TIMEZONE)
    return datetime.now(tz)


def is_within_active_hours() -> bool:
    now = current_local_time()
    return ACTIVE_START <= now.hour < ACTIVE_END


def month_key() -> str:
    return current_local_time().strftime("%Y-%m")


# ---------------------------------------------------------------------------
# API call tracking
# ---------------------------------------------------------------------------


def get_call_count(state: dict) -> int:
    return state.get("api_calls", {}).get(month_key(), 0)


def increment_call_count(state: dict, n: int = 1) -> None:
    calls = state.setdefault("api_calls", {})
    key = month_key()
    calls[key] = calls.get(key, 0) + n


def can_make_calls(state: dict, needed: int) -> bool:
    return get_call_count(state) + needed <= MONTHLY_CALL_CAP


# ---------------------------------------------------------------------------
# Amadeus API
# ---------------------------------------------------------------------------


def get_amadeus_token() -> str:
    resp = requests.post(
        f"{AMADEUS_BASE}/v1/security/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": AMADEUS_CLIENT_ID,
            "client_secret": AMADEUS_CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_cheapest(token: str, route: dict) -> float | None:
    """Return the cheapest total price for a route, or None on failure."""
    params = {
        "originLocationCode": route["origin"],
        "destinationLocationCode": route["destination"],
        "departureDate": route["departure_date"],
        "adults": route.get("adults", 1),
        "nonStop": str(route.get("non_stop", True)).lower(),
        "travelClass": route.get("travel_class", "ECONOMY"),
        "currencyCode": CURRENCY,
        "max": 1,
    }
    if route.get("return_date"):
        params["returnDate"] = route["return_date"]

    resp = requests.get(
        f"{AMADEUS_BASE}/v2/shopping/flight-offers",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  API error {resp.status_code}: {resp.text[:200]}")
        return None

    data = resp.json().get("data", [])
    if not data:
        return None
    return float(data[0]["price"]["total"])


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"  [Telegram disabled] {message}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [Telegram error] {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if not AMADEUS_CLIENT_ID or not AMADEUS_CLIENT_SECRET:
        sys.exit("Error: AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET must be set.")

    if not is_within_active_hours():
        print(f"Outside active hours ({ACTIVE_START}:00–{ACTIVE_END}:00 {TIMEZONE}). Skipping.")
        return

    routes = load_json(ROUTES_FILE)
    if not isinstance(routes, list) or not routes:
        sys.exit("Error: routes.json must contain a non-empty JSON array.")

    state = load_json(STATE_FILE)

    # 1 token call + 1 search call per route
    calls_needed = 1 + len(routes)
    if not can_make_calls(state, calls_needed):
        print(
            f"Monthly cap would be exceeded ({get_call_count(state)}/{MONTHLY_CALL_CAP}). "
            f"Need {calls_needed} calls. Skipping."
        )
        save_json(STATE_FILE, state)
        return

    token = get_amadeus_token()
    increment_call_count(state, 1)  # token call

    prices = state.setdefault("prices", {})
    now_str = current_local_time().isoformat()

    for route in routes:
        label = f"{route['origin']}-{route['destination']} {route['departure_date']}"
        print(f"Checking {label} ...")

        price = search_cheapest(token, route)
        increment_call_count(state, 1)

        if price is None:
            print(f"  No offers found for {label}")
            continue

        prev = prices.get(label, {}).get("price")
        history = prices.get(label, {}).get("history", [])
        history.append({"price": price, "timestamp": now_str})
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        prices[label] = {"price": price, "updated": now_str, "history": history}
        print(f"  Current: {CURRENCY} {price:.2f}" + (f" | Previous: {CURRENCY} {prev:.2f}" if prev else ""))

        if prev is not None and prev > 0:
            pct_change = ((price - prev) / prev) * 100
            if abs(pct_change) >= ALERT_THRESHOLD_PCT:
                direction = "dropped" if pct_change < 0 else "rose"
                send_telegram(
                    f"{'✈️' if pct_change < 0 else '⚠️'} *{label}*\n"
                    f"Price {direction} *{abs(pct_change):.1f}%*\n"
                    f"{CURRENCY} {prev:.2f} → {CURRENCY} {price:.2f}"
                )
        else:
            print(f"  First check — baseline recorded.")

    state["last_run"] = now_str
    save_json(STATE_FILE, state)
    print(f"Done. API calls this month: {get_call_count(state)}/{MONTHLY_CALL_CAP}")


if __name__ == "__main__":
    main()
