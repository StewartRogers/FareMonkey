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
    if [ "$PY_OK" = "1" ]; then
        ok "python3 $PY_VER"
    else
        fail "python3 found but is $PY_VER (need 3.9+)"
        missing=1
    fi
else
    fail "python3 not found"
    missing=1
fi

# --- pip -----------------------------------------------------------------
if python3 -m pip --version >/dev/null 2>&1; then
    ok "pip ($(python3 -m pip --version | awk '{print $2}'))"
else
    fail "pip not found for python3"
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

# --- Python dependencies ----------------------------------------------------
echo
echo "Checking Python dependencies from requirements.txt ..."

deps_satisfied() {
    python3 - <<'EOF'
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

if deps_satisfied >/tmp/faremonkey_deps_check.$$ 2>&1; then
    ok "Python dependencies already satisfy requirements.txt — nothing to install"
    rm -f /tmp/faremonkey_deps_check.$$
else
    cat /tmp/faremonkey_deps_check.$$
    rm -f /tmp/faremonkey_deps_check.$$
    if python3 -m pip install --user -r requirements.txt 2>/tmp/faremonkey_pip_err.$$; then
        ok "Python dependencies installed"
        rm -f /tmp/faremonkey_pip_err.$$
    elif grep -q "externally-managed-environment" /tmp/faremonkey_pip_err.$$; then
        rm -f /tmp/faremonkey_pip_err.$$
        warn "This Python is \"externally managed\" (Debian/Raspberry Pi OS PEP 668) and refuses plain pip installs."
        echo "  You said no virtual environment, so the remaining option is --break-system-packages,"
        echo "  which installs into the system Python directly and *can* conflict with apt-managed packages."
        read -r -p "  Install with --break-system-packages? [y/N] " reply
        if [[ "$reply" =~ ^[Yy]$ ]]; then
            python3 -m pip install --user --break-system-packages -r requirements.txt
            ok "Python dependencies installed (--break-system-packages)"
        else
            fail "Dependencies not installed — install manually or re-run and accept --break-system-packages"
            exit 1
        fi
    else
        cat /tmp/faremonkey_pip_err.$$
        rm -f /tmp/faremonkey_pip_err.$$
        fail "pip install failed"
        exit 1
    fi
fi

# --- Optional dev dependency for the test suite -----------------------------
if ! python3 -m pip show pytest >/dev/null 2>&1; then
    echo
    read -r -p "pytest is not installed and is needed to run tests/. Install it now? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        python3 -m pip install --user pytest
        ok "pytest installed"
    else
        warn "Skipped — run 'pip install pytest' later if you want to run the test suite."
    fi
else
    ok "pytest ($(python3 -m pip show pytest | awk '/^Version:/{print $2}'))"
fi

# --- Local config files ------------------------------------------------------
echo
if [ ! -f .env ]; then
    cp .env.example .env
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
echo "Next steps:"
echo "  1. Edit .env with your SERPAPI_API_KEY (and Telegram credentials, optional)."
echo "  2. Edit routes.json with your routes, or use the dashboard's route editor."
echo "  3. Run 'python3 flight_monitor.py' to do a manual check."
echo "  4. Run 'python3 app.py' to start the dashboard at http://localhost:5000"
