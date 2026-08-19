#!/usr/bin/env python3
"""FareMonkey – Flask dashboard for flight price history."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv is optional; cron/CI inject env vars directly

app = Flask(__name__)

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
ROUTES_FILE = BASE_DIR / "routes.json"
MONITOR_SCRIPT = BASE_DIR / "flight_monitor.py"
CURRENCY = os.environ.get("CURRENCY", "USD")

TRAVEL_CLASSES = ("ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST")
REQUIRED_ROUTE_FIELDS = ("origin", "destination", "departure_date")
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
    out = {
        "origin": str(route["origin"]).upper(),
        "destination": str(route["destination"]).upper(),
        "departure_date": route["departure_date"],
    }
    if route.get("return_date"):
        out["return_date"] = route["return_date"]
    if route.get("adults") not in (None, ""):
        out["adults"] = int(route["adults"])
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

    total_calls = sum(api_calls.values())

    return render_template(
        "dashboard.html",
        routes=route_data,
        currency=CURRENCY,
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


def route_label(route: dict) -> str:
    """Same label flight_monitor.py uses for state.json keys."""
    return f"{route.get('origin', '?')}-{route.get('destination', '?')} {route.get('departure_date', '?')}"


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
    for route in routes:
        if not isinstance(route, dict):
            continue
        label = route_label(route)
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

    searches_per_day = sum(len(labels) for labels in by_time.values())
    start, end = active_window()
    return {
        "times": union,
        "by_time": by_time,
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
        minute, hour = parts[0], parts[1]
        if minute.isdigit() and hour.isdigit():
            times.append(f"{int(hour):02d}:{int(minute):02d}")
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
