# CLAUDE.md

## Project overview

FareMonkey is a Python-based flight price monitor. It queries the Amadeus Flight Offers Search API (production) for the cheapest fares on configured routes, compares prices to previously recorded values, and sends Telegram alerts when prices change beyond a configurable threshold. It runs hourly via GitHub Actions and commits updated state back to the repository.

## Tech stack

- **Language**: Python 3.12+
- **Dependencies**: `requests`, `tzdata` (see `requirements.txt`)
- **CI/CD**: GitHub Actions (`.github/workflows/monitor.yml`)
- **External APIs**: Amadeus Flight Offers Search v2, Telegram Bot API

## Repository structure

```
FareMonkey/
├── flight_monitor.py          # Main application script
├── routes.json                # Route definitions to monitor (user-edited)
├── state.json                 # Auto-generated: prices + API call counts (committed by CI)
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
├── .github/workflows/
│   └── monitor.yml            # Hourly cron workflow
├── CLAUDE.md                  # This file
├── README.md                  # User-facing documentation
├── LICENSE                    # MIT license
└── .gitignore                 # Python-focused gitignore
```

## Key files

- **`flight_monitor.py`**: Single-file application. Reads config from env vars, loads routes from `routes.json`, authenticates with Amadeus OAuth2, searches for cheapest flights, compares against `state.json`, sends Telegram alerts on significant price changes, and tracks API call counts per month.
- **`routes.json`**: JSON array of route objects with fields: `origin`, `destination`, `departure_date`, optional `return_date`, optional `adults`.
- **`state.json`**: Persisted state including `prices` (keyed by route label), `api_calls` (keyed by `YYYY-MM`), and `last_run` timestamp. Auto-generated on first run. Committed back by GitHub Actions.

## Environment variables

All configuration is read from environment variables (no hardcoded credentials):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AMADEUS_CLIENT_ID` | Yes | - | Amadeus API key |
| `AMADEUS_CLIENT_SECRET` | Yes | - | Amadeus API secret |
| `TELEGRAM_BOT_TOKEN` | No | - | Telegram bot token (alerts disabled if unset) |
| `TELEGRAM_CHAT_ID` | No | - | Telegram chat ID |
| `CURRENCY` | No | `USD` | Currency for price queries |
| `TIMEZONE` | No | `UTC` | IANA timezone for active-hours check |
| `ACTIVE_START` | No | `7` | Start of active window (hour, local time) |
| `ACTIVE_END` | No | `22` | End of active window (hour, local time) |
| `ALERT_THRESHOLD_PCT` | No | `3` | Min % change to trigger alert |
| `MONTHLY_CALL_CAP` | No | `1900` | Max Amadeus API calls per calendar month |

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env  # then fill in credentials
python flight_monitor.py
```

## Development conventions

- **Single-file architecture**: All application logic lives in `flight_monitor.py`. Keep it that way unless complexity warrants splitting.
- **No frameworks**: Pure `requests` for HTTP. No CLI framework.
- **Config via env vars only**: Never hardcode credentials or API keys. Use `os.environ.get()` with sensible defaults.
- **State file**: `state.json` is the only mutable file. It must remain JSON-serializable and human-readable.
- **API call safety**: Always check `can_make_calls()` before making Amadeus requests. The monthly cap exists to prevent billing — never bypass it.
- **Active hours**: The script self-skips outside the configured active window. This is intentional, not a bug.

## Common tasks

### Add a new route
Edit `routes.json`. Each entry needs at minimum `origin`, `destination`, and `departure_date` (IATA codes and ISO date).

### Change alert sensitivity
Set the `ALERT_THRESHOLD_PCT` environment variable. Lower = more alerts.

### Reset API call counter
Delete the current month's entry from `state.json` → `api_calls`, or delete `state.json` entirely (prices baseline will also reset).

## Guardrails

- The `MONTHLY_CALL_CAP` (default 1900) is set 100 calls below the Amadeus free tier limit of 2000. Do not raise it above 2000 unless the user has a paid plan.
- `state.json` is committed by GitHub Actions with `[skip ci]` in the commit message to prevent recursive workflow triggers.
- Credentials are stored as GitHub repository secrets, never in code or config files.
