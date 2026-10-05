#!/usr/bin/env bash
# Start the FareMonkey Flask dashboard using the project's virtual environment.
#
# Always launch the dashboard through this script (or .venv/bin/python app.py)
# rather than a bare `python app.py`: the schedule editor writes crontab entries
# using sys.executable, so starting the app with the system Python would install
# cron lines pointing at an interpreter that has no dependencies installed.
#
# Any arguments and environment variables are passed through, e.g.:
#   PORT=8080 ./startweb.sh
#   FLASK_DEBUG=true ./startweb.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

VENV_PY="$PWD/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "  [MISSING] No virtual environment at .venv" >&2
    echo "  Run ./setup.sh first to create it and install dependencies." >&2
    exit 1
fi

if ! "$VENV_PY" -c "import flask" >/dev/null 2>&1; then
    echo "  [MISSING] Flask is not installed in .venv" >&2
    echo "  Run ./setup.sh (or .venv/bin/pip install -r requirements.txt)." >&2
    exit 1
fi

echo "Starting FareMonkey dashboard with $VENV_PY"
if [[ "${FLASK_DEBUG:-false}" =~ ^(1|true|yes)$ ]]; then
    echo "Dashboard will be at http://localhost:${PORT:-5000} — debug mode, this machine only  (Ctrl-C to stop)"
else
    echo "Dashboard will be at http://localhost:${PORT:-5000} and on this machine's LAN address  (Ctrl-C to stop)"
    echo "No login: anyone on your network can use it. Only run it on a trusted network."
fi
echo

# exec so Ctrl-C / systemd signals reach Python directly instead of this wrapper.
exec "$VENV_PY" app.py "$@"
