#!/usr/bin/env bash
# Splunk traffic dashboard launcher (see README.md).
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)"

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

echo "[dash] starting on http://127.0.0.1:8091  (Ctrl+C to stop)"
exec "$PY" -u -m traffic_dashboard.server
