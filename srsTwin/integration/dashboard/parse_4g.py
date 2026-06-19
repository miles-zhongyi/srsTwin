#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
4G LTE log parser for the srsTwin dashboard.

Reads srsUE LTE log (ue4g.log) and srseNB log (enb.log) and produces:
  - events_4g: signaling ladder events for the 4G LTE tab
  - rrc_4g: RRC PDU list for the 4G RRC tab
  - trace_4g: 22_decoded records aligned with live messages for the 4G Trace tab

Signal flow lanes:
  UE (srsUE) | ZMQ IQ | eNB (srseNB) | EPC (srsEPC)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from message_catalog import lookup_message_info_4g

# ---------------------------------------------------------------------------
# Log timestamp regex — same format as parse_callflow.py
# ---------------------------------------------------------------------------
TS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T(?P<short>\d{2}:\d{2}:\d{2}\.\d+))\s+"
    r"\[(?P<layer>[^\]]+)\]\s+\[(?P<lvl>[IDWE])\]\s+(?P<txt>.*)$"
)

# ---------------------------------------------------------------------------
# LTE pretty-names
# ---------------------------------------------------------------------------
PRETTY_4G: dict[str, str] = {
    "rrcConnectionRequest":                  "RRC Connection Request (Msg3)",
    "rrcConnectionSetup":                    "RRC Connection Setup (Msg4)",
    "rrcConnectionSetupComplete":            "RRC Connection Setup Complete",
    "rrcConnectionReject":                   "RRC Connection Reject",
    "rrcConnectionReconfiguration":          "RRC Connection Reconfiguration",
    "rrcConnectionReconfigurationComplete":  "RRC Conn Reconfig Complete",
    "rrcConnectionRelease":                  "RRC Connection Release",
    "systemInformation":                     "systemInformation (SIB2+)",
    "rrcConnectionReestablishmentRequest":   "RRC Connection Reestablishment Request",
    "rrcConnectionReestablishmentComplete":  "RRC Connection Reestablishment Complete",
    "securityModeCommand":                   "Security Mode Command",
    "securityModeComplete":                  "Security Mode Complete",
    "ueCapabilityEnquiry":                   "UE Capability Enquiry",
    "ueCapabilityInformation":               "UE Capability Information",
    "measurementReport":                     "Measurement Report",
    "dlInformationTransfer":                 "DL Information Transfer (NAS)",
    "ulInformationTransfer":                 "UL Information Transfer (NAS)",
    "systemInformationBlockType1":           "SIB1",
    "counterCheck":                          "Counter Check",
    "counterCheckResponse":                  "Counter Check Response",
    # NAS
    "AttachRequest":                         "NAS Attach Request",
    "AttachAccept":                          "NAS Attach Accept",
    "AttachComplete":                        "NAS Attach Complete",
    "AuthenticationRequest":                 "NAS Authentication Request",
    "AuthenticationResponse":                "NAS Authentication Response",
    "SecurityModeCommand":                   "NAS Security Mode Command",
    "SecurityModeComplete":                  "NAS Security Mode Complete",
    # S1AP
    "initialUEMessage":                      "S1AP Initial UE Message",
    "downlinkNASTransport":                  "S1AP DL NAS Transport",
    "uplinkNASTransport":                    "S1AP UL NAS Transport",
    "initialContextSetupRequest":            "S1AP Initial Context Setup Request",
    "initialContextSetupResponse":           "S1AP Initial Context Setup Response",
    "ueContextReleaseCommand":               "S1AP UE Context Release Command",
    "ueContextReleaseComplete":              "S1AP UE Context Release Complete",
    "ueContextReleaseRequest":               "S1AP UE Context Release Request",
    "s1Setup":                               "S1AP Setup",
}


def pretty4g(name: str) -> str:
    return PRETTY_4G.get(name, name)


# ---------------------------------------------------------------------------
# 3GPP TS 36.331 / TS 24.301 attach flow ordering (initial attach skeleton)
# Log timestamps can be wrong (e.g. UE logs Msg3 when RRC builds it, before PRACH).
# We sort by flow_rank within each attach procedure, then by timestamp.
# ---------------------------------------------------------------------------
# (substring in normalised label, rank, phase name)
_ATTACH_FLOW: list[tuple[str, int, str]] = [
    # Phase 1 — cell acquisition
    ("cell found",                          100, "1 — Cell acquisition"),
    ("sib1",                                110, "1 — Cell acquisition"),
    ("systeminformation",                   120, "1 — Cell acquisition"),
    # Phase 2 — random access
    ("prach preamble",                      200, "2 — Random access"),
    ("random access response",              210, "2 — Random access"),
    ("rrc connection request",              220, "2 — Random access"),
    ("rrc connection setup",              230, "2 — Random access"),
    ("rrc connection reject",             235, "2 — Random access"),
    # Phase 3 — setup complete + NAS kickoff (NAS attach rides in Setup Complete on air)
    ("rrc connection setup complete",     300, "3 — Setup complete"),
    ("nas attach request",                  305, "3 — Setup complete"),
    ("s1ap initial ue message",             320, "3 — Setup complete"),
    # Phase 4 — NAS authentication & security
    ("nas authentication request",          400, "4 — NAS auth & security"),
    ("nas authentication response",         410, "4 — NAS auth & security"),
    ("nas security mode command",           420, "4 — NAS auth & security"),
    ("nas security mode complete",          430, "4 — NAS auth & security"),
    ("s1ap dl nas transport",               440, "4 — NAS auth & security"),
    ("s1ap ul nas transport",               450, "4 — NAS auth & security"),
    # Phase 5 — AS security + bearer setup
    ("security mode command",               500, "5 — Bearer setup"),
    ("security mode complete",              510, "5 — Bearer setup"),
    ("ue capability enquiry",               520, "5 — Bearer setup"),
    ("ue capability information",           530, "5 — Bearer setup"),
    ("s1ap initial context setup request",  540, "5 — Bearer setup"),
    ("rrc connection reconfiguration",      550, "5 — Bearer setup"),
    ("nas attach accept",                   560, "5 — Bearer setup"),
    ("s1ap initial context setup response", 570, "5 — Bearer setup"),
    ("rrc conn reconfig complete",          580, "5 — Bearer setup"),
    # Phase 6 — attach finalization
    ("nas attach complete",                 600, "6 — Attach complete"),
    ("rrc connection release",              900, "Release"),
    ("s1ap ue context release",             910, "Release"),
]

_FLOW_RANK_DEFAULT = 8000


def _norm_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.lower().strip())


def flow_rank_and_phase(label: str) -> tuple[int, str]:
    n = _norm_label(label)
    for needle, rank, phase in _ATTACH_FLOW:
        if needle in n:
            return rank, phase
    return _FLOW_RANK_DEFAULT, "Other"


def _parse_ts(ts: str) -> float:
    """Parse ISO timestamp to sortable float (seconds)."""
    try:
        from datetime import datetime
        # Handle trailing Z or +00:00
        t = ts.replace("Z", "+00:00")
        if "." in t:
            base, frac = t.split(".", 1)
            tz = ""
            if "+" in frac:
                frac, tz = frac.split("+", 1)
                tz = "+" + tz
            elif frac.count("-") > 0 and frac.rfind("-") > 6:
                idx = frac.rfind("-")
                tz = frac[idx:]
                frac = frac[:idx]
            dt = datetime.fromisoformat(f"{base}.{frac[:6]}{tz}")
        else:
            dt = datetime.fromisoformat(t)
        return dt.timestamp()
    except Exception:
        return 0.0


PROCEDURE_SPLIT_TS_GAP_S = 15.0


def split_attach_procedures(events: list[dict]) -> list[list[dict]]:
    """Split merged log events into attach procedure cycles."""
    if not events:
        return []
    by_time = sorted(events, key=lambda e: e.get("ts", ""))
    groups: list[list[dict]] = []
    current: list[dict] = []
    last_sib_ts: float | None = None

    for ev in by_time:
        n = _norm_label(ev.get("label", ""))
        is_start = "sib1" in n or "cell found" in n
        ts = _parse_ts(ev.get("ts", ""))

        if is_start and current:
            prev = _norm_label(current[-1].get("label", ""))
            ended = any(x in prev for x in (
                "reject", "release", "attach complete", "random access complete",
            ))
            gap = (ts - last_sib_ts) if last_sib_ts is not None else 999.0
            if ended or gap >= PROCEDURE_SPLIT_TS_GAP_S:
                groups.append(current)
                current = []

        current.append(ev)
        if is_start:
            last_sib_ts = ts

    if current:
        groups.append(current)
    return groups


def order_attach_flow(events: list[dict]) -> list[dict]:
    """Order events by 3GPP attach phase within each procedure cycle."""
    ordered: list[dict] = []
    for group in split_attach_procedures(events):
        for ev in group:
            rank, phase = flow_rank_and_phase(ev.get("label", ""))
            ev["flow_rank"] = rank
            ev["flow_phase"] = phase
        group.sort(key=lambda e: (e.get("flow_rank", _FLOW_RANK_DEFAULT), e.get("ts", "")))
        ordered.extend(group)
    return ordered


def dedupe_mirror_events(events: list[dict], window_s: float = 0.5) -> list[dict]:
    """Drop near-duplicate UL/DL mirror of the same message (UE Tx + eNB Rx)."""
    if not events:
        return []
    # Assign flow rank before comparing so mirror pairs align.
    for ev in events:
        if "flow_rank" not in ev:
            rank, phase = flow_rank_and_phase(ev.get("label", ""))
            ev["flow_rank"] = rank
            ev["flow_phase"] = phase

    def _dedupe_key(ev: dict) -> tuple[int, str]:
        rank = ev.get("flow_rank", _FLOW_RANK_DEFAULT)
        return rank, _norm_label(ev.get("label", ""))

    kept: list[dict] = []
    for ev in events:
        rank, n = _dedupe_key(ev)
        ts = _parse_ts(ev.get("ts", ""))
        dup = False
        for prev in kept:
            prev_rank, prev_n = _dedupe_key(prev)
            if prev_rank != rank or prev_n != n:
                continue
            if abs(_parse_ts(prev.get("ts", "")) - ts) <= window_s:
                # Prefer UE PHY for Msg1, eNB for DL RRC, else keep first
                prefer_new = (
                    (n == "prach preamble (msg1)" and ev.get("src") == "UE"
                     and prev.get("src") != "UE")
                    or (ev.get("dst") == "UE" and prev.get("dst") != "UE"
                        and ev.get("src") == "eNB")
                    or (ev.get("src") == "eNB" and "rrc connection request" in n
                        and prev.get("src") == "UE")
                )
                if prefer_new:
                    kept.remove(prev)
                else:
                    dup = True
                break
        if not dup:
            kept.append(ev)
    return kept


# ---------------------------------------------------------------------------
# Log reader (same as parse_callflow)
# ---------------------------------------------------------------------------
def read_entries(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    out = []
    cur: dict | None = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = TS_RE.match(line)
            if m:
                if cur:
                    out.append(cur)
                cur = {
                    "ts":    m.group("ts"),
                    "short": m.group("short"),
                    "layer": m.group("layer").strip(),
                    "lvl":   m.group("lvl"),
                    "txt":   m.group("txt"),
                    "extra": [],
                }
            elif cur is not None:
                cur["extra"].append(line)
        if cur:
            out.append(cur)
    return out


def detail_of(e: dict) -> str:
    body = "\n".join(e["extra"]).rstrip()
    head = f'[{e["layer"]}] {e["txt"]}'
    return head + ("\n" + body if body else "")


def mk_ev(e: dict, src: str, dst: str, layer: str, label: str,
          kind: str = "signaling", via_zmq: bool = False) -> dict:
    return {
        "ts":       e["ts"],
        "short":    e["short"],
        "src":      src,
        "dst":      dst,
        "layer":    layer,
        "label":    label,
        "kind":     kind,
        "via_zmq":  via_zmq,
        "raw_layer": e["layer"],
        "detail":   detail_of(e),
        "stack":    "4G",
    }


# ---------------------------------------------------------------------------
# srsUE LTE log parser
# ---------------------------------------------------------------------------
_SRB_RE  = re.compile(r"SRB\d\s*-\s*(Tx|Rx)\s+(\w+)")
_ENB_RRC_PDU_RE = re.compile(
    r"(?:Rx|Tx)\s+SRB\d\s+PDU.*?-\s+(\w+)"
)
_BCCH_RE = re.compile(r"BCCH-DLSCH\s*-\s*(Tx|Rx)\s+(\w+)")
_NAS_RE  = re.compile(r"^(Sending|Handling|Received)\s+([A-Z][A-Za-z0-9 ]+)")
_NAS_ATTACH_RE = re.compile(r"^Attach Request with cause")
_INJECT_RE = re.compile(r"TELUS trace LTE m_tmsi=(\S+)")


def parse_ue4g(entries: list[dict]) -> tuple[list[dict], dict]:
    """Parse srsUE LTE log into events.  Returns (events, inject_meta)."""
    events: list[dict] = []
    inject_meta: dict = {}
    seen_ssb = False

    for e in entries:
        L, t = e["layer"], e["txt"]

        # Physical layer — LTE uses PHY or PHY-SA
        if L in ("PHY", "PHY-SA"):
            if not seen_ssb and "Cell search found" in t:
                seen_ssb = True
                events.append(mk_ev(e, "eNB", "UE", "PHY",
                                    "Cell found (DL sync)", "radio", True))
            elif "PRACH: Transmitted preamble" in t or "Transmitted preamble" in t:
                events.append(mk_ev(e, "UE", "eNB", "PHY",
                                    "PRACH preamble (Msg1)", "radio", True))

        # MAC — LTE random access
        elif L in ("MAC", "MAC-LTE"):
            if "Random Access Complete" in t:
                events.append(mk_ev(e, "eNB", "UE", "MAC",
                                    "Random Access Response (Msg2)", "radio", True))
            elif "RACH" in t and "ra_rnti" in t:
                events.append(mk_ev(e, "UE", "eNB", "MAC",
                                    "RACH transmission", "radio", True))

        # NAS — LTE (attach may log before RRC Tx)
        elif L in ("NAS", "NAS5G"):
            if _NAS_ATTACH_RE.search(t):
                events.append(mk_ev(e, "UE", "EPC", "NAS",
                                    "NAS Attach Request", "signaling"))
                continue
            m = _NAS_RE.match(t)
            if m:
                verb, name = m.group(1), m.group(2).strip()
                label = pretty4g(name.replace(" ", "")) if name.replace(" ", "") in PRETTY_4G else f"NAS {name}"
                if verb in ("Sending",):
                    events.append(mk_ev(e, "UE", "EPC", "NAS", label, "signaling"))
                else:
                    events.append(mk_ev(e, "EPC", "UE", "NAS", label, "signaling"))

        # RRC — SRB Tx/Rx (LTE RRC uses SRB0/1/2)
        elif L in ("RRC", "RRC-NR"):
            m = _BCCH_RE.search(t)
            if m:
                events.append(mk_ev(e, "eNB", "UE", "RRC",
                                    pretty4g(m.group(2)), "signaling"))
                continue
            m = _SRB_RE.search(t)
            if m:
                direction, name = m.group(1), m.group(2)
                # UE logs SRB0 Tx for Msg3 when RRC builds the PDU, before PRACH;
                # eNB Rx is the on-air receive time — skip the early UE-side event.
                if direction == "Tx" and name == "rrcConnectionRequest" and "SRB0" in t:
                    pass
                elif direction == "Tx":
                    events.append(mk_ev(e, "UE", "eNB", "RRC",
                                        pretty4g(name), "signaling"))
                else:
                    events.append(mk_ev(e, "eNB", "UE", "RRC",
                                        pretty4g(name), "signaling"))
            # Log entry for TELUS trace injection confirmation
            mi = _INJECT_RE.search(t)
            if mi:
                inject_meta["m_tmsi"] = mi.group(1)
                inject_meta["source"] = "rrc.cc patch"

    return events, inject_meta


# ---------------------------------------------------------------------------
# srseNB log parser (S1AP + RRC server side)
# ---------------------------------------------------------------------------
_S1AP_PDU_RE = re.compile(r"\b(Tx|Rx) PDU\b.*?:\s*([A-Za-z][\w-]*)")
_ENB_RRC_RE  = re.compile(r"(Sent|Received|Tx|Rx)\s+(\w+)\s+to\s+RNTI|"
                           r"RRC\s+(Tx|Rx)\s+(\w+)")


def parse_enb(entries: list[dict]) -> list[dict]:
    """Parse srseNB log into events."""
    events: list[dict] = []
    for e in entries:
        L, t = e["layer"], e["txt"]

        if L in ("PHY", "PHY-SA"):
            if "PRACH:" in t and "preamble=" in t:
                events.append(mk_ev(e, "UE", "eNB", "PHY",
                                    "PRACH preamble (Msg1)", "radio", True))

        elif L in ("MAC", "MAC-LTE"):
            if "SCHED: New PRACH" in t or (
                "RACH:" in t and "temp_crnti" in t
            ):
                # Same Msg1 as PHY PRACH preamble — omit to avoid duplicate ladder row
                pass

        elif L == "S1AP":
            m = _S1AP_PDU_RE.search(t)
            if not m:
                continue
            direction, name = m.group(1), m.group(2)
            if direction == "Tx":
                events.append(mk_ev(e, "eNB", "EPC", "S1AP",
                                    pretty4g(name), "signaling"))
            else:
                events.append(mk_ev(e, "EPC", "eNB", "S1AP",
                                    pretty4g(name), "signaling"))
        elif L in ("RRC", "RRC-NR"):
            m = _ENB_RRC_PDU_RE.search(t)
            if m:
                name = m.group(1)
                if "Tx SRB" in t or t.startswith("Tx"):
                    events.append(mk_ev(e, "eNB", "UE", "RRC",
                                        pretty4g(name), "signaling"))
                else:
                    events.append(mk_ev(e, "UE", "eNB", "RRC",
                                        pretty4g(name), "signaling"))
                continue
            m = _SRB_RE.search(t)
            if m:
                direction, name = m.group(1), m.group(2)
                if direction == "Tx":
                    events.append(mk_ev(e, "eNB", "UE", "RRC",
                                        pretty4g(name), "signaling"))
                else:
                    events.append(mk_ev(e, "UE", "eNB", "RRC",
                                        pretty4g(name), "signaling"))

    return events


# ---------------------------------------------------------------------------
# 22_decoded trace alignment
# ---------------------------------------------------------------------------
def load_trace_records(trace_dir: str | None) -> list[dict]:
    """Load 22_decoded records relevant to a 4G attach procedure."""
    if not trace_dir:
        return []
    p = Path(trace_dir)
    files = sorted(p.glob("**/*.json"))[:1]  # use first file
    if not files:
        return []
    try:
        with open(files[0], encoding="utf-8", errors="replace") as f:
            recs = json.load(f)
        # Filter to RRC and S1AP records
        out = []
        for r in recs:
            iface = (r.get("interface") or "").upper()
            if iface in ("RRC", "S1"):
                out.append(r)
        return out
    except Exception:
        return []


def align_trace_to_events(
    events: list[dict],
    trace_recs: list[dict],
) -> list[dict]:
    """Create a side-by-side list matching live events with 22_decoded records.

    Returns list of {live: event|None, trace: record|None, label: str}.
    """
    # Build lookup by decoded message choice
    trace_by_name: dict[str, list[dict]] = {}
    for r in trace_recs:
        dmeta = r.get("decoding_metadata") or {}
        choice = dmeta.get("decoded_message_choice") or r.get("message_name") or ""
        # Normalise c1 wrapper
        msg = (r.get("decoded") or {}).get("message")
        if choice == "c1" and isinstance(msg, list) and len(msg) == 2:
            inner = msg[1]
            if isinstance(inner, list) and len(inner) >= 1:
                choice = inner[0]
        if choice:
            trace_by_name.setdefault(choice.lower(), []).append(r)

    result: list[dict] = []
    used_trace: set[int] = set()

    for ev_idx, ev in enumerate(events):
        if ev.get("layer") not in ("RRC", "S1AP"):
            result.append({"ev_idx": ev_idx, "live": ev, "trace": None, "label": ev["label"]})
            continue
        # Match by label
        label_norm = (ev["label"].lower()
                      .replace("rrc connection ", "rrcconnection")
                      .replace(" ", ""))
        matched_trace = None
        for t_name, t_recs in trace_by_name.items():
            if t_name.replace("-", "").lower() in label_norm:
                for tr in t_recs:
                    rid = id(tr)
                    if rid not in used_trace:
                        matched_trace = tr
                        used_trace.add(rid)
                        break
                if matched_trace:
                    break
        result.append({"ev_idx": ev_idx, "live": ev, "trace": matched_trace, "label": ev["label"]})

    # Append unmatched trace records
    for r in trace_recs:
        if id(r) not in used_trace:
            choice = (r.get("decoding_metadata") or {}).get("decoded_message_choice") or ""
            result.append({"live": None, "trace": r,
                           "label": PRETTY_4G.get(choice, choice or r.get("message_name", "?"))})

    return result


# ---------------------------------------------------------------------------
# lte_per_templates loading
# ---------------------------------------------------------------------------
def load_per_templates(trace_dir: str | None) -> dict[str, dict]:
    """Load lte_per_templates.json if present."""
    if not trace_dir:
        return {}
    p = Path(trace_dir) / "lte_per_templates" / "lte_per_templates.json"
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("templates", {})
    except Exception:
        return {}


_STATUS_RANK = {"exact": 4, "reconstructed": 3, "minimal": 2, "encode_failed": 1}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _best_status(statuses: dict[str, int]) -> str:
    if not statuses:
        return "none"
    return max(statuses, key=lambda k: _STATUS_RANK.get(k, 0))


def load_per_record_status() -> dict[str, dict]:
    """Load per-record PER/reconstruction report summaries for RRC and S1AP."""
    root = _repo_root()
    reports = [
        root / "srsTwin" / "22_decoded_per_records" / "by_decoded_choice_reconstructed2" / "_per_record_report.json",
        root / "srsTwin" / "22_decoded_per_records" / "s1ap_by_decoded_choice2" / "_s1ap_per_record_report.json",
    ]
    out: dict[str, dict] = {}
    for report in reports:
        if not report.is_file():
            continue
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for group in data.get("groups", []):
            key = str(group.get("key") or "")
            statuses = group.get("statuses") or {}
            if not key:
                continue
            out[key] = {
                "status": _best_status(statuses),
                "statuses": statuses,
                "processed_count": group.get("processed_count"),
                "result_file": group.get("result_file"),
                "report": str(report),
            }
    return out


def trace_choice(rec: dict) -> str:
    dmeta = rec.get("decoding_metadata") or {}
    choice = dmeta.get("decoded_message_choice") or rec.get("_group_key") or rec.get("message_name") or ""
    msg = (rec.get("decoded") or {}).get("message")
    if choice == "c1" and isinstance(msg, list) and len(msg) >= 2:
        inner = msg[1]
        if isinstance(inner, list) and inner and isinstance(inner[0], str):
            choice = inner[0]
    return str(choice)


def annotate_trace_records(trace_recs: list[dict], per_status: dict[str, dict]) -> None:
    for rec in trace_recs:
        key = trace_choice(rec)
        st = per_status.get(key) or {}
        rec["_truth_level"] = "22_decoded"
        rec["_template_status"] = st.get("status", "none")
        rec["_template_statuses"] = st.get("statuses", {})


# ---------------------------------------------------------------------------
# Master build function
# ---------------------------------------------------------------------------
def build_4g(
    log_dir: str,
    trace_dir: str | None = None,
) -> dict[str, Any]:
    """Parse 4G logs and trace data.  Returns a dict with all 4G dashboard data.

    Keys:
      events    — list of signaling events for the 4G ladder
      inject_meta — trace injection fields from srsUE log
      aligned   — side-by-side live + trace records
      per_templates — pycrate-encoded PER bytes from lte_per_templates.json
      has_live  — bool: do any live log files exist?
    """
    ue_log  = os.path.join(log_dir, "ue4g.log")
    enb_log = os.path.join(log_dir, "enb.log")

    ue_entries  = read_entries(ue_log)
    enb_entries = read_entries(enb_log)

    ue_events, inject_meta = parse_ue4g(ue_entries)
    enb_events = parse_enb(enb_entries)

    # Merge, dedupe mirrors, then order by 3GPP attach flow (not raw log timestamp)
    combined = dedupe_mirror_events(ue_events + enb_events)
    events = order_attach_flow(combined)

    for ev in events:
        if not ev.get("info", {}).get("purpose"):
            ev["info"] = lookup_message_info_4g(ev["label"])

    trace_recs   = load_trace_records(trace_dir)
    per_record_status = load_per_record_status()
    annotate_trace_records(trace_recs, per_record_status)
    aligned      = align_trace_to_events(events, trace_recs)
    per_templates = load_per_templates(trace_dir)

    return {
        "events":       events,
        "inject_meta":  inject_meta,
        "aligned":      aligned,
        "per_templates": per_templates,
        "per_record_status": per_record_status,
        "trace_recs":   trace_recs,
        "has_live":     bool(ue_entries or enb_entries),
    }
