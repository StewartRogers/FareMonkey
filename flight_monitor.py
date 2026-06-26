#!/usr/bin/env python3
"""FareMonkey – Flight price monitor using the SerpAPI Google Flights API."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv is optional; cron/CI inject env vars directly

# Cron and CI frequently run under a non-UTF-8 locale (e.g. latin-1), which makes
# print() raise UnicodeEncodeError on the emoji/en-dash characters used below.
# Force UTF-8 on the output streams so logging can't crash the monitor.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # already UTF-8, or a stream that doesn't support reconfigure

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
ARCHIVE_RESPONSES = os.environ.get("ARCHIVE_RESPONSES", "true").lower() in ("1", "true", "yes", "on")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

SERPAPI_BASE = "https://serpapi.com/search"
STATE_FILE = Path(__file__).parent / "state.json"
ROUTES_FILE = Path(__file__).parent / "routes.json"
# Append-only archive of every raw API response (kept out of state.json so the
# dashboard, which parses state.json on every request, stays fast).
RESPONSES_FILE = Path(__file__).parent / "responses.jsonl"

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
        try:
            with open(path) as f:
                return json.load(f)
        except (ValueError, OSError) as e:
            # Fail loudly rather than returning {} — a silent empty dict would
            # wipe price history / api_calls on the next save_json.
            sys.exit(f"Error: could not read {path.name}: {e}")
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


def load_routes() -> list:
    """Load routes.json (a personal, gitignored config), or exit with guidance."""
    routes = load_json(ROUTES_FILE)
    if not isinstance(routes, list) or not routes:
        sys.exit(
            f"Error: {ROUTES_FILE.name} must contain a non-empty JSON array. "
            f"Copy routes.example.json to {ROUTES_FILE.name} and edit it."
        )
    return routes


def current_local_time() -> datetime:
    """Return the current time in the configured TIMEZONE."""
    import zoneinfo

    tz = zoneinfo.ZoneInfo(TIMEZONE)
    return datetime.now(tz)


def log(msg: str = "") -> None:
    """Print a timestamped log line (in the configured TIMEZONE).

    Blank calls stay blank so spacing between sections is preserved.
    """
    if not msg:
        print()
        return
    ts = current_local_time().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}  {msg}")


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


def _summarize(flight: dict) -> dict:
    """Compact summary of one offer, for the alternatives list."""
    airlines: list[str] = []
    for seg in flight.get("flights", []):
        airline = seg.get("airline")
        if airline and airline not in airlines:
            airlines.append(airline)
    return {
        "price": float(flight["price"]),
        "airlines": airlines,
        "stops": len(flight.get("layovers", [])),
        "total_duration": flight.get("total_duration"),
    }


def archive_response(route: dict, params: dict, data: dict) -> None:
    """Append the full raw API response to RESPONSES_FILE as one JSON line.

    Records everything the API returned (all offers, price_insights, airports,
    booking tokens, etc.) so nothing is discarded. The api_key is stripped from
    the stored query. A failure here must never break monitoring, so errors are
    only logged.
    """
    if not ARCHIVE_RESPONSES:
        return
    record = {
        "timestamp": current_local_time().isoformat(),
        "route": f"{route['origin']}-{route['destination']}",
        "query": {k: v for k, v in params.items() if k != "api_key"},
        "response": data,
    }
    try:
        with open(RESPONSES_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        log(f"  [archive error] {e}")


def _is_older_than(timestamp: str, cutoff: datetime) -> bool:
    """True if an ISO timestamp is strictly before cutoff. Unparseable → False
    (keep the record rather than risk discarding data we can't date)."""
    try:
        return datetime.fromisoformat(timestamp) < cutoff
    except (TypeError, ValueError):
        return False


def trim_history(state: dict, cutoff: datetime) -> int:
    """Drop price-history points older than cutoff. Returns the number removed."""
    removed = 0
    for info in state.get("prices", {}).values():
        history = info.get("history", [])
        kept = [h for h in history if not _is_older_than(h.get("timestamp", ""), cutoff)]
        removed += len(history) - len(kept)
        info["history"] = kept
    return removed


def trim_responses(cutoff: datetime) -> int:
    """Drop archived responses older than cutoff. Returns the number removed."""
    if not RESPONSES_FILE.exists():
        return 0
    kept: list[str] = []
    removed = 0
    with open(RESPONSES_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                kept.append(line)  # keep unparseable lines rather than lose data
                continue
            ts = obj.get("timestamp", "") if isinstance(obj, dict) else ""
            if _is_older_than(ts, cutoff):
                removed += 1
            else:
                kept.append(line)
    if removed:
        fd, tmp = tempfile.mkstemp(dir=RESPONSES_FILE.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                for line in kept:
                    f.write(line + "\n")
            os.replace(tmp, RESPONSES_FILE)
        except BaseException:
            os.unlink(tmp)
            raise
    return removed


def trim_old_data(state: dict, days: int) -> tuple[int, int]:
    """Prune history points and archived responses older than `days`.

    Returns (history_points_removed, responses_removed). Mutates `state` in place
    (caller is responsible for persisting it).
    """
    cutoff = current_local_time() - timedelta(days=days)
    return trim_history(state, cutoff), trim_responses(cutoff)


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

    try:
        resp = requests.get(SERPAPI_BASE, params=params, timeout=30)
    except requests.RequestException as e:
        # A transient network failure must not abort the whole run — skip this
        # route and let the remaining ones still be checked.
        log(f"  Request failed: {e}")
        return None
    if resp.status_code != 200:
        log(f"  API error {resp.status_code}: {resp.text[:200]}")
        return None

    try:
        data = resp.json()
    except ValueError as e:  # 200 OK but body isn't JSON (e.g. an HTML error page)
        log(f"  Invalid JSON from API: {e}")
        return None
    archive_response(route, params, data)
    if data.get("error"):
        log(f"  API error: {data['error']}")
        return None

    candidates = [
        flight
        for key in ("best_flights", "other_flights")
        for flight in data.get(key, [])
        if flight.get("price") is not None
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda f: f["price"])
    cheapest = candidates[0]
    offer = {"price": float(cheapest["price"])}
    offer.update(_extract_details(cheapest))

    # A single search already returns the full result set and Google's own price
    # assessment — capture it instead of discarding everything but the minimum.
    offer["alternatives"] = [_summarize(f) for f in candidates[:3]]

    # Cheapest nonstop option (so we can show the nonstop premium when a route
    # allows connections). When the route is nonstop-only this equals the price.
    nonstop = [f for f in candidates if not f.get("layovers")]
    offer["nonstop_price"] = float(nonstop[0]["price"]) if nonstop else None

    # Google Flights' own verdict for this query (no extra API cost).
    insights = data.get("price_insights") or {}
    offer["price_level"] = insights.get("price_level")
    offer["typical_price_range"] = insights.get("typical_price_range")

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
    level = offer.get("price_level")
    if level:
        parts.append(f"{level} vs typical")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log(f"  [Telegram disabled] {message}")
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
        log(f"  [Telegram error] {body.get('description')}; retrying without Markdown")
        payload.pop("parse_mode", None)
        resp = requests.post(url, json=payload, timeout=15)
        body = resp.json()
        if not body.get("ok"):
            log(f"  [Telegram error] {body.get('description')}")
    except requests.RequestException as e:
        log(f"  [Telegram error] {e}")
    except ValueError as e:  # non-JSON response body
        log(f"  [Telegram error] invalid response: {e}")


# ---------------------------------------------------------------------------
# Flexible-date scan (on-demand)
# ---------------------------------------------------------------------------


def run_scan(days: int) -> None:
    """Scan each route across departure_date ± `days` to find the cheapest date.

    On-demand only (not part of the 6-hour cron): each date in the window costs
    one SerpAPI search, so a 7-day window for one route spends 7 searches. The
    monthly cap is still enforced. Round trips shift the return date by the same
    offset so the trip length stays constant.
    """
    if not SERPAPI_API_KEY:
        sys.exit("Error: SERPAPI_API_KEY must be set.")

    routes = load_routes()

    state = load_json(STATE_FILE)
    offsets = list(range(-days, days + 1))
    needed = len(routes) * len(offsets)
    if not can_make_calls(state, needed):
        sys.exit(
            f"Monthly cap would be exceeded ({get_call_count(state)}/{MONTHLY_CALL_CAP}). "
            f"A {len(offsets)}-day scan of {len(routes)} route(s) needs {needed} searches. "
            f"Lower --days or wait for the next month."
        )

    flex = state.setdefault("flex_scans", {})
    now_str = current_local_time().isoformat()
    log(f"Flexible-date scan: ±{days} days ({needed} searches)\n")

    for route in routes:
        base_dep = datetime.strptime(route["departure_date"], "%Y-%m-%d").date()
        base_ret = (
            datetime.strptime(route["return_date"], "%Y-%m-%d").date()
            if route.get("return_date")
            else None
        )
        # Match the price-history key format ("ORIGIN-DEST DATE") so multiple
        # routes sharing an origin/destination but differing by date don't
        # collide and overwrite each other in flex_scans.
        label = f"{route['origin']}-{route['destination']} {route['departure_date']}"
        log(f"Scanning {label} ...")

        results: list[dict] = []
        for off in offsets:
            dep = base_dep + timedelta(days=off)
            probe = dict(route)
            probe["departure_date"] = dep.isoformat()
            if base_ret is not None:
                probe["return_date"] = (base_ret + timedelta(days=off)).isoformat()

            offer = search_cheapest(probe)
            increment_call_count(state, 1)
            price = offer["price"] if offer else None
            results.append(
                {
                    "date": dep.isoformat(),
                    "return_date": probe.get("return_date"),
                    "price": price,
                }
            )
            marker = "  ← base" if off == 0 else ""
            price_str = f"{CURRENCY} {price:.2f}" if price is not None else "no offers"
            log(f"  {dep.isoformat()}: {price_str}{marker}")

        priced = [r for r in results if r["price"] is not None]
        cheapest = min(priced, key=lambda r: r["price"]) if priced else None
        base = next((r for r in results if r["date"] == base_dep.isoformat()), None)
        flex[label] = {
            "scanned": now_str,
            "base_date": base_dep.isoformat(),
            "days": days,
            "results": results,
            "cheapest": cheapest,
        }

        if cheapest:
            saving = ""
            if base and base["price"] is not None and cheapest["date"] != base["date"]:
                diff = base["price"] - cheapest["price"]
                if diff > 0:
                    saving = f" (saves {CURRENCY} {diff:.2f} vs {base['date']})"
            log(f"  Cheapest: {cheapest['date']} at {CURRENCY} {cheapest['price']:.2f}{saving}\n")
            send_telegram(
                f"📅 *{label}* date scan (±{days}d)\n"
                f"Cheapest: {cheapest['date']} — {CURRENCY} {cheapest['price']:.2f}{saving}"
            )
        else:
            log("  No offers found in window.\n")

    save_json(STATE_FILE, state)
    log(f"Done. API calls this month: {get_call_count(state)}/{MONTHLY_CALL_CAP}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _days_arg(argv: list[str], default: int) -> int:
    """Parse an optional `--days N` flag, falling back to `default`."""
    if "--days" not in argv:
        return default
    try:
        days = int(argv[argv.index("--days") + 1])
    except (IndexError, ValueError):
        sys.exit("Error: --days requires an integer, e.g. --days 5")
    if days < 1:
        sys.exit("Error: --days must be >= 1")
    return days


def run_trim(days: int) -> None:
    """Manually prune history and the response archive to the last `days` days."""
    state = load_json(STATE_FILE)
    hist_removed, resp_removed = trim_old_data(state, days)
    save_json(STATE_FILE, state)
    log(
        f"Trimmed to last {days} days: removed {hist_removed} history point(s) "
        f"and {resp_removed} archived response(s)."
    )


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--scan":
        run_scan(_days_arg(argv, 3))
        return
    if argv and argv[0] == "--trim":
        run_trim(_days_arg(argv, RETENTION_DAYS))
        return

    if not SERPAPI_API_KEY:
        sys.exit("Error: SERPAPI_API_KEY must be set.")

    if not is_within_active_hours():
        log(f"Outside active hours ({ACTIVE_START}:00–{ACTIVE_END}:00 {TIMEZONE}). Skipping.")
        return

    routes = load_routes()

    state = load_json(STATE_FILE)

    # 1 search call per route (SerpAPI uses a single API key, no token call)
    calls_needed = len(routes)
    if not can_make_calls(state, calls_needed):
        log(
            f"Monthly cap would be exceeded ({get_call_count(state)}/{MONTHLY_CALL_CAP}). "
            f"Need {calls_needed} calls. Skipping."
        )
        save_json(STATE_FILE, state)
        return

    prices = state.setdefault("prices", {})
    now_str = current_local_time().isoformat()

    for route in routes:
        label = f"{route['origin']}-{route['destination']} {route['departure_date']}"
        log(f"Checking {label} ...")

        offer = search_cheapest(route)
        increment_call_count(state, 1)

        if offer is None:
            log(f"  No offers found for {label}")
            continue

        price = offer["price"]
        details = {k: v for k, v in offer.items() if k != "price"}

        prev = prices.get(label, {}).get("price")
        history = prices.get(label, {}).get("history", [])
        history.append({"price": price, "timestamp": now_str})
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        prices[label] = {"price": price, "updated": now_str, "details": details, "history": history}
        log(f"  Current: {CURRENCY} {price:.2f}" + (f" | Previous: {CURRENCY} {prev:.2f}" if prev else ""))
        log(f"  {format_offer(offer)}")

        pct_change = ((price - prev) / prev) * 100 if (prev is not None and prev > 0) else None
        significant = pct_change is not None and abs(pct_change) >= ALERT_THRESHOLD_PCT

        if pct_change is None:
            icon = "🐒"
            change_line = f"Baseline: {CURRENCY} {price:.2f}"
            log("  First check — baseline recorded.")
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

    # Keep the dataset bounded: prune history and archived responses past the
    # retention window every run so the committed files don't grow forever.
    hist_removed, resp_removed = trim_old_data(state, RETENTION_DAYS)
    save_json(STATE_FILE, state)
    if hist_removed or resp_removed:
        log(
            f"Trimmed {hist_removed} history point(s) and {resp_removed} "
            f"archived response(s) older than {RETENTION_DAYS} days."
        )
    log(f"Done. API calls this month: {get_call_count(state)}/{MONTHLY_CALL_CAP}")


if __name__ == "__main__":
    main()
