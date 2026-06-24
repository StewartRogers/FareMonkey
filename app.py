#!/usr/bin/env python3
"""FareMonkey – Flask dashboard for flight price history."""

import json
import os
from pathlib import Path

from flask import Flask, render_template

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv is optional; cron/CI inject env vars directly

app = Flask(__name__)

STATE_FILE = Path(__file__).parent / "state.json"
ROUTES_FILE = Path(__file__).parent / "routes.json"
CURRENCY = os.environ.get("CURRENCY", "USD")


def load_json(path: Path):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


@app.route("/")
def dashboard():
    state = load_json(STATE_FILE)
    routes = load_json(ROUTES_FILE)
    prices = state.get("prices", {})
    api_calls = state.get("api_calls", {})
    last_run = state.get("last_run")

    route_data = []
    for label, info in prices.items():
        history = info.get("history", [])
        timestamps = [h["timestamp"] for h in history]
        price_values = [h["price"] for h in history]

        prev = price_values[-2] if len(price_values) >= 2 else None
        current = info.get("price")
        pct_change = None
        if prev is not None and current is not None and prev > 0:
            pct_change = round(((current - prev) / prev) * 100, 1)

        route_data.append({
            "label": label,
            "current_price": current,
            "previous_price": prev,
            "pct_change": pct_change,
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
        monthly_cap=int(os.environ.get("MONTHLY_CALL_CAP", "240")),
    )


@app.route("/api/state")
def api_state():
    return load_json(STATE_FILE)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
