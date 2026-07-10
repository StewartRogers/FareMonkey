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
# When true, drop any itinerary that connects through a US airport (see US_HUBS).
EXCLUDE_US_CONNECTIONS = os.environ.get("EXCLUDE_US_CONNECTIONS", "false").lower() in ("1", "true", "yes", "on")
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

# Major US connecting hubs, used to drop itineraries that layover in the US when
# EXCLUDE_US_CONNECTIONS is on. Not every US airport — just the ones that realistically
# appear as connection points. Add codes here if you see a US layover slip through.
US_HUBS = {
    # West
    "SEA", "PDX", "SFO", "OAK", "SJC", "LAX", "SAN", "LAS", "PHX", "SLC", "DEN",
    # Central / South-central
    "DFW", "IAH", "AUS", "MSP", "ORD", "MDW", "STL", "MCI", "MSY", "BNA", "MEM",
    # Southeast
    "ATL", "CLT", "MIA", "FLL", "MCO", "TPA", "RDU", "RSW",
    # Northeast / Mid-Atlantic
    "JFK", "EWR", "LGA", "BOS", "PHL", "BWI", "IAD", "DCA", "PIT", "CLE", "DTW",
    # Other commonly-seen
    "HNL", "ANC", "SAT", "SMF", "ONT", "BUR", "CVG", "IND", "CMH",
}

# Set once per process after a quota/credits alert is sent, so a run that hits the
# limit on every route only pings Telegram once instead of per route.
_QUOTA_ALERTED = False

# Live remaining-searches counter, synced from the SerpAPI account endpoint at
# process start and decremented locally after every search.  None means no sync
# has happened yet (the account call failed or was skipped).
_searches_left: int | None = None

# Real monthly usage as reported by SerpAPI at process start (None if the sync
# failed), plus a running tally of searches this process has since made. Used
# for the monthly-cap check and "Done" log instead of the locally-reported
# state.json counter, since that counter only sees calls made through this
# script and can drift from the account's actual usage.
_this_month_usage: int | None = None
_calls_made_this_run = 0

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


REQUIRED_ROUTE_FIELDS = ("origin", "destination", "departure_date")


def load_routes() -> list:
    """Load routes.json (a personal, gitignored config), or exit with guidance."""
    routes = load_json(ROUTES_FILE)
    if not isinstance(routes, list) or not routes:
        sys.exit(
            f"Error: {ROUTES_FILE.name} must contain a non-empty JSON array. "
            f"Copy routes.example.json to {ROUTES_FILE.name} and edit it."
        )
    for i, route in enumerate(routes):
        missing = [f for f in REQUIRED_ROUTE_FIELDS if not route.get(f)]
        if missing:
            sys.exit(
                f"Error: {ROUTES_FILE.name} entry {i} is missing required field(s): "
                f"{', '.join(missing)}"
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


def route_runs_this_hour(route: dict, hour: int) -> bool:
    """Decide whether a route should be checked on the current cron firing.

    A route may set an optional ``run_hours`` list (local-time hours) to limit
    which firings it runs on — e.g. ``"run_hours": [13]`` checks once a day on the
    13:xx firing. Without ``run_hours`` the route runs on every firing (the
    default). The values must match the hours the cron actually fires at.
    """
    run_hours = route.get("run_hours")
    if run_hours is None:
        return True
    return hour in run_hours


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


def can_make_calls(needed: int) -> bool:
    """Whether `needed` more searches can be made without exceeding the cap.

    Based on SerpAPI's real ``this_month_usage`` (synced at process start) plus
    any calls this process has already made, not the locally-reported counter
    in state.json — that counter only sees calls made through this script and
    can drift from the account's actual usage. If the sync failed, we can't
    verify usage, so fail closed rather than risk exceeding the cap.
    """
    if _this_month_usage is None:
        return False
    return _this_month_usage + _calls_made_this_run + needed <= MONTHLY_CALL_CAP


def current_usage() -> int | None:
    """Real SerpAPI usage this month, or None if the account sync failed."""
    if _this_month_usage is None:
        return None
    return _this_month_usage + _calls_made_this_run


def record_call() -> None:
    """Track a search actually sent to SerpAPI this process, for current_usage()."""
    global _calls_made_this_run
    _calls_made_this_run += 1


def sync_account_quota() -> int | None:
    """Fetch remaining searches and this month's real usage from the SerpAPI
    account endpoint.

    Called once per process (on startup) to seed ``_searches_left`` and
    ``_this_month_usage``.  Returns the number of plan searches remaining, or
    None on failure.  This call does **not** consume a search credit.
    """
    global _searches_left, _this_month_usage
    url = "https://serpapi.com/account.json"
    try:
        resp = requests.get(url, params={"api_key": SERPAPI_API_KEY}, timeout=10)
        if resp.status_code != 200:
            log(f"  Account sync failed (HTTP {resp.status_code})")
            return None
        data = resp.json()
        usage = data.get("this_month_usage")
        if usage is not None:
            _this_month_usage = int(usage)
        left = data.get("plan_searches_left")
        if left is None:
            left = data.get("searches_left")
        if left is None:
            total = data.get("total_searches_left")
            if total is not None:
                left = total
        if left is not None:
            _searches_left = int(left)
            usage_str = f", {_this_month_usage} used this month" if _this_month_usage is not None else ""
            log(f"SerpAPI account synced: {_searches_left} searches remaining{usage_str}")
            return _searches_left
        log(f"  Account sync: could not find searches_left in response")
        return None
    except (requests.RequestException, ValueError, KeyError) as e:
        log(f"  Account sync failed: {e}")
        return None


def decrement_searches_left() -> None:
    """Decrement the local remaining-searches counter after a search call."""
    global _searches_left
    if _searches_left is not None:
        _searches_left -= 1


def log_searches_left() -> str:
    """Return a short string showing remaining SerpAPI searches, for log lines."""
    if _searches_left is not None:
        return f" [{_searches_left} left on plan]"
    return ""


# ---------------------------------------------------------------------------
# SerpAPI Google Flights
# ---------------------------------------------------------------------------


def _has_us_layover(flight: dict) -> bool:
    """True if any layover (connection) airport is a known US hub (see US_HUBS).

    Only connection airports are checked — the origin and destination are ignored,
    so a US-bound or US-origin route isn't excluded, only ones that *transit* the US.
    """
    return any(
        (lo.get("id") or "").upper() in US_HUBS
        for lo in flight.get("layovers", [])
    )


def _dedup_airlines(flight: dict) -> list[str]:
    """Ordered, deduplicated list of airline names across a flight's segments."""
    airlines: list[str] = []
    for seg in flight.get("flights", []):
        airline = seg.get("airline")
        if airline and airline not in airlines:
            airlines.append(airline)
    return airlines


def _extract_details(flight: dict) -> dict:
    """Pull human-readable details from a SerpAPI flight option."""
    segments = flight.get("flights", [])
    layovers = flight.get("layovers", [])
    airlines = _dedup_airlines(flight)
    flight_numbers: list[str] = []
    for seg in segments:
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
    return {
        "price": float(flight["price"]),
        "airlines": _dedup_airlines(flight),
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


def _maybe_alert_quota(label: str, status: int | None, message: str) -> bool:
    """If an API failure looks like exhausted SerpAPI searches, alert via Telegram.

    Returns True when the failure was identified as a quota/credits problem.
    HTTP 429 or a message mentioning running out of / exceeding searches both count.
    Only the first such failure per process sends an alert (see ``_QUOTA_ALERTED``)
    so a fully-exhausted account doesn't fire one Telegram message per route.
    """
    global _QUOTA_ALERTED
    text = (message or "").lower()
    is_quota = status == 429 or any(
        kw in text
        for kw in (
            "run out of searches",
            "ran out of searches",
            "out of searches",
            "searches per month",
            "exceeded your",
            "run out of",
            "upgrade your plan",
        )
    )
    if not is_quota:
        return False
    if not _QUOTA_ALERTED:
        _QUOTA_ALERTED = True
        send_telegram(
            "🚨 *FareMonkey: SerpAPI searches exhausted*\n"
            f"Could not check *{label}* — your SerpAPI plan appears to be out of "
            f"searches.\n`{(message or '').strip()[:300]}`"
        )
    return True


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
    # A search credit is consumed once the API returns, regardless of status.
    decrement_searches_left()
    record_call()
    if resp.status_code != 200:
        log(f"  API error {resp.status_code}: {resp.text[:200]}{log_searches_left()}")
        label = f"{route['origin']}-{route['destination']} {route['departure_date']}"
        _maybe_alert_quota(label, resp.status_code, resp.text)
        return None

    try:
        data = resp.json()
    except ValueError as e:  # 200 OK but body isn't JSON (e.g. an HTML error page)
        log(f"  Invalid JSON from API: {e}")
        return None
    archive_response(route, params, data)
    if data.get("error"):
        log(f"  API error: {data['error']}")
        label = f"{route['origin']}-{route['destination']} {route['departure_date']}"
        _maybe_alert_quota(label, resp.status_code, str(data["error"]))
        return None

    candidates = [
        flight
        for key in ("best_flights", "other_flights")
        for flight in data.get(key, [])
        if flight.get("price") is not None
    ]
    if not candidates:
        return None

    if EXCLUDE_US_CONNECTIONS:
        kept = [f for f in candidates if not _has_us_layover(f)]
        dropped = len(candidates) - len(kept)
        if dropped:
            log(f"  Excluded {dropped} itinerary(ies) connecting through a US airport.")
        candidates = kept
        if not candidates:
            log("  No itineraries left after excluding US connections.")
            return None

    # SerpAPI's "stops" param only distinguishes nonstop vs. any — cap at one
    # stop client-side since 2+ stop itineraries are never wanted.
    kept = [f for f in candidates if len(f.get("layovers", [])) <= 1]
    dropped = len(candidates) - len(kept)
    if dropped:
        log(f"  Excluded {dropped} itinerary(ies) with 2+ stops.")
    candidates = kept
    if not candidates:
        log("  No itineraries left after excluding 2+ stop options.")
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


def _format_price(amount: float) -> str:
    """Format price with comma grouping; drop decimals when whole."""
    return f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"


def _overnight(dep_raw: str | None, arr_raw: str | None) -> str:
    """Return ' (+1)' / ' (+2)' etc. when arrival is a later calendar day."""
    if not dep_raw or not arr_raw:
        return ""
    try:
        dep = datetime.strptime(dep_raw, "%Y-%m-%d %H:%M")
        arr = datetime.strptime(arr_raw, "%Y-%m-%d %H:%M")
        diff = (arr.date() - dep.date()).days
        return f" (+{diff})" if diff > 0 else ""
    except ValueError:
        return ""


def _format_date(iso: str) -> str:
    """'2026-12-30' → 'Dec 30'."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %d")
    except ValueError:
        return iso


def format_offer(offer: dict) -> str:
    """One-line summary for console logging."""
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


def format_telegram(route: dict, offer: dict, icon: str,
                    pct_change: float | None) -> str:
    """Build the full Telegram alert message for a route check."""
    price = offer["price"]
    adults = route.get("adults", 1)
    header = f"{icon} {route['origin']} → {route['destination']} ({adults} pax)"

    # Price + change + flight summary — all on one line
    price_str = f"{CURRENCY} {_format_price(price)}"
    if pct_change is not None and pct_change != 0:
        arrow = "↓" if pct_change < 0 else "↑"
        price_str += f" ({arrow}{abs(pct_change):.1f}%)"
    parts = [price_str]
    level = offer.get("price_level")
    if level:
        parts.append(f"{level} vs typical")
    airlines = ", ".join(offer.get("airlines") or []) or "?"
    parts.append(airlines)
    stops = offer.get("stops", 0)
    layovers = offer.get("layover_airports") or []
    if stops == 0:
        parts.append("nonstop")
    elif layovers:
        parts.append(f"{stops} stop {'→'.join(layovers)}")
    else:
        parts.append(f"{stops} stop{'s' if stops > 1 else ''}")
    dur = offer.get("total_duration")
    if dur:
        parts.append(f"{dur // 60}h {dur % 60:02d}m")
    summary = " · ".join(parts)

    # Outbound itinerary
    dep_raw = offer.get("departure_time")
    arr_raw = offer.get("arrival_time")
    if dep_raw and arr_raw:
        dep_date = _format_date(dep_raw[:10])
        dep_time = dep_raw[11:]
        arr_time = arr_raw[11:]
        plus = _overnight(dep_raw, arr_raw)
        outbound = f"Outbound: {dep_date} | {dep_time} → {arr_time}{plus}"
    else:
        outbound = None

    # Inbound (return date is known but flight times require a second search)
    ret_date = route.get("return_date")
    if ret_date:
        inbound = f"Inbound: {_format_date(ret_date)} | flight times not available"
    else:
        inbound = None

    lines = [header, summary]
    if outbound or inbound:
        lines.append("")
        if outbound:
            lines.append(outbound)
        if inbound:
            lines.append(inbound)
    return "\n".join(lines)


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

    On-demand only (not part of the scheduled cron): each date in the window costs
    one SerpAPI search, so a 7-day window for one route spends 7 searches. The
    monthly cap is still enforced. Round trips shift the return date by the same
    offset so the trip length stays constant.
    """
    if not SERPAPI_API_KEY:
        sys.exit("Error: SERPAPI_API_KEY must be set.")

    sync_account_quota()
    routes = load_routes()

    state = load_json(STATE_FILE)
    offsets = list(range(-days, days + 1))
    needed = len(routes) * len(offsets)
    if not can_make_calls(needed):
        usage = current_usage()
        if usage is None:
            sys.exit(
                "Could not verify real SerpAPI usage (account sync failed). "
                "Refusing to scan without a reliable usage count."
            )
        sys.exit(
            f"Monthly cap would be exceeded ({usage}/{MONTHLY_CALL_CAP} real SerpAPI searches used). "
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
            log(f"  {dep.isoformat()}: {price_str}{marker}{log_searches_left()}")

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
    usage = current_usage()
    log(f"Done. API calls this month: {usage if usage is not None else 'unknown'}/{MONTHLY_CALL_CAP}{log_searches_left()}")


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

    sync_account_quota()

    if not is_within_active_hours():
        log(f"Outside active hours ({ACTIVE_START}:00–{ACTIVE_END}:00 {TIMEZONE}). Skipping.")
        return

    all_routes = load_routes()

    # Some routes opt into a reduced schedule via "run_hours"; skip the ones that
    # aren't scheduled for this firing so they don't spend a search.
    this_hour = current_local_time().hour
    routes = [r for r in all_routes if route_runs_this_hour(r, this_hour)]
    skipped = len(all_routes) - len(routes)
    if skipped:
        log(f"Skipping {skipped} route(s) not scheduled for the {this_hour}:00 firing.")
    if not routes:
        log("No routes scheduled for this firing. Nothing to do.")
        return

    state = load_json(STATE_FILE)

    # 1 search call per route (SerpAPI uses a single API key, no token call)
    calls_needed = len(routes)
    if not can_make_calls(calls_needed):
        usage = current_usage()
        if usage is None:
            log(
                "Could not verify real SerpAPI usage (account sync failed). "
                "Skipping this run rather than risk exceeding the cap."
            )
            return
        log(
            f"Monthly cap would be exceeded ({usage}/{MONTHLY_CALL_CAP} real SerpAPI searches used). "
            f"Need {calls_needed} calls. Skipping."
        )
        # Notify once per month so you know checks have paused, without spamming on
        # every subsequent run until the counter resets.
        if state.get("cap_alert_month") != month_key():
            state["cap_alert_month"] = month_key()
            send_telegram(
                "⛔ *FareMonkey: monthly search cap reached*\n"
                f"{usage}/{MONTHLY_CALL_CAP} real SerpAPI searches used this month "
                f"({month_key()}). Price checks are paused until next month or until "
                "you raise `MONTHLY_CALL_CAP`."
            )
        save_json(STATE_FILE, state)
        return

    prices = state.setdefault("prices", {})
    now_str = current_local_time().isoformat()

    for route in routes:
        label = f"{route['origin']}-{route['destination']} {route['departure_date']}"
        try:
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
            log(f"  Current: {CURRENCY} {price:.2f}" + (f" | Previous: {CURRENCY} {prev:.2f}" if prev else "") + log_searches_left())
            log(f"  {format_offer(offer)}")

            pct_change = ((price - prev) / prev) * 100 if (prev is not None and prev > 0) else None
            significant = pct_change is not None and abs(pct_change) >= ALERT_THRESHOLD_PCT

            if pct_change is None:
                icon = "🐒"
                log("  First check — baseline recorded.")
            elif pct_change <= -ALERT_THRESHOLD_PCT:
                icon = "✈️"
            elif pct_change >= ALERT_THRESHOLD_PCT:
                icon = "⚠️"
            elif pct_change == 0:
                icon = "➡️"
            else:
                icon = "🔹"

            if NOTIFY_EVERY_RUN or significant:
                send_telegram(format_telegram(route, offer, icon, pct_change))
        except Exception as e:
            log(f"  Error processing {label}: {e}")
            continue

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
    usage = current_usage()
    log(f"Done. API calls this month: {usage if usage is not None else 'unknown'}/{MONTHLY_CALL_CAP}{log_searches_left()}")


if __name__ == "__main__":
    main()
