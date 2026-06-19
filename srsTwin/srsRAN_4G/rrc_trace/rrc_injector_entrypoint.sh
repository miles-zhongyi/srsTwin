#!/bin/sh
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
#
# srsUE entrypoint: inject TELUS LTE trace identity into RRC Connection Request
# before launching srsue, for both the LTE (4G) and NR (5G) paths.
#
# When RRC_TRACE_DIR is set (mounted volume of 22_decoded JSON files), the first
# RRC Connection Request record is scanned and its m_tmsi / establishmentCause
# are exported as:
#   RRC_TRACE_M_TMSI / RRC_TRACE_CAUSE          — read by the patched rrc_nr.cc (5G NR)
#   RRC_TRACE_LTE_M_TMSI / RRC_TRACE_LTE_CAUSE  — read by the patched rrc.cc (4G LTE)
#
# If the scan fails for any reason, srsUE falls back to its own random values —
# the ZMQ IQ attach still works, just without the trace identity.

set -e

if [ -n "${RRC_TRACE_DIR}" ] && [ -d "${RRC_TRACE_DIR}" ]; then
    TRACE_FILE=$(find "${RRC_TRACE_DIR}" -maxdepth 2 -name "*.json" -type f \
                 | sort | head -1)
    if [ -n "${TRACE_FILE}" ]; then
        echo "[rrc-inject] Scanning trace: ${TRACE_FILE}"
        # Emit variable assignments; lines starting with '#' are ignored by eval.
        # rrc_trace_fields.py now emits both NR and LTE variable names.
        FIELDS=$(python3 /rrc_trace_fields.py "${TRACE_FILE}" 2>/dev/stderr \
                 | grep -v '^#') || true
        if [ -n "${FIELDS}" ]; then
            eval "${FIELDS}"
            export RRC_TRACE_M_TMSI RRC_TRACE_CAUSE
            export RRC_TRACE_LTE_M_TMSI RRC_TRACE_LTE_CAUSE
            echo "[rrc-inject] UE identity: m_tmsi=${RRC_TRACE_M_TMSI}  cause=${RRC_TRACE_CAUSE}"
        else
            echo "[rrc-inject] No usable RRC record found; using srsUE random identity"
        fi
    else
        echo "[rrc-inject] No JSON files in ${RRC_TRACE_DIR}; using srsUE random identity"
    fi
else
    echo "[rrc-inject] RRC_TRACE_DIR not set; using srsUE random identity"
fi

exec srsue "$@"
