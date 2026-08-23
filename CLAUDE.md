# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

FareMonkey is a Python-based flight price monitor with a web dashboard. It queries the SerpAPI Google Flights API for the cheapest fares on configured routes, compares prices to previously recorded values, sends Telegram alerts when prices change beyond a configurable threshold, and stores full price history for visualization in a Flask dashboard. It runs 3 times a day (at 7:30, 13:30, 19:30 — 6 hours apart, all within active hours) via local cron. `state.json`, `responses.jsonl`, and `flight_monitor.log` are kept **local only** (gitignored) and are never committed to the repo.

## Tech stack

- **Language**: Python **3.9–3.13** (uses `from __future__ import annotations` so `X | None` type hints work on 3.9/3.10, including the module-level variable annotations in `flight_monitor.py`). Deployed on Raspberry Pi OS Python 3.9.2. `.github/workflows/tests.yml` runs the suite on every version in that matrix, so keep the code within the 3.9 subset: no `match` statements, no `datetime.UTC`, no PEP 604/585 types in runtime-evaluated positions, and no stdlib modules removed in 3.12/3.13 (`distutils`, `cgi`, `telnetlib`, …). Cross-version gotchas already handled: `datetime.fromisoformat()` on 3.9/3.10 only parses `isoformat()` output (the code only ever parses its own timestamps), and `_is_older_than()` catches the `TypeError` from comparing a naive stored timestamp against an aware cutoff.
- **Web framework**: Flask (dashboard only)
- **Charting**: Chart.js v4 (CDN, no build step)
- **Dependencies**: `flask`, `requests`, `tzdata`, `python-dotenv` (see `requirements.txt`). The version specifiers are deliberately open-ended ranges, not pins: pip honours each package's `Requires-Python`, so 3.9 resolves to the last releases supporting it (e.g. `requests` 2.32.5, `python-dotenv` 1.2.1) while 3.10+ gets current ones (`requests` 2.34+ and `python-dotenv` 1.2.2 dropped 3.9). Don't pin exact versions — that breaks one end of the supported range. Both entry points auto-load a local `.env` via `python-dotenv` if installed; it's optional (cron/CI inject env vars directly).
- **Environment**: dependencies live in a project-local venv at `.venv/` (gitignored), created by `setup.sh`. Raspberry Pi OS / Debian mark the system Python "externally managed" (PEP 668) and refuse plain `pip install`; the venv is the supported way around that and keeps this project's packages from shadowing apt-managed ones for every Python process on the box. Never install this project's deps with `pip install --user` or `--break-system-packages`. Always invoke `.venv/bin/python` (or `.venv/bin/pip`) by path — no `activate` needed, which is what lets the cron line work.
- **CI/CD**: GitHub Actions (`.github/workflows/monitor.yml`)
- **External APIs**: SerpAPI Google Flights (`engine=google_flights`), Telegram Bot API

## Repository structure

```
FareMonkey/
├── flight_monitor.py          # Price monitor script (runs via cron)
├── app.py                     # Flask web dashboard
├── templates/
│   ├── dashboard.html         # Dashboard template with Chart.js charts
│   ├── routes_editor.html     # /routes — route editor, incl. per-route run_times
│   └── schedule.html          # /schedule — schedule derived from routes + publish
├── routes.example.json        # Template routes (committed) — copy to routes.json
├── routes.json                # Personal route definitions, local only (gitignored)
├── state.json                 # Auto-generated, local only (gitignored): prices, history, API calls
├── responses.jsonl            # Auto-generated, local only (gitignored): raw API response archive
├── flight_monitor.log         # Auto-generated, local only (gitignored): timestamped run log
├── requirements.txt           # Python dependencies
├── setup.sh                   # Dependency check, venv creation, local config bootstrap (idempotent)
├── startweb.sh                # Starts the dashboard with the venv interpreter
├── .venv/                     # Project virtual environment, local only (gitignored)
├── .env.example               # Environment variable template
├── tests/
│   └── test_flight_monitor.py # Pytest suite for pure-logic functions (no live API calls)
├── .github/workflows/
│   ├── monitor.yml            # Manual-only workflow (smoke test; commits no data)
│   └── tests.yml              # Test matrix across Python 3.9–3.13 (no API calls, no secrets)
├── CLAUDE.md                  # This file
├── README.md                  # User-facing documentation
├── LICENSE                    # MIT license
└── .gitignore                 # Python-focused gitignore
```

## Key files

- **`flight_monitor.py`**: Monitor script. Reads config from env vars, loads routes from `routes.json`, queries SerpAPI Google Flights (single API key, no OAuth) for the cheapest flights, compares against `state.json`, sends Telegram alerts on significant price changes, appends to price history, and tracks API call counts per month. Maps `routes.json` fields to SerpAPI params: `travel_class` strings → integer codes (`TRAVEL_CLASS_MAP`), `non_stop` → `stops` (1 = nonstop only, 0 = any), `return_date` presence → `type` (1 = round trip, 2 = one way), `teens` folded into the `adults` count sent to SerpAPI (no separate fare tier exists for 12-17 year olds), and `max_duration_hours` × 60 → SerpAPI's minutes-based `max_duration` param (omitted entirely when not set — no default cap). Takes the minimum price across `best_flights` and `other_flights`. From the **same** (already-paid-for) response it also captures the top-3 cheapest `alternatives`, the cheapest `nonstop_price`, and the `price_insights` verdict (`price_level`, `typical_price_range`) — no extra API cost. Always drops itineraries with 2 or more stops — SerpAPI's `stops` param only distinguishes nonstop from any, so only nonstop/1-stop options are kept client-side. Optionally drops itineraries that connect through a US airport (`EXCLUDE_US_CONNECTIONS`, checked via `_has_us_layover()` against the `US_HUBS` set). Routes own their schedule: each carries `run_times` (local `"HH:MM"` clock times) and `route_runs_at()` checks a route when the process starts within `RUN_TIME_TOLERANCE_MIN` of one of them, so a single crontab line can serve routes on different schedules. A route with no `run_times` runs on every firing; the legacy `run_hours` field (hour-only, could filter firings but never create one) is still honoured when `run_times` is absent. `--force` bypasses both the time filter and the active-hours check for an ad-hoc "check everything now" run — without it, an ad-hoc run at an unscheduled minute correctly does nothing. Also supports an on-demand **flexible-date scan** via `.venv/bin/python flight_monitor.py --scan [--days N]` (`run_scan`): for each route it searches `departure_date ± N` days (default 3 → 7 searches/route; round trips shift `return_date` by the same offset, multi-leg routes shift every leg's date by the same offset, to keep trip length/leg gaps constant), finds the cheapest date, stores it under `state.json` → `flex_scans`, and sends a Telegram summary. The scan is **not** part of the cron — each date costs one search, so it is run deliberately and is still bounded by `MONTHLY_CALL_CAP` (an over-cap scan is refused before any calls). At process start, `sync_account_quota()` calls the free SerpAPI `account.json` endpoint to seed a local "searches remaining on plan" counter (logged after each search, never persisted) and `_this_month_usage`, SerpAPI's own real count of searches used this calendar month. `can_make_calls()` and the "Done" log use `_this_month_usage` plus any calls already made this process (`record_call()`, called once per real search) as the source of truth for the `MONTHLY_CALL_CAP` check — **not** the locally-reported `state.json` → `api_calls` counter, which only sees calls made through this script and can drift from the account's actual usage (e.g. calls made through a different process, or the SerpAPI dashboard, against the same key). `api_calls` is still incremented per search and persisted, but only feeds the dashboard's historical usage chart now. If the account sync fails, `_this_month_usage` stays `None` and `can_make_calls()` fails closed (refuses all calls) rather than risk exceeding the cap on stale data. If a search fails with HTTP 429 or an error message indicating exhausted searches, `_maybe_alert_quota()` sends one Telegram alert per process run.
  - **Multi-leg (multi-city) routes**: a route with a `legs` array (see `routes.json` below) instead of `origin`/`destination`/`departure_date` is a multi-city itinerary. `search_cheapest()` maps it to SerpAPI `type=3` with `multi_city_json` (a JSON array of `{departure_id, arrival_id, date}` per leg) instead of the simple route's `departure_id`/`arrival_id`/`outbound_date`/`return_date`; `adults`/`travel_class`/`stops`/`currency` are shared between both shapes. `MULTI_CITY_DEEP_SEARCH` (default `false`) adds `deep_search=true` to these requests only — SerpAPI's docs flag multi-city as a case where results may not otherwise match the Google Flights website, at extra cost/latency. The shared `route_label(route)` helper builds the `state.json`/archive label for either shape: `"ORIGIN-DEST DATE"` for a simple route, or the airport chain joined by `-` (first leg's origin, then every leg's destination) plus the first leg's date for a legs route, e.g. `"JFK-HEL-BER-JFK 2026-09-15"` for JFK-HEL, HEL-BER, BER-JFK. `format_telegram()` lists each leg's origin→destination and date instead of Outbound/Inbound for these routes. **Known limitation**: the "reject itineraries with 2+ total stops" cap only applies to simple routes — a legitimate multi-leg itinerary can have one connection per leg, which would look like "2+ stops" in aggregate even though each leg is individually fine, so multi-leg routes instead trust SerpAPI's own `stops` param (nonstop-only vs. any) with no client-side ceiling on total connections across all legs.
- **`responses.jsonl`**: Append-only archive (one JSON object per line) of every raw API response received — the full payload (all offers, `price_insights`, airports, booking tokens, etc.) with the `api_key` stripped from the recorded query. Kept **out of `state.json`** so the dashboard (which parses `state.json` on every request) stays fast. Written by `archive_response()` whenever `ARCHIVE_RESPONSES` is true. Bounded by `RETENTION_DAYS`: each run (and the on-demand `--trim`) drops lines older than the window. **Local only** — gitignored and never committed/pushed to the repo.
- **`flight_monitor.log`**: Plain-text run log — every `log()` call (the same timestamped lines printed to stdout/cron output) is also appended here via `_append_log_line()`, best-effort (a write failure is swallowed rather than crashing the monitor). Bounded by `RETENTION_DAYS` just like `responses.jsonl`: `trim_logs()` drops lines whose leading timestamp is older than the window (blank/unparseable lines are always kept), run automatically every cycle via `trim_old_data()` and on demand via `--trim`. **Local only** — gitignored and never committed/pushed to the repo. Because the app manages its own retention, cron does **not** need to redirect output to an external file (no `/var/log` growth, no logrotate needed).
- **`app.py`**: Flask app serving the dashboard at `http://localhost:5000`. Reads `state.json` on each request. Also exposes `/api/state` as raw JSON, and `DELETE /api/routes/<label>` to remove one route's price history/chart (and matching `flex_scans` entry) — the "Delete" button on each dashboard chart, for routes the user has stopped tracking. Serves two editors: `/routes` (route CRUD, including each route's `run_times`) and `/schedule` (a *derived* view — `schedule_plan()` computes the union of route times, which routes fire at each one, the search budget, and any conflict with the active-hours window; `POST /api/schedule` takes no times and installs exactly that union, refusing to publish a time the monitor would skip as outside active hours). Launch it via `startweb.sh` (or `.venv/bin/python app.py`), never a bare `python app.py`: `build_cron_block()` writes crontab entries using `sys.executable`, so the interpreter the app runs under is the one baked into the schedule the route/schedule editor installs. `validate_routes()`/`normalize_route()`/`route_label()` all branch on whether a route has a `legs` array: a legs-shaped route skips the simple-route required-field/IATA/date checks (validated only as "≥2 legs, each with `origin`/`destination`/`date`") and is passed through `normalize_route()` largely as-given (the `/routes` editor already uppercases/shapes each leg client-side before posting). `validate_routes()`/`normalize_route()` also handle the shared optional `teens` (non-negative int, folded into `adults` for the actual SerpAPI call — see the `flight_monitor.py` bullet) and `max_duration_hours` (positive number) fields, which apply to both route shapes.
- **`startweb.sh`**: Thin wrapper that `exec`s `app.py` under `.venv/bin/python` after verifying the venv exists and has Flask. Passes through env vars (`PORT`, `FLASK_DEBUG`) and any arguments.
- **`templates/dashboard.html`**: Single-page dashboard with dark theme, per-route price charts (Chart.js), percentage-change badges, a price-level verdict and cheapest alternatives per route, flexible-date scan grids, and API usage bar charts.
- **`routes.json`**: JSON array of route objects — the user's **personal, gitignored** config (copied from `routes.example.json`, the only tracked routes file). Loaded via `load_routes()`, which exits with a "copy routes.example.json" hint if the file is missing or not a non-empty array. Required fields: `origin`, `destination`, `departure_date` (IATA codes, ISO dates). Optional: `return_date` (presence makes it a round trip), `adults` (default 1), `teens` (default 0 — see below), `non_stop` (default `true` → nonstop only), `travel_class` (`ECONOMY`/`PREMIUM_ECONOMY`/`BUSINESS`/`FIRST`, default `ECONOMY`), `max_duration_hours` (drop itineraries longer than this — see below), `run_times` (local `"HH:MM"` times this route is checked at). **`run_times` is the single source of truth for scheduling**: `app.py`'s `schedule_plan()` derives the crontab from the union of every route's times, and `flight_monitor.route_runs_at()` filters each firing down to the routes due then. A route with no `run_times` runs at every firing the other routes schedule. `run_hours` (a list of hours) is the deprecated predecessor — still honoured by `route_runs_at()` when `run_times` is absent, but as a *filter* it could only subtract from firings the crontab already had, never add one, which is what let a route with `run_hours: [8]` and a 7:30 crontab silently never run. `schedule_plan()` therefore maps a legacy route's hours onto real times (`hours_as_times()`: the published firing in that hour if there is one, else `HH:00`) so publishing before migrating preserves its firings instead of dropping them; the routes editor does the same mapping in the UI and converts the field to `run_times` on save.
  - **`teens`** — count of 12-17 year olds. Google Flights/SerpAPI have no distinct fare tier for that age band (only Adults, Children 2-11, and Infants), so teens fly on adult fares: `search_cheapest()` sends `adults = adults + teens` to SerpAPI, and `teens` is tracked purely so `format_telegram()` can label the alert "2 adults + 2 teens" instead of just "4 pax".
  - **`max_duration_hours`** — optional, no default cap. When set, `search_cheapest()` converts it to minutes (SerpAPI's `max_duration` param wants minutes; routes.json takes hours since that's more natural to configure) and SerpAPI filters out longer itineraries server-side.
  - **Multi-leg (multi-city) route** — a `legs` array of ≥2 `{"origin", "destination", "date"}` objects, mutually exclusive with `origin`/`destination`/`departure_date`/`return_date` on the same route (`load_routes()` exits if both are present). Route-level `adults`/`teens`/`non_stop`/`travel_class`/`max_duration_hours`/`run_times`/`run_hours` still apply on top, unchanged. Buildable from the `/routes` editor via the "+ Add multi-leg route" button (add/remove leg rows; at least 2 required), or by hand-editing `routes.json`.
- **`tests/test_flight_monitor.py`**: Pytest suite covering the pure-logic functions in `flight_monitor.py` (state load/save, trimming, route scheduling, quota tracking, etc.) — no live API calls. Not in `requirements.txt`; `setup.sh` offers to install `pytest` into `.venv`, or add it yourself with `.venv/bin/pip install pytest`. Run with `.venv/bin/python -m pytest tests/`.
- **`tests/test_app.py`**: Pytest suite for `app.py` using Flask's test client — route/state validation (`validate_routes`, `normalize_route`), the `DELETE /api/routes/<label>` data-deletion endpoint, dashboard rendering, and cron schedule logic (`schedule_plan`, `outside_active_hours`, `build_cron_block`, `current_schedule`, `publish_schedule`). Never touches the real crontab: an autouse fixture stubs `read_crontab()` (every schedule-aware endpoint now consults it, including `POST /api/routes`), and any code path that would shell out to `crontab` has `subprocess.run` mocked.
- **`state.json`**: Persisted state including `prices` (keyed by route label `"ORIGIN-DEST DATE"`, each containing `price`, `previous_price` (the price it was compared against for that run's alert, `null` on the first check — persisted rather than re-derived from `history` so the dashboard's displayed change always matches what was actually alerted on, even after retention trimming prunes older history points), `updated`, a `details` object with the cheapest offer's airlines/stops/duration plus `alternatives`/`nonstop_price`/`price_level`/`typical_price_range`, and a `history` array), `api_calls` (keyed by `YYYY-MM`), `last_run` timestamp, and `flex_scans` (keyed by the same `"ORIGIN-DEST DATE"` route label, each holding the most recent flexible-date scan: `base_date`, `days`, per-date `results`, and the `cheapest` entry). Written atomically via a temp file + `os.replace` so a crash mid-write can't corrupt it.

## Data model (state.json)

```json
{
  "prices": {
    "JFK-LHR 2026-09-15": {
      "price": 450.00,
      "previous_price": 480.00,
      "updated": "2026-06-20T10:00:00-04:00",
      "details": {
        "airlines": ["..."], "stops": 0, "total_duration": 420,
        "nonstop_price": 450.00,
        "price_level": "low", "typical_price_range": [500, 900],
        "alternatives": [
          {"price": 450.00, "airlines": ["..."], "stops": 0, "total_duration": 420},
          {"price": 470.00, "airlines": ["..."], "stops": 1, "total_duration": 540}
        ]
      },
      "history": [
        {"price": 480.00, "timestamp": "2026-06-19T10:00:00-04:00"},
        {"price": 450.00, "timestamp": "2026-06-20T10:00:00-04:00"}
      ]
    }
  },
  "api_calls": {"2026-06": 45},
  "last_run": "2026-06-20T10:00:00-04:00",
  "flex_scans": {
    "JFK-LHR 2026-09-15": {
      "scanned": "2026-06-20T09:00:00-04:00",
      "base_date": "2026-09-15",
      "days": 3,
      "results": [
        {"date": "2026-09-14", "return_date": null, "price": 470.00},
        {"date": "2026-09-15", "return_date": null, "price": 450.00}
      ],
      "cheapest": {"date": "2026-09-15", "return_date": null, "price": 450.00}
    }
  }
}
```

A multi-leg (multi-city) route's `prices`/`flex_scans` entry is keyed the same way, just with the chain-joined label (see the `routes.json` bullet above) — e.g. a JFK-HEL, HEL-BER, BER-JFK itinerary keys as `"JFK-HEL-BER-JFK 2026-09-15"`, with `details.nonstop_price` typically `null` (a multi-city trip essentially never has zero layovers) and no other shape difference.

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
| `RUN_TIME_TOLERANCE_MIN` | No | `10` | How far (minutes) from a route's `run_times` the process may start and still count as that firing. Cron fires on the minute, but interpreter startup and `sync_account_quota()` run before routes are filtered, so an exact match would be brittle. |
| `ALERT_THRESHOLD_PCT` | No | `3` | Min % change to trigger alert |
| `NOTIFY_EVERY_RUN` | No | `true` | Send Telegram message on every run, not just significant changes |
| `MONTHLY_CALL_CAP` | No | `240` | Max SerpAPI searches per calendar month |
| `MAX_HISTORY` | No | `1000` | Max price history entries kept per route |
| `ARCHIVE_RESPONSES` | No | `true` | Append every raw API response to `responses.jsonl` |
| `RETENTION_DAYS` | No | `30` | Prune history points, archived responses, and log lines older than this (each run) |
| `EXCLUDE_US_CONNECTIONS` | No | `false` | Drop itineraries that layover in a US airport (matched against the `US_HUBS` set). Origin/destination are not checked, only connections. |
| `MULTI_CITY_DEEP_SEARCH` | No | `false` | Adds `deep_search=true` to multi-leg (`legs`) route searches only — SerpAPI docs note this sometimes better matches what Google Flights' website shows for multi-city itineraries, at extra cost/latency. |
| `FLASK_DEBUG` | No | `false` | Enable Flask's debug mode (`app.py` only) — auto-reload and the Werkzeug interactive debugger. Leave off outside local development; the debugger allows arbitrary code execution if the dashboard is ever reachable beyond localhost. |
| `PORT` | No | `5000` | Port `app.py` listens on. If that port is already in use, `app.py` automatically falls back to an OS-assigned free port and prints which one it picked. |

## Running locally

```bash
./setup.sh                        # creates .venv, installs deps, seeds .env + routes.json
# equivalent by hand:
#   python3 -m venv .venv && .venv/bin/pip install -U pip -r requirements.txt
#   cp .env.example .env && cp routes.example.json routes.json

# then fill in credentials in .env and your routes in routes.json

# Run the monitor once
.venv/bin/python flight_monitor.py

# Start the dashboard
./startweb.sh  # http://localhost:5000 (PORT=8080 ./startweb.sh to change port)

# Run the tests
.venv/bin/python -m pytest tests/
```

## Development conventions

- **Two entry points**: `flight_monitor.py` (cron job) and `app.py` (web server). They share `state.json` but are otherwise independent.
- **No build step**: The Flask app uses a Jinja2 template with Chart.js from CDN. No webpack, npm, or frontend toolchain.
- **Config via env vars only**: Never hardcode credentials or API keys. Use `os.environ.get()` with sensible defaults.
- **State file**: `state.json` is the only mutable data store. It must remain JSON-serializable and human-readable. The `history` array grows over time — this is intentional for charting.
- **API call safety**: Always check `can_make_calls()` before making SerpAPI requests. It's gated on SerpAPI's real `this_month_usage` (synced via `sync_account_quota()`), not the locally-reported `state.json` counter — the monthly cap exists to prevent billing, so it must reflect actual account usage, not just what this script itself has recorded.
- **Active hours**: The monitor self-skips outside the configured active window. This is intentional, not a bug.
- **Dashboard is otherwise read-only**: `app.py` only writes to `state.json` via `DELETE /api/routes/<label>` (removing a single route's price history/chart and any matching `flex_scans` entry, for routes the user has stopped tracking). It never does a general read-modify-write of `prices`/`history` — `flight_monitor.py` remains the sole writer of live price data.

## Common tasks

### Add a new route
Edit your local `routes.json` (gitignored; create it from `routes.example.json` if absent), or use the dashboard's `/routes` editor. Each entry needs at minimum `origin`, `destination`, and `departure_date` (IATA codes and ISO date), plus `run_times` if it should run on its own schedule rather than at every firing.

### Add a multi-leg (multi-city) route
Use the `/routes` editor's "+ Add multi-leg route" button (add/remove leg rows with "+ Add leg"/the per-row "×"; at least 2 legs required), or hand-edit `routes.json` with a `legs` array instead of `origin`/`destination`/`departure_date`/`return_date`:
```json
{
  "legs": [
    {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
    {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
    {"origin": "BER", "destination": "JFK", "date": "2026-09-25"}
  ],
  "adults": 1,
  "non_stop": true,
  "run_times": ["07:30"]
}
```

### Change when a route runs
Set the route's `run_times` (e.g. `["07:30", "19:30"]`), then publish from `/schedule` — the crontab is the union of every route's times, so a new time only starts firing once it's published. Adding a time to `routes.json` without publishing means the monitor is never invoked at that time; publishing a crontab line no route asks for means that firing checks only the routes that declare no times of their own.

### Run a check right now
`.venv/bin/python flight_monitor.py --force` — checks every route regardless of `run_times` or active hours. A plain `flight_monitor.py` at an unscheduled minute deliberately does nothing.

### Find the cheapest date for a route
Run `.venv/bin/python flight_monitor.py --scan` (optionally `--days N`) to sweep each route's `departure_date ± N` days and report the cheapest date. On-demand only; costs one search per date and respects `MONTHLY_CALL_CAP`.

### Prune old data
Trimming runs automatically at the end of every monitor run (drops history points, `responses.jsonl` lines, and `flight_monitor.log` lines older than `RETENTION_DAYS`). To prune on demand without a monitor run: `.venv/bin/python flight_monitor.py --trim` (optionally `--days N`). No API cost.

### Change alert sensitivity
Set the `ALERT_THRESHOLD_PCT` environment variable. Lower = more alerts.

### Reset API call counter
Delete the current month's entry from `state.json` -> `api_calls`, or delete `state.json` entirely (price history will also reset).

### Run the dashboard in production
Use a WSGI server, installed into the venv: `.venv/bin/pip install gunicorn && .venv/bin/gunicorn app:app -b 0.0.0.0:5000`

## Guardrails

- The `MONTHLY_CALL_CAP` (default 240) leaves a buffer below the user's 250-search/month SerpAPI plan. Each firing costs 1 search per route **due at that time** (no separate token call), so the monthly figure is the sum over `schedule_plan()`'s `by_time` × 30 — which `/schedule` displays before you publish. Do not raise the cap above the user's plan limit. The local cron runs 3 times a day at 7:30/13:30/19:30 (`30 7,13,19 * * *`) — 6 hours apart and all inside the default active-hours window (`ACTIVE_START=7`, `ACTIVE_END=22`). A plain `0 */6 * * *` schedule would fire at 00:00 and 06:00, which the monitor self-skips as outside active hours, wasting two firings. Do not run the monitor hourly — that would far exceed 250/month. The cron entry must invoke the venv interpreter by absolute path (cron has a minimal environment and never sources a shell profile, so `activate` is not an option):

```
30 7,13,19 * * * cd /home/pi/myfiles/FareMonkey && /home/pi/myfiles/FareMonkey/.venv/bin/python flight_monitor.py >/dev/null 2>&1
```
- `state.json`, `responses.jsonl`, and `flight_monitor.log` are runtime data, kept **local only** (gitignored). They are never committed or pushed to the repo. The monitor runs locally (e.g. cron on a Raspberry Pi); the GitHub Actions workflow is manual-only (`workflow_dispatch`), has no `schedule`, and commits nothing — so it cannot push data or double-spend the SerpAPI budget against the local cron.
- Credentials are stored as GitHub repository secrets or in `.env` (gitignored), never in code.
- The Flask dashboard (`app.py`) binds to `0.0.0.0:5000` by default (override the port with `PORT`; it auto-falls-back to a free port if that one's taken) with debug mode off by default; set `FLASK_DEBUG=true` for local development. Binding to `0.0.0.0` exposes the dashboard, and its `/api/routes` and `/api/schedule` POST endpoints (route editor, cron scheduler — see below), to the whole network with **no authentication** — only run this on a trusted LAN. For production or untrusted-network access, put gunicorn behind a reverse proxy with real auth in front of it, and leave `FLASK_DEBUG` unset — the Werkzeug debugger allows arbitrary code execution if reachable.
