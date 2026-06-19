#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
#
# Storm UE lifecycle: attach -> brief data -> detach, then exit.
#
# The container exits when the cycle finishes, so the orchestrator can `docker
# start` the same container again for the next scheduled arrival (slot recycle).
# One run == one full signaling cycle (RACH/RRC/NAS register -> deregister) — the
# churn that makes a signaling storm.
#
# Markers on stdout (consumed by storm/metrics.py via `docker logs`):
#   LIFECYCLE start    t=<unix>
#   LIFECYCLE attached t=<unix> latency=<s> ip=<ip>
#   LIFECYCLE failed   t=<unix> reason=<...>
#   LIFECYCLE detach   t=<unix>
#   LIFECYCLE done     t=<unix>
#
# Tunables (env): PING_COUNT, PING_TARGET, ATTACH_TIMEOUT, IDLE_AFTER.
set -u

CONF="${1:-/ue_zmq.conf}"
PING_COUNT="${PING_COUNT:-5}"
PING_TARGET="${PING_TARGET:-10.45.0.1}"
ATTACH_TIMEOUT="${ATTACH_TIMEOUT:-90}"
IDLE_AFTER="${IDLE_AFTER:-0}"

now() { date +%s; }
mark() { echo "LIFECYCLE $* t=$(now)"; }

t_start=$(now)
mark start
srsue "$CONF" &
UE_PID=$!

# Wait for tun_srsue to get an IPv4 (== PDU session established) or time out.
ip=""
while [ "$(( $(now) - t_start ))" -lt "$ATTACH_TIMEOUT" ]; do
  if ! kill -0 "$UE_PID" 2>/dev/null; then
    mark failed reason=srsue_exited_early
    wait "$UE_PID" 2>/dev/null
    exit 1
  fi
  ip=$(ip -4 addr show tun_srsue 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}')
  [ -n "$ip" ] && break
  sleep 1
done

if [ -z "$ip" ]; then
  mark failed reason=attach_timeout
  kill -INT "$UE_PID" 2>/dev/null
  wait "$UE_PID" 2>/dev/null
  exit 2
fi
mark attached latency="$(( $(now) - t_start ))" ip="$ip"

# Brief data plane: a short ping burst over the ZMQ radio.
if [ "$PING_COUNT" -gt 0 ]; then
  ping -c "$PING_COUNT" -I tun_srsue "$PING_TARGET" || echo "[lifecycle] ping had losses"
fi

[ "$IDLE_AFTER" -gt 0 ] && sleep "$IDLE_AFTER"

# Detach: SIGINT makes srsUE deregister cleanly (NAS Deregistration).
mark detach
kill -INT "$UE_PID" 2>/dev/null
wait "$UE_PID" 2>/dev/null
mark done
