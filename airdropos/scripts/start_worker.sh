#!/usr/bin/env bash
# AirdropOS 24/7 worker daemon.
#
# Runs the scheduler, claims and executes tasks, and recovers interrupted work.
# Intended to run under systemd or Docker, not in the foreground of a dev shell.
#
# Usage: ./scripts/start_worker.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found. Run: cp .env.example .env, then edit it." >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a
source .env
set +a

# shellcheck disable=SC1091
source .venv/bin/activate

echo "AirdropOS worker starting."
echo "  Fixed model : ${LLM_MODEL_ID:-unset} @ ${LLM_BASE_URL:-unset}"
echo "  Concurrency : ${MAX_CONCURRENT_TASKS:-5} tasks"
echo "  Tx limit    : \$${MAX_TRANSACTION_VALUE_USD:-50} per tx, \$${MAX_DAILY_GAS_SPEND_USD:-25} per day"

exec python -m backend.app.scheduler.worker
