#!/usr/bin/env bash
# FareMonkey setup: checks for required software and installs missing Python
# dependencies. Safe to re-run any time.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

ok()   { echo "  [OK]   $1"; }
warn() { echo "  [WARN] $1"; }
fail() { echo "  [MISSING] $1"; }

echo "FareMonkey setup"
echo "================"

missing=0

# --- Python 3.9+ -------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')
    # Supported and CI-tested range: 3.9 (Raspberry Pi OS bullseye) through 3.13.
    PY_NEW=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 14) else 0)')
    if [ "$PY_OK" = "1" ] && [ "$PY_NEW" = "0" ]; then
        ok "python3 $PY_VER"
    elif [ "$PY_OK" = "1" ]; then
        warn "python3 $PY_VER is newer than the tested range (3.9–3.13) — probably fine, but untested"
    else
        fail "python3 found but is $PY_VER (need 3.9+)"
        missing=1
    fi
else
    fail "python3 not found"
    missing=1
fi

# --- venv ------------------------------------------------------------------
# Dependencies live in a project-local virtual environment (.venv), never in the
# system Python. Debian/Raspberry Pi OS mark their Python "externally managed"
# (PEP 668) and refuse plain pip installs; a venv is the supported way around
# that, and it keeps this project's packages from shadowing apt-managed ones.
if python3 -m venv --help >/dev/null 2>&1; then
    ok "venv module available"
else
    fail "python3 venv module not found"
    missing=1
fi

# --- git -------------------------------------------------------------------
if command -v git >/dev/null 2>&1; then
    ok "git ($(git --version | awk '{print $3}'))"
else
    fail "git not found"
    missing=1
fi

# --- cron (only relevant if this host runs the scheduled monitor) ----------
if command -v crontab >/dev/null 2>&1; then
    ok "cron (crontab available)"
else
    warn "crontab not found — only needed on the host that runs the scheduled monitor"
fi

if [ "$missing" = "1" ]; then
    echo
    echo "One or more required tools are missing."
    if command -v apt-get >/dev/null 2>&1; then
        echo "On Debian/Raspberry Pi OS you can install them with:"
        echo "  sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv git cron"
    fi
    echo "Re-run this script after installing them."
    exit 1
fi

# --- Virtual environment ------------------------------------------------------
echo
VENV_DIR=".venv"
VENV_PY="$PWD/$VENV_DIR/bin/python"

if [ -x "$VENV_PY" ]; then
    ok "virtual environment already exists ($VENV_DIR)"
else
    echo "Creating virtual environment in $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
    ok "virtual environment created ($VENV_DIR)"
fi

"$VENV_PY" -m pip install --quiet --upgrade pip

# --- Python dependencies ----------------------------------------------------
echo
echo "Checking Python dependencies from requirements.txt ..."

deps_satisfied() {
    "$VENV_PY" - <<'EOF'
import importlib.metadata as m
import re
import sys
import zoneinfo

ok = True
with open("requirements.txt") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.match(r"[A-Za-z0-9_.\-]+", line).group(0)
        try:
            m.version(name)
            continue
        except m.PackageNotFoundError:
            pass
        if name == "tzdata":
            # The PyPI tzdata package is only a fallback for platforms without
            # an OS-level IANA timezone database (e.g. Windows, slim containers).
            # If zoneinfo can already resolve a real zone, the system has one.
            try:
                zoneinfo.ZoneInfo("America/New_York")
                continue
            except zoneinfo.ZoneInfoNotFoundError:
                pass
        print(f"missing: {name}")
        ok = False
sys.exit(0 if ok else 1)
EOF
}

DEPS_CHECK_TMP="$(mktemp)"
if deps_satisfied >"$DEPS_CHECK_TMP" 2>&1; then
    ok "Python dependencies already satisfy requirements.txt — nothing to install"
    rm -f "$DEPS_CHECK_TMP"
else
    cat "$DEPS_CHECK_TMP"
    rm -f "$DEPS_CHECK_TMP"
    if "$VENV_PY" -m pip install -r requirements.txt; then
        ok "Python dependencies installed into $VENV_DIR"
    else
        fail "pip install failed"
        exit 1
    fi
fi

# --- Optional dev dependency for the test suite -----------------------------
if ! "$VENV_PY" -m pip show pytest >/dev/null 2>&1; then
    echo
    read -r -p "pytest is not installed and is needed to run tests/. Install it now? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        "$VENV_PY" -m pip install pytest
        ok "pytest installed"
    else
        warn "Skipped — run '$VENV_DIR/bin/pip install pytest' later if you want to run the test suite."
    fi
else
    ok "pytest ($("$VENV_PY" -m pip show pytest | awk '/^Version:/{print $2}'))"
fi

# --- Local config files ------------------------------------------------------
echo
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    warn "Created .env from .env.example — edit it and fill in your credentials."
else
    ok ".env already exists"
fi

if [ ! -f routes.json ]; then
    cp routes.example.json routes.json
    warn "Created routes.json from routes.example.json — edit it with your routes."
else
    ok "routes.json already exists"
fi

echo
echo "Setup complete."
echo "Everything is installed in $VENV_DIR — always run the project with"
echo "$VENV_DIR/bin/python (no 'activate' needed, the path is enough)."
echo
echo "Next steps:"
echo "  1. Edit .env with your SERPAPI_API_KEY (and Telegram credentials, optional)."
echo "  2. Edit routes.json with your routes, or use the dashboard's route editor."
echo "  3. Run '$VENV_DIR/bin/python flight_monitor.py' to do a manual check."
echo "  4. Run '$VENV_DIR/bin/python app.py' to start the dashboard at http://localhost:5000"
echo "  5. Schedule it with 'crontab -e' using the venv interpreter:"
echo "     30 7,13,19 * * * cd $PWD && $VENV_PY flight_monitor.py >/dev/null 2>&1"
