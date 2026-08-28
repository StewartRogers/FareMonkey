# FareMonkey

Flight price monitor that tracks fares via the [SerpAPI Google Flights API](https://serpapi.com/google-flights-api) and sends Telegram alerts when prices move significantly. Includes a Flask web dashboard for viewing price history.

## How it works

1. Reads routes from `routes.json`
2. Queries SerpAPI (Google Flights) for the cheapest current fare on each route, and from the same response also records the top alternatives, the cheapest nonstop option, and Google's own price verdict (`low`/`typical`/`high` vs typical range) — all at no extra API cost
3. Compares to the last saved price in `state.json`
4. Sends a Telegram message when the price changes by more than `ALERT_THRESHOLD_PCT` (default 3%)
5. Stores full price history for each route as a time-series
6. Runs on a schedule via local cron (state is kept local only, never pushed to GitHub)
7. Flask dashboard at `http://localhost:5000` shows live charts of price history

## Quick start

### 1. Get API credentials

- **SerpAPI**: Create a free account at [serpapi.com](https://serpapi.com). Copy your single API key from the [dashboard](https://serpapi.com/manage-api-key). The free plan includes 100 searches/month.
- **Telegram**: Message [@BotFather](https://t.me/BotFather) to create a bot. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot).

### 2. Configure routes

Routes are your personal config. Copy the template and edit it:

```bash
cp routes.example.json routes.json
```

`routes.json` is **gitignored** (it holds your own itineraries) — only the template is tracked. Edit it with the flights you want to track:

```json
[
  {
    "origin": "JFK",
    "destination": "LHR",
    "departure_date": "2026-09-15",
    "return_date": "2026-09-22",
    "adults": 1
  }
]
```

Fields: `origin` and `destination` are IATA airport codes. `departure_date` is required. Optional fields: `return_date` (one-way if omitted), `adults` (default 1), `teens` (default 0 — see below), `non_stop` (default `true`), `travel_class` (`ECONOMY`, `PREMIUM_ECONOMY`, `BUSINESS`, or `FIRST` — default `ECONOMY`), `max_duration_hours` (drop itineraries longer than this — see below), `run_times` (the local clock times this route is checked at, e.g. `["07:30", "19:30"]` — see `routes.example.json` for a working example).

**Teens (12-17)**: Google Flights/SerpAPI have no distinct fare tier for that age band — only Adults, Children (2-11), and Infants — so a 12-17 year old flies on an adult fare. Set `teens` alongside `adults` (e.g. `"adults": 2, "teens": 2` for two adults + two teens) and the monitor sends `adults + teens` to SerpAPI; `teens` is tracked separately purely so Telegram alerts read "2 adults + 2 teens" instead of just "4 pax".

**Max duration**: set `max_duration_hours` (e.g. `24`) to have SerpAPI drop itineraries longer than that — no default cap, opt in per route. Values are hours in `routes.json`/the editor; the monitor converts to the minutes SerpAPI's `max_duration` param expects.

**Multi-leg (multi-city) trips** — three or more stops on different dates, e.g. JFK → HEL, HEL → BER, BER → JFK — use a `legs` array instead of `origin`/`destination`/`departure_date`/`return_date`:

```json
{
  "legs": [
    {"origin": "JFK", "destination": "HEL", "date": "2026-09-15"},
    {"origin": "HEL", "destination": "BER", "date": "2026-09-20"},
    {"origin": "BER", "destination": "JFK", "date": "2026-09-25"}
  ],
  "adults": 1,
  "run_times": ["07:30"]
}
```

`adults`/`teens`/`non_stop`/`travel_class`/`max_duration_hours`/`run_times` all still apply. Each leg is priced as its own independent one-way search and the prices are summed, so an N-leg route costs **N** SerpAPI searches per check, not 1 — see "Quota math" below. (SerpAPI's own multi-city API only returns pricing for the first leg in a single call; summing independent legs avoids relying on its undocumented multi-step continuation flow and — unlike that first-leg-only price — never understates the real cost.) Build one from the `/routes` editor's "+ Add multi-leg route" button, or by hand-editing `routes.json`.

**Routes decide when the monitor runs.** The crontab is the union of every route's `run_times`, so adding a time to a route adds a firing and removing the last route that used a time removes it. A route with no `run_times` is checked at *every* firing the other routes schedule. The dashboard's Schedule page shows that union — which routes fire at each time and what it costs in searches — and installs it (see "Schedule" below). The older `run_hours` field (a list of hours that could only *filter* firings the crontab already had, never create one) still works: the Schedule page maps those hours onto real times — reusing the published firing's minutes where there is one — so publishing before you migrate keeps the route running, and the routes editor converts the field to `run_times` when you save.

### 3. Install and configure

**Requires Python 3.9–3.13.** Every version in that range is exercised by the
test matrix in `.github/workflows/tests.yml`, so the same checkout runs unchanged
on a Raspberry Pi OS bullseye box (3.9) and a current 3.13 system.

Dependencies go in a project-local virtual environment (`.venv`), never in the
system Python:

```bash
python3 -m venv .venv
.venv/bin/pip install -U pip -r requirements.txt
```

On Debian/Raspberry Pi OS this is the only clean option: those systems mark their
Python "externally managed" ([PEP 668](https://peps.python.org/pep-0668/)) and
refuse plain `pip install`. The workaround flag `--break-system-packages` installs
packages that shadow apt-managed ones for *every* Python process you run — a venv
keeps them scoped to this project instead.

`.venv/` is gitignored. You don't need `source .venv/bin/activate` — running
`.venv/bin/python` directly is enough, which is what makes the cron line below work.

`setup.sh` does all of this for you (creates the venv, installs dependencies,
seeds `.env` and `routes.json`) and is safe to re-run:

```bash
./setup.sh
```

```bash
cp .env.example .env
```

Edit `.env` with your credentials.

### 4. Run the dashboard

```bash
./startweb.sh
```

That wrapper launches `app.py` with the venv interpreter (equivalent to
`.venv/bin/python app.py`) after checking the venv exists, and passes environment
variables through:

```bash
PORT=8080 ./startweb.sh
FLASK_DEBUG=true ./startweb.sh
```

> **Start the dashboard this way, not with a bare `python app.py`.** The schedule
> page writes crontab entries using the interpreter the app is running under
> (`sys.executable`), so launching it with the system Python would install cron
> lines pointing at an interpreter that has none of the dependencies.

Open `http://localhost:5000` in your browser. The dashboard shows price charts, percentage changes, and API usage stats. It reads from `state.json` on each page load.

### 5. Run the monitor

One-off:
```bash
.venv/bin/python flight_monitor.py --force   # check every route right now
.venv/bin/python flight_monitor.py           # check only the routes due at this minute
```

Without `--force` the monitor checks the routes whose `run_times` match the
current clock time (within `RUN_TIME_TOLERANCE_MIN`, default 10), which is what
makes one crontab line serve routes on different schedules. An ad-hoc run at an
unscheduled minute would therefore do nothing — `--force` checks every route
regardless of its times or the active-hours window.

### Installing the schedule

The crontab is derived from your routes, so the easiest path is the dashboard's
**Schedule** page: it shows the union of every route's `run_times`, which routes
fire at each one, the resulting monthly search count, and a Publish button that
installs exactly that into the crontab of the host running the dashboard. It
refuses to publish a time outside the active-hours window, since the monitor
would silently skip it.

To do the same by hand, one line per distinct run time (adjust the path):

```bash
crontab -e
```

```
30 7,13,19 * * * cd /path/to/FareMonkey && /path/to/FareMonkey/.venv/bin/python flight_monitor.py >/dev/null 2>&1
```

Point cron at the **venv's** interpreter, not `/usr/bin/python` — cron runs with a
minimal environment and never sources a shell profile, so an absolute path to
`.venv/bin/python` is what gives the job its dependencies. No `activate` step is
needed.

The runs fire at **7:30, 13:30, and 19:30** so they all fall inside the default
active-hours window (`ACTIVE_START=7`, `ACTIVE_END=22`). A plain `0 */6 * * *`
schedule would fire at 00:00 and 06:00 too, but the monitor self-skips those
because they're outside active hours — wasting two of the four daily firings. If
you widen the active window, adjust these times to match.

Each firing only checks the routes due at that time, so a crontab line is
harmless for routes that don't want it — but a `run_times` entry with no matching
crontab line never runs. Publishing from the Schedule page keeps the two in step;
if you edit the crontab by hand, make sure every distinct `run_times` value has a
line.

No output redirect is needed: every run appends to `flight_monitor.log` in the
project directory (gitignored, local only), which is pruned by `RETENTION_DAYS`
on every run just like history and `responses.jsonl` — see "Data archive &
retention" below. If you still want cron's own mail-on-output behavior
disabled, redirect stdout/stderr to `/dev/null` instead of a growing file:
`... flight_monitor.py >/dev/null 2>&1`.

### 6. Find the cheapest date (flexible-date scan)

The regular monitor checks one fixed date per route. To see whether shifting your
trip a few days is cheaper, run an on-demand scan:

```bash
.venv/bin/python flight_monitor.py --scan            # ± 3 days around each route's date (7 searches/route)
.venv/bin/python flight_monitor.py --scan --days 5   # ± 5 days (11 searches/route)
```

For each route it queries every date in the window, prints a price-per-date table,
and records the cheapest date. Round trips keep their trip length constant (the
return date shifts by the same number of days). Results are saved to `state.json`
under `flex_scans` and shown on the dashboard as a date grid with the best day
highlighted; a Telegram summary is sent if alerts are configured.

> **Budget note:** a scan costs one SerpAPI search *per date in the window* for a
> simple route, or N searches per date for an N-leg multi-leg route (each leg is
> its own search — see "Quota math" below), so it is **not** part of the
> scheduled cron — run it deliberately when planning. The `MONTHLY_CALL_CAP` is
> still enforced; an over-cap scan is refused before any calls are made.

### 7. GitHub Actions (manual smoke test only)

> **Data stays local.** `state.json` and `responses.jsonl` are gitignored and are **never committed or pushed to GitHub**. The monitor is meant to run on your own machine (local cron); the included workflow is **manual-only** (`workflow_dispatch`, no schedule) and commits nothing. Because it has no persisted state, a CI run always behaves like a first check (baseline alert, no price comparison) — it's only useful as a connectivity/credentials smoke test.

To run that manual test, add these as repository secrets (*Settings > Secrets and variables > Actions*):

| Secret | Description |
|--------|-------------|
| `SERPAPI_API_KEY` | SerpAPI API key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |

Optional repository variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CURRENCY` | `USD` | Currency code for prices |
| `TIMEZONE` | `America/New_York` | IANA timezone for active hours |
| `ACTIVE_START` | `7` | Hour to start checking (local time) |
| `ACTIVE_END` | `22` | Hour to stop checking (local time) |
| `ALERT_THRESHOLD_PCT` | `3` | Price change % to trigger alert |
| `MONTHLY_CALL_CAP` | `240` | Max API calls per month |
| `NOTIFY_EVERY_RUN` | `true` | Send alerts on every run, not just significant changes |

Trigger it from the *Actions* tab → *Flight Price Monitor* → *Run workflow*. It runs once and persists nothing.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SERPAPI_API_KEY` | Yes | - | SerpAPI API key |
| `TELEGRAM_BOT_TOKEN` | No | - | Telegram bot token (alerts disabled if unset) |
| `TELEGRAM_CHAT_ID` | No | - | Telegram chat ID |
| `CURRENCY` | No | `USD` | Currency for price queries |
| `TIMEZONE` | No | `America/New_York` | IANA timezone for active-hours check |
| `ACTIVE_START` | No | `7` | Start of active window (hour) |
| `ACTIVE_END` | No | `22` | End of active window (hour) |
| `RUN_TIME_TOLERANCE_MIN` | No | `10` | How far from a route's `run_times` the process may start and still count as that firing |
| `ALERT_THRESHOLD_PCT` | No | `3` | Min % change to trigger alert |
| `NOTIFY_EVERY_RUN` | No | `true` | Send Telegram message on every run, not just significant changes |
| `MONTHLY_CALL_CAP` | No | `240` | Max SerpAPI searches per calendar month |
| `MAX_HISTORY` | No | `1000` | Max price history entries kept per route |
| `ARCHIVE_RESPONSES` | No | `true` | Append every raw API response to `responses.jsonl` |
| `RETENTION_DAYS` | No | `30` | Prune history and archived responses older than this (each run) |
| `EXCLUDE_US_CONNECTIONS` | No | `false` | Drop itineraries that connect through a US airport (nonstop and non-US connections kept) |
| `FLASK_DEBUG` | No | `false` | Enable Flask debug mode for the dashboard (`app.py`) — local development only |

## SerpAPI account sync & quota alerts

On startup, the monitor calls SerpAPI's free `account.json` endpoint to fetch how many searches remain on your plan, and logs that count after every search (e.g. `[142 left on plan]`) — this call doesn't cost a search itself. If a search fails with HTTP 429 or an error message indicating the plan has run out of searches, a single Telegram alert is sent for that run (further failures in the same run stay silent to avoid spamming one alert per route).

## Running tests

The pure-logic parts of `flight_monitor.py` (no live API calls) are covered by a pytest suite:

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/
```

The same suite runs in CI on Python 3.9, 3.10, 3.11, 3.12, and 3.13
(`.github/workflows/tests.yml`) on every push and pull request. It makes no API
calls and needs no secrets. Note that `requirements.txt` uses open version ranges
rather than pins on purpose — pip honours each package's `Requires-Python`, so
Python 3.9 installs the last releases that still support it while 3.13 gets
current ones. Pinning exact versions would break one end of the range.

## Data archive & retention

Every raw API response is appended to `responses.jsonl` (one JSON object per line) so the full payload — all offers, `price_insights`, airports, booking tokens — is preserved, even though alerts and the dashboard only surface the single cheapest fare. The API key is stripped from the archived query. Set `ARCHIVE_RESPONSES=false` to turn this off.

Every run also appends its console output to `flight_monitor.log` in the project directory (gitignored, local only) — the same timestamped lines printed to the terminal/cron output, so you have a durable run history without needing to redirect cron's own output anywhere.

To keep these local files from growing forever, each monitor run prunes the in-state price history, `responses.jsonl`, **and** `flight_monitor.log` to the last `RETENTION_DAYS` days (default 30). You can also prune on demand without making any API calls:

```bash
.venv/bin/python flight_monitor.py --trim            # prune to RETENTION_DAYS
.venv/bin/python flight_monitor.py --trim --days 60  # keep the last 60 days
```

## Quota math

SerpAPI charges **1 search per simple route per run** — there is no separate token/auth request. A **multi-leg route costs N searches for N legs**: each leg is priced as its own independent one-way search rather than one combined multi-city call (SerpAPI's actual multi-city API only prices the first leg in a single request — see "Multi-leg (multi-city) trips" above). Budget your runs against your plan's monthly search allowance; the `/schedule` page's search-budget figure already accounts for this per-route.

| Resource | Calls |
|----------|-------|
| Flight search (per simple route) | 1 per run |
| Flight search (per N-leg route) | N per run |
| **Total per run** (2 simple routes) | **2** |
| Runs per day (7:30, 13:30, 19:30) | **3** |
| **Calls per day** | **6** |
| **Calls per month** (30 days) | **~180** |

The default `MONTHLY_CALL_CAP=240` leaves a comfortable buffer below a 250-search/month plan. The monitor stops making calls once the cap is reached; the cap is tracked in `state.json` and resets each calendar month.

**Be economical**: hourly checks would burn ~1,440 searches/month with 2 routes — far above 250. The cron therefore runs **3 times a day, 6 hours apart** (`30 7,13,19 * * *`), all within active hours. To adjust your budget: change the cron interval, narrow the active-hours window, reduce the number of routes, or raise `MONTHLY_CALL_CAP` if you upgrade your SerpAPI plan.

## Files

| File | Purpose |
|------|---------|
| `flight_monitor.py` | Price monitor script (runs via cron) |
| `app.py` | Flask web dashboard |
| `templates/dashboard.html` | Dashboard template with Chart.js charts |
| `routes.example.json` | Template routes — copy to `routes.json` |
| `routes.json` | Routes to track — your personal config (**local only / gitignored**) |
| `state.json` | Persisted prices, history, and API call counts (auto-generated, **local only / gitignored**) |
| `responses.jsonl` | Append-only archive of raw API responses, pruned to `RETENTION_DAYS` (auto-generated, **local only / gitignored**) |
| `flight_monitor.log` | Timestamped run log, pruned to `RETENTION_DAYS` (auto-generated, **local only / gitignored**) |
| `requirements.txt` | Python dependencies |
| `setup.sh` | Dependency check + venv creation + local config bootstrap (safe to re-run) |
| `startweb.sh` | Starts the dashboard using the venv interpreter |
| `.venv/` | Project virtual environment holding all dependencies (auto-generated, **local only / gitignored**) |
| `.env.example` | Template for local environment variables |
| `tests/test_flight_monitor.py` | Pytest suite for pure-logic functions (no live API calls) |
| `tests/test_app.py` | Pytest suite for the Flask dashboard: route validation, the delete endpoint, and cron schedule logic (crontab mocked) |
| `.github/workflows/monitor.yml` | GitHub Actions workflow (manual-only smoke test; commits no data) |
| `.github/workflows/tests.yml` | GitHub Actions test matrix across Python 3.9–3.13 (no API calls) |

## License

MIT
