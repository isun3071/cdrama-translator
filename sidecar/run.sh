#!/usr/bin/env bash
# Launch the sidecar. Run from anywhere; it cd's to its own dir so the flat
# module imports (app, contract, ocr, ...) resolve. Ctrl-C to stop.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
