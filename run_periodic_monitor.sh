#!/usr/bin/env bash
#
# Periodic job monitor runner (Linux / HPC).
# Designed to run 24/7 inside a tmux session.
#
# Usage:
#   ./run_periodic_monitor.sh [config_file] [interval_minutes]
#
# Examples:
#   ./run_periodic_monitor.sh                       # config_all.json, every 15 min
#   ./run_periodic_monitor.sh config_batch_1.json 30
#
set -euo pipefail

CONFIG="${1:-config_all.json}"
INTERVAL_MINUTES="${2:-15}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer a Linux virtualenv if one exists, else fall back to python3 on PATH.
if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
else
    PYTHON="$(command -v python3 || command -v python)"
fi

if [[ -z "$PYTHON" ]]; then
    echo "No python interpreter found (looked for .venv/bin/python, python3, python)." >&2
    exit 1
fi

echo "Starting periodic job monitor"
echo "Python:            $PYTHON"
echo "Config:            $CONFIG"
echo "Interval (min):    $INTERVAL_MINUTES"
echo

exec "$PYTHON" "$SCRIPT_DIR/job_monitor.py" \
    --config "$CONFIG" \
    --interval-minutes "$INTERVAL_MINUTES"
