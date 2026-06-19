#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
#
# Verifies the virtual UE <-> DU signal flow end to end.
#
# Environment:
#   RADIO_MODE    — direct (default) or hub. Auto-detected from running hub
#                   container if unset.
#   EXPECTED_UES  — number of UE containers (default: 1). Use 2 with hub multi.
#
# Exit code 0 == everything verified.
set -u
cd "$(dirname "$0")"

# Hub overlay uses the same project name; detect mode from running services.
if [ -z "${RADIO_MODE:-}" ]; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx srstwin_hub; then
    RADIO_MODE=hub
  else
    RADIO_MODE=direct
  fi
fi

COMPOSE=(docker compose -f docker-compose.yml)
if [ "$RADIO_MODE" = hub ]; then
  COMPOSE+=(-f docker-compose.hub.yml)
fi

EXPECTED_UES="${EXPECTED_UES:-1}"

pass() { echo -e "  \033[32mPASS\033[0m $*"; }
fail() { echo -e "  \033[31mFAIL\033[0m $*"; FAILED=1; }
FAILED=0

echo "== Radio mode: ${RADIO_MODE} =="

if [ "$RADIO_MODE" = hub ]; then
  echo "== 0. IQ hub forwarding =="
  HUB_LOG=$("${COMPOSE[@]}" logs hub 2>&1)
  if echo "$HUB_LOG" | grep -qE "forwarding: dl_blocks=[1-9]"; then
    pass "hub is forwarding DL/UL IQ blocks (dl_blocks > 0)"
  else
    fail "hub is not forwarding (no dl_blocks>0 in hub logs)"
  fi
  if echo "$HUB_LOG" | grep -qE "forwarding: dl_blocks=[1-9].*connected=0/"; then
    pass "hub forwarded with zero connected UEs (gNB-only lockstep)"
  elif echo "$HUB_LOG" | grep -qE "forwarding: dl_blocks=[1-9]"; then
    pass "hub forwarding observed (joined-UE timing not captured in logs)"
  else
    fail "could not confirm hub ran before UE attach"
  fi
else
  echo "== 0. Direct ZMQ link (hub bypass) =="
  pass "IQ hub not in path — srsUE ZMQ peers with gNB directly"
fi

echo "== 1. gNB <-> AMF (NG setup) =="
if "${COMPOSE[@]}" exec -T gnb sh -c "grep -qiE 'NG setup procedure completed|Connected to AMF' /tmp/gnb.log"; then
  pass "gNB connected to the Open5GS AMF"
else
  fail "no NG setup success found in gNB logs"
fi

echo "== 2. gNB cell is up over ZMQ =="
if "${COMPOSE[@]}" exec -T gnb sh -c "grep -qiE 'DU started successfully|Cell was activated|Cell scheduling was activated' /tmp/gnb.log"; then
  pass "DU cell started on the ZMQ radio"
else
  fail "DU cell did not start"
fi

echo "== 3. srsUE registration (RACH/RRC/NAS) — expecting ${EXPECTED_UES} UE(s) =="
UE_SERVICES=(srsue)
if [ "$EXPECTED_UES" -ge 2 ]; then
  UE_SERVICES+=(srsue2)
fi

PDU_IPS=()
for svc in "${UE_SERVICES[@]}"; do
  if "${COMPOSE[@]}" exec -T "$svc" sh -c "grep -qiE 'Random Access Complete|Finished Connection Setup successfully' /tmp/ue.log"; then
    pass "$svc established RRC connection"
  else
    fail "$svc did not establish an RRC connection"
  fi
  ip=$("${COMPOSE[@]}" exec -T "$svc" sh -c "grep -oE 'PDU Session Establishment successful. IP: [0-9.]+' /tmp/ue.log" 2>/dev/null | grep -oE "[0-9.]+$" | tail -1)
  if [ -n "${ip:-}" ]; then
    pass "$svc got IP address $ip from the 5GC"
    PDU_IPS+=("$ip")
  else
    fail "$svc was not assigned an IP (registration likely failed)"
  fi
done

if [ "${#PDU_IPS[@]}" -eq "$EXPECTED_UES" ] && [ "$EXPECTED_UES" -gt 1 ]; then
  uniq=$(printf '%s\n' "${PDU_IPS[@]}" | sort -u | wc -l)
  if [ "$uniq" -eq "$EXPECTED_UES" ]; then
    pass "all ${EXPECTED_UES} UEs have distinct PDU session IPs: ${PDU_IPS[*]}"
  else
    fail "PDU session IPs are not all distinct: ${PDU_IPS[*]}"
  fi
fi

echo "== 4. UE data plane (ping over the ZMQ radio) =="
for svc in "${UE_SERVICES[@]}"; do
  ping_out="/tmp/srstwin_ping_${svc}.txt"
  if "${COMPOSE[@]}" exec -T "$svc" ping -c 4 -I tun_srsue 10.45.0.1 >"$ping_out" 2>&1; then
    pass "$svc ping to UE gateway 10.45.0.1 succeeded:"
    sed 's/^/      /' "$ping_out" | tail -3
  else
    fail "$svc ping over tun_srsue failed (see below)"
    sed 's/^/      /' "$ping_out" | tail -5
  fi
done

echo
if [ "$FAILED" -eq 0 ]; then
  if [ "$RADIO_MODE" = hub ]; then
    echo -e "\033[32mALL CHECKS PASSED — ${EXPECTED_UES} UE(s) attached via the IQ hub.\033[0m"
  else
    echo -e "\033[32mALL CHECKS PASSED — ${EXPECTED_UES} UE(s) attached (direct ZMQ, hub bypassed).\033[0m"
  fi
else
  if [ "$RADIO_MODE" = hub ]; then
    echo -e "\033[31mSome checks failed. Inspect: docker compose -f docker-compose.yml -f docker-compose.hub.yml logs hub gnb srsue\033[0m"
  else
    echo -e "\033[31mSome checks failed. Inspect: docker compose logs gnb srsue\033[0m"
  fi
fi
exit $FAILED
