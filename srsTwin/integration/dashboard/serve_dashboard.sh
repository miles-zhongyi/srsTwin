#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
# Start srsTwin dashboard on localhost.
#
# Usage:
#   bash serve_dashboard.sh              # direct mode
#   bash serve_dashboard.sh hub          # hub overlay for log pull
#   bash serve_dashboard.sh direct --pull
set -e
cd "$(dirname "$0")"
PY=""
for c in python3 python py; do
  if "$c" -c "import sys" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then echo "ERROR: no Python 3 found." >&2; exit 1; fi

MODE="${1:-direct}"
shift || true
exec "$PY" serve_dashboard.py --mode "$MODE" "$@"
