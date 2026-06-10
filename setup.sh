#!/usr/bin/env bash
#
# One-time environment setup (Linux / HPC).
# Creates a local Python virtualenv, installs dependencies, and downloads the
# headless Chromium browser used by 'browser'-mode targets.
#
# Run this once after cloning the repo:
#   ./setup.sh
#
# The run_*.sh scripts auto-detect the venv created here at .venv/bin/python.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python)}"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "No python3/python found on PATH. Load a Python module first (e.g. 'module load python')." >&2
    exit 1
fi

echo "Using interpreter: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

# A venv is not portable across machines — always (re)build it locally.
if [[ ! -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    echo "Creating virtualenv at .venv ..."
    "$PYTHON_BIN" -m venv .venv
else
    echo "Reusing existing .venv"
fi

VENV_PY="$SCRIPT_DIR/.venv/bin/python"

echo "Upgrading pip ..."
"$VENV_PY" -m pip install --upgrade pip

echo "Installing dependencies from requirements.txt ..."
"$VENV_PY" -m pip install -r requirements.txt

echo "Installing Chromium for Playwright (needed for 'browser'-mode targets) ..."
# On HPC nodes without root, --with-deps may fail; fall back to browser-only.
"$VENV_PY" -m playwright install chromium || \
    echo "WARNING: 'playwright install chromium' failed. browser-mode targets won't work, but html/api/eightfold targets will." >&2

echo
echo "Setup complete. Start the monitor with:"
echo "  ./run_periodic_monitor.sh config_all.json 15"
