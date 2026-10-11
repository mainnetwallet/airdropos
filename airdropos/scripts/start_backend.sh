#!/usr/bin/env bash
# AirdropOS backend startup script.
#
# Creates a virtual environment, installs dependencies, initializes the database
# if needed, and starts the API server.
#
# Usage: ./scripts/start_backend.sh

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

if [[ -z "${LLM_API_KEY:-}" || "${LLM_API_KEY}" == *"mock-placeholder"* ]]; then
  echo "WARNING: LLM_API_KEY is unset or still the placeholder value." >&2
  echo "         The agent will report LLM failures rather than silently falling back." >&2
fi

if [[ ! -d .venv ]]; then
  echo "Creating virtual environment..."
  python3.11 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r backend/requirements.txt

if [[ ! -d .venv/playwright-browsers ]]; then
  echo "Installing Playwright Chromium..."
  playwright install --with-deps chromium || \
    echo "WARNING: Playwright install failed. Browser tools will report UNAVAILABLE." >&2
fi

echo "Initializing database..."
python -m backend.app.services.init_db

echo "Starting API on ${BACKEND_HOST:-0.0.0.0}:${BACKEND_PORT:-8000}"
exec uvicorn backend.app.main:app \
  --host "${BACKEND_HOST:-0.0.0.0}" \
  --port "${BACKEND_PORT:-8000}" \
  --reload
