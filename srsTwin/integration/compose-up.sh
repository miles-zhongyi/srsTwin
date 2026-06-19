#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
#
# Bring up srsTwin with direct ZMQ (default) or via the IQ hub.
#
# Usage:
#   bash compose-up.sh              # direct: 5gc + gnb + srsue
#   bash compose-up.sh hub          # hub:    5gc + gnb + hub + srsue
#   bash compose-up.sh hub multi    # hub + 2 UEs (srsue + srsue2)
#
# Then:  RADIO_MODE=direct bash verify.sh
#    or  RADIO_MODE=hub bash verify.sh
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-direct}"
MULTI="${2:-}"

COMPOSE=(docker compose -f docker-compose.yml)
if [ "$MODE" = hub ]; then
  COMPOSE+=(-f docker-compose.hub.yml)
elif [ "$MODE" != direct ]; then
  echo "Usage: $0 [direct|hub] [multi]" >&2
  exit 1
fi

echo "== srsTwin radio mode: $MODE =="

"${COMPOSE[@]}" build

"${COMPOSE[@]}" up -d 5gc
echo "Waiting for 5GC..."
until [ "$(docker inspect -f '{{.State.Health.Status}}' srstwin_5gc 2>/dev/null)" = healthy ]; do
  sleep 2
done

"${COMPOSE[@]}" up -d gnb
echo "Waiting for gNB cell..."
until "${COMPOSE[@]}" exec -T gnb sh -c "grep -qiE 'DU started successfully|Cell was activated|Cell scheduling was activated' /tmp/gnb.log" 2>/dev/null; do
  sleep 2
done

if [ "$MODE" = hub ]; then
  "${COMPOSE[@]}" up -d hub
  sleep 3
fi

"${COMPOSE[@]}" up -d srsue

if [ "$MULTI" = multi ]; then
  if [ "$MODE" != hub ]; then
    echo "Multi-UE requires hub mode: $0 hub multi" >&2
    exit 1
  fi
  "${COMPOSE[@]}" --profile multi up -d srsue2
fi

echo
echo "Stack is up ($MODE). Verify with:"
echo "  RADIO_MODE=$MODE bash verify.sh"
echo "Logs:"
if [ "$MODE" = hub ]; then
  echo "  ${COMPOSE[*]} logs -f hub gnb srsue"
else
  echo "  ${COMPOSE[*]} logs -f gnb srsue"
fi
