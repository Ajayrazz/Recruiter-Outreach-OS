#!/usr/bin/env bash
# Starts the background worker and the dashboard. Ctrl+C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
python -m recruiter_os.worker & WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT INT TERM
streamlit run app.py
