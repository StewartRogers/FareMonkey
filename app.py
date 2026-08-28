#!/usr/bin/env python3
"""FareMonkey – Flask dashboard for flight price history."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import zoneinfo
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# route_label()/route_search_cost() must stay identical to flight_monitor.py's
# versions — they derive the exact state.json keys the monitor writes and the
# dashboard reads, and the real per-route SerpAPI cost the schedule page and
# MONTHLY_CALL_CAP pre-checks rely on. Import rather than re-implement so the
# two processes can't silently drift out of sync.
from flight_monitor import _is_legs_route, route_label, route_search_cost, state_lock  # noqa: F401

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv is optional; cron/CI inject env vars directly

app = Flask(__name__)
# The dashboard is LAN-reachable with no auth (see CLAUDE.md); cap request
# bodies so a stray or malformed POST to /api/routes can't buffer an
# arbitrarily large body into memory before validation even runs.
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # 1 MB

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
ROUTES_FILE = BASE_DIR / "routes.json"
MONITOR_SCRIPT = BASE_DIR / "flight_monitor.py"
CURRENCY = os.environ.get("CURRENCY", "USD")
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")

TRAVEL_CLASSES = ("ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST")
REQUIRED_ROUTE_FIELDS = ("origin", "destination", "departure_date")
REQUIRED_LEG_FIELDS = ("origin", "destination", "date")
IATA_RE = re.compile(r"^[A-Z]{3}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Used only when no route declares a run_time and the host has no FareMonkey
# crontab block yet — the schedule documented in README/CLAUDE.md.
DEFAULT_TIMES = ("07:30", "13:30", "19:30")

# Cron lines this app manages are wrapped in these markers so publishing a new
# schedule only touches FareMonkey's own block, leaving any other crontab
# entries the user has untouched.
CRON_BEGIN = "# BEGIN FAREMONKEY"
CRON_END = "# END FAREMONKEY"


def load_json(path: Path):
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (ValueError, OSError):
            app.logger.error("Failed to load %s; dashboard will render as empty", path, exc_info=True)
            return {}
    return {}


def save_json_atomic(path: Path, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def validate_routes(routes) -> list[str]:
    """Return a list of human-readable validation errors, empty if routes is valid."""
    errors = []
    if not isinstance(routes, list) or not routes:
        return ["routes must be a non-empty array"]
    for i, route in enumerate(routes):
        if not isinstance(route, dict):
            errors.append(f"Route {i + 1}: must be an object")
            continue
        if "legs" in route:
            # Multi-leg (multi-city) route: validation here is deliberately loose (no
            # IATA/date format check per leg, unlike the simple-route fields below) so
            # a hand-edited routes.json keeps working — just enough to keep an
            # obviously-malformed entry from being silently saved. The /routes editor
            # itself uppercases/shapes each leg client-side before posting, and now
            # escapes every field before re-rendering it, so a leg value that fails
            # this loose check still can't inject HTML into that page.
            legs = route.get("legs")
            if not isinstance(legs, list) or len(legs) < 2:
                errors.append(f"Route {i + 1}: 'legs' must be a list of at least 2 legs")
            else:
                for j, leg in enumerate(legs):
                    missing = [
                        f for f in REQUIRED_LEG_FIELDS
                        if not (
                            isinstance(leg, dict)
                            and isinstance(leg.get(f), str)
                            and leg.get(f).strip()
                        )
                    ]
                    if missing:
                        errors.append(
                            f"Route {i + 1} leg {j + 1}: missing required field(s): "
                            f"{', '.join(missing)}"
                        )
        else:
            for field in REQUIRED_ROUTE_FIELDS:
                if not route.get(field):
                    errors.append(f"Route {i + 1}: missing required field '{field}'")
            origin = route.get("origin")
            if origin and not IATA_RE.match(str(origin).upper()):
                errors.append(f"Route {i + 1}: origin must be a 3-letter IATA code")
            destination = route.get("destination")
            if destination and not IATA_RE.match(str(destination).upper()):
                errors.append(f"Route {i + 1}: destination must be a 3-letter IATA code")
            for date_field in ("departure_date", "return_date"):
                value = route.get(date_field)
                if value and not DATE_RE.match(str(value)):
                    errors.append(f"Route {i + 1}: {date_field} must be an ISO date (YYYY-MM-DD)")
        if "adults" in route and route["adults"] not in (None, ""):
            try:
                if int(route["adults"]) < 1:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"Route {i + 1}: adults must be a positive integer")
        if "teens" in route and route["teens"] not in (None, ""):
            try:
                if int(route["teens"]) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"Route {i + 1}: teens must be a non-negative integer")
        if "max_duration_hours" in route and route["max_duration_hours"] not in (None, ""):
            try:
                duration = float(route["max_duration_hours"])
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"Route {i + 1}: max_duration_hours must be a positive number")
        if "non_stop" in route and not isinstance(route["non_stop"], bool):
            errors.append(f"Route {i + 1}: non_stop must be true or false")
        travel_class = route.get("travel_class")
        if travel_class and travel_class not in TRAVEL_CLASSES:
            errors.append(f"Route {i + 1}: travel_class must be one of {', '.join(TRAVEL_CLASSES)}")
        run_times = route.get("run_times")
        if run_times not in (None, "", []):
            if not isinstance(run_times, list) or not all(
                isinstance(t, str) and TIME_RE.match(t.strip()) for t in run_times
            ):
                errors.append(
                    f"Route {i + 1}: run_times must be a list of 24h \"HH:MM\" times"
                )
        run_hours = route.get("run_hours")
        if run_hours not in (None, ""):
            if not isinstance(run_hours, list) or not all(
                isinstance(h, int) and 0 <= h <= 23 for h in run_hours
            ):
                errors.append(f"Route {i + 1}: run_hours must be a list of hours (0-23)")
    return errors


def active_window() -> tuple[int, int]:
    try:
        start = int(os.environ.get("ACTIVE_START", "7"))
        end = int(os.environ.get("ACTIVE_END", "22"))
    except ValueError:
        return 7, 22
    return start, end


def outside_active_hours(times) -> list[str]:
    """Return the times the monitor would self-skip as outside its active window.

    A firing outside ACTIVE_START..ACTIVE_END installs fine in cron and then does
    nothing, which is the silent failure this check exists to surface.
    """
    start, end = active_window()
    return [t for t in times if not (start <= int(t.split(":")[0]) < end)]


def normalize_route(route: dict) -> dict:
    """Coerce a validated route dict into the canonical shape/types for routes.json."""
    if "legs" in route:
        # The /routes editor already uppercases/shapes each leg client-side
        # before posting — pass legs through as given rather than re-validating
        # per-leg IATA/date formatting server-side (kept loose, like the rest
        # of this legs branch, since routes.json can also be hand-edited).
        out = {"legs": route.get("legs")}
    else:
        out = {
            "origin": str(route["origin"]).upper(),
            "destination": str(route["destination"]).upper(),
            "departure_date": route["departure_date"],
        }
        if route.get("return_date"):
            out["return_date"] = route["return_date"]
    if route.get("adults") not in (None, ""):
        out["adults"] = int(route["adults"])
    if route.get("teens") not in (None, ""):
        out["teens"] = int(route["teens"])
    if route.get("max_duration_hours") not in (None, ""):
        out["max_duration_hours"] = float(route["max_duration_hours"])
    if "non_stop" in route:
        out["non_stop"] = bool(route["non_stop"])
    if route.get("travel_class"):
        out["travel_class"] = route["travel_class"]
    if route.get("run_times"):
        out["run_times"] = sorted({normalize_time(t) for t in route["run_times"]})
    elif route.get("run_hours"):
        # Legacy filter-only field: kept as-is so a hand-edited routes.json keeps
        # working. flight_monitor.route_runs_at() still honours it.
        out["run_hours"] = sorted(set(route["run_hours"]))
    return out


def normalize_time(value: str) -> str:
    hour, minute = value.strip().split(":")
    return f"{int(hour):02d}:{int(minute):02d}"


def current_month_key() -> str:
    """Mirrors flight_monitor.month_key(): the "YYYY-MM" api_calls is keyed by."""
    return datetime.now(zoneinfo.ZoneInfo(TIMEZONE)).strftime("%Y-%m")


@app.route("/")
def dashboard():
    state = load_json(STATE_FILE)
    prices = state.get("prices", {})
    api_calls = state.get("api_calls", {})
    last_run = state.get("last_run")
    flex_scans = state.get("flex_scans", {})

    route_data = []
    for label, info in prices.items():
        history = info.get("history", [])
        timestamps = [h["timestamp"] for h in history]
        price_values = [h["price"] for h in history]

        # Use the exact previous price the monitor compared against when it
        # last alerted, rather than re-deriving it from history — retention
        # trimming can prune history independently of what was compared at
        # alert time, so the two would otherwise drift apart.
        prev = info.get("previous_price")
        current = info.get("price")
        pct_change = None
        if prev is not None and current is not None and prev > 0:
            pct_change = round(((current - prev) / prev) * 100, 1)

        cheapest_ever = min(price_values) if price_values else None
        average = round(sum(price_values) / len(price_values), 2) if price_values else None

        route_data.append({
            "label": label,
            "current_price": current,
            "previous_price": prev,
            "pct_change": pct_change,
            "cheapest_ever": cheapest_ever,
            "average": average,
            "timestamps": timestamps,
            "prices": price_values,
            "checks": len(history),
            "details": info.get("details"),
        })

    # Prefer SerpAPI's own real usage (persisted by flight_monitor.py's
    # sync_and_persist_account_quota(), free to refresh) over the locally-counted
    # api_calls, which only sees searches made through this script and drifts
    # from the account's actual usage (e.g. calls made via the SerpAPI dashboard
    # directly). Only trust it if it was synced this month — a stale prior-month
    # sync (e.g. the monitor hasn't run yet this month) would understate usage.
    month_key = current_month_key()
    account_usage = state.get("account_usage") or {}
    real_usage = account_usage.get("this_month_usage")
    synced_this_month = (account_usage.get("synced") or "").startswith(month_key)
    if real_usage is not None and synced_this_month:
        total_calls = real_usage
        usage_is_real = True
    else:
        total_calls = api_calls.get(month_key, 0)
        usage_is_real = False

    return render_template(
        "dashboard.html",
        routes=route_data,
        currency=CURRENCY,
        usage_is_real=usage_is_real,
        api_calls=api_calls,
        total_calls=total_calls,
        last_run=last_run,
        flex_scans=flex_scans,
        monthly_cap=int(os.environ.get("MONTHLY_CALL_CAP", "240")),
    )


@app.route("/api/state")
def api_state():
    return load_json(STATE_FILE)


@app.route("/api/routes/<path:label>", methods=["DELETE"])
def api_route_data_delete(label):
    """Delete a route's price history/chart data (and any flex scan) from state.json.

    This is the one place app.py writes state.json (see CLAUDE.md) — it's a
    narrow, user-initiated removal of a single route label, not a
    read-modify-write of prices the monitor is actively maintaining.
    """
    with state_lock():
        state = load_json(STATE_FILE)
        prices = state.get("prices", {})
        flex_scans = state.get("flex_scans", {})
        if label not in prices and label not in flex_scans:
            return jsonify({"errors": [f"No data found for '{label}'"]}), 404
        prices.pop(label, None)
        flex_scans.pop(label, None)
        save_json_atomic(STATE_FILE, state)
    return jsonify({"ok": True})


@app.route("/routes")
def routes_editor():
    return render_template("routes_editor.html", travel_classes=TRAVEL_CLASSES)


@app.route("/api/routes", methods=["GET"])
def api_routes_get():
    routes = load_json(ROUTES_FILE)
    if not isinstance(routes, list):
        routes = []
    return jsonify(routes)


@app.route("/api/routes", methods=["POST"])
def api_routes_save():
    payload = request.get_json(silent=True)
    if payload is None or not isinstance(payload, list):
        return jsonify({"errors": ["Request body must be a JSON array of routes"]}), 400

    errors = validate_routes(payload)
    if errors:
        return jsonify({"errors": errors}), 400

    normalized = [normalize_route(r) for r in payload]
    save_json_atomic(ROUTES_FILE, normalized)

    # Saving a route changes the schedule the crontab is derived from, so hand
    # back the recomputed plan (and anything the monitor would silently skip)
    # instead of making the user go and re-read the schedule page to find out.
    plan = schedule_plan(normalized)
    published = current_schedule()
    warnings = []
    if plan["outside_active_hours"]:
        warnings.append(
            "These run times are outside active hours "
            f"({plan['active_start']}:00–{plan['active_end']}:00) and would be skipped: "
            + ", ".join(plan["outside_active_hours"])
        )
    if plan["searches_per_month"] > plan["monthly_cap"]:
        warnings.append(
            f"This schedule needs about {plan['searches_per_month']} searches/month, "
            f"over the {plan['monthly_cap']} cap."
        )
    if published != plan["times"]:
        warnings.append("The published crontab no longer matches these routes — publish it on the Schedule page.")
    return jsonify({
        "ok": True,
        "routes": normalized,
        "schedule": schedule_response(plan, published),
        "warnings": warnings,
    })


# ---------------------------------------------------------------------------
# Cron schedule
#
# Routes are the single source of truth for *when* the monitor runs: each route
# carries its own run_times, and the crontab block is the union of them. The
# schedule page is a derived view of that union plus the Publish button — it has
# no times of its own to get out of step with routes.json.
#
# Publishing writes to *this host's* crontab — the Flask app may itself be
# running on the machine that runs the scheduled monitor, so "publish" always
# means "the crontab of whatever machine app.py is currently running on."
# ---------------------------------------------------------------------------


def route_times(route: dict) -> list[str]:
    """The clock times a route declares, normalized. Empty means 'every firing'."""
    times = route.get("run_times")
    if isinstance(times, list):
        return sorted({
            normalize_time(t) for t in times
            if isinstance(t, str) and TIME_RE.match(t.strip())
        })
    # A legacy run_hours route declares no times: that field can only filter
    # firings, never create one, so it contributes nothing to the union.
    return []


def hours_as_times(hours, published) -> list[str]:
    """Turn legacy run_hours into concrete times.

    An hour that matches a published firing keeps that firing's minutes (so 7 on a
    7:30 crontab stays 07:30 rather than becoming a second, useless 07:00 line);
    anything else lands on the hour.
    """
    times = []
    for h in hours:
        match = next((t for t in published if int(t.split(":")[0]) == h), None)
        times.append(match or f"{int(h):02d}:00")
    return sorted(set(times))


def schedule_plan(routes=None) -> dict:
    """Derive the whole schedule from routes.json.

    Returns the union of run_times (the crontab to publish), which routes fire at
    each time, the routes with no schedule of their own (they run at *every*
    time), the resulting search budget, and any times the monitor would skip as
    outside active hours.
    """
    if routes is None:
        routes = load_json(ROUTES_FILE)
    if not isinstance(routes, list):
        routes = []

    published = current_schedule()

    scheduled, unscheduled, legacy = {}, [], []
    cost_by_label = {}
    leg_chains = {}
    for route in routes:
        if not isinstance(route, dict):
            continue
        label = route_label(route)
        cost_by_label[label] = route_search_cost(route)
        legs = route.get("legs")
        if isinstance(legs, list) and legs:
            # route_label() joins the chain as if each leg picks up where the last
            # left off (e.g. "YVR-HEL-BER-YVR"), which reads fine for a closed loop
            # but hides a gap when legs aren't contiguous (e.g. a leg skipped to cut
            # cost). Spell out each leg on its own so that's never ambiguous.
            leg_chains[label] = [
                f"{(leg or {}).get('origin', '?')}-{(leg or {}).get('destination', '?')}"
                for leg in legs
            ]
        times = route_times(route)
        if times:
            scheduled[label] = times
        elif isinstance(route.get("run_hours"), list):
            hours = sorted(set(route["run_hours"]))
            legacy.append({"label": label, "run_hours": hours, "as_times": hours_as_times(hours, published)})
        else:
            unscheduled.append(label)

    union = sorted(
        {t for times in scheduled.values() for t in times}
        # A not-yet-migrated run_hours route declares no times, so leaving it out
        # of the union would publish a crontab that drops the firings it currently
        # relies on. Map its hours onto real times instead, so publishing before
        # migrating preserves the schedule rather than silently killing the route.
        | {t for entry in legacy for t in entry["as_times"]}
    )
    if not union:
        # Nothing declares a time yet: keep whatever is already published, or fall
        # back to the documented default so the page has something to show.
        union = published or list(DEFAULT_TIMES)

    by_time = {}
    for t in union:
        at_this_time = [label for label, times in scheduled.items() if t in times]
        # A route with no run_times of its own runs on every firing, and a legacy
        # run_hours route runs on the firings whose hour it lists.
        at_this_time += unscheduled
        at_this_time += [
            entry["label"] for entry in legacy if int(t.split(":")[0]) in entry["run_hours"]
        ]
        by_time[t] = sorted(at_this_time)

    searches_per_day = sum(
        cost_by_label.get(label, 1) for labels in by_time.values() for label in labels
    )
    start, end = active_window()
    return {
        "times": union,
        "by_time": by_time,
        "leg_chains": leg_chains,
        "unscheduled": sorted(unscheduled),
        "legacy": legacy,
        "searches_per_day": searches_per_day,
        "searches_per_month": searches_per_day * 30,
        "monthly_cap": int(os.environ.get("MONTHLY_CALL_CAP", "240")),
        "outside_active_hours": outside_active_hours(union),
        "active_start": start,
        "active_end": end,
    }


def read_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return ""  # no crontab installed for this user yet
    return result.stdout


def _expand_cron_field(field: str) -> list[int] | None:
    """Expand a comma-separated cron field of plain integers (e.g. "7,13,19")
    into individual values. Returns None if the field isn't plain digits/commas
    (a range or step like "1-5" or "*/6" isn't something this app ever writes
    or accepts from /schedule, so such a line is left alone, same as before).
    """
    parts = field.split(",")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return [int(p) for p in parts]


def current_schedule() -> list[str]:
    """Extract HH:MM times from the FareMonkey-managed crontab block, if any."""
    body = read_crontab()
    if CRON_BEGIN not in body:
        return []
    block = body.split(CRON_BEGIN, 1)[1].split(CRON_END, 1)[0]
    times = []
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        # A hand-installed line following CLAUDE.md's documented default
        # ("30 7,13,19 * * * ...") uses a comma-separated hour list rather
        # than one line per time — expand both fields so that's recognized too.
        minutes = _expand_cron_field(parts[0])
        hours = _expand_cron_field(parts[1])
        if minutes and hours:
            for h in hours:
                for m in minutes:
                    times.append(f"{h:02d}:{m:02d}")
    return sorted(set(times))


def build_cron_block(times: list[str]) -> str:
    python = sys.executable
    lines = [CRON_BEGIN]
    for t in sorted(set(times)):
        hour, minute = t.split(":")
        lines.append(
            f"{int(minute)} {int(hour)} * * * cd {BASE_DIR} && {python} {MONITOR_SCRIPT} "
            f">> /dev/null 2>&1"
        )
    lines.append(CRON_END)
    return "\n".join(lines)


def publish_schedule(times: list[str]) -> None:
    existing = read_crontab()
    if CRON_BEGIN in existing and CRON_END not in existing:
        # Already-malformed crontab (truncated FareMonkey block) — refuse
        # rather than append a second, well-formed block after the broken one.
        raise RuntimeError(
            f"Crontab has a {CRON_BEGIN!r} marker with no matching {CRON_END!r} — "
            "fix or remove the incomplete FareMonkey block by hand before publishing."
        )
    if CRON_BEGIN in existing and CRON_END in existing:
        before = existing.split(CRON_BEGIN, 1)[0]
        after = existing.split(CRON_END, 1)[1]
        new_body = before.rstrip("\n") + "\n" + build_cron_block(times) + "\n" + after.lstrip("\n")
    else:
        sep = "\n" if existing and not existing.endswith("\n") else ""
        new_body = existing + sep + build_cron_block(times) + "\n"
    result = subprocess.run(["crontab", "-"], input=new_body, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "crontab install failed")


@app.route("/schedule")
def schedule_editor():
    return render_template(
        "schedule.html",
        active_start=os.environ.get("ACTIVE_START", "7"),
        active_end=os.environ.get("ACTIVE_END", "22"),
        crontab_available=bool(_which("crontab")),
    )


def _which(cmd: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(d) / cmd
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def schedule_response(plan: dict, published: list[str]) -> dict:
    out = dict(plan)
    out["published"] = published
    out["in_sync"] = published == plan["times"]
    out["preview"] = build_cron_block(plan["times"])
    return out


@app.route("/api/schedule", methods=["GET"])
def api_schedule_get():
    return jsonify(schedule_response(schedule_plan(), current_schedule()))


@app.route("/api/schedule", methods=["POST"])
def api_schedule_publish():
    """Install the schedule derived from routes.json.

    There is deliberately nothing to pass in: the times come from the routes, so
    the only thing this endpoint decides is whether the derived schedule is safe
    to install.
    """
    plan = schedule_plan()
    if not plan["times"]:
        return jsonify({"errors": ["No run times to publish — give at least one route a run time."]}), 400
    if plan["outside_active_hours"]:
        return jsonify({"errors": [
            "Refusing to publish: "
            + ", ".join(plan["outside_active_hours"])
            + f" fall outside active hours ({plan['active_start']}:00–{plan['active_end']}:00), so the "
            "monitor would skip those firings. Change the route times, or widen "
            "ACTIVE_START/ACTIVE_END."
        ]}), 400

    try:
        publish_schedule(plan["times"])
    except (OSError, RuntimeError) as e:
        app.logger.error("Failed to publish crontab", exc_info=True)
        return jsonify({"errors": [f"Could not install crontab: {e}"]}), 500

    response = schedule_response(plan, plan["times"])
    response["ok"] = True
    return jsonify(response)


def _port_available(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _free_port(host: str) -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    host = "0.0.0.0"
    requested_port = int(os.environ.get("PORT", "5000"))
    port = requested_port
    if not _port_available(host, port):
        port = _free_port(host)
        print(f"Port {requested_port} is already in use — using free port {port} instead.")
    app.run(host=host, port=port, debug=debug)
