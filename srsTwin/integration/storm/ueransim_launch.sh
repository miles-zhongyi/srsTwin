#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
#
# Launch a batch of UERANSIM UEs inside the ueransim container. The orchestrator
# `docker exec`s this once per arrival bucket, timed by the storm pattern:
#
#   ueransim_launch.sh <imsi-digits> <count>
#
# `nr-ue -n <count> -i <imsi>` carries `count` UEs in one process, each
# registering + establishing its PDU session against the AMF — the scale-layer
# signaling load. Logs go to /tmp so metrics can count registrations.
set -u
IMSI="${1:?usage: ueransim_launch.sh <imsi> <count>}"
COUNT="${2:-1}"

mkdir -p /tmp/uelogs
nohup nr-ue -c /ue.yaml -i "$IMSI" -n "$COUNT" \
    >>"/tmp/uelogs/ue_${IMSI}.log" 2>&1 &
echo "UERANSIM launched n=$COUNT from imsi-$IMSI pid=$!"
