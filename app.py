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
        run_hours = route.get("run_hours")
        if run_hours not in (None, ""):
            if not isinstance(run_hours, list) or not all(
                isinstance(h, int) and 0 <= h <= 23 for h in run_hours
            ):
                errors.append(f"Route {i + 1}: run_hours must be a list of hours (0-23)")
    return errors


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
    if route.get("run_hours"):
        out["run_hours"] = sorted(set(route["run_hours"]))
    return out


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
    return jsonify({"ok": True, "routes": normalized})


# ---------------------------------------------------------------------------
# Cron schedule editor
#
# Publishes to *this host's* crontab — the Flask app may itself be running on
# the machine that runs the scheduled monitor, so "publish" always means "the
# crontab of whatever machine app.py is currently running on."
# ---------------------------------------------------------------------------

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


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


@app.route("/api/schedule", methods=["GET"])
def api_schedule_get():
    return jsonify({"times": current_schedule(), "preview": build_cron_block(current_schedule() or ["07:30", "13:30", "19:30"])})


@app.route("/api/schedule", methods=["POST"])
def api_schedule_publish():
    payload = request.get_json(silent=True) or {}
    times = payload.get("times")
    if not isinstance(times, list) or not times:
        return jsonify({"errors": ["times must be a non-empty array of \"HH:MM\" strings"]}), 400
    bad = [t for t in times if not isinstance(t, str) or not TIME_RE.match(t)]
    if bad:
        return jsonify({"errors": [f"Invalid time(s): {', '.join(bad)} — use 24h HH:MM"]}), 400

    normalized = sorted({f"{int(h):02d}:{int(m):02d}" for h, m in (t.split(":") for t in times)})
    try:
        publish_schedule(normalized)
    except (OSError, RuntimeError) as e:
        app.logger.error("Failed to publish crontab", exc_info=True)
        return jsonify({"errors": [f"Could not install crontab: {e}"]}), 500

    return jsonify({"ok": True, "times": normalized, "preview": build_cron_block(normalized)})


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
