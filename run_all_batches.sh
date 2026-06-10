#!/usr/bin/env bash
#
# Run all batch configs sequentially, once each (Linux / HPC).
# Each batch's combined stdout+stderr is captured to logs/<timestamp>_<batch>.log
#
# Usage:
#   ./run_all_batches.sh [--dry-run] [--continue-on-error]
#
set -uo pipefail

DRY_RUN=0
CONTINUE_ON_ERROR=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)            DRY_RUN=1 ;;
        --continue-on-error)  CONTINUE_ON_ERROR=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$WORKSPACE/job_monitor.py"

if [[ -x "$WORKSPACE/.venv/bin/python" ]]; then
    PYTHON="$WORKSPACE/.venv/bin/python"
else
    PYTHON="$(command -v python3 || command -v python)"
fi

if [[ -z "$PYTHON" ]]; then
    echo "No python interpreter found (looked for .venv/bin/python, python3, python)." >&2
    exit 1
fi
if [[ ! -f "$SCRIPT_PATH" ]]; then
    echo "job_monitor.py not found: $SCRIPT_PATH" >&2
    exit 1
fi

BATCHES=(
    "config_batch_1.json"
    "config_batch_2.json"
    "config_batch_3.json"
    "config_batch_4.json"
)

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="$WORKSPACE/logs"
mkdir -p "$LOG_DIR"

echo "Workspace:        $WORKSPACE"
echo "Python:           $PYTHON"
echo "DryRun:           $DRY_RUN"
echo "ContinueOnError:  $CONTINUE_ON_ERROR"
echo

declare -a SUMMARY

for batch in "${BATCHES[@]}"; do
    batch_path="$WORKSPACE/$batch"
    base="${batch%.json}"
    log_path="$LOG_DIR/${TIMESTAMP}_${base}.log"

    if [[ ! -f "$batch_path" ]]; then
        echo "Missing batch config: $batch_path" >&2
        SUMMARY+=("$batch | missing | 0s")
        if [[ "$CONTINUE_ON_ERROR" -eq 0 ]]; then
            exit 1
        fi
        continue
    fi

    echo "=== Running $batch ==="

    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "[DRY-RUN] $PYTHON $SCRIPT_PATH --config $batch"
        SUMMARY+=("$batch | dry-run | 0s")
        echo
        continue
    fi

    start=$(date +%s)
    "$PYTHON" "$SCRIPT_PATH" --config "$batch" >"$log_path" 2>&1
    exit_code=$?
    end=$(date +%s)
    duration=$((end - start))

    if [[ "$exit_code" -eq 0 ]]; then
        status="ok"
        echo "Completed $batch in ${duration}s"
    else
        status="failed(exit $exit_code)"
        echo "Failed $batch (exit $exit_code)" >&2
    fi

    echo "Last log lines ($batch):"
    tail -n 8 "$log_path" || true
    SUMMARY+=("$batch | $status | ${duration}s")

    if [[ "$exit_code" -ne 0 && "$CONTINUE_ON_ERROR" -eq 0 ]]; then
        echo "Batch failed: $batch (exit $exit_code)" >&2
        exit "$exit_code"
    fi
    echo
done

echo "=== Summary ==="
printf '%s\n' "${SUMMARY[@]}"
