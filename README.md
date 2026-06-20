# FareMonkey

Flight price monitor that tracks fares via the [Amadeus Flight Offers Search API](https://developers.amadeus.com/self-service/category/flights/api-doc/flight-offers-search) and sends Telegram alerts when prices move significantly. Includes a Flask web dashboard for viewing price history.

## How it works

1. Reads routes from `routes.json`
2. Queries Amadeus for the cheapest current fare on each route
3. Compares to the last saved price in `state.json`
4. Sends a Telegram message when the price changes by more than `ALERT_THRESHOLD_PCT` (default 3%)
5. Stores full price history for each route as a time-series
6. Runs hourly via cron (local or GitHub Actions)
7. Flask dashboard at `http://localhost:5000` shows live charts of price history

## Quick start

### 1. Get API credentials

- **Amadeus**: Create a free account at [developers.amadeus.com](https://developers.amadeus.com). Register an app under *My Self-Service Apps* to get your production API key and secret.
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

Set up an hourly cron job on your Linux server:
```bash
crontab -e
# Add this line (adjust the path):
0 * * * * cd /path/to/FareMonkey && /path/to/python flight_monitor.py >> /var/log/faremonkey.log 2>&1
```

### 6. GitHub Actions (alternative to local cron)

If you prefer running the monitor via GitHub Actions instead of local cron, add these as repository secrets (*Settings > Secrets and variables > Actions*):

| Secret | Description |
|--------|-------------|
| `AMADEUS_CLIENT_ID` | Amadeus API key |
| `AMADEUS_CLIENT_SECRET` | Amadeus API secret |
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
| `MONTHLY_CALL_CAP` | `1900` | Max API calls per month |

The workflow runs automatically every hour and commits `state.json` back.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AMADEUS_CLIENT_ID` | Yes | - | Amadeus API key |
| `AMADEUS_CLIENT_SECRET` | Yes | - | Amadeus API secret |
| `TELEGRAM_BOT_TOKEN` | No | - | Telegram bot token (alerts disabled if unset) |
| `TELEGRAM_CHAT_ID` | No | - | Telegram chat ID |
| `CURRENCY` | No | `USD` | Currency for price queries |
| `TIMEZONE` | No | `America/New_York` | IANA timezone for active-hours check |
| `ACTIVE_START` | No | `7` | Start of active window (hour) |
| `ACTIVE_END` | No | `22` | End of active window (hour) |
| `ALERT_THRESHOLD_PCT` | No | `3` | Min % change to trigger alert |
| `MONTHLY_CALL_CAP` | No | `1900` | Max Amadeus API calls per calendar month |

## Free quota math

Amadeus provides **2,000 free API calls per month** on self-service production keys.

| Resource | Calls |
|----------|-------|
| OAuth token requests | ~1 per run |
| Flight search (per route) | 1 per run |
| **Total per run** (2 routes) | **3** |
| Runs per day (hourly, 7 AM-10 PM = 15 hrs) | **15** |
| **Calls per day** | **45** |
| **Calls per month** (30 days) | **~1,350** |

With `MONTHLY_CALL_CAP=1900` (default), the monitor stops making calls before hitting the 2,000 free limit, so you are never billed. The cap is tracked in `state.json` and resets each calendar month.

Scaling: with 2 routes you have comfortable headroom. If you add more routes, reduce the active window or increase the cron interval to stay within quota.

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
