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

Fields: `origin` and `destination` are IATA airport codes. `departure_date` is required. Optional fields: `return_date` (one-way if omitted), `adults` (default 1), `non_stop` (default `true`), `travel_class` (`ECONOMY`, `PREMIUM_ECONOMY`, `BUSINESS`, or `FIRST` — default `ECONOMY`), `run_hours` (a list of local-time hours, e.g. `[13]`, to check this route on only some of the cron firings instead of every one — see `routes.example.json` for a working example).

### 3. Install and configure

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Edit `.env` with your credentials.

### 4. Run the dashboard

```bash
python app.py
```

Open `http://localhost:5000` in your browser. The dashboard shows price charts, percentage changes, and API usage stats. It reads from `state.json` on each page load.

### 5. Run the monitor

One-off:
```bash
python flight_monitor.py
```

Set up a cron job on your Linux server (3 runs/day, 6 hours apart, to stay within the SerpAPI search budget):

```bash
crontab -e
```

Add this line (adjust the path):

```
30 7,13,19 * * * cd /path/to/FareMonkey && /path/to/python flight_monitor.py >> /var/log/faremonkey.log 2>&1
```

The runs fire at **7:30, 13:30, and 19:30** so they all fall inside the default
active-hours window (`ACTIVE_START=7`, `ACTIVE_END=22`). A plain `0 */6 * * *`
schedule would fire at 00:00 and 06:00 too, but the monitor self-skips those
because they're outside active hours — wasting two of the four daily firings. If
you widen the active window, adjust these times to match.

### 6. Find the cheapest date (flexible-date scan)

The regular monitor checks one fixed date per route. To see whether shifting your
trip a few days is cheaper, run an on-demand scan:

```bash
python flight_monitor.py --scan            # ± 3 days around each route's date (7 searches/route)
python flight_monitor.py --scan --days 5   # ± 5 days (11 searches/route)
```

For each route it queries every date in the window, prints a price-per-date table,
and records the cheapest date. Round trips keep their trip length constant (the
return date shifts by the same number of days). Results are saved to `state.json`
under `flex_scans` and shown on the dashboard as a date grid with the best day
highlighted; a Telegram summary is sent if alerts are configured.

> **Budget note:** a scan costs one SerpAPI search *per date in the window*, so it
> is **not** part of the scheduled cron — run it deliberately when planning. The
> `MONTHLY_CALL_CAP` is still enforced; an over-cap scan is refused before any
> calls are made.

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
| `ALERT_THRESHOLD_PCT` | No | `3` | Min % change to trigger alert |
| `NOTIFY_EVERY_RUN` | No | `true` | Send Telegram message on every run, not just significant changes |
| `MONTHLY_CALL_CAP` | No | `240` | Max SerpAPI searches per calendar month |
| `MAX_HISTORY` | No | `1000` | Max price history entries kept per route |
| `ARCHIVE_RESPONSES` | No | `true` | Append every raw API response to `responses.jsonl` |
| `RETENTION_DAYS` | No | `30` | Prune history and archived responses older than this (each run) |
| `EXCLUDE_US_CONNECTIONS` | No | `false` | Drop itineraries that connect through a US airport (nonstop and non-US connections kept) |

## SerpAPI account sync & quota alerts

On startup, the monitor calls SerpAPI's free `account.json` endpoint to fetch how many searches remain on your plan, and logs that count after every search (e.g. `[142 left on plan]`) — this call doesn't cost a search itself. If a search fails with HTTP 429 or an error message indicating the plan has run out of searches, a single Telegram alert is sent for that run (further failures in the same run stay silent to avoid spamming one alert per route).

## Running tests

The pure-logic parts of `flight_monitor.py` (no live API calls) are covered by a pytest suite:

```bash
pip install pytest
pytest tests/
```

## Data archive & retention

Every raw API response is appended to `responses.jsonl` (one JSON object per line) so the full payload — all offers, `price_insights`, airports, booking tokens — is preserved, even though alerts and the dashboard only surface the single cheapest fare. The API key is stripped from the archived query. Set `ARCHIVE_RESPONSES=false` to turn this off.

To keep these local files from growing forever, each monitor run prunes both the in-state price history and `responses.jsonl` to the last `RETENTION_DAYS` days (default 30). You can also prune on demand without making any API calls:

```bash
python flight_monitor.py --trim            # prune to RETENTION_DAYS
python flight_monitor.py --trim --days 60  # keep the last 60 days
```

## Quota math

SerpAPI charges **1 search per route per run** — there is no separate token/auth request. Budget your runs against your plan's monthly search allowance.

| Resource | Calls |
|----------|-------|
| Flight search (per route) | 1 per run |
| **Total per run** (2 routes) | **2** |
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
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for local environment variables |
| `tests/test_flight_monitor.py` | Pytest suite for pure-logic functions (no live API calls) |
| `.github/workflows/monitor.yml` | GitHub Actions workflow (manual-only smoke test; commits no data) |

## License

MIT
