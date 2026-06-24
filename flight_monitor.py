#!/usr/bin/env python3
"""FareMonkey – Flight price monitor using the SerpAPI Google Flights API."""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv is optional; cron/CI inject env vars directly

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CURRENCY = os.environ.get("CURRENCY", "USD")
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")
ACTIVE_START = int(os.environ.get("ACTIVE_START", "7"))
ACTIVE_END = int(os.environ.get("ACTIVE_END", "22"))
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "3"))
NOTIFY_EVERY_RUN = os.environ.get("NOTIFY_EVERY_RUN", "true").lower() in ("1", "true", "yes", "on")
MONTHLY_CALL_CAP = int(os.environ.get("MONTHLY_CALL_CAP", "240"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "1000"))

SERPAPI_BASE = "https://serpapi.com/search"
STATE_FILE = Path(__file__).parent / "state.json"
ROUTES_FILE = Path(__file__).parent / "routes.json"

# SerpAPI Google Flights travel_class codes
TRAVEL_CLASS_MAP = {
    "ECONOMY": 1,
    "PREMIUM_ECONOMY": 2,
    "BUSINESS": 3,
    "FIRST": 4,
}

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
# SerpAPI Google Flights
# ---------------------------------------------------------------------------


def _extract_details(flight: dict) -> dict:
    """Pull human-readable details from a SerpAPI flight option."""
    segments = flight.get("flights", [])
    layovers = flight.get("layovers", [])
    airlines: list[str] = []
    flight_numbers: list[str] = []
    for seg in segments:
        airline = seg.get("airline")
        if airline and airline not in airlines:
            airlines.append(airline)
        if seg.get("flight_number"):
            flight_numbers.append(seg["flight_number"])
    dep = segments[0].get("departure_airport", {}) if segments else {}
    arr = segments[-1].get("arrival_airport", {}) if segments else {}
    return {
        "airlines": airlines,
        "flight_numbers": flight_numbers,
        "departure_time": dep.get("time"),
        "arrival_time": arr.get("time"),
        "total_duration": flight.get("total_duration"),  # minutes
        "stops": len(layovers),
        "layover_airports": [lo.get("id") for lo in layovers],
    }


def search_cheapest(route: dict) -> dict | None:
    """Return the cheapest option (price + details) for a route, or None on failure."""
    travel_class = TRAVEL_CLASS_MAP.get(
        str(route.get("travel_class", "ECONOMY")).upper(), 1
    )
    has_return = bool(route.get("return_date"))
    params = {
        "engine": "google_flights",
        "api_key": SERPAPI_API_KEY,
        "departure_id": route["origin"],
        "arrival_id": route["destination"],
        "outbound_date": route["departure_date"],
        "adults": route.get("adults", 1),
        "travel_class": travel_class,
        # SerpAPI stops: 0 = any, 1 = nonstop only
        "stops": 1 if route.get("non_stop", True) else 0,
        "currency": CURRENCY,
        # SerpAPI type: 1 = round trip, 2 = one way
        "type": 1 if has_return else 2,
    }
    if has_return:
        params["return_date"] = route["return_date"]

    resp = requests.get(SERPAPI_BASE, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"  API error {resp.status_code}: {resp.text[:200]}")
        return None

    data = resp.json()
    if data.get("error"):
        print(f"  API error: {data['error']}")
        return None

    candidates = [
        flight
        for key in ("best_flights", "other_flights")
        for flight in data.get(key, [])
        if flight.get("price") is not None
    ]
    if not candidates:
        return None

    cheapest = min(candidates, key=lambda f: f["price"])
    offer = {"price": float(cheapest["price"])}
    offer.update(_extract_details(cheapest))
    return offer


def format_offer(offer: dict) -> str:
    """One-line summary of a flight offer for console/Telegram output."""
    airlines = ", ".join(offer.get("airlines") or []) or "?"
    stops = offer.get("stops", 0)
    stops_str = "nonstop" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"
    parts = [airlines, stops_str]
    dur = offer.get("total_duration")
    if dur:
        parts.append(f"{dur // 60}h {dur % 60:02d}m")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"  [Telegram disabled] {message}")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        body = resp.json()
        if body.get("ok"):
            return
        # Telegram returns HTTP 200 with ok=false on Markdown parse errors caused by
        # unescaped *, _, ` or [ in dynamic text (airline names, labels). Retry as
        # plain text so the alert still lands instead of being silently dropped.
        print(f"  [Telegram error] {body.get('description')}; retrying without Markdown")
        payload.pop("parse_mode", None)
        resp = requests.post(url, json=payload, timeout=15)
        body = resp.json()
        if not body.get("ok"):
            print(f"  [Telegram error] {body.get('description')}")
    except requests.RequestException as e:
        print(f"  [Telegram error] {e}")
    except ValueError as e:  # non-JSON response body
        print(f"  [Telegram error] invalid response: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if not SERPAPI_API_KEY:
        sys.exit("Error: SERPAPI_API_KEY must be set.")

    if not is_within_active_hours():
        print(f"Outside active hours ({ACTIVE_START}:00–{ACTIVE_END}:00 {TIMEZONE}). Skipping.")
        return

    routes = load_json(ROUTES_FILE)
    if not isinstance(routes, list) or not routes:
        sys.exit("Error: routes.json must contain a non-empty JSON array.")

    state = load_json(STATE_FILE)

    # 1 search call per route (SerpAPI uses a single API key, no token call)
    calls_needed = len(routes)
    if not can_make_calls(state, calls_needed):
        print(
            f"Monthly cap would be exceeded ({get_call_count(state)}/{MONTHLY_CALL_CAP}). "
            f"Need {calls_needed} calls. Skipping."
        )
        save_json(STATE_FILE, state)
        return

    prices = state.setdefault("prices", {})
    now_str = current_local_time().isoformat()

    for route in routes:
        label = f"{route['origin']}-{route['destination']} {route['departure_date']}"
        print(f"Checking {label} ...")

        offer = search_cheapest(route)
        increment_call_count(state, 1)

        if offer is None:
            print(f"  No offers found for {label}")
            continue

        price = offer["price"]
        details = {k: v for k, v in offer.items() if k != "price"}

        prev = prices.get(label, {}).get("price")
        history = prices.get(label, {}).get("history", [])
        history.append({"price": price, "timestamp": now_str})
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        prices[label] = {"price": price, "updated": now_str, "details": details, "history": history}
        print(f"  Current: {CURRENCY} {price:.2f}" + (f" | Previous: {CURRENCY} {prev:.2f}" if prev else ""))
        print(f"  {format_offer(offer)}")

        pct_change = ((price - prev) / prev) * 100 if (prev is not None and prev > 0) else None
        significant = pct_change is not None and abs(pct_change) >= ALERT_THRESHOLD_PCT

        if pct_change is None:
            icon = "🐒"
            change_line = f"Baseline: {CURRENCY} {price:.2f}"
            print("  First check — baseline recorded.")
        elif pct_change <= -ALERT_THRESHOLD_PCT:
            icon = "✈️"
            change_line = f"Price *dropped {abs(pct_change):.1f}%*: {CURRENCY} {prev:.2f} → {CURRENCY} {price:.2f}"
        elif pct_change >= ALERT_THRESHOLD_PCT:
            icon = "⚠️"
            change_line = f"Price *rose {pct_change:.1f}%*: {CURRENCY} {prev:.2f} → {CURRENCY} {price:.2f}"
        elif pct_change == 0:
            icon = "➡️"
            change_line = f"No change: {CURRENCY} {price:.2f}"
        else:
            icon = "🔹"
            arrow = "▼" if pct_change < 0 else "▲"
            change_line = f"{arrow} {abs(pct_change):.1f}%: {CURRENCY} {prev:.2f} → {CURRENCY} {price:.2f}"

        if NOTIFY_EVERY_RUN or significant:
            send_telegram(
                f"{icon} *{label}*\n"
                f"{change_line}\n"
                f"{format_offer(offer)}"
            )

    state["last_run"] = now_str
    save_json(STATE_FILE, state)
    print(f"Done. API calls this month: {get_call_count(state)}/{MONTHLY_CALL_CAP}")


if __name__ == "__main__":
    main()
