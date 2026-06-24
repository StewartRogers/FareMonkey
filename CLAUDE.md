# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

FareMonkey is a Python-based flight price monitor with a web dashboard. It queries the SerpAPI Google Flights API for the cheapest fares on configured routes, compares prices to previously recorded values, sends Telegram alerts when prices change beyond a configurable threshold, and stores full price history for visualization in a Flask dashboard. It runs every 6 hours via local cron or GitHub Actions.

## Tech stack

- **Language**: Python 3.9+ (uses `from __future__ import annotations` so `X | None` type hints work on 3.9/3.10; CI runs 3.12). Deployed on Raspberry Pi OS Python 3.9.2.
- **Web framework**: Flask (dashboard only)
- **Charting**: Chart.js v4 (CDN, no build step)
- **Dependencies**: `flask`, `requests`, `tzdata`, `python-dotenv` (see `requirements.txt`). Both entry points auto-load a local `.env` via `python-dotenv` if installed; it's optional (cron/CI inject env vars directly).
- **CI/CD**: GitHub Actions (`.github/workflows/monitor.yml`)
- **External APIs**: SerpAPI Google Flights (`engine=google_flights`), Telegram Bot API

## Repository structure

```
FareMonkey/
├── flight_monitor.py          # Price monitor script (runs via cron)
├── app.py                     # Flask web dashboard
├── templates/
│   └── dashboard.html         # Dashboard template with Chart.js charts
├── routes.json                # Route definitions to monitor (user-edited)
├── state.json                 # Auto-generated: prices, history, API call counts
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
├── .github/workflows/
│   └── monitor.yml            # Scheduled cron workflow (every 6 hours)
├── CLAUDE.md                  # This file
├── README.md                  # User-facing documentation
├── LICENSE                    # MIT license
└── .gitignore                 # Python-focused gitignore
```

## Key files

- **`flight_monitor.py`**: Monitor script. Reads config from env vars, loads routes from `routes.json`, queries SerpAPI Google Flights (single API key, no OAuth) for the cheapest flights, compares against `state.json`, sends Telegram alerts on significant price changes, appends to price history, and tracks API call counts per month. Maps `routes.json` fields to SerpAPI params: `travel_class` strings → integer codes (`TRAVEL_CLASS_MAP`), `non_stop` → `stops` (1 = nonstop only, 0 = any), and `return_date` presence → `type` (1 = round trip, 2 = one way). Takes the minimum price across `best_flights` and `other_flights`.
- **`app.py`**: Flask app serving the dashboard at `http://localhost:5000`. Reads `state.json` on each request. Also exposes `/api/state` as raw JSON.
- **`templates/dashboard.html`**: Single-page dashboard with dark theme, per-route price charts (Chart.js), percentage-change badges, and API usage bar charts.
- **`routes.json`**: JSON array of route objects. Required: `origin`, `destination`, `departure_date` (IATA codes, ISO dates). Optional: `return_date` (presence makes it a round trip), `adults` (default 1), `non_stop` (default `true` → nonstop only), `travel_class` (`ECONOMY`/`PREMIUM_ECONOMY`/`BUSINESS`/`FIRST`, default `ECONOMY`).
- **`state.json`**: Persisted state including `prices` (keyed by route label `"ORIGIN-DEST DATE"`, each containing `price`, `updated`, a `details` object with the cheapest offer's airlines/stops/duration, and a `history` array), `api_calls` (keyed by `YYYY-MM`), and `last_run` timestamp. Written atomically via a temp file + `os.replace` so a crash mid-write can't corrupt it.

## Data model (state.json)

```json
{
  "prices": {
    "JFK-LHR 2026-09-15": {
      "price": 450.00,
      "updated": "2026-06-20T10:00:00-04:00",
      "details": {"airlines": ["..."], "stops": 0, "total_duration": 420, "...": "..."},
      "history": [
        {"price": 480.00, "timestamp": "2026-06-19T10:00:00-04:00"},
        {"price": 450.00, "timestamp": "2026-06-20T10:00:00-04:00"}
      ]
    }
  },
  "api_calls": {"2026-06": 45},
  "last_run": "2026-06-20T10:00:00-04:00"
}
```

## Environment variables

All configuration is read from environment variables (no hardcoded credentials):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SERPAPI_API_KEY` | Yes | - | SerpAPI API key (single key for Google Flights) |
| `TELEGRAM_BOT_TOKEN` | No | - | Telegram bot token (alerts disabled if unset) |
| `TELEGRAM_CHAT_ID` | No | - | Telegram chat ID |
| `CURRENCY` | No | `USD` | Currency for price queries |
| `TIMEZONE` | No | `America/New_York` | IANA timezone for active-hours check |
| `ACTIVE_START` | No | `7` | Start of active window (hour, local time) |
| `ACTIVE_END` | No | `22` | End of active window (hour, local time) |
| `ALERT_THRESHOLD_PCT` | No | `3` | Min % change to trigger alert |
| `NOTIFY_EVERY_RUN` | No | `true` | Send Telegram message on every run, not just significant changes |
| `MONTHLY_CALL_CAP` | No | `240` | Max SerpAPI searches per calendar month |
| `MAX_HISTORY` | No | `1000` | Max price history entries kept per route |

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env  # then fill in credentials

# Run the monitor once
python flight_monitor.py

# Start the dashboard
python app.py  # http://localhost:5000
```

## Development conventions

- **Two entry points**: `flight_monitor.py` (cron job) and `app.py` (web server). They share `state.json` but are otherwise independent.
- **No build step**: The Flask app uses a Jinja2 template with Chart.js from CDN. No webpack, npm, or frontend toolchain.
- **Config via env vars only**: Never hardcode credentials or API keys. Use `os.environ.get()` with sensible defaults.
- **State file**: `state.json` is the only mutable data store. It must remain JSON-serializable and human-readable. The `history` array grows over time — this is intentional for charting.
- **API call safety**: Always check `can_make_calls()` before making SerpAPI requests. The monthly cap exists to prevent billing — never bypass it.
- **Active hours**: The monitor self-skips outside the configured active window. This is intentional, not a bug.
- **Dashboard is read-only**: `app.py` never writes to `state.json`. Only `flight_monitor.py` writes state.

## Common tasks

### Add a new route
Edit `routes.json`. Each entry needs at minimum `origin`, `destination`, and `departure_date` (IATA codes and ISO date).

### Change alert sensitivity
Set the `ALERT_THRESHOLD_PCT` environment variable. Lower = more alerts.

### Reset API call counter
Delete the current month's entry from `state.json` -> `api_calls`, or delete `state.json` entirely (price history will also reset).

### Run the dashboard in production
Use a WSGI server: `pip install gunicorn && gunicorn app:app -b 0.0.0.0:5000`

## Guardrails

- The `MONTHLY_CALL_CAP` (default 240) leaves a small buffer below the user's 250-search/month SerpAPI plan. Each run costs 1 search per route (no separate token call). Do not raise it above the user's plan limit. The GitHub Actions workflow runs every 6 hours (not hourly) to stay within budget — hourly checks would far exceed 250/month.
- `state.json` is committed by GitHub Actions with `[skip ci]` in the commit message to prevent recursive workflow triggers.
- Credentials are stored as GitHub repository secrets or in `.env` (gitignored), never in code.
- The Flask dashboard binds to `127.0.0.1:5000` with `debug=True` in dev mode (`app.py`). For production or LAN access, use gunicorn behind a reverse proxy (gunicorn's `-b 0.0.0.0:5000` exposes it on all interfaces).
