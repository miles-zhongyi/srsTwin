#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Extract TELUS LTE RRC Connection Request fields from a 22_decoded JSON trace file
and emit shell variable assignments that srsUE's entrypoint will eval + export:

  RRC_TRACE_M_TMSI=<decimal uint32>
  RRC_TRACE_CAUSE=<string, e.g. "mo-Signalling">

The extracted values replace the random UE identity and establishment cause in
srsUE's rrcSetupRequest so the live NR attach carries real TELUS subscriber data.
srsUE's own ASN.1 PER encoder and the ZMQ IQ transport path are unchanged.

Usage:
  python3 rrc_trace_fields.py <22_decoded_json_file>
"""
from __future__ import annotations

import json
import sys

# record_id 12 is RRC_RRC_CONNECTION_REQUEST in CallFlow/record-id-messages.txt
_RID_RRC_CONN_REQ = 12

# LTE → NR cause string mapping (identical names, different hyphen/underscore)
_CAUSE_MAP: dict[str, str] = {
    "emergency":           "emergency",
    "highPriorityAccess":  "highPriorityAccess",
    "mt-Access":           "mt-Access",
    "mo-Signalling":       "mo-Signalling",
    "mo-Data":             "mo-Data",
    "mo-VoiceCall":        "mo-VoiceCall",
    "mo-VideoCall":        "mo-VideoCall",
    "mo-SMS":              "mo-SMS",
    "mps-PriorityAccess":  "mps-PriorityAccess",
    "mcs-PriorityAccess":  "mcs-PriorityAccess",
}
_DEFAULT_CAUSE = "mo-Signalling"


def _iter_records(path: str):
    """Stream JSON objects from a top-level array without loading the full file."""
    dec = json.JSONDecoder()
    with open(path, encoding="utf-8", errors="replace") as fp:
        buf = ""
        while "[" not in buf:
            extra = fp.read(65536)
            if not extra:
                return
            buf += extra
        pos = buf.index("[") + 1

        while True:
            buf = buf[pos:].lstrip()
            pos = 0
            if not buf:
                extra = fp.read(262144)
                if not extra:
                    return
                buf = extra
                continue
            if buf[pos] == "]":
                return
            while True:
                try:
                    obj, end = dec.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    extra = fp.read(262144)
                    if not extra:
                        return
                    buf += extra
                    continue
                yield obj
                pos = end
                while pos < len(buf) and buf[pos] in ", \r\n\t":
                    pos += 1
                if pos < len(buf) and buf[pos] == "]":
                    return
                break


def _extract_cause(rec: dict) -> str:
    """Try to pull LTE establishmentCause out of the decoded message tree."""
    try:
        decoded = rec.get("decoded") or {}
        msg = decoded.get("message") or []
        # LTE UL-CCCH pycrate shape:
        # ["c1", ["rrcConnectionRequest", {"criticalExtensions": ["rrcConnectionRequest-r8", {
        #     "ue-Identity": [...], "establishmentCause": "mo-Signalling" }]}]]
        if isinstance(msg, list) and len(msg) >= 2:
            inner_list = msg[1]
            if isinstance(inner_list, list) and len(inner_list) >= 2:
                ies_wrapper = inner_list[1]
                if isinstance(ies_wrapper, dict):
                    exts = ies_wrapper.get("criticalExtensions") or []
                    if isinstance(exts, list) and len(exts) >= 2:
                        ies = exts[1] or {}
                        cause = ies.get("establishmentCause")
                        if cause and cause in _CAUSE_MAP:
                            return _CAUSE_MAP[cause]
    except Exception:
        pass
    return _DEFAULT_CAUSE


def _find_record(path: str) -> dict | None:
    """Return first record with record_id=12 and a non-zero m_tmsi."""
    for rec in _iter_records(path):
        if rec.get("record_id") == _RID_RRC_CONN_REQ and rec.get("m_tmsi"):
            return rec
    # Fallback: any RRC record with a usable m_tmsi
    for rec in _iter_records(path):
        if rec.get("interface") == "RRC" and rec.get("m_tmsi"):
            return rec
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: rrc_trace_fields.py <trace_json_file>", file=sys.stderr)
        return 1

    path = sys.argv[1]
    rec = _find_record(path)
    if rec is None:
        print(f"# rrc_trace_fields: no usable record in {path}", file=sys.stderr)
        return 0  # non-fatal: srsUE falls back to its own random value

    m_tmsi = int(rec.get("m_tmsi") or 0) & 0xFFFFFFFF
    cause = _extract_cause(rec)

    # Shell-eval-safe: no quotes needed, values are plain alphanumeric/hyphen.
    # Emit both NR and LTE variable names so the same script works for both stacks.
    print(f"RRC_TRACE_M_TMSI={m_tmsi}")
    print(f"RRC_TRACE_CAUSE={cause}")
    print(f"RRC_TRACE_LTE_M_TMSI={m_tmsi}")
    print(f"RRC_TRACE_LTE_CAUSE={cause}")
    print(
        f"# Loaded from record_id={rec.get('record_id')} "
        f"plmn={rec.get('serving_plmn','?')} "
        f"enb={rec.get('enb_id','?')} cell={rec.get('cell_id','?')} "
        f"ts={rec.get('timestamp','?')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
