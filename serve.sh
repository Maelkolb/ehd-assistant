#!/usr/bin/env bash
# Start the Humboldt assistant locally on http://localhost:8000
# Uses the repo's .venv and the API key from .env automatically.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON=".venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "No .venv found — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

if [ ! -f "data/processed/chunk_store.json" ]; then
    echo "No indexes found — run: ./run.sh fetch && ./run.sh ingest"
    exit 1
fi

# Free the port if a previous instance is still holding it
fuser -k 8000/tcp 2>/dev/null && sleep 1 || true

echo "Starting on http://localhost:8000 (Ctrl-C to stop)"
exec env PYTHONPATH=. "$PYTHON" -m uvicorn src.api.main:app --host 0.0.0.0 --port 8000
