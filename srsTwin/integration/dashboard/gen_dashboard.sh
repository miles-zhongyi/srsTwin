#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
#
# Pull logs from running srsTwin containers and regenerate the dashboard.
#
# Usage (from integration/dashboard):
#   bash gen_dashboard.sh              # direct mode (default compose)
#   bash gen_dashboard.sh hub          # hub overlay compose
#
# Open:  index.html  (Overview + Signaling flow + Messages)
set -e
cd "$(dirname "$0")"
mkdir -p logs

MODE="${1:-direct}"
COMPOSE=(docker compose -f ../docker-compose.yml)
if [ "$MODE" = hub ]; then
  COMPOSE+=(-f ../docker-compose.hub.yml)
fi

echo "== Pulling logs (radio mode: $MODE) =="
"${COMPOSE[@]}" cp gnb:/tmp/gnb.log logs/gnb.log 2>/dev/null || echo "  (gnb log unavailable)"
"${COMPOSE[@]}" cp srsue:/tmp/ue.log logs/ue.log 2>/dev/null || echo "  (srsue log unavailable)"
if [ "$MODE" = hub ]; then
  "${COMPOSE[@]}" cp hub:/tmp/stdout  logs/hub.log 2>/dev/null || \
  "${COMPOSE[@]}" logs hub > logs/hub.log 2>/dev/null || echo "  (hub log unavailable)"
else
  : > logs/hub.log
fi

echo "== Generating dashboard =="
PY=""
for c in python3 python py; do
  if "$c" -c "import sys" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "ERROR: no Python 3 found." >&2
  exit 1
fi
"$PY" parse_callflow.py

echo
echo "Done. Open: $(pwd)/index.html"
echo "  Overview · Signaling flow ladder · Searchable message log"
