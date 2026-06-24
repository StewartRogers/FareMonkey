# FareMonkey

Flight price monitor that tracks fares via the [SerpAPI Google Flights API](https://serpapi.com/google-flights-api) and sends Telegram alerts when prices move significantly. Includes a Flask web dashboard for viewing price history.

## How it works

1. Reads routes from `routes.json`
2. Queries SerpAPI (Google Flights) for the cheapest current fare on each route
3. Compares to the last saved price in `state.json`
4. Sends a Telegram message when the price changes by more than `ALERT_THRESHOLD_PCT` (default 3%)
5. Stores full price history for each route as a time-series
6. Runs on a schedule via cron (local or GitHub Actions)
7. Flask dashboard at `http://localhost:5000` shows live charts of price history

## Quick start

### 1. Get API credentials

- **SerpAPI**: Create a free account at [serpapi.com](https://serpapi.com). Copy your single API key from the [dashboard](https://serpapi.com/manage-api-key). The free plan includes 100 searches/month.
- **Telegram**: Message [@BotFather](https://t.me/BotFather) to create a bot. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot).

### 2. Configure routes

Edit `routes.json` with the flights you want to track:

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

Fields: `origin` and `destination` are IATA airport codes. `departure_date` is required. Optional fields: `return_date` (one-way if omitted), `adults` (default 1), `non_stop` (default `true`), `travel_class` (`ECONOMY`, `PREMIUM_ECONOMY`, `BUSINESS`, or `FIRST` — default `ECONOMY`).

### 3. Install and configure

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

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

Set up a cron job on your Linux server (every 6 hours to stay within the SerpAPI search budget):
```bash
crontab -e
# Add this line (adjust the path):
0 */6 * * * cd /path/to/FareMonkey && /path/to/python flight_monitor.py >> /var/log/faremonkey.log 2>&1
```

### 6. GitHub Actions (alternative to local cron)

If you prefer running the monitor via GitHub Actions instead of local cron, add these as repository secrets (*Settings > Secrets and variables > Actions*):

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

The workflow runs automatically every 6 hours and commits `state.json` back.

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
| `MONTHLY_CALL_CAP` | No | `240` | Max SerpAPI searches per calendar month |

## Quota math

SerpAPI charges **1 search per route per run** — there is no separate token/auth request. Budget your runs against your plan's monthly search allowance.

| Resource | Calls |
|----------|-------|
| Flight search (per route) | 1 per run |
| **Total per run** (2 routes) | **2** |
| Runs per day (every 6 hours) | **4** |
| **Calls per day** | **8** |
| **Calls per month** (30 days) | **~240** |

The default `MONTHLY_CALL_CAP=240` leaves a small buffer below a 250-search/month plan. The monitor stops making calls once the cap is reached; the cap is tracked in `state.json` and resets each calendar month.

**Be economical**: hourly checks would burn ~1,440 searches/month with 2 routes — far above 250. The workflow therefore runs **every 6 hours** (`0 */6 * * *`). To adjust your budget: change the cron interval, narrow the active-hours window, reduce the number of routes, or raise `MONTHLY_CALL_CAP` if you upgrade your SerpAPI plan.

## Files

| File | Purpose |
|------|---------|
| `flight_monitor.py` | Price monitor script (runs via cron) |
| `app.py` | Flask web dashboard |
| `templates/dashboard.html` | Dashboard template with Chart.js charts |
| `routes.json` | Routes to track (edit this) |
| `state.json` | Persisted prices, history, and API call counts (auto-generated) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for local environment variables |
| `.github/workflows/monitor.yml` | Hourly GitHub Actions workflow |

## License

MIT
