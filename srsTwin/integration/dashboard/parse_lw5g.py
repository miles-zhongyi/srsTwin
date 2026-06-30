"""
parse_lw5g.py  –  Lightweight 5G Twin log parser
=================================================
Reads /tmp/gnb.log produced by the OCUDU gnb in test-mode (ru_dummy +
test_mode.test_ue) and extracts:

  events   – ordered list of {ts, ue_id, rnti, src, dst, label, dir, phase}
             ready for the signaling-ladder renderer
  ue_kpis  – per-UE dicts with attach_latency_ms, pdu_latency_ms, cycles,
             success, fail
  summary  – aggregate KPIs: total_attach, success_rate, median_latency_ms,
             p90_latency_ms, reg_rate_per_min, active_ues

The gnb log lines look like:
  2026-06-09T12:40:01.174331 [RRC     ] [D] ue=0 c-rnti=0x4601: Rx SRB0 CCCH UL rrcSetupRequest (6 B)
  2026-06-09T12:40:01.247095 [NGAP    ] [I] Rx PDU ue=0 ran_ue=0 amf_ue=1: DownlinkNASTransport
"""
from __future__ import annotations

import os
import re
import statistics
import subprocess
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Log tailing – reads last N lines from the gnb.log docker volume copy
# (serve_dashboard.py provides the resolved path in LOG_DIR)
# ---------------------------------------------------------------------------

LOG_TAIL_LINES = 200_000  # ~20 min of gnb log at debug level

# ---------------------------------------------------------------------------
# Pattern → event mapping
# Each tuple: (compiled_re, src_node, dst_node, label, direction, phase)
# direction: "up" UE→AMF, "down" AMF→UE, "internal" gNB-only
# phase:     attach | nas | security | bearer | release | setup
# ---------------------------------------------------------------------------
_TS_PAT = r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)"
_UE_PAT = r"ue=(\d+)"
_RNTI_PAT = r"c-rnti=(0x[0-9a-fA-F]+)"

# Returns (ts_str, ue_id_str, rnti_str|None)  from a matched line
def _ue_rnti(m: re.Match, ts_g=1, ue_g=2, rnti_g=None):
    ts = m.group(ts_g)
    ue = m.group(ue_g)
    rnti = m.group(rnti_g) if rnti_g else None
    return ts, ue, rnti


PATTERNS = [
    # ---- RRC ----
    (
        re.compile(_TS_PAT + r".*\[RRC\s*\].*" + _UE_PAT + r".*" + _RNTI_PAT +
                   r".*Rx SRB0.*rrcSetupRequest"),
        "UE", "gNB", "RRC Setup Request", "up", "attach",
    ),
    (
        re.compile(_TS_PAT + r".*\[RRC\s*\].*" + _UE_PAT + r".*" + _RNTI_PAT +
                   r".*Tx SRB0.*rrcSetup\b"),
        "gNB", "UE", "RRC Setup", "down", "attach",
    ),
    (
        re.compile(_TS_PAT + r".*\[RRC\s*\].*" + _UE_PAT + r".*" + _RNTI_PAT +
                   r".*Rx SRB1.*rrcSetupComplete"),
        "UE", "gNB", "RRC Setup Complete", "up", "attach",
    ),
    (
        re.compile(_TS_PAT + r".*\[RRC\s*\].*" + _UE_PAT + r".*" + _RNTI_PAT +
                   r".*Awaiting RRC Security Mode Complete"),
        "gNB", "UE", "Security Mode Command", "down", "security",
    ),
    (
        re.compile(_TS_PAT + r".*\[RRC\s*\].*" + _UE_PAT + r".*" + _RNTI_PAT +
                   r".*Received RRC Security Mode Complete"),
        "UE", "gNB", "Security Mode Complete", "up", "security",
    ),
    (
        re.compile(_TS_PAT + r".*\[RRC\s*\].*" + _UE_PAT + r".*" + _RNTI_PAT +
                   r".*Tx SRB1.*rrcReconfiguration\b"),
        "gNB", "UE", "RRC Reconfiguration (Bearer)", "down", "bearer",
    ),
    (
        re.compile(_TS_PAT + r".*\[RRC\s*\].*" + _UE_PAT + r".*" + _RNTI_PAT +
                   r".*Rx SRB1.*rrcReconfigurationComplete"),
        "UE", "gNB", "RRC Reconfig Complete", "up", "bearer",
    ),
    (
        re.compile(_TS_PAT + r".*\[RRC\s*\].*" + _UE_PAT + r".*" + _RNTI_PAT +
                   r".*Tx SRB1.*rrcRelease"),
        "gNB", "UE", "RRC Release", "down", "release",
    ),
    # ---- NGAP (gNB ↔ AMF) ----
    # NOTE: in gnb.log the NGAP PDU lines have the form:
    #   [NGAP    ] [I] Tx PDU ue=X ran_ue=X amf_ue=Y: MessageName
    # so ue=X comes AFTER "Tx PDU" / "Rx PDU", not before.
    (
        re.compile(_TS_PAT + r".*\[NGAP\s*\].*Tx PDU\s+" + _UE_PAT + r".*InitialUEMessage"),
        "gNB", "AMF", "InitialUEMessage (Reg Req)", "up", "nas",
    ),
    (
        re.compile(_TS_PAT + r".*\[NGAP\s*\].*Rx PDU\s+" + _UE_PAT + r".*DownlinkNASTransport"),
        "AMF", "gNB", "DownlinkNASTransport", "down", "nas",
    ),
    (
        re.compile(_TS_PAT + r".*\[NGAP\s*\].*Tx PDU\s+" + _UE_PAT + r".*UplinkNASTransport"),
        "gNB", "AMF", "UplinkNASTransport", "up", "nas",
    ),
    (
        re.compile(_TS_PAT + r".*\[NGAP\s*\].*Rx PDU\s+" + _UE_PAT + r".*InitialContextSetupRequest"),
        "AMF", "gNB", "InitialContextSetup Req (Reg Accept)", "down", "nas",
    ),
    (
        re.compile(_TS_PAT + r".*\[NGAP\s*\].*Tx PDU\s+" + _UE_PAT + r".*InitialContextSetupResponse"),
        "gNB", "AMF", "InitialContextSetup Resp", "up", "nas",
    ),
    (
        re.compile(_TS_PAT + r".*\[NGAP\s*\].*Rx PDU\s+" + _UE_PAT + r".*PDUSessionResourceSetupRequest"),
        "AMF", "gNB", "PDU Session Setup Req", "down", "bearer",
    ),
    (
        re.compile(_TS_PAT + r".*\[NGAP\s*\].*Tx PDU\s+" + _UE_PAT + r".*PDUSessionResourceSetupResponse"),
        "gNB", "AMF", "PDU Session Setup Resp", "up", "bearer",
    ),
    # UEContextReleaseRequest has NO ue= in the line (Tx PDU: UEContextReleaseRequest)
    (
        re.compile(_TS_PAT + r".*\[NGAP\s*\].*Tx PDU:\s*UEContextReleaseRequest"),
        "gNB", "AMF", "UE Context Release Req", "up", "release",
    ),
    (
        re.compile(_TS_PAT + r".*\[NGAP\s*\].*Rx PDU\s+" + _UE_PAT + r".*UEContextReleaseCommand"),
        "AMF", "gNB", "UE Context Release Cmd", "down", "release",
    ),
]

# Pattern for NG Setup (no per-UE id)
_NG_SETUP_REQ = re.compile(_TS_PAT + r".*\[NGAP\s*\].*Tx PDU: NGSetupRequest")
_NG_SETUP_RSP = re.compile(_TS_PAT + r".*\[NGAP\s*\].*Rx PDU: NGSetupResponse")


def _parse_ts(ts_str: str) -> float:
    """Return POSIX timestamp (seconds since epoch)."""
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _tail_log(path: str, n: int) -> list[str]:
    """Return last n lines from a text file (or as many as exist)."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return lines[-n:]
    except OSError:
        return []


def _docker_tail(container: str, src_path: str, n: int) -> list[str]:
    """Tail src_path inside a running container."""
    try:
        result = subprocess.run(
            ["docker", "exec", container, "tail", "-n", str(n), src_path],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.splitlines(keepends=True)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def build_lw5g(log_path: Optional[str] = None,
               container: str = "srstwin_gnb") -> dict:
    """
    Parse gnb.log and return structured data for the dashboard.

    log_path: direct filesystem path to gnb.log (use when log is available
              via docker volume mount on the host).
    container: docker container name to exec-tail from when log_path is None.
    """
    if log_path and os.path.isfile(log_path):
        lines = _tail_log(log_path, LOG_TAIL_LINES)
    else:
        lines = _docker_tail(container, "/tmp/gnb.log", LOG_TAIL_LINES)

    if not lines:
        return _empty_result()

    events: list[dict] = []
    # per-UE state for latency tracking: ue_id → {rrc_req_ts, ics_ts, ...}
    ue_state: dict[str, dict] = {}
    ue_kpis: dict[str, dict] = {}
    ng_setup_done = False
    ng_setup_ts: Optional[float] = None
    gnb_start_ts: Optional[float] = None

    for line in lines:
        line = line.rstrip("\n")

        # NG Setup (gNB startup)
        m = _NG_SETUP_REQ.match(line)
        if m:
            ts = _parse_ts(m.group(1))
            gnb_start_ts = gnb_start_ts or ts
            events.append({
                "ts": ts, "ts_str": m.group(1),
                "ue_id": None, "rnti": None,
                "src": "gNB", "dst": "AMF",
                "label": "NG Setup Request",
                "dir": "up", "phase": "setup",
            })
            continue

        m = _NG_SETUP_RSP.match(line)
        if m:
            ts = _parse_ts(m.group(1))
            gnb_start_ts = gnb_start_ts or ts
            ng_setup_done = True
            ng_setup_ts = ts
            events.append({
                "ts": ts, "ts_str": m.group(1),
                "ue_id": None, "rnti": None,
                "src": "AMF", "dst": "gNB",
                "label": "NG Setup Response",
                "dir": "down", "phase": "setup",
            })
            continue

        # Per-UE events
        for (pat, src, dst, label, dir_, phase) in PATTERNS:
            m = pat.match(line)
            if not m:
                continue
            ts_str = m.group(1)
            ts = _parse_ts(ts_str)
            # Group 2 = ue_id (from _UE_PAT), group 3 = rnti (only RRC patterns)
            try:
                ue_id = m.group(2)
            except (IndexError, re.error):
                ue_id = None
            try:
                rnti = m.group(3)
            except (IndexError, re.error):
                rnti = None
            # rnti group will be None for NGAP patterns that have no c-rnti in the line
            if rnti and not rnti.startswith("0x"):
                rnti = None

            ev = {
                "ts": ts, "ts_str": ts_str,
                "ue_id": ue_id, "rnti": rnti,
                "src": src, "dst": dst,
                "label": label, "dir": dir_, "phase": phase,
                "raw": line[:120],
            }
            events.append(ev)

            # KPI tracking
            if ue_id is not None:
                st = ue_state.setdefault(ue_id, {})
                kpi = ue_kpis.setdefault(ue_id, {
                    "cycles": 0, "success": 0, "fail": 0,
                    "attach_latencies_ms": [], "pdu_latencies_ms": [],
                    "rnti": rnti,
                })
                if rnti:
                    kpi["rnti"] = rnti

                if label == "RRC Setup Request":
                    st["rrc_req_ts"] = ts
                    st["phase"] = "attaching"
                elif label == "InitialContextSetup Resp":
                    if "rrc_req_ts" in st:
                        lat_ms = (ts - st["rrc_req_ts"]) * 1000
                        kpi["attach_latencies_ms"].append(lat_ms)
                    st["ics_ts"] = ts
                    st["phase"] = "registered"
                    kpi["cycles"] += 1
                    kpi["success"] += 1
                elif label == "PDU Session Setup Req":
                    st["pdu_req_ts"] = ts
                elif label == "PDU Session Setup Resp":
                    if "pdu_req_ts" in st:
                        lat_ms = (ts - st["pdu_req_ts"]) * 1000
                        kpi["pdu_latencies_ms"].append(lat_ms)
                elif label == "RRC Release":
                    st["phase"] = "released"
            break  # only first matching pattern per line

    # Derive per-UE summary stats
    for ue_id, kpi in ue_kpis.items():
        lats = kpi["attach_latencies_ms"]
        kpi["median_attach_ms"] = round(statistics.median(lats), 1) if lats else None
        kpi["max_attach_ms"] = round(max(lats), 1) if lats else None
        plats = kpi["pdu_latencies_ms"]
        kpi["median_pdu_ms"] = round(statistics.median(plats), 1) if plats else None

    # Aggregate summary
    all_lats = [l for k in ue_kpis.values() for l in k["attach_latencies_ms"]]
    total_success = sum(k["success"] for k in ue_kpis.values())
    total_cycles = sum(k["cycles"] for k in ue_kpis.values())
    elapsed_min = 0.0
    if events and gnb_start_ts:
        elapsed_min = max((events[-1]["ts"] - gnb_start_ts) / 60.0, 0.01)

    # Active UEs = those not in "released" state
    active_ues = sum(
        1 for ue_id, st in ue_state.items()
        if st.get("phase") not in ("released", None)
    )

    summary = {
        "ng_setup_done": ng_setup_done,
        "total_attach": total_success,
        "total_cycles": total_cycles,
        "success_rate": round(total_success / max(total_cycles, 1) * 100, 1),
        "median_attach_ms": round(statistics.median(all_lats), 1) if all_lats else None,
        "p90_attach_ms": round(sorted(all_lats)[int(len(all_lats) * 0.9)], 1) if len(all_lats) >= 5 else None,
        "reg_rate_per_min": round(total_success / elapsed_min, 2) if elapsed_min > 0 else 0.0,
        "active_ues": active_ues,
        "nof_ues_seen": len(ue_kpis),
        "elapsed_min": round(elapsed_min, 2),
    }

    # Keep only last 200 events for the ladder (avoid huge payloads)
    # But always keep setup events
    setup_evs = [e for e in events if e["phase"] == "setup"]
    ue_evs = [e for e in events if e["phase"] != "setup"]
    trimmed = setup_evs + ue_evs[-180:]
    trimmed.sort(key=lambda e: e["ts"])

    return {
        "events": trimmed,
        "ue_kpis": ue_kpis,
        "summary": summary,
        "has_live": bool(lines),
    }


def _empty_result() -> dict:
    return {
        "events": [],
        "ue_kpis": {},
        "summary": {
            "ng_setup_done": False,
            "total_attach": 0,
            "total_cycles": 0,
            "success_rate": 0.0,
            "median_attach_ms": None,
            "p90_attach_ms": None,
            "reg_rate_per_min": 0.0,
            "active_ues": 0,
            "nof_ues_seen": 0,
            "elapsed_min": 0.0,
        },
        "has_live": False,
    }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json, sys

    log = sys.argv[1] if len(sys.argv) > 1 else None
    result = build_lw5g(log_path=log)
    print(f"Events parsed:  {len(result['events'])}")
    print(f"UEs seen:       {result['summary']['nof_ues_seen']}")
    print(f"Total attaches: {result['summary']['total_attach']}")
    print(f"Success rate:   {result['summary']['success_rate']}%")
    print(f"Median attach:  {result['summary']['median_attach_ms']} ms")
    print(f"Reg rate:       {result['summary']['reg_rate_per_min']} /min")
    if result["events"]:
        print("\nFirst 10 events:")
        for ev in result["events"][:10]:
            print(f"  [{ev['ts_str']}] ue={ev['ue_id']} {ev['src']}→{ev['dst']}: {ev['label']}")
