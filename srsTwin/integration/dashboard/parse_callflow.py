#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Parse srsUE + ocudu gNB logs (and optional hub logs) into a self-contained
srsTwin dashboard: overview, signaling ladder, and searchable message log.

Lanes: UE | RU (ZMQ) | DU/gNB | 5GC

Usage:
  python parse_callflow.py [LOG_DIR] [-o index.html]
  LOG_DIR defaults to ./logs (gnb.log, ue.log; optional hub.log).
"""
import argparse
import json
import os
import re
from datetime import datetime

from message_catalog import LABEL_RULES, MESSAGE_INFO, lookup_message_info
from parse_4g import build_4g
from parse_rrc import build_rrc
from parse_signaling import build_signaling
from trace_replay import apply_trace_replay
from trace_transmit import default_transmit_plan_path, write_transmit_plan

TS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T(?P<short>\d{2}:\d{2}:\d{2}\.\d+))\s+"
    r"\[(?P<layer>[^\]]+)\]\s+\[(?P<lvl>[IDWE])\]\s+(?P<txt>.*)$"
)

PRETTY = {
    "rrcSetupRequest": "RRC Setup Request (Msg3)",
    "rrcSetup": "RRC Setup (Msg4)",
    "rrcSetupComplete": "RRC Setup Complete (+ Registration Request)",
    "dlInformationTransfer": "DL Information Transfer (NAS)",
    "ulInformationTransfer": "UL Information Transfer (NAS)",
    "securityModeCommand": "Security Mode Command",
    "securityModeComplete": "Security Mode Complete",
    "ueCapabilityEnquiry": "UE Capability Enquiry",
    "ueCapabilityInformation": "UE Capability Information",
    "rrcReconfiguration": "RRC Reconfiguration",
    "rrcReconfigurationComplete": "RRC Reconfiguration Complete",
    "systemInformationBlockType1": "SIB1 (System Information Block 1)",
    "rrcRelease": "RRC Release",
    "NGSetupRequest": "NG Setup Request",
    "NGSetupResponse": "NG Setup Response",
    "InitialUEMessage": "Initial UE Message",
    "DownlinkNASTransport": "Downlink NAS Transport",
    "UplinkNASTransport": "Uplink NAS Transport",
    "InitialContextSetupRequest": "Initial Context Setup Request",
    "InitialContextSetupResponse": "Initial Context Setup Response",
    "PDUSessionResourceSetupRequest": "PDU Session Resource Setup Request",
    "PDUSessionResourceSetupResponse": "PDU Session Resource Setup Response",
    "UEContextReleaseCommand": "UE Context Release Command",
    "UEContextReleaseComplete": "UE Context Release Complete",
    "UEContextReleaseRequest": "UE Context Release Request",
    "UERadioCapabilityInfoIndication": "UE Radio Capability Info Indication",
}


def pretty(name):
    return PRETTY.get(name, name)


def read_entries(path):
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        cur = None
        for line in f:
            line = line.rstrip("\n")
            m = TS_RE.match(line)
            if m:
                if cur:
                    out.append(cur)
                cur = {
                    "ts": m.group("ts"),
                    "short": m.group("short"),
                    "layer": m.group("layer").strip(),
                    "lvl": m.group("lvl"),
                    "txt": m.group("txt"),
                    "extra": [],
                }
            elif cur is not None:
                cur["extra"].append(line)
        if cur:
            out.append(cur)
    return out


def detail_of(e):
    body = "\n".join(e["extra"]).rstrip()
    head = f'[{e["layer"]}] {e["txt"]}'
    return head + ("\n" + body if body else "")


def ev(e, src, dst, layer, label, kind, via_ru=False):
    return {
        "ts": e["ts"],
        "short": e["short"],
        "src": src,
        "dst": dst,
        "layer": layer,
        "label": label,
        "kind": kind,
        "via_ru": via_ru,
        "raw_layer": e["layer"],
        "detail": detail_of(e),
    }


def parse_ue(entries):
    events = []
    nas_re = re.compile(r"^(Sending|Handling) ([A-Z][A-Za-z0-9].*)$")
    srb_re = re.compile(r"SRB\d\s*-\s*(Tx|Rx)\s+(\w+)")
    bcch_re = re.compile(r"BCCH-DLSCH\s*-\s*(Tx|Rx)\s+(\w+)")
    seen_ssb = False
    for e in entries:
        L, t = e["layer"], e["txt"]
        if L == "PHY-SA":
            if (not seen_ssb) and t.startswith("Cell search found"):
                seen_ssb = True
                events.append(ev(e, "DU", "UE", "PHY",
                                 "SSB / PBCH-MIB detected (cell search)", "radio", True))
            elif "PRACH: Transmitted preamble" in t:
                events.append(ev(e, "UE", "DU", "PHY", "PRACH preamble (Msg1)", "radio", True))
        elif L == "MAC-NR":
            if "Random Access Complete" in t:
                events.append(ev(e, "DU", "UE", "MAC",
                                 "Random Access Complete (contention resolved)", "radio", True))
        elif L == "RRC-NR":
            m = bcch_re.search(t)
            if m:
                events.append(ev(e, "DU", "UE", "RRC", pretty(m.group(2)), "signaling"))
                continue
            m = srb_re.search(t)
            if m:
                direction, name = m.group(1), m.group(2)
                if direction == "Tx":
                    events.append(ev(e, "UE", "DU", "RRC", pretty(name), "signaling"))
                else:
                    events.append(ev(e, "DU", "UE", "RRC", pretty(name), "signaling"))
        elif L == "NAS5G":
            if t.startswith("PDU Session Establishment successful"):
                ip_m = re.search(r"IP:\s*([\d.]+)", t)
                ip = ip_m.group(1) if ip_m else "?"
                events.append(ev(e, "5GC", "UE", "NAS",
                                 f"PDU Session Establishment Accept (IP: {ip})", "signaling"))
                continue
            m = nas_re.match(t)
            if m:
                verb, name = m.group(1), m.group(2).strip()
                name = re.sub(r"\s+in UL NAS transport\.?\s*$", "", name).strip()
                if name.lower() in ("rrc nr connection",):
                    continue
                if verb == "Sending":
                    events.append(ev(e, "UE", "5GC", "NAS", "NAS: " + name, "signaling"))
                else:
                    events.append(ev(e, "5GC", "UE", "NAS", "NAS: " + name, "signaling"))
    return events


def parse_gnb(entries):
    events = []
    pdu_re = re.compile(r"\b(Tx|Rx) PDU\b.*?:\s*([A-Za-z][\w-]*)")
    for e in entries:
        if e["layer"] != "NGAP":
            continue
        m = pdu_re.search(e["txt"])
        if not m:
            continue
        direction, name = m.group(1), m.group(2)
        if direction == "Tx":
            events.append(ev(e, "DU", "5GC", "NGAP", pretty(name), "signaling"))
        else:
            events.append(ev(e, "5GC", "DU", "NGAP", pretty(name), "signaling"))
    return events


def parse_status(log_dir):
    """Stack health summary from raw logs."""
    gnb_path = os.path.join(log_dir, "gnb.log")
    ue_path = os.path.join(log_dir, "ue.log")
    hub_path = os.path.join(log_dir, "hub.log")

    def slurp(path):
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    gnb = slurp(gnb_path)
    ue = slurp(ue_path)
    hub = slurp(hub_path)

    pdu_ip = None
    m = re.search(r"PDU Session Establishment successful\. IP:\s*([\d.]+)", ue)
    if m:
        pdu_ip = m.group(1)

    hub_fwd = None
    hm = re.findall(
        r"forwarding: dl_blocks=(\d+) ul_blocks=(\d+) connected=(\d+)/(\d+)", hub
    )
    if hm:
        dl, ul, conn, slots = hm[-1]
        hub_fwd = {"dl_blocks": int(dl), "ul_blocks": int(ul),
                   "connected": int(conn), "slots": int(slots)}

    return {
        "radio_mode": "hub" if hub.strip() else "direct",
        "ng_setup": bool(re.search(
            r"NG setup procedure completed|Connected to AMF", gnb, re.I)),
        "cell_up": bool(re.search(
            r"DU started successfully|Cell was activated|Cell scheduling was activated", gnb, re.I)),
        "rrc_complete": bool(re.search(
            r"Random Access Complete|Finished Connection Setup successfully", ue, re.I)),
        "pdu_session": bool(re.search(r"PDU Session Establishment successful", ue)),
        "pdu_ip": pdu_ip,
        "hub_forwarding": hub_fwd,
        "has_gnb_log": bool(gnb.strip()),
        "has_ue_log": bool(ue.strip()),
        "has_hub_log": bool(hub.strip()),
    }


def build(log_dir):
    ue = parse_ue(read_entries(os.path.join(log_dir, "ue.log")))
    gnb = parse_gnb(read_entries(os.path.join(log_dir, "gnb.log")))
    events = sorted(ue + gnb, key=lambda x: x["ts"])
    if events:
        t0 = datetime.fromisoformat(events[0]["ts"])
        for x in events:
            x["t"] = round((datetime.fromisoformat(x["ts"]) - t0).total_seconds(), 3)
    for i, x in enumerate(events):
        x["id"] = i
        x["info"] = lookup_message_info(x["label"])

    status = parse_status(log_dir)
    by_layer = {}
    for e in events:
        by_layer[e["layer"]] = by_layer.get(e["layer"], 0) + 1

    meta = {
        "duration": events[-1]["t"] if events else 0,
        "captured": events[0]["ts"][:19].replace("T", " ") if events else "n/a",
        "message_count": len(events),
        "by_layer": by_layer,
        "status": status,
    }
    return events, meta


_EMPTY_4G = {"events": [], "inject_meta": {}, "aligned": [],
             "per_templates": {}, "trace_recs": [], "has_live": False,
             "kpis": {"phases": [], "attach_ms": None, "session_ms": None,
                      "total_ms": None, "outcome": "none", "event_count": 0}}


# Small inline-SVG line icons for the 4 elements of the 4G stack — reused in
# the ladder's lane headers and the Overview tab's topology diagram so both
# places use the exact same visual vocabulary. `stroke="currentColor"` so
# they pick up whatever color the containing element sets.
ICON_UE = ('<svg viewBox="0 0 20 20" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6">'
           '<rect x="6" y="2" width="8" height="16" rx="2"/><line x1="8.5" y1="15" x2="11.5" y2="15"/></svg>')
ICON_IQ = ('<svg viewBox="0 0 20 20" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5">'
           '<path d="M2 10h2.5l1.8-5 3 10 2.6-8 1.6 3h3.5"/></svg>')
ICON_ENB = ('<svg viewBox="0 0 20 20" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3">'
            '<path d="M10 2l-3.4 16h1.8l0.9-4h1.4l0.9 4h1.8L10 2z"/>'
            '<line x1="6.4" y1="10" x2="13.6" y2="10"/><line x1="7.4" y1="14" x2="12.6" y2="14"/></svg>')
ICON_EPC = ('<svg viewBox="0 0 20 20" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5">'
            '<ellipse cx="10" cy="4.2" rx="6" ry="2.1"/>'
            '<path d="M4 4.2v11.6c0 1.16 2.7 2.1 6 2.1s6-.94 6-2.1V4.2"/>'
            '<path d="M4 10c0 1.16 2.7 2.1 6 2.1s6-.94 6-2.1"/></svg>')


def render_html(events, meta, rrc_twin, rrc_trace, rrc_meta, signaling=None,
                data_4g=None, data_4g_multi=None, data_4g_shared=None,
                container_status_4g=None, kpi_history=None):
    if signaling is None:
        signaling = build_signaling(rrc_twin, events)
    if data_4g is None:
        data_4g = dict(_EMPTY_4G)
    if data_4g_multi is None:
        data_4g_multi = {"1": data_4g}
    if data_4g_shared is None:
        # Caller didn't pre-compute the dedup (serve_dashboard.py does;
        # the standalone `python parse_callflow.py` CLI path doesn't) — do
        # it here so every render_html() caller gets the same saving.
        # trace_recs/per_templates/per_record_status are identical across
        # every pair (same trace_dir); embedding them once per pair instead
        # of once total was the single biggest contributor to the
        # dashboard HTML bloating to ~24MB. Build new dicts rather than
        # mutating the caller's — render_html() shouldn't have side effects
        # on its arguments.
        shared_keys = ("trace_recs", "per_templates", "per_record_status")
        data_4g_shared = {}
        for k in shared_keys:
            for pair_data in data_4g_multi.values():
                if k in pair_data:
                    data_4g_shared[k] = pair_data[k]
                    break
        data_4g_multi = {key: {k: v for k, v in pair_data.items() if k not in shared_keys}
                         for key, pair_data in data_4g_multi.items()}
        data_4g = {k: v for k, v in data_4g.items() if k not in shared_keys}
    if container_status_4g is None:
        container_status_4g = {}
    if kpi_history is None:
        kpi_history = []
    rules = [[prefix, key] for prefix, key in LABEL_RULES]
    return (HTML
            .replace("__DATA__", json.dumps(events))
            .replace("__META__", json.dumps(meta))
            .replace("__MESSAGE_INFO__", json.dumps(MESSAGE_INFO))
            .replace("__LABEL_RULES__", json.dumps(rules))
            .replace("__RRC_TWIN__", json.dumps(rrc_twin))
            .replace("__RRC_TRACE__", json.dumps(rrc_trace))
            .replace("__RRC_META__", json.dumps(rrc_meta))
            .replace("__SIGNALING__", json.dumps(signaling))
            .replace("__4G_DATA__", json.dumps(data_4g))
            .replace("__4G_DATA_MULTI__", json.dumps(data_4g_multi))
            .replace("__4G_DATA_SHARED__", json.dumps(data_4g_shared))
            .replace("__4G_CONTAINER_STATUS__", json.dumps(container_status_4g))
            .replace("__4G_KPI_HISTORY__", json.dumps(kpi_history))
            .replace("__ICON_UE__", ICON_UE)
            .replace("__ICON_IQ__", ICON_IQ)
            .replace("__ICON_ENB__", ICON_ENB)
            .replace("__ICON_EPC__", ICON_EPC))


HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>srsTwin Dashboard</title>
<link rel="icon" type="image/png" href="favicon.png">
<style>
:root{
  --bg:#0a0e14; --panel:#121820; --panel2:#1a2230; --line:#2d3640; --muted:#8b949e;
  --txt:#e6edf3; --ok:#3fb950; --warn:#d29922; --bad:#f85149;
  --PHY:#a371f7; --MAC:#22d3ee; --RRC:#58a6ff; --NAS:#3fb950; --NGAP:#f0883e;
}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--txt)}
header{padding:6px 18px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#101620,#0a0e14);
  display:flex;align-items:center;gap:14px;min-height:44px;flex-shrink:0}
header h1{margin:0;font-size:14px;font-weight:600;white-space:nowrap}
.icon-btn{background:transparent;border:1px solid var(--line);color:var(--txt);border-radius:6px;
  padding:4px 9px;cursor:pointer;font-size:15px;line-height:1;flex-shrink:0}
.icon-btn:hover{border-color:var(--RRC)}
.livebar{display:none;align-items:center;gap:8px;font-size:12px;margin:0 0 0 auto;flex-shrink:0}
.livebar .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.livebar button{padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--panel2);
  color:var(--txt);cursor:pointer;font-size:11px}.livebar button:hover{border-color:var(--RRC)}
.tabs{display:flex;gap:4px;flex-shrink:0}
.tab{padding:7px 14px;border:none;background:transparent;color:var(--muted);cursor:pointer;font-size:12px;
  border-radius:6px}
.tab:hover{color:var(--txt)}
.tab.on{color:var(--txt);background:var(--panel2);font-weight:600}
.sidebar-overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);opacity:0;pointer-events:none;
  transition:opacity .15s ease;z-index:40}
.sidebar-overlay.open{opacity:1;pointer-events:auto}
.sidebar{position:fixed;top:0;left:0;bottom:0;width:260px;background:var(--panel);border-right:1px solid var(--line);
  transform:translateX(-100%);transition:transform .18s ease;z-index:50;padding:14px;overflow-y:auto}
.sidebar.open{transform:translateX(0)}
.sidebar-hdr{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:10px}
.sidebar-item{display:block;width:100%;text-align:left;padding:9px 10px;border-radius:6px;border:1px solid var(--line);
  background:transparent;color:var(--txt);cursor:pointer;font-size:13px;margin-bottom:6px}
.sidebar-item.on{background:#f9826c;color:#0a0e14;font-weight:600;border-color:#f9826c}
.sidebar-item[data-twin="simulation"].on{background:#3fb950;color:#06170a;border-color:#3fb950}
.sidebar-item[data-twin="lightweight5g"].on{background:#58a6ff;color:#08131f;border-color:#58a6ff}
.sidebar-item-tag{float:right;font-size:9px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);
  border:1px solid var(--line);border-radius:4px;padding:1px 5px}
.sidebar-item.on .sidebar-item-tag{color:inherit;border-color:rgba(0,0,0,.25)}
.sidebar-item-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:8px;
  vertical-align:1px;background:#484f58}
.sidebar-item-dot.running{background:#3fb950}
.sidebar-item-dot.partial{background:#d29922}
.sidebar-note{font-size:11px;color:var(--muted);margin-top:8px}
.panel{display:none;height:calc(100vh - 60px);overflow:hidden;min-height:0}
.panel.on{display:flex;flex-direction:column}
.analytics-placeholder{padding:24px;color:var(--muted);font-size:13px}
/* --- twin start/stop control bar (shared by every twin's panel) --- */
.twin-ctrl-bar{display:flex;align-items:center;gap:8px;flex-shrink:0;font-size:12px}
.twin-ctrl-status{color:var(--muted);white-space:nowrap}
.twin-ctrl-status .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:1px}
.twin-ctrl-status .dot.running{background:#3fb950}
.twin-ctrl-status .dot.stopped{background:#484f58}
.twin-ctrl-status .dot.partial{background:#d29922}
.twin-ctrl-status .dot.busy{background:#58a6ff;animation:pulse 1s infinite}
.twin-ctrl-btn{padding:4px 10px;border-radius:6px;border:1px solid var(--line);white-space:nowrap;
  background:var(--panel2);color:var(--txt);cursor:pointer;font-size:11.5px;font-weight:600}
.twin-ctrl-btn:hover{border-color:#f9826c}
.twin-ctrl-btn.accent-sim:hover{border-color:#3fb950}
.twin-ctrl-btn.accent-lw5g:hover{border-color:#58a6ff}
.twin-ctrl-btn:disabled{opacity:.45;cursor:not-allowed}
.twin-ctrl-btn.busy{opacity:.5;pointer-events:none}
/* --- simulation twin panel --- */
.sim-wrap{padding:20px 22px;overflow:auto;flex:1}
.sim-desc{color:var(--muted);font-size:12.5px;margin:0 0 18px;line-height:1.5;max-width:760px}
.sim-empty{color:var(--muted);font-size:12px;padding:14px 0}
.sim-ue-row{display:flex;align-items:center;gap:14px;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:14px 16px;margin-bottom:16px}
.sim-ue-row b{font-size:13px;min-width:120px;flex-shrink:0}
.sim-ue-row input[type=range]{flex:1;accent-color:#3fb950}
.sim-ue-row .sim-ue-val{min-width:64px;text-align:right;font-variant-numeric:tabular-nums;font-size:12.5px}
.sim-stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.sim-stats .stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 14px;min-width:84px}
.sim-stats .stat .v{font-size:16px;font-weight:600;color:#3fb950}
.sim-stats .stat .l{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.04em}
.sim-geo-box{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:18px}
.sim-geo-hdr{display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:12.5px}
.sim-geo-hdr b{color:#3fb950}
.sim-geo-count{color:var(--muted);font-size:11px}
.sim-geo-cols{display:flex;gap:16px}
.sim-geo-left{flex:1 1 50%;min-width:0}
.sim-geo-right{flex:1 1 50%;min-width:0;display:flex;flex-direction:column;gap:10px}
#sim-geo-canvas{display:block;width:100%;height:340px;background:#0a0e14;border-radius:6px}
.sim-geo-legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;font-size:10.5px;color:var(--muted)}
.sim-geo-legend-item{display:inline-flex;align-items:center;gap:5px}
.sim-geo-legend-item i{display:inline-block;width:8px;height:8px;border-radius:50%}
.sim-chart-box{background:#0a0e14;border-radius:6px;padding:10px 12px;flex:1;display:flex;flex-direction:column;min-height:0}
.sim-chart-box b{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}
.sim-chart-box canvas{flex:1;width:100%;min-height:130px}
@media (max-width: 980px){ .sim-geo-cols{flex-direction:column} }
.sim-sites{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.sim-site{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.sim-site h4{margin:0 0 8px;font-size:12.5px;color:#3fb950}
.sim-cell-row{display:flex;align-items:center;gap:8px;font-size:11px;margin:5px 0}
.sim-cell-row .cid{flex:0 0 44px;color:var(--muted)}
.sim-cell-row .bar{flex:1;height:8px;background:var(--line);border-radius:3px;overflow:hidden}
.sim-cell-row .bar i{display:block;height:100%;border-radius:3px}
.sim-cell-row .pct{flex:0 0 36px;text-align:right;color:#c9d1d9;font-variant-numeric:tabular-nums}
/* --- lightweight 5G twin (preview, no backend yet) --- */
.lw5g-wrap{padding:20px 22px;overflow:auto;flex:1}
.lw5g-desc{color:var(--muted);font-size:12.5px;margin:0 0 16px;line-height:1.5;max-width:760px}
.lw5g-params-bar{display:flex;align-items:center;gap:14px;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:12px 16px;margin-bottom:10px;flex-wrap:wrap}
.lw5g-params-bar b{font-size:13px;min-width:110px;flex-shrink:0}
.lw5g-params-bar input[type=range]{flex:1;min-width:160px;accent-color:#58a6ff}
.lw5g-params-bar .lw5g-ue-val{min-width:54px;text-align:right;font-size:12.5px;font-variant-numeric:tabular-nums}
.lw5g-params-toggle{padding:5px 10px;border-radius:6px;border:1px solid var(--line);background:transparent;
  color:var(--muted);cursor:pointer;font-size:12px}
.lw5g-params-toggle:hover{color:var(--txt);border-color:#58a6ff}
.lw5g-params-panel{padding:14px 16px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);
  margin-bottom:16px;font-size:12.5px}
.lw5g-params-panel b{display:block;margin-bottom:8px;color:#58a6ff;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.lw5g-pattern-row{display:flex;gap:8px;flex-wrap:wrap}
.lw5g-pattern-btn{padding:7px 14px;border-radius:6px;border:1px solid var(--line);background:var(--panel);
  color:var(--muted);cursor:pointer;font-size:12.5px;font-weight:600}
.lw5g-pattern-btn:hover{border-color:#58a6ff;color:var(--txt)}
.lw5g-pattern-btn.on{background:#58a6ff;color:#08131f;border-color:#58a6ff}
.lw5g-pattern-desc{color:var(--muted);font-size:11.5px;margin:8px 0 0}
.lw5g-empty{background:var(--panel);border:1px dashed var(--line);border-radius:8px;padding:28px;
  color:var(--muted);font-size:13px;text-align:center;line-height:1.6}
.lw5g-empty b{color:var(--txt);display:block;margin-bottom:6px;font-size:14px}
.lw5g-analytics{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.lw5g-chart-box{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px;min-height:140px}
.lw5g-chart-box b{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px}
.lw5g-chart-empty{color:var(--muted);font-size:12px;font-style:italic;margin:24px 0 0}
/* --- overview --- */
.overview{padding:20px 22px;overflow:auto;flex:1}
.overview-kpi-row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px}
.overview-kpi-row .lte-hist,.overview-kpi-row .lte-kpi{flex:1 1 360px;max-height:340px;
  border:1px solid var(--line);border-top:1px solid var(--line);border-radius:8px}
.lte4g-topo-row{display:flex;align-items:center;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:26px 18px;overflow-x:auto}
.lte4g-topo-node{display:flex;flex-direction:column;align-items:center;gap:7px;background:var(--panel2);
  border:1px solid var(--line);border-radius:10px;padding:14px 18px;cursor:pointer;color:var(--txt);
  min-width:104px;flex-shrink:0;transition:border-color .15s,background-color .15s;font:inherit}
.lte4g-topo-node:hover{border-color:#f9826c}
.lte4g-topo-node.sel{border-color:#f9826c;background:#2a1c16}
.lte4g-topo-node .lte4g-topo-icon{color:#f9826c;width:28px;height:28px;display:flex;align-items:center;justify-content:center}
.lte4g-topo-node .lte4g-topo-icon svg{width:26px;height:26px}
.lte4g-topo-node b{font-size:13px}
.lte4g-topo-node small{font-size:10px;color:var(--muted);font-family:monospace}
.lte4g-topo-link{flex:1;min-width:36px;height:2px;background:var(--line);position:relative;margin:0 2px;align-self:center}
.lte4g-topo-pulse{position:absolute;top:-3px;left:0;width:8px;height:8px;border-radius:50%;
  background:#58a6ff;box-shadow:0 0 6px #58a6ff;animation:lte4gFlow 2.4s linear infinite;opacity:0}
@keyframes lte4gFlow{0%{left:0;opacity:0}8%{opacity:1}88%{opacity:1}100%{left:calc(100% - 8px);opacity:0}}
.lte4g-topo-def{margin-top:14px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;
  padding:14px 16px;font-size:12.5px;color:var(--txt);line-height:1.55}
.lte4g-topo-def b{color:#f9826c;display:block;margin-bottom:6px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.card .val{font-size:18px;font-weight:600;margin-top:6px}
.card .val.ok{color:var(--ok)} .card .val.bad{color:var(--bad)} .card .val.muted{color:var(--muted)}
.topo{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:20px}
.topo svg{width:100%;max-width:720px;height:auto;display:block;margin:0 auto}
.layer-bars{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}
.bar-item{display:flex;align-items:center;gap:8px;font-size:12px}
.bar{width:80px;height:8px;background:var(--panel2);border-radius:4px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:4px}
/* hidden scrollbars — scroll still works (wheel / trackpad / touch) */
.scroll-hide{scrollbar-width:none;-ms-overflow-style:none}
.scroll-hide::-webkit-scrollbar{display:none;width:0;height:0}
/* --- call flow --- */
.cf-wrap{display:flex;flex:1;overflow:hidden;min-height:0}
.diagram{flex:0 0 34%;max-width:40%;min-width:240px;overflow:auto;position:relative;padding:0 8px 32px}
.lanes{position:sticky;top:0;z-index:5;display:flex;background:var(--bg);border-bottom:1px solid var(--line)}
.lanehdr{flex:1;text-align:center;padding:10px 0;font-weight:600;letter-spacing:.3px}
.lanehdr small{display:block;color:var(--muted);font-weight:400;font-size:11px}
#canvas{position:relative;margin-top:4px}
.vline{position:absolute;top:0;bottom:0;width:2px;background:var(--line);transform:translateX(-1px)}
.row{position:absolute;left:0;right:0;cursor:pointer;padding:0 8px}
.row:hover .lbl{color:#fff}.row:hover .seg{filter:brightness(1.35)}.row:hover .row-desc{color:#c9d1d9}
.row.sel{background:rgba(88,166,255,.08)}.row.sel .lbl{color:#fff;font-weight:600}.row.sel .row-desc{color:var(--txt)}
.seg{position:absolute;height:2px}
.head{position:absolute;width:0;height:0;border-top:5px solid transparent;border-bottom:5px solid transparent}
.lbl{position:absolute;transform:translateX(-50%);white-space:nowrap;font-size:12px;color:var(--txt);
  background:rgba(10,14,20,.88);padding:0 6px;border-radius:4px;pointer-events:none;max-width:92%;overflow:hidden;text-overflow:ellipsis}
.row-desc{position:absolute;transform:translateX(-50%);font-size:10.5px;line-height:1.35;color:var(--muted);
  text-align:center;width:min(520px,88%);pointer-events:none;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.tg{position:absolute;left:2px;color:var(--muted);font-size:10.5px;font-variant-numeric:tabular-nums}
.rudot{position:absolute;width:9px;height:9px;border-radius:50%;border:2px solid var(--bg);transform:translate(-50%,-50%)}
.detail{flex:1;min-width:340px;width:auto;max-width:none;border-left:1px solid var(--line);background:var(--panel);
  display:flex;flex-direction:column;overflow:hidden;min-height:0}
.detail .dh{padding:14px 16px;border-bottom:1px solid var(--line);flex-shrink:0}
.detail .dh .t{font-size:15px;font-weight:600;margin-bottom:6px}
.detail-body{flex:1;overflow:auto;min-height:0;padding:0 0 32px;display:flex;flex-direction:column}
.detail-panes{display:flex;flex:1;min-height:200px;border-bottom:1px solid var(--line)}
.detail-pane{flex:1;min-width:0;display:flex;flex-direction:column;overflow:hidden}
.detail-pane+.detail-pane{border-left:1px solid var(--line)}
.detail-pane .json-block{flex:1;overflow:auto;min-height:0;margin:0}
.detail-pane .log-hdr{flex-shrink:0}
.msg-info{padding:12px 16px;border-top:1px solid var(--line);flex-shrink:0;font-size:12.5px;line-height:1.5}
.msg-info .info-lead{color:var(--txt);margin:0 0 10px}
.msg-info dl{margin:0}
.msg-info dt{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:10px 0 4px}
.msg-info dt:first-child{margin-top:0}
.msg-info dd{margin:0 0 4px;color:#c9d1d9}
.log-hdr{padding:10px 16px 0;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);flex-shrink:0}
.badge{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600;color:#0a0e14}
.route{color:var(--muted);font-size:12px;margin-top:8px}.route b{color:var(--txt)}
.json-block{margin:0;padding:12px 16px 16px;font:12px/1.5 ui-monospace,Consolas,monospace;
  white-space:pre-wrap;word-break:break-word;color:#c9d1d9;flex-shrink:0}
.detail pre.json-block,.msg-detail pre.json-block{flex-shrink:0}
.empty{color:var(--muted);padding:16px;font-style:italic}
.copy{float:right;margin-left:8px;font-size:11px;color:var(--muted);border:1px solid var(--line);background:var(--panel2);
  border-radius:5px;padding:3px 8px;cursor:pointer}.copy:hover{color:#fff}
.legend{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border:1px solid var(--line);
  border-radius:20px;cursor:pointer;user-select:none;font-size:11px;background:var(--panel)}
.chip .dot{width:8px;height:8px;border-radius:50%}.chip.off{opacity:.35;text-decoration:line-through}
.cf-toolbar{padding:6px 14px;border-bottom:1px solid var(--line);flex-shrink:0}
.cf-toolbar .legend{margin:0}
/* --- messages --- */
.msg-wrap{display:flex;flex:1;overflow:hidden;min-height:0}
.msg-list{flex:0 0 34%;max-width:40%;min-width:220px;overflow:auto;border-right:1px solid var(--line);padding-bottom:32px}
.msg-toolbar{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.msg-toolbar input{flex:1;min-width:180px;padding:8px 12px;border-radius:6px;border:1px solid var(--line);
  background:var(--panel2);color:var(--txt);font-size:13px}
.msg-toolbar select{padding:8px;border-radius:6px;border:1px solid var(--line);background:var(--panel2);color:var(--txt)}
table.msg{width:100%;border-collapse:collapse;font-size:12.5px}
table.msg th{position:sticky;top:0;background:var(--panel);text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);color:var(--muted);font-weight:500}
table.msg td{padding:8px 12px;border-bottom:1px solid var(--line);cursor:pointer;vertical-align:top}
table.msg tr:hover td{background:rgba(88,166,255,.06)}
table.msg tr.sel td{background:rgba(88,166,255,.12)}
.msg-detail{flex:1;min-width:340px;width:auto;max-width:none;display:flex;flex-direction:column;background:var(--panel);overflow:hidden;min-height:0}
/* --- rrc --- */
.rrc-wrap{display:flex;flex:1;overflow:hidden}
.rrc-list{flex:1;overflow:auto;border-right:1px solid var(--line);display:flex;flex-direction:column}
.rrc-toolbar{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.rrc-toolbar input,.rrc-toolbar select{padding:8px;border-radius:6px;border:1px solid var(--line);background:var(--panel2);color:var(--txt);font-size:13px}
.rrc-toolbar input{flex:1;min-width:140px}
.rrc-src{display:flex;gap:4px}
.rrc-src button{padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--panel2);color:var(--muted);cursor:pointer;font-size:12px}
.rrc-src button.on{background:var(--RRC);color:#0a0e14;border-color:var(--RRC);font-weight:600}
.rrc-pipeline{margin:0;padding:0;list-style:none}
.rrc-pipeline li{padding:8px 0;border-bottom:1px solid var(--line);font-size:12px}
.rrc-pipeline li b{color:var(--RRC);display:inline-block;min-width:52px}
pre.json{flex:1;overflow:auto;margin:0;padding:14px 16px;font:12px/1.45 ui-monospace,Consolas,monospace;color:#c9d1d9;white-space:pre-wrap}
.rrc-detail{width:48%;max-width:720px;min-width:320px;display:flex;flex-direction:column;background:var(--panel);overflow:hidden}
.rrc-names{padding:8px 14px;font-size:12px;color:var(--muted);border-bottom:1px solid var(--line)}
/* --- signaling json --- */
.sig-wrap{display:flex;flex:1;overflow:hidden;flex-direction:column}
.sig-proto{display:flex;gap:6px;padding:10px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.sig-proto button{padding:6px 14px;border-radius:6px;border:1px solid var(--line);background:var(--panel2);
  color:var(--muted);cursor:pointer;font-size:12px;font-weight:500}
.sig-proto button.on{color:#0a0e14;font-weight:600}
.sig-proto button[data-p="RRC"].on{background:var(--RRC);border-color:var(--RRC)}
.sig-proto button[data-p="S1"].on{background:var(--NGAP);border-color:var(--NGAP)}
.sig-proto button[data-p="X2"].on{background:var(--warn);border-color:var(--warn)}
.sig-body{display:flex;flex:1;overflow:hidden}
.sig-pane{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.sig-pane+.sig-pane{border-left:1px solid var(--line)}
.sig-hdr{padding:10px 14px;border-bottom:1px solid var(--line);font-size:12px;color:var(--muted);flex-shrink:0}
.sig-hdr b{color:var(--txt);font-size:13px}
.sig-list{flex:1;overflow:auto;border-bottom:1px solid var(--line);max-height:42%}
.sig-json{flex:1;overflow:auto;min-height:120px}
.sig-meta{padding:6px 14px;font-size:11px;color:var(--muted);border-bottom:1px solid var(--line)}
table.sig{width:100%;border-collapse:collapse;font-size:12px}
table.sig th{position:sticky;top:0;background:var(--panel);text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);color:var(--muted);font-weight:500}
table.sig td{padding:7px 10px;border-bottom:1px solid var(--line);cursor:pointer;vertical-align:top}
table.sig tr:hover td{background:rgba(88,166,255,.06)}
table.sig tr.sel td{background:rgba(88,166,255,.12)}
.sig-found{color:var(--ok)} .sig-miss{color:var(--muted)}
.sig-sources{padding:8px 14px;border-bottom:1px solid var(--line);font-size:12px;max-height:180px;overflow:auto}
.sig-sources summary{cursor:pointer;color:var(--muted);user-select:none}
.sig-sources table{font-size:11px;margin-top:8px}
.sig-sources select{font-size:11px;padding:4px}
/* --- 4G LTE tabs --- */
.tab-4g{border-bottom:2px solid #f9826c !important}
.tab-4g.on{background:#f9826c !important;color:#0a0e14 !important;border-color:#f9826c !important}
.tab-sim{border-bottom:2px solid #3fb950 !important}
.tab-sim.on{background:#3fb950 !important;color:#06170a !important;border-color:#3fb950 !important}
.tab-lw5g{border-bottom:2px solid #58a6ff !important}
.tab-lw5g.on{background:#58a6ff !important;color:#08131f !important;border-color:#58a6ff !important}
/* 4G ladder */
.lte-pair-bar{display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid var(--line);background:var(--panel2);flex-shrink:0}
.lte-pair-bar b{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-right:4px}
.lte-pair-btn{padding:5px 12px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--muted);cursor:pointer;font-size:12px;font-weight:600}
.lte-pair-btn:hover{border-color:#f9826c;color:var(--txt)}
.lte-pair-btn.on{background:#f9826c;color:#0a0e14;border-color:#f9826c}
.lte-pair-btn.down{opacity:.55;border-style:dashed}
.lte-pair-btn.down.on{opacity:.85}
.lte-pair-btn .dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:5px}
.lte-pair-btn .dot.attached{background:#3fb950}
.lte-pair-btn .dot.rejected{background:#f85149}
.lte-pair-btn .dot.released{background:#8b949e}
.lte-pair-btn .dot.in_progress{background:#d29922}
.lte-pair-btn .dot.none{background:#484f58}
.lte-pin-btn{padding:5px 12px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--muted);cursor:pointer;font-size:12px;font-weight:600}
.lte-pin-btn:hover{border-color:#f9826c;color:var(--txt)}
.lte-pin-btn.on{background:#d29922;color:#1a1400;border-color:#d29922}
.lte-pair-chip{display:inline-flex;align-items:center;gap:2px}
.lte-pair-power{padding:5px 7px;border-radius:6px;border:1px solid var(--line);background:var(--panel);
  color:var(--muted);cursor:pointer;font-size:11px;line-height:1}
.lte-pair-power:hover{border-color:#f9826c;color:var(--txt)}
.lte-pair-power.busy{opacity:.5;pointer-events:none}
.lte-params-toggle{padding:5px 10px;border-radius:6px;border:1px solid transparent;background:transparent;
  color:var(--muted);cursor:pointer;font-size:12px}
.lte-params-toggle:hover{color:var(--txt)}
.lte-params-panel{padding:12px 14px;border-bottom:1px solid var(--line);background:var(--panel);
  color:var(--muted);font-size:12px;flex-shrink:0}
.lte-wrap{display:flex;flex:1;overflow:hidden}
/* Fixed to the SVG ladder's native width (4 lanes x 120px, see buildLteLadderSvg)
   plus padding — sizing this to content (not flex:1) is what frees up the rest
   of the panel for .lte-detail instead of leaving it empty. */
.lte-ladder{flex:0 0 520px;overflow:auto;border-right:1px solid var(--line);display:flex;flex-direction:column}
.lte-lanes{display:flex;gap:0;padding:8px 14px;border-bottom:2px solid #f9826c;background:var(--panel2)}
.lte-lanehdr{flex:1;text-align:center;font-size:12px;font-weight:600;color:#f9826c;padding:4px 0}
.lte-icon{display:inline-flex;vertical-align:-2px;margin-right:5px;color:#f9826c;opacity:.85}
.lte-ev-list{flex:1;overflow:auto;padding:4px 0}
.lte-ev{display:flex;align-items:center;padding:5px 14px;border-bottom:1px solid var(--line);cursor:pointer;font-size:12px}
.lte-ev:hover{background:rgba(249,130,108,.06)}
.lte-ev.sel{background:rgba(249,130,108,.12)}
.lte-ev-dir{flex:0 0 90px;color:var(--muted);font-size:11px}
.lte-ev-layer{flex:0 0 56px;font-weight:600;font-size:11px}
.lte-ev-label{flex:1}
.lte-ev-time{flex:0 0 70px;font-size:10px;color:var(--muted);text-align:right}
.lte-sv-row.trace-backed{background:rgba(63,185,80,.07);box-shadow:inset 3px 0 0 #3fb950}
.lte-sv-row.trace-backed:hover{background:rgba(63,185,80,.13)}
.lte-inject{padding:6px 14px;font-size:11px;border-bottom:1px solid var(--line)}
.lte-inject b{color:#f9826c}
.lte-right{flex:1;min-width:380px;display:flex;flex-direction:column;overflow:hidden}
.lte-detail{flex:1;display:flex;flex-direction:column;background:var(--panel);overflow:hidden;min-height:0}
.lte-detail-body{flex:1;overflow:auto;padding:14px 16px;font-size:12.5px;line-height:1.55}
.lte-detail-body .info-lead{color:var(--txt);margin:0 0 8px;font-size:13px}
.lte-detail-body dl{margin:0 0 4px}
.lte-detail-body dt{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:10px 0 4px}
.lte-detail-body dt:first-child{margin-top:0}
.lte-detail-body dd{margin:0 0 4px;color:#c9d1d9}
.lte-enc{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:14px 0}
.lte-enc h4{margin:0 0 6px;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.lte-enc pre{margin:0;padding:8px 10px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;
  font-size:11px;line-height:1.45;white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto}
.lte-enc pre.empty{color:var(--muted);font-style:italic;white-space:normal}
.lte-raw-box{margin-top:14px;border:1px solid var(--line);border-radius:6px;background:var(--panel2)}
.lte-raw-box summary{cursor:pointer;padding:8px 10px;font-size:11px;color:#f9826c;font-weight:600;list-style:none;user-select:none}
.lte-raw-box summary::-webkit-details-marker{display:none}
.lte-raw-box summary::before{content:'\25b8\00a0';display:inline-block}
.lte-raw-box[open] summary::before{content:'\25be\00a0'}
.lte-raw-box pre{margin:0;padding:10px;border-top:1px solid var(--line);font-size:11px;line-height:1.45;max-height:360px;overflow:auto}
/* 4G attach KPI panel — bottom-right of the 4G LTE tab */
.lte-kpi{flex:0 0 auto;max-height:38%;overflow:auto;border-top:1px solid var(--line);background:var(--panel2);padding:10px 14px}
.lte-kpi-hdr{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.lte-kpi-hdr b{color:#f9826c;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.lte-kpi-total{font-size:11px;color:var(--muted);margin:0 0 8px}
.lte-kpi-row{display:flex;align-items:center;gap:8px;font-size:11px;margin:5px 0}
.lte-kpi-row .ph{flex:0 0 140px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lte-kpi-row .bar{flex:1;height:8px;background:var(--line);border-radius:3px;overflow:hidden}
.lte-kpi-row .bar i{display:block;height:100%;background:#f9826c;border-radius:3px}
.lte-kpi-row .ms{flex:0 0 56px;text-align:right;color:#c9d1d9;font-variant-numeric:tabular-nums}
.lte-kpi-badge{display:inline-block;padding:1px 7px;border-radius:4px;font-size:10px;font-weight:700;text-transform:capitalize}
.lte-kpi-badge.attached{background:#3fb950;color:#07120a}
.lte-kpi-badge.rejected{background:#f85149;color:#1a0707}
.lte-kpi-badge.released{background:#8b949e;color:#0a0e14}
.lte-kpi-badge.in_progress{background:#d29922;color:#1a1400}
.lte-kpi-badge.none{background:var(--line);color:var(--muted)}
/* 4G live KPI histogram — bottom-left of the 4G LTE tab, accumulated across
   every completed call flow demo3ue/live_cycler.py has recorded, not just
   the one currently shown in the ladder. */
.lte-hist{flex:0 0 auto;max-height:42%;overflow:auto;border-top:1px solid var(--line);background:var(--panel2);padding:10px 14px}
.lte-hist-hdr{display:flex;align-items:center;gap:6px;margin-bottom:6px;flex-wrap:wrap}
.lte-hist-hdr b{color:#f9826c;font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-right:4px}
.lte-hist-metric-btn{padding:3px 9px;border-radius:5px;border:1px solid var(--line);background:var(--panel);color:var(--muted);cursor:pointer;font-size:10.5px}
.lte-hist-metric-btn.on{background:#f9826c;color:#0a0e14;border-color:#f9826c}
.lte-hist-summary{font-size:10.5px;color:var(--muted);margin:0 0 6px}
.lte-hist-bar-row{display:flex;align-items:center;gap:6px;font-size:10.5px;margin:2px 0}
.lte-hist-bar-row .bin{flex:0 0 64px;color:var(--muted);text-align:right;white-space:nowrap;overflow:hidden}
.lte-hist-bar-row .bar{flex:1;height:7px;background:var(--line);border-radius:3px;overflow:hidden}
.lte-hist-bar-row .bar i{display:block;height:100%;background:#58a6ff;border-radius:3px}
.lte-hist-bar-row .cnt{flex:0 0 22px;color:#c9d1d9;font-variant-numeric:tabular-nums}
.lte-hist-per-pair{margin-top:8px;font-size:10.5px;color:var(--muted);border-top:1px solid var(--line);padding-top:6px}
.lte-hist-per-pair span{margin-right:10px}
.lte-hist-empty{color:var(--muted);font-size:11px;font-style:italic}
/* 4G trace tab */
.ltetrace-wrap{display:flex;flex:1;overflow:hidden;flex-direction:column}
.ltetrace-bar{display:flex;gap:8px;padding:8px 14px;border-bottom:1px solid var(--line);align-items:center;flex-wrap:wrap}
.ltetrace-bar b{color:#f9826c;font-size:13px}
.ltetrace-body{display:flex;flex:1;overflow:hidden}
.ltetrace-pane{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.ltetrace-pane+.ltetrace-pane{border-left:1px solid var(--line)}
.ltetrace-hdr{padding:8px 14px;border-bottom:1px solid var(--line);font-size:12px;color:var(--muted);flex-shrink:0}
.ltetrace-hdr b{color:var(--txt);font-size:13px}
.ltetrace-list{flex:1;overflow:auto;max-height:50%;border-bottom:1px solid var(--line)}
table.ltetrace{width:100%;border-collapse:collapse;font-size:12px}
table.ltetrace th{position:sticky;top:0;background:var(--panel);text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);color:var(--muted);font-weight:500}
table.ltetrace td{padding:6px 10px;border-bottom:1px solid var(--line);cursor:pointer;vertical-align:top}
table.ltetrace tr:hover td{background:rgba(249,130,108,.06)}
table.ltetrace tr.sel td{background:rgba(249,130,108,.14)}
.ltetrace-detail-json{flex:1;overflow:auto;min-height:80px}
.per-badge{display:inline-block;background:#f9826c;color:#0a0e14;border-radius:4px;padding:1px 6px;font-size:10px;font-weight:700;margin-left:6px}
.trace-badge{display:inline-block;background:#3fb950;color:#07120a;border-radius:4px;padding:1px 6px;font-size:10px;font-weight:700;margin-left:6px}
.status-badge{display:inline-block;border-radius:4px;padding:1px 6px;font-size:10px;font-weight:700;margin-left:6px}
.status-exact{background:#3fb950;color:#07120a}
.status-reconstructed{background:#d29922;color:#140d00}
.status-minimal{background:#8b949e;color:#0a0e14}
.status-encode_failed{background:#f85149;color:#190407}
.status-none{background:#30363d;color:#c9d1d9}
.diagram,.msg-list,.detail-body,.detail-pane .json-block,.sig-list,.sig-json,.rrc-list,.rrc-detail,.overview{scrollbar-width:none;-ms-overflow-style:none}
.diagram::-webkit-scrollbar,.msg-list::-webkit-scrollbar,.detail-body::-webkit-scrollbar,
.detail-pane .json-block::-webkit-scrollbar,
.sig-list::-webkit-scrollbar,.sig-json::-webkit-scrollbar,.rrc-list::-webkit-scrollbar,
.rrc-detail::-webkit-scrollbar,.overview::-webkit-scrollbar{display:none;width:0;height:0}
</style></head>
<body>
<header>
  <button type="button" class="icon-btn" id="sidebar-toggle" title="Choose digital twin dashboard" aria-label="Open sidebar">&#9776;</button>
  <h1 id="header-title">Full-stack 4G LTE Digital Twin</h1>
  <nav class="tabs">
    <button class="tab tab-4g on" data-tab="lte4g" data-twin="lte4g_full">4G LTE</button>
    <button class="tab" data-tab="overview" data-twin="lte4g_full">Overview</button>
    <button class="tab tab-sim" data-tab="simulation" data-twin="simulation" style="display:none">Simulation</button>
    <button class="tab tab-lw5g" data-tab="lw5g_callflow" data-twin="lightweight5g" style="display:none">Call Flow</button>
    <button class="tab tab-lw5g" data-tab="lw5g_overview" data-twin="lightweight5g" style="display:none">Overview</button>
  </nav>
  <div class="twin-ctrl-bar" id="twin-ctrl-bar">
    <span class="twin-ctrl-status" id="twin-ctrl-status">checking…</span>
    <button type="button" class="twin-ctrl-btn" id="twin-ctrl-btn">Stop backend</button>
  </div>
  <div class="livebar" id="livebar">
    <span class="dot"></span><span id="live-label">Live</span>
    <button type="button" id="btn-refresh">Refresh</button>
  </div>
</header>
<div class="sidebar-overlay" id="sidebar-overlay"></div>
<aside class="sidebar" id="sidebar">
  <div class="sidebar-hdr">Digital Twin Dashboards</div>
  <button type="button" class="sidebar-item on" data-twin="lte4g_full" data-title="Full-stack 4G LTE Digital Twin">
    <span class="sidebar-item-dot" id="sidebar-dot-lte4g_full"></span>Full-stack 4G LTE Digital Twin
  </button>
  <button type="button" class="sidebar-item" data-twin="simulation" data-title="Simulation">
    <span class="sidebar-item-dot" id="sidebar-dot-simulation"></span>Simulation
  </button>
  <button type="button" class="sidebar-item" data-twin="lightweight5g" data-title="Lightweight 5G Twin">
    <span class="sidebar-item-dot" id="sidebar-dot-lightweight5g"></span>Lightweight 5G Twin
    <span class="sidebar-item-tag">preview</span>
  </button>
  <div class="sidebar-note">Selecting a twin starts its backend and stops the other twin's — only one runs at a time.</div>
</aside>

<section class="panel on" id="panel-lte4g" data-twin="lte4g_full">
  <div class="lte-pair-bar" id="lte-pair-bar"></div>
  <div class="lte-params-panel" id="lte-params-panel" style="display:none">
    No configurable parameters yet for this digital twin.
  </div>
  <div class="lte-wrap">
    <div class="lte-ladder">
      <div class="lte-lanes">
        <div class="lte-lanehdr"><span class="lte-icon" title="UE — the simulated phone (srsue)">__ICON_UE__</span>srsUE 4G</div>
        <div class="lte-lanehdr"><span class="lte-icon" title="The ZeroMQ-emulated radio link carrying IQ samples">__ICON_IQ__</span>ZMQ IQ</div>
        <div class="lte-lanehdr"><span class="lte-icon" title="eNB — the LTE base station (srsenb)">__ICON_ENB__</span>srseNB</div>
        <div class="lte-lanehdr"><span class="lte-icon" title="EPC — the LTE core network (srsepc)">__ICON_EPC__</span>srsEPC</div>
      </div>
      <div id="lte-inject-bar" class="lte-inject" style="display:none"></div>
      <div class="lte-ev-list" id="lte-ev-list"></div>
    </div>
    <div class="lte-right">
      <div class="lte-detail" id="lte-detail">
        <div class="sig-hdr" style="border-bottom:1px solid var(--line);padding:10px 14px">
          <b id="lte-detail-title" style="color:#f9826c">Select a 4G event</b>
        </div>
        <div class="lte-detail-body" id="lte-detail-body">
          <p class="info-lead">Click a message in the ladder to see what it does.</p>
        </div>
      </div>
    </div>
  </div>
</section>

<section class="panel" id="panel-lte4gtrace" data-twin="lte4g_full">
  <div class="ltetrace-wrap">
    <div class="ltetrace-bar">
      <b>4G Trace</b>
      <span style="color:var(--muted);font-size:12px">TELUS 302/221 · 22_decoded records aligned with live LTE events</span>
      <span id="ltetrace-count" style="font-size:11px;color:var(--muted)"></span>
    </div>
    <div class="ltetrace-body">
      <div class="ltetrace-pane">
        <div class="ltetrace-hdr"><b>22_decoded Trace Messages</b></div>
        <div class="ltetrace-list">
          <table class="ltetrace" id="ltetrace-table">
            <thead><tr>
              <th>Type</th><th>Direction</th><th>Choice</th>
              <th>PER</th><th>Semantic Fields</th>
            </tr></thead>
            <tbody id="ltetrace-tbody"></tbody>
          </table>
        </div>
        <div class="ltetrace-detail-json">
          <pre class="json" id="ltetrace-json">Select a trace record</pre>
        </div>
      </div>
      <div class="ltetrace-pane">
        <div class="ltetrace-hdr"><b>PER Template (pycrate-encoded)</b></div>
        <div class="ltetrace-detail-json">
          <pre class="json" id="per-template-json">Select a trace record to view PER template</pre>
        </div>
      </div>
    </div>
  </div>
</section>

<section class="panel" id="panel-overview" data-twin="lte4g_full">
  <div class="overview">
    <h3 style="font-size:14px;margin:0 0 4px">4G LTE Full-Stack Topology</h3>
    <p style="margin:0 0 12px;font-size:11.5px;color:var(--muted)">Click any element for what it is and what it does.</p>
    <div id="lte4g-topo"></div>
    <div id="lte4g-topo-def" class="lte4g-topo-def" style="display:none"></div>

    <h3 style="font-size:14px;margin:22px 0 10px">4G LTE KPIs</h3>
    <div class="overview-kpi-row">
      <div class="lte-hist" id="lte-hist"></div>
      <div class="lte-kpi" id="lte-kpi"></div>
    </div>

    <div class="cards" id="cards"></div>
    <div class="topo">
      <h3 style="margin:0 0 12px;font-size:14px">Topology · <span id="topo-mode">direct</span></h3>
      <div id="topo-svg"></div>
    </div>
    <h3 style="font-size:14px;margin:0 0 8px">Messages by layer</h3>
    <div class="layer-bars" id="layer-bars"></div>
  </div>
</section>

<section class="panel" id="panel-simulation" data-twin="simulation">
  <div class="sim-wrap">
    <p class="sim-desc">A lightweight, large-scale 5G RU stress-test twin — 1 DU + 3 RU sites
      (9 sector cells, 250 PRBs each) + a UE simulator that scales into the thousands as
      asyncio tasks. No real PHY/ASN.1: built for capacity and admission-control testing,
      not protocol-exact validation (that's what the 4G LTE twin is for).</p>
    <div id="sim-content"><p class="sim-empty">Start the backend to see live metrics.</p></div>
  </div>
</section>

<section class="panel" id="panel-lw5g-callflow" data-twin="lightweight5g">
  <div class="lw5g-wrap">
    <p class="lw5g-desc">A planned lightweight 5G twin: PHY-abstract UEs (no real radio stack) generating
      real RRC/NAS signaling toward a 5G core, scaling far beyond what a real protocol stack can run on
      one host. This is a dashboard preview built ahead of the backend — the controls below don't do
      anything yet, but the layout is what they'll drive once it exists.</p>
    <div class="lw5g-params-bar" id="lw5g-params-bar-callflow"></div>
    <div class="lw5g-params-panel" id="lw5g-params-panel-callflow" style="display:none"></div>
    <div class="lw5g-empty">
      <b>Call-flow view — coming once the backend exists</b>
      A live signaling ladder (RRC/NAS), similar to the 4G LTE twin's, will appear here once the
      lightweight 5G twin's UE engine is built.
    </div>
  </div>
</section>

<section class="panel" id="panel-lw5g-overview" data-twin="lightweight5g">
  <div class="lw5g-wrap">
    <div class="lw5g-params-bar" id="lw5g-params-bar-overview"></div>
    <div class="lw5g-params-panel" id="lw5g-params-panel-overview" style="display:none"></div>
    <h3 style="font-size:14px;margin:18px 0 10px;color:#58a6ff">Analytics (preview)</h3>
    <div class="lw5g-analytics">
      <div class="lw5g-chart-box"><b>Admission outcomes</b><p class="lw5g-chart-empty">No data yet — backend not built.</p></div>
      <div class="lw5g-chart-box"><b>Signaling load over time</b><p class="lw5g-chart-empty">No data yet — backend not built.</p></div>
    </div>
  </div>
</section>

<section class="panel" id="panel-callflow" data-twin="lte4g_full">
  <div class="cf-toolbar"><div class="legend" id="legend"></div></div>
  <div class="cf-wrap">
    <div class="diagram scroll-hide">
      <div class="lanes">
        <div class="lanehdr">UE<small>srsUE</small></div>
        <div class="lanehdr">RU<small>ZMQ IQ</small></div>
        <div class="lanehdr">DU / gNB<small>ocudu</small></div>
        <div class="lanehdr">5GC<small>Open5GS</small></div>
      </div>
      <div id="canvas"></div>
    </div>
    <div class="detail">
      <div class="dh"><div class="t" id="dt">Select a message</div><div id="dmeta"></div></div>
      <div class="detail-body scroll-hide">
        <div class="detail-panes">
          <div class="detail-pane">
            <div class="log-hdr">Decoded JSON <span id="djson-src" style="font-weight:400;text-transform:none;letter-spacing:0"></span></div>
            <pre id="djson" class="json-block empty scroll-hide">Select a message to view decoded ASN.1 JSON.</pre>
          </div>
          <div class="detail-pane">
            <div class="log-hdr">Raw message</div>
            <pre id="dbody" class="json-block empty scroll-hide">Full stack log excerpt.</pre>
          </div>
        </div>
        <div class="msg-info" id="dinfo"><p class="info-lead">Click any arrow for protocol reference.</p></div>
      </div>
    </div>
  </div>
</section>

<section class="panel" id="panel-messages" data-twin="lte4g_full">
  <div class="msg-wrap">
    <div class="msg-list scroll-hide">
      <div class="msg-toolbar">
        <input type="search" id="msg-q" placeholder="Search labels, routes, detail…">
        <select id="msg-layer"><option value="">All layers</option></select>
      </div>
      <table class="msg"><thead><tr>
        <th>#</th><th>+t</th><th>Layer</th><th>Route</th><th>Message</th>
      </tr></thead><tbody id="msg-tbody"></tbody></table>
    </div>
    <div class="msg-detail">
      <div class="dh"><div class="t" id="mt">Select a row</div><div id="mmeta"></div></div>
      <div class="detail-body scroll-hide">
        <div class="detail-panes">
          <div class="detail-pane">
            <div class="log-hdr">Decoded JSON <span id="mjson-src" style="font-weight:400;text-transform:none;letter-spacing:0"></span></div>
            <pre id="mjson" class="json-block empty scroll-hide">Select a message to view decoded ASN.1 JSON.</pre>
          </div>
          <div class="detail-pane">
            <div class="log-hdr">Raw message</div>
            <pre id="mbody" class="json-block empty scroll-hide">Full log excerpt for the selected message.</pre>
          </div>
        </div>
        <div class="msg-info" id="minfo"><p class="info-lead">Select a message to see purpose, format, and flow context.</p></div>
      </div>
    </div>
  </div>
</section>

<section class="panel" id="panel-rrc" data-twin="lte4g_full">
  <div class="rrc-wrap">
    <div class="rrc-list">
      <div class="rrc-toolbar">
        <div class="rrc-src">
          <button type="button" class="on" id="rrc-src-twin">srsTwin logs</button>
          <button type="button" id="rrc-src-trace">Field trace</button>
        </div>
        <input type="search" id="rrc-q" placeholder="Filter message name…">
        <select id="rrc-ch"><option value="">All channels</option></select>
      </div>
      <div class="rrc-names" id="rrc-names"></div>
      <table class="msg"><thead><tr>
        <th>#</th><th>Name</th><th>Ch</th><th>Dir</th><th>B</th><th>Source</th>
      </tr></thead><tbody id="rrc-tbody"></tbody></table>
    </div>
    <div class="rrc-detail">
      <div class="dh"><div class="t" id="rt">Select an RRC message</div><div id="rmeta"></div></div>
      <div class="msg-info" id="riq"></div>
      <div class="log-hdr">Decoded JSON</div>
      <pre class="json" id="rjson">Exact ASN.1 JSON from srsUE/ocudu Content:[ … ] blocks.</pre>
      <div class="log-hdr">PER hex (wire bytes before PHY)</div>
      <pre class="json" id="rhex" style="max-height:120px;flex:0"></pre>
    </div>
  </div>
</section>

<section class="panel" id="panel-signaling" data-twin="lte4g_full">
  <div class="sig-wrap">
    <div class="sig-proto" id="sig-proto">
      <button type="button" class="on" data-p="RRC">RRC</button>
      <button type="button" data-p="S1">S1</button>
      <button type="button" data-p="X2">X2</button>
    </div>
    <div class="sig-meta" id="sig-meta"></div>
    <details class="sig-sources" id="sig-sources-wrap">
      <summary>Per-message source: trace replay vs ML (hybrid) · <span id="sig-src-status">loading…</span></summary>
      <table class="sig"><thead><tr>
        <th>Message</th><th>Proto</th><th>Trace</th><th>ML</th><th>Mode</th>
      </tr></thead><tbody id="sig-src-tbody"></tbody></table>
      <button type="button" class="copy" id="sig-src-save" style="margin-top:8px">Save sources</button>
    </details>
    <div class="sig-body">
      <div class="sig-pane">
        <div class="sig-hdr"><b>Live call flow</b> · JSON from srsTwin logs</div>
        <div class="sig-list">
          <table class="sig"><thead><tr>
            <th>#</th><th>+t</th><th>Message</th><th>Source</th>
          </tr></thead><tbody id="sig-live-tbody"></tbody></table>
        </div>
        <div class="log-hdr">Live JSON</div>
        <pre class="json sig-json" id="sig-live-json">Select a live message.</pre>
      </div>
      <div class="sig-pane">
        <div class="sig-hdr"><b>22_decoded reference</b> · by record_id</div>
        <div class="sig-list">
          <table class="sig"><thead><tr>
            <th>record_id</th><th>Message name</th><th>Sample</th>
          </tr></thead><tbody id="sig-ref-tbody"></tbody></table>
        </div>
        <div class="log-hdr">Trace JSON</div>
        <pre class="json sig-json" id="sig-ref-json">Select a catalog entry or a live message to load the matching trace sample.</pre>
      </div>
    </div>
  </div>
</section>

<script>
let EVENTS = __DATA__;
let META = __META__;
const MESSAGE_INFO = __MESSAGE_INFO__;
const LABEL_RULES = __LABEL_RULES__;

function resolveInfo(e){
  if(e && e.info && e.info.purpose) return e.info;
  const label = (e && e.label) || '';
  for(const rule of LABEL_RULES){
    const prefix = rule[0], key = rule[1];
    if(label.startsWith(prefix) || label.includes(prefix)){
      const base = MESSAGE_INFO[key];
      if(base) return Object.assign({key}, base);
    }
  }
  return {
    key: 'unknown',
    summary: 'Signaling event parsed from stack logs.',
    purpose: 'Event: ' + label + '. See log excerpt below for ASN.1/hex detail from srsUE or ocudu.',
    protocol: 'See log layer and route in the header.',
    structure: 'Refer to decoded ASN.1 in the log excerpt (3GPP TS 38.331 / 38.413 / 24.501).',
    flow: 'Part of the 5G SA attach or session procedure shown in the ladder.'
  };
}

function enrichEvents(list){
  (list || []).forEach(e => { e.info = resolveInfo(e); });
  return list;
}
enrichEvents(EVENTS);

const LANES = {UE:0, RU:1, DU:2, "5GC":3};
const LANEX = i => (i+0.5)/4*100;
const COLORS = {PHY:'--PHY',MAC:'--MAC',RRC:'--RRC',NAS:'--NAS',NGAP:'--NGAP'};
const cssv = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const ROW=92, active={PHY:1,MAC:1,RRC:1,NAS:1,NGAP:1};
let selId=null, msgSelId=null, liveMode=false;

/* tabs */
function activateTab(tabName){
  const btn = document.querySelector(`.tab[data-tab="${tabName}"]`);
  const panel = document.getElementById('panel-'+tabName);
  if(!btn || !panel) return;
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));
  btn.classList.add('on');
  panel.classList.add('on');
  if(tabName==='callflow') render();
}

document.querySelectorAll('.tab').forEach(btn=>{
  btn.onclick=()=>{
    activateTab(btn.dataset.tab);
  };
});

/* ===================  Twin manager  ===================
   The dashboard process is always on; only one twin's *backend containers*
   run at a time. Selecting a twin from the sidebar is the preferred path
   (auto-starts it, auto-stops the other) — the Start/Stop button on each
   twin's own panel does the same thing manually, for when you don't want
   to navigate away, or just want to shut everything down. */
const TWIN_TITLES = {
  lte4g_full: 'Full-stack 4G LTE Digital Twin',
  simulation: 'Simulation',
  lightweight5g: 'Lightweight 5G Twin',
};
// Registered in the sidebar/tabs so its dashboard can be designed ahead of
// time, but it has no compose stack yet — the backend doesn't know this key
// at all. The control bar reflects that honestly (disabled, "not built
// yet") instead of pretending a start/stop action would do anything.
const NOT_BUILT_TWINS = new Set(['lightweight5g']);
const TWIN_ACCENT_CLASS = {simulation: 'accent-sim', lightweight5g: 'accent-lw5g'};
let activeTwin = 'lte4g_full';
let twinSwitching = false;   // one shared header bar now, not per-twin busy state
let twinStatusCache = {};

function twinDotClass(status){
  return (status && status.overall) || 'stopped';
}

function renderTwinCtrlBar(){
  const statusEl = document.getElementById('twin-ctrl-status');
  const btnEl = document.getElementById('twin-ctrl-btn');
  if(!statusEl || !btnEl) return;
  Object.values(TWIN_ACCENT_CLASS).forEach(c => btnEl.classList.remove(c));
  if(TWIN_ACCENT_CLASS[activeTwin]) btnEl.classList.add(TWIN_ACCENT_CLASS[activeTwin]);

  if(NOT_BUILT_TWINS.has(activeTwin)){
    statusEl.innerHTML = '<span class="dot stopped"></span>Not built yet';
    btnEl.textContent = 'Start backend';
    btnEl.disabled = true;
    btnEl.classList.remove('busy');
    return;
  }
  btnEl.disabled = false;
  const cls = twinSwitching ? 'busy' : twinDotClass(twinStatusCache[activeTwin]);
  const label = twinSwitching ? 'Working…' : (cls === 'running' ? 'Backend running'
    : cls === 'partial' ? 'Backend partially running' : 'Backend stopped');
  statusEl.innerHTML = `<span class="dot ${cls}"></span>${label}`;
  const action = (cls === 'running' || cls === 'partial') ? 'stop' : 'start';
  btnEl.textContent = action === 'stop' ? 'Stop backend' : 'Start backend';
  btnEl.dataset.action = action;
  btnEl.classList.toggle('busy', twinSwitching);
}

function renderSidebarDots(){
  for(const key of Object.keys(TWIN_TITLES)){
    const dot = document.getElementById(`sidebar-dot-${key}`);
    if(dot) dot.className = 'sidebar-item-dot ' + (NOT_BUILT_TWINS.has(key) ? 'stopped' : twinDotClass(twinStatusCache[key]));
  }
}

function renderAllTwinUi(){
  renderTwinCtrlBar();
  renderSidebarDots();
}

async function pollTwinStatus(){
  try{
    const r = await fetch('/api/twins/status');
    twinStatusCache = await r.json();
  }catch(e){ /* keep last known status rather than blank it out */ }
  renderAllTwinUi();
}

async function setTwinBackend(key, action){
  if(NOT_BUILT_TWINS.has(key)){
    alert(`${TWIN_TITLES[key] || key} doesn't have a backend yet — this is a dashboard preview only.`);
    return;
  }
  if(twinSwitching) return;
  twinSwitching = true;
  renderAllTwinUi();
  try{
    const url = action === 'start' ? '/api/twins/activate' : '/api/twins/stop';
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                                  body: JSON.stringify({twin: key})});
    const d = await r.json();
    if(d.twins) twinStatusCache = d.twins;
    if(!d.ok){
      alert(`Failed to ${action} ${TWIN_TITLES[key] || key}: ${d.error || 'unknown error'}`);
    }
  }catch(e){
    alert(`Failed to ${action} ${TWIN_TITLES[key] || key}: ${e}`);
  }finally{
    twinSwitching = false;
    renderAllTwinUi();
    if(action === 'start' && key === 'simulation') pollSimulation();
  }
}

document.getElementById('twin-ctrl-btn').onclick = () =>
  setTwinBackend(activeTwin, document.getElementById('twin-ctrl-btn').dataset.action || 'stop');

function showTwinView(key){
  activeTwin = key;
  document.querySelectorAll('.sidebar-item').forEach(b=>b.classList.toggle('on', b.dataset.twin === key));
  document.getElementById('header-title').textContent = TWIN_TITLES[key] || key;
  document.querySelectorAll('.tab').forEach(b=>{ b.style.display = b.dataset.twin === key ? '' : 'none'; });
  const firstTab = document.querySelector(`.tab[data-twin="${key}"]`);
  if(firstTab) activateTab(firstTab.dataset.tab);
  renderTwinCtrlBar();
  if(key === 'simulation') pollSimulation();
}

pollTwinStatus();
setInterval(pollTwinStatus, 5000);

/* sidebar: digital-twin dashboard picker */
function toggleSidebar(force){
  const sb = document.getElementById('sidebar');
  const ov = document.getElementById('sidebar-overlay');
  const open = force !== undefined ? force : !sb.classList.contains('open');
  sb.classList.toggle('open', open);
  ov.classList.toggle('open', open);
}
document.getElementById('sidebar-toggle').onclick = () => toggleSidebar();
document.getElementById('sidebar-overlay').onclick = () => toggleSidebar(false);
document.querySelectorAll('.sidebar-item').forEach(btn=>{
  btn.onclick = () => {
    const key = btn.dataset.twin;
    showTwinView(key);
    toggleSidebar(false);
    // Preferred design: selecting a twin opens it, closes the other — but
    // don't fire that (and its alert) for a twin with no backend yet; just
    // show its dashboard preview.
    if(!NOT_BUILT_TWINS.has(key)) setTwinBackend(key, 'start');
  };
});

/* ===================  Simulation twin (poc_StressTest)  =================== */
// Distinct color per cell name, stable across redraws (assigned in first-seen
// order) — used by both the mobility map and its legend.
const SIM_CELL_PALETTE = ['#3fb950','#58a6ff','#d29922','#a371f7','#f0883e','#22d3ee','#f85149','#7ee787','#79c0ff'];
const simCellColors = new Map();
function simCellColor(name){
  if(!name) return '#5b6472';
  if(!simCellColors.has(name)){
    simCellColors.set(name, SIM_CELL_PALETTE[simCellColors.size % SIM_CELL_PALETTE.length]);
  }
  return simCellColors.get(name);
}

// Port of the original poc_StressTest dashboard's mobility map
// (dashboard/static/index.html's drawGeoMap) — simplified: no trails, no
// anomaly rings, no hover/click, just a clean snapshot of where every RU
// site/sector and every UE actually is, redrawn each poll.
function drawSimGeoMap(geo){
  const canvas = document.getElementById('sim-geo-canvas');
  if(!canvas) return;
  if(!geo || !(geo.sites || geo.ues)){
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 600, h = canvas.clientHeight || 360;
  if(canvas.width !== Math.round(w*dpr) || canvas.height !== Math.round(h*dpr)){
    canvas.width = Math.round(w*dpr); canvas.height = Math.round(h*dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const R = (geo.bounds && geo.bounds.max_radius_m) || 1100;
  const pad = 30;
  const scale = (Math.min(w, h) / 2 - pad) / (R * 1.05);
  const cx = w / 2, cy = h / 2;
  const toScreen = (x, y) => [cx + x * scale, cy - y * scale];

  ctx.strokeStyle = '#1e2533';
  ctx.lineWidth = 1;
  for(const frac of [0.33, 0.66, 1.0]){
    ctx.beginPath(); ctx.arc(cx, cy, R * scale * frac, 0, Math.PI * 2); ctx.stroke();
  }

  const cov = (geo.bounds && geo.bounds.coverage_hint_m) || 1300;
  const cellsBySite = {};
  (geo.cells || []).forEach(c => { (cellsBySite[c.site] = cellsBySite[c.site] || []).push(c); });
  (geo.sites || []).forEach(site => {
    const [sx, sy] = toScreen(site.x, site.y);
    const covR = Math.max(60, cov * scale * 0.85);
    (cellsBySite[site.name] || []).forEach((cell, i) => {
      const az = cell.azimuth_deg != null ? cell.azimuth_deg : i * 120;
      const span = cell.sector_width_deg || 120;
      const col = simCellColor(cell.name);
      const start = ((az - span / 2) - 90) * Math.PI / 180;
      const end = ((az + span / 2) - 90) * Math.PI / 180;
      ctx.beginPath(); ctx.moveTo(sx, sy); ctx.arc(sx, sy, covR, start, end); ctx.closePath();
      ctx.fillStyle = col + '22'; ctx.fill();
      ctx.strokeStyle = col + '77'; ctx.lineWidth = 1; ctx.stroke();
    });
    ctx.fillStyle = '#e8eaed';
    ctx.beginPath(); ctx.arc(sx, sy, 4, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#c9d1d9'; ctx.font = '700 11px Segoe UI, system-ui, sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(site.name || 'site', sx, sy - 12);
  });

  (geo.ues || []).forEach(u => {
    const [ux, uy] = toScreen(u.x, u.y);
    const col = simCellColor(u.cell);
    if(u.state === 'attaching'){
      ctx.strokeStyle = col; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(ux, uy, 3.5, 0, Math.PI * 2); ctx.stroke();
    } else {
      ctx.fillStyle = u.state === 'connected' ? col : '#5b6472';
      ctx.beginPath(); ctx.arc(ux, uy, u.state === 'connected' ? 2.6 : 2, 0, Math.PI * 2); ctx.fill();
    }
  });
}

function _simCanvasCtx(id){
  const canvas = document.getElementById(id);
  if(!canvas) return null;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 200, h = canvas.clientHeight || 130;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return {ctx, w, h};
}

function drawSimPrbDonut(cells){
  const c = _simCanvasCtx('sim-prb-donut');
  if(!c) return;
  const {ctx, w, h} = c;
  const totalUsed = (cells || []).reduce((s, c2) => s + (c2.used_prbs || 0), 0);
  const totalFree = (cells || []).reduce((s, c2) => s + (c2.free_prbs || 0), 0);
  const total = totalUsed + totalFree;
  const cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 6, rInner = r * 0.6;
  if(total <= 0){
    ctx.fillStyle = '#5b6472'; ctx.font = '11px Segoe UI,system-ui,sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('no data', cx, cy);
    return;
  }
  const usedFrac = totalUsed / total;
  let start = -Math.PI / 2;
  [[usedFrac, '#3fb950'], [1 - usedFrac, '#30363d']].forEach(([frac, col]) => {
    const end = start + frac * Math.PI * 2;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.arc(cx, cy, r, start, end); ctx.closePath();
    ctx.fillStyle = col; ctx.fill();
    start = end;
  });
  ctx.globalCompositeOperation = 'destination-out';
  ctx.beginPath(); ctx.arc(cx, cy, rInner, 0, Math.PI * 2); ctx.fill();
  ctx.globalCompositeOperation = 'source-over';
  ctx.fillStyle = '#c9d1d9'; ctx.font = '700 15px Segoe UI,system-ui,sans-serif';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(Math.round(usedFrac * 100) + '%', cx, cy - 4);
  ctx.font = '10px Segoe UI,system-ui,sans-serif'; ctx.fillStyle = '#8b949e';
  ctx.fillText('used', cx, cy + 12);
}

function drawSimOutcomeBar(ue){
  const c = _simCanvasCtx('sim-outcome-bar');
  if(!c) return;
  const {ctx, w, h} = c;
  const rows = [
    ['Admitted', ue.admitted || 0, '#3fb950'],
    ['Rejected', ue.rejected || 0, '#f85149'],
    ['Released', ue.released || 0, '#8b949e'],
    ['Handovers', ue.handovers || 0, '#58a6ff'],
  ];
  const max = Math.max(...rows.map(r => r[1]), 1);
  const padL = 66, padR = 8, padTop = 4, gap = 7;
  const barH = (h - padTop * 2 - gap * (rows.length - 1)) / rows.length;
  rows.forEach(([label, val, col], i) => {
    const y = padTop + i * (barH + gap);
    ctx.fillStyle = '#30363d';
    ctx.fillRect(padL, y, w - padL - padR, barH);
    ctx.fillStyle = col;
    ctx.fillRect(padL, y, (w - padL - padR) * (val / max), barH);
    ctx.fillStyle = '#9aa3ad'; ctx.font = '10px Segoe UI,system-ui,sans-serif';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    ctx.fillText(label, padL - 8, y + barH / 2);
    ctx.fillStyle = '#e8eaed'; ctx.textAlign = 'left';
    ctx.fillText(String(val), padL + 6, y + barH / 2);
  });
}

function renderSimulation(metrics){
  const box = document.getElementById('sim-content');
  const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  if(!metrics || metrics.ok === false || metrics.error){
    box.innerHTML = `<p class="sim-empty">${esc((metrics && metrics.error) || 'No data yet — start the backend.')}</p>`;
    return;
  }
  const du = metrics.du || {cells: []};
  const ue = metrics.ue || {};
  const geo = metrics.geo_fresh ? metrics.geo : null;
  const cells = du.cells || [];
  const configured = ue.num_ues_configured || 0;
  const running = ue.num_ues_running || 0;
  const maxUes = Math.max(ue.num_ues_max || 5000, configured, 100);

  let html = `<div class="sim-ue-row">
      <b>UE count: ${configured}${running !== configured ? ` (${running} running)` : ''}</b>
      <input type="range" id="sim-ue-slider" min="0" max="${maxUes}" value="${configured}">
      <span class="sim-ue-val" id="sim-ue-val">${configured}</span>
    </div>`;

  const stat = (l, v) => `<div class="stat"><div class="v">${v ?? '—'}</div><div class="l">${esc(l)}</div></div>`;
  html += '<div class="sim-stats">'
    + stat('Active', ue.active) + stat('Admitted', ue.admitted) + stat('Rejected', ue.rejected)
    + stat('Released', ue.released) + stat('Handovers', ue.handovers) + stat('HO fail', ue.ho_fail)
    + '</div>';

  // Mobility map (left half) — RU sites with their sector fans, every UE
  // positioned and colored by serving cell, ported from the original
  // poc_StressTest dashboard's main page — plus analytics charts (right
  // half) so the box isn't mostly empty space either side of the map.
  const cellNames = [...new Set((geo && geo.cells ? geo.cells : cells.map(c=>({name:c.cell_id}))).map(c=>c.name))];
  const legend = cellNames.map(n=>`<span class="sim-geo-legend-item">`
    + `<i style="background:${simCellColor(n)}"></i>${esc(n)}</span>`).join('');
  html += `<div class="sim-geo-box">
      <div class="sim-geo-hdr"><b>Mobility map &amp; analytics</b>`
      + (geo ? `<span class="sim-geo-count">${(geo.ues||[]).length} UEs shown</span>` : '<span class="sim-geo-count">waiting for geo data…</span>')
      + `</div>
      <div class="sim-geo-cols">
        <div class="sim-geo-left">
          <canvas id="sim-geo-canvas"></canvas>
          <div class="sim-geo-legend">${legend}</div>
        </div>
        <div class="sim-geo-right">
          <div class="sim-chart-box"><b>PRB usage (all 9 cells)</b><canvas id="sim-prb-donut"></canvas></div>
          <div class="sim-chart-box"><b>Session outcomes</b><canvas id="sim-outcome-bar"></canvas></div>
        </div>
      </div>
    </div>`;

  const bySite = {};
  for(const c of cells){
    const site = (c.cell_id || '?').split('-')[0];
    (bySite[site] = bySite[site] || []).push(c);
  }
  const siteKeys = Object.keys(bySite).sort();
  html += '<div class="sim-sites">' + siteKeys.map(site=>{
    const rows = bySite[site].sort((a,b)=>a.cell_id.localeCompare(b.cell_id)).map(c=>{
      const pct = Math.round((c.utilization || 0) * 100);
      const color = pct >= 90 ? '#f85149' : pct >= 70 ? '#d29922' : '#3fb950';
      return `<div class="sim-cell-row"><span class="cid">${esc(c.cell_id)}</span>`
        + `<span class="bar"><i style="width:${pct}%;background:${color}"></i></span>`
        + `<span class="pct">${pct}%</span></div>`;
    }).join('');
    return `<div class="sim-site"><h4>${esc(site)}</h4>${rows}</div>`;
  }).join('') + '</div>';

  box.innerHTML = html;
  drawSimGeoMap(geo);
  drawSimPrbDonut(cells);
  drawSimOutcomeBar(ue);
  const slider = document.getElementById('sim-ue-slider');
  const val = document.getElementById('sim-ue-val');
  slider.oninput = () => { val.textContent = slider.value; };
  slider.onchange = () => {
    fetch('/api/sim/ues', {method:'POST', headers:{'Content-Type':'application/json'},
                            body: JSON.stringify({num_ues: +slider.value})})
      .catch(e=>console.error('sim UE scale failed', e));
  };
}

async function pollSimulation(){
  if(activeTwin !== 'simulation') return;
  try{
    const r = await fetch('/api/sim/metrics');
    renderSimulation(await r.json());
  }catch(e){
    renderSimulation(null);
  }
}
setInterval(pollSimulation, 4000);

/* ===================  Lightweight 5G twin (preview, no backend yet)  ===================
   Dashboard built ahead of the implementation — both controls here are
   real/interactive (so the page doesn't feel broken) but don't call any
   API, since there's nothing behind them yet. State is just kept in JS and
   mirrored across both tabs that show it (Call Flow / Overview). */
let lw5gUeCount = 50;
let lw5gPattern = 'bursty';
const LW5G_PATTERNS = [
  {key:'bursty', label:'Bursty',
   desc:'UEs arrive in short, dense bursts separated by quiet periods — models a flash-crowd or paging-storm style surge.'},
  {key:'step-increase', label:'Step increase',
   desc:'UE count rises in discrete steps and holds at each plateau — models a gradual, deliberate ramp-up in load.'},
  {key:'random', label:'Random',
   desc:'UEs arrive at uniformly random times — models steady, uncorrelated background load.'},
];

function renderLw5gParamsBar(suffix){
  const bar = document.getElementById(`lw5g-params-bar-${suffix}`);
  const panel = document.getElementById(`lw5g-params-panel-${suffix}`);
  if(!bar || !panel) return;
  const panelOpen = panel.style.display !== 'none';
  bar.innerHTML = `<b>UE count: ${lw5gUeCount}</b>
    <input type="range" min="0" max="2000" value="${lw5gUeCount}" id="lw5g-ue-slider-${suffix}">
    <span class="lw5g-ue-val" id="lw5g-ue-val-${suffix}">${lw5gUeCount}</span>
    <button type="button" class="lw5g-params-toggle" id="lw5g-params-toggle-${suffix}">Parameters ${panelOpen ? '▲' : '▾'}</button>`;
  const slider = document.getElementById(`lw5g-ue-slider-${suffix}`);
  const val = document.getElementById(`lw5g-ue-val-${suffix}`);
  slider.oninput = () => { val.textContent = slider.value; };
  slider.onchange = () => {
    lw5gUeCount = +slider.value;
    ['callflow', 'overview'].forEach(renderLw5gParamsBar);   // keep both tabs in sync
  };
  document.getElementById(`lw5g-params-toggle-${suffix}`).onclick = () => {
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    renderLw5gParamsBar(suffix);
  };
}

function renderLw5gParamsPanel(suffix){
  const panel = document.getElementById(`lw5g-params-panel-${suffix}`);
  if(!panel) return;
  const current = LW5G_PATTERNS.find(p => p.key === lw5gPattern);
  panel.innerHTML = `<b>Activation pattern</b>
    <div class="lw5g-pattern-row">${LW5G_PATTERNS.map(p =>
      `<button type="button" class="lw5g-pattern-btn${p.key === lw5gPattern ? ' on' : ''}" data-pattern="${p.key}">${p.label}</button>`
    ).join('')}</div>
    <p class="lw5g-pattern-desc">${current ? current.desc : ''}</p>`;
  panel.querySelectorAll('.lw5g-pattern-btn').forEach(btn => {
    btn.onclick = () => {
      lw5gPattern = btn.dataset.pattern;
      ['callflow', 'overview'].forEach(renderLw5gParamsPanel);
    };
  });
}

['callflow', 'overview'].forEach(suffix => {
  renderLw5gParamsBar(suffix);
  renderLw5gParamsPanel(suffix);
});

/* overview cards */
function card(lbl, val, cls=''){
  return `<div class="card"><div class="lbl">${lbl}</div><div class="val ${cls}">${val}</div></div>`;
}
function updateOverview(){
  const s = META.status;
  const ok = v => v ? 'ok' : 'bad';
  const yn = v => v ? 'Yes' : 'No';
  let html = '';
  html += card('Radio path', s.radio_mode === 'hub' ? 'Via IQ hub' : 'Direct ZMQ');
  html += card('NG setup', yn(s.ng_setup), ok(s.ng_setup));
  html += card('Cell up', yn(s.cell_up), ok(s.cell_up));
  html += card('RRC attach', yn(s.rrc_complete), ok(s.rrc_complete));
  html += card('PDU session', s.pdu_ip || yn(s.pdu_session), ok(s.pdu_session));
  if(s.hub_forwarding){
    const h=s.hub_forwarding;
    html += card('Hub IQ', `dl=${h.dl_blocks} ul=${h.ul_blocks} · ${h.connected}/${h.slots} UE`, 'ok');
  }
  document.getElementById('cards').innerHTML = html;
  document.getElementById('topo-mode').textContent = s.radio_mode;

  const hub = s.radio_mode === 'hub';
  document.getElementById('topo-svg').innerHTML = hub ? `
<svg viewBox="0 0 720 120" xmlns="http://www.w3.org/2000/svg">
  <rect x="20" y="40" width="90" height="44" rx="6" fill="#1a2230" stroke="#58a6ff"/>
  <text x="65" y="67" text-anchor="middle" fill="#e6edf3" font-size="13">srsUE</text>
  <rect x="280" y="40" width="90" height="44" rx="6" fill="#1a2230" stroke="#22d3ee"/>
  <text x="325" y="62" text-anchor="middle" fill="#e6edf3" font-size="12">IQ hub</text>
  <text x="325" y="76" text-anchor="middle" fill="#8b949e" font-size="10">fan-out / sum</text>
  <rect x="540" y="40" width="100" height="44" rx="6" fill="#1a2230" stroke="#58a6ff"/>
  <text x="590" y="67" text-anchor="middle" fill="#e6edf3" font-size="13">gNB/DU</text>
  <rect x="660" y="40" width="44" height="44" rx="6" fill="#1a2230" stroke="#f0883e"/>
  <text x="682" y="67" text-anchor="middle" fill="#e6edf3" font-size="11">5GC</text>
  <line x1="110" y1="62" x2="280" y2="62" stroke="#8b949e" stroke-width="2" marker-end="url(#ar)"/>
  <line x1="370" y1="62" x2="540" y2="62" stroke="#8b949e" stroke-width="2" marker-end="url(#ar)"/>
  <line x1="640" y1="62" x2="660" y2="62" stroke="#8b949e" stroke-width="2" marker-end="url(#ar)"/>
  <defs><marker id="ar" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="#8b949e"/></marker></defs>
</svg>` : `
<svg viewBox="0 0 720 120" xmlns="http://www.w3.org/2000/svg">
  <rect x="80" y="40" width="90" height="44" rx="6" fill="#1a2230" stroke="#58a6ff"/>
  <text x="125" y="67" text-anchor="middle" fill="#e6edf3" font-size="13">srsUE</text>
  <rect x="320" y="40" width="100" height="44" rx="6" fill="#1a2230" stroke="#58a6ff"/>
  <text x="370" y="67" text-anchor="middle" fill="#e6edf3" font-size="13">gNB/DU</text>
  <rect x="520" y="40" width="44" height="44" rx="6" fill="#1a2230" stroke="#f0883e"/>
  <text x="542" y="67" text-anchor="middle" fill="#e6edf3" font-size="11">5GC</text>
  <line x1="170" y1="62" x2="320" y2="62" stroke="#22d3ee" stroke-width="2" marker-end="url(#ar2)"/>
  <text x="245" y="52" text-anchor="middle" fill="#22d3ee" font-size="10">ZMQ IQ</text>
  <line x1="420" y1="62" x2="520" y2="62" stroke="#8b949e" stroke-width="2" marker-end="url(#ar2)"/>
  <text x="470" y="52" text-anchor="middle" fill="#8b949e" font-size="10">N2/N3</text>
  <defs><marker id="ar2" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="#8b949e"/></marker></defs>
</svg>`;

  const max = Math.max(1, ...Object.values(META.by_layer||{}));
  document.getElementById('layer-bars').innerHTML = Object.entries(META.by_layer||{}).map(([L,n])=>{
    const w = Math.round(100*n/max);
    return `<div class="bar-item"><span>${L}</span><div class="bar"><i style="width:${w}%;background:${cssv(COLORS[L]||'--muted')}"></i></div><span>${n}</span></div>`;
  }).join('');
}

/* legend + call flow */
function buildLegend(){
  const el=document.getElementById('legend'); el.innerHTML='';
  for(const L of Object.keys(COLORS)){
    const c=document.createElement('span'); c.className='chip'; c.dataset.layer=L;
    c.innerHTML=`<span class="dot" style="background:${cssv(COLORS[L])}"></span>${L} (${EVENTS.filter(e=>e.layer===L).length})`;
    c.onclick=()=>{active[L]=!active[L]; c.classList.toggle('off',!active[L]); render(); renderMsgTable();};
    el.appendChild(c);
  }
}

function render(){
  const c=document.getElementById('canvas'); if(!c) return;
  c.innerHTML='';
  for(const k in LANES){const v=document.createElement('div');v.className='vline';v.style.left=LANEX(LANES[k])+'%';c.appendChild(v);}
  const vis=EVENTS.filter(e=>active[e.layer]);
  c.style.height=(vis.length*ROW+20)+'px';
  vis.forEach((e,k)=>{
    const col=cssv(COLORS[e.layer]);
    const xs=LANEX(LANES[e.src]), xd=LANEX(LANES[e.dst]);
    const a=Math.min(xs,xd), b=Math.max(xs,xd), right=xd>xs;
    const row=document.createElement('div'); row.className='row'+(e.id===selId?' sel':'');
    row.style.top=(k*ROW)+'px'; row.style.height=ROW+'px';
    row.onclick=()=>selectFlow(e.id);
    const tg=document.createElement('div'); tg.className='tg'; tg.style.top=(ROW/2)+'px';
    tg.textContent='+'+e.t.toFixed(3)+'s'; row.appendChild(tg);
    const seg=document.createElement('div'); seg.className='seg';
    seg.style.left=a+'%'; seg.style.width=(b-a)+'%'; seg.style.top=(ROW/2+5)+'px';
    seg.style.background=col; row.appendChild(seg);
    const h=document.createElement('div'); h.className='head'; h.style.top=(ROW/2+1)+'px';
    if(right){h.style.left='calc('+xd+'% - 8px)';h.style.borderLeft='8px solid '+col;}
    else{h.style.left=xd+'%';h.style.borderRight='8px solid '+col;}
    row.appendChild(h);
    if(e.via_ru){const d=document.createElement('div');d.className='rudot';d.style.background=col;
      d.style.left=LANEX(LANES.RU)+'%';d.style.top=(ROW/2+6)+'px';row.appendChild(d);}
    const lbl=document.createElement('div'); lbl.className='lbl';
    lbl.style.left=((a+b)/2)+'%'; lbl.style.top=(ROW/2-22)+'px'; lbl.textContent=e.label;
    row.appendChild(lbl);
    const desc=document.createElement('div'); desc.className='row-desc';
    desc.style.left=((a+b)/2)+'%'; desc.style.top=(ROW/2+6)+'px';
    desc.textContent=resolveInfo(e).summary||'';
    row.appendChild(desc);
    c.appendChild(row);
  });
}

function selectFlow(id){
  selId=id; msgSelId=id;
  const e=EVENTS.find(x=>x.id===id);
  showDetail('dt','dmeta','dinfo','dbody','djson',e,'djson-src');
  render(); renderMsgTable();
}

function extractJsonFromDetail(detail){
  if(!detail) return null;
  let blob = '';
  const contentIdx = detail.indexOf('Content:');
  if(contentIdx >= 0){
    blob = detail.slice(contentIdx);
    const bracket = blob.search(/[\[{]/);
    if(bracket >= 0) blob = blob.slice(bracket);
  } else {
    const brace = detail.indexOf('{');
    const bracket = detail.indexOf('[');
    let start = -1;
    if(brace >= 0 && (bracket < 0 || brace < bracket)) start = brace;
    else if(bracket >= 0) start = bracket;
    if(start < 0) return null;
    blob = detail.slice(start);
  }
  while(blob.length){
    try{
      const parsed = JSON.parse(blob);
      return JSON.stringify(parsed, null, 2);
    }catch(err){
      const lines = blob.split('\n');
      if(lines.length <= 1) break;
      blob = lines.slice(0, -1).join('\n');
    }
  }
  return null;
}

function embeddedTraceSample(key){
  const cat = SIGNALING.catalog || {};
  const byId = cat.trace_samples || {};
  const byName = cat.trace_samples_by_name || {};
  const k = String(key);
  if(byId[k]) return byId[k];
  if(byName[k]) return byName[k];
  return null;
}

function traceReplayForEvent(e){
  if(!e) return null;
  if(e.transmit_record) return e.transmit_record;
  if(e.decoded_trace) return e.decoded_trace;
  if(e.trace_lookup){
    const sample = embeddedTraceSample(e.trace_lookup);
    if(sample) return sample;
  }
  if(e.layer === 'RRC' && typeof RRC_TWIN !== 'undefined'){
    const hit = RRC_TWIN.find(m =>
      m.ts === e.ts || (m.short && e.short && m.short === e.short)
    );
    if(hit && hit.transmit_record) return hit.transmit_record;
    if(hit && hit.trace_record) return hit.trace_record;
    if(hit && hit.decoded && hit.trace_source === '22_decoded') return hit.decoded;
  }
  return null;
}

function decodedJsonForEvent(e){
  if(!e) return null;
  const trace = traceReplayForEvent(e);
  if(trace) return JSON.stringify(trace, null, 2);
  if(e.layer === 'RRC' && typeof RRC_TWIN !== 'undefined'){
    const hit = RRC_TWIN.find(m =>
      m.ts === e.ts || (m.short && e.short && m.short === e.short)
    );
    if(hit && hit.decoded) return JSON.stringify(hit.decoded, null, 2);
    if(hit && hit.json_raw) return hit.json_raw;
  }
  return extractJsonFromDetail(e.detail);
}

function infoHtml(info){
  if(!info || !info.purpose) return '<p class="info-lead">No reference entry for this event.</p>';
  const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  return `<p class="info-lead">${esc(info.summary)}</p>`+
    `<dl>`+
    `<dt>Purpose</dt><dd>${esc(info.purpose)}</dd>`+
    `<dt>Protocol &amp; channel</dt><dd>${esc(info.protocol)}</dd>`+
    `<dt>Format &amp; structure</dt><dd>${esc(info.structure)}</dd>`+
    `<dt>In this flow</dt><dd>${esc(info.flow)}</dd>`+
    `</dl>`;
}

function showDetail(tId,mId,iId,bId,jId,e,sId){
  if(!e) return;
  document.getElementById(tId).textContent=e.label;
  const col=cssv(COLORS[e.layer]);
  document.getElementById(mId).innerHTML=
    `<span class="badge" style="background:${col}">${e.layer}</span>`+
    `<span class="copy" onclick="copyText(EVENTS.find(x=>x.id===${e.id}).detail)">copy raw</span>`+
    `<div class="route"><b>${e.src}</b> → <b>${e.dst}</b> · ${e.raw_layer} · +${e.t.toFixed(3)}s</div>`;
  const jsonEl = document.getElementById(jId);
  const srcEl = sId ? document.getElementById(sId) : null;
  const trace = traceReplayForEvent(e);
  const decoded = decodedJsonForEvent(e);
  if(trace){
    if(srcEl) srcEl.textContent = ' · 22_decoded replay';
    jsonEl.textContent = JSON.stringify(trace, null, 2);
    jsonEl.className = 'json-block scroll-hide';
  } else if(decoded){
    if(srcEl) srcEl.textContent = '';
    jsonEl.textContent = decoded;
    jsonEl.className = 'json-block scroll-hide';
  } else {
    if(srcEl) srcEl.textContent = '';
    jsonEl.textContent = '(no decoded JSON in log — see raw message)';
    jsonEl.className = 'json-block empty scroll-hide';
  }
  const body=document.getElementById(bId);
  body.className = 'json-block scroll-hide';
  body.textContent = e.detail || '(empty)';
  document.getElementById(iId).innerHTML = infoHtml(resolveInfo(e));
  const scroll = jsonEl.closest('.detail-body');
  if(scroll) scroll.scrollTop = 0;
  document.querySelectorAll('.detail-pane .json-block').forEach(el => { el.scrollTop = 0; });
}

function copyText(t){ if(t&&navigator.clipboard) navigator.clipboard.writeText(t); }

/* messages table */
const layerSel=document.getElementById('msg-layer');
Object.keys(COLORS).forEach(L=>{const o=document.createElement('option');o.value=L;o.textContent=L;layerSel.appendChild(o);});

function renderMsgTable(){
  const q=(document.getElementById('msg-q').value||'').toLowerCase();
  const lf=layerSel.value;
  const tb=document.getElementById('msg-tbody'); tb.innerHTML='';
  EVENTS.filter(e=>{
    if(!active[e.layer]) return false;
    if(lf && e.layer!==lf) return false;
    if(!q) return true;
    const hay=(e.label+e.src+e.dst+e.detail+e.layer+(e.info?e.info.purpose+e.info.summary:'')).toLowerCase();
    return hay.includes(q);
  }).forEach(e=>{
    const tr=document.createElement('tr');
    if(e.id===msgSelId) tr.className='sel';
    tr.onclick=()=>{msgSelId=e.id; selId=e.id; showDetail('mt','mmeta','minfo','mbody','mjson',e,'mjson-src'); renderMsgTable(); render();};
    tr.innerHTML=`<td>${e.id+1}</td><td>+${e.t.toFixed(3)}s</td><td>${e.layer}</td><td>${e.src}→${e.dst}</td><td>${e.label}</td>`;
    tb.appendChild(tr);
  });
}
document.getElementById('msg-q').oninput=renderMsgTable;
layerSel.onchange=renderMsgTable;

// Pulls fresh 4G data into DATA4G_MULTI/CONTAINER_STATUS_4G/KPI_HISTORY,
// always re-renders the pair-bar + histogram (cheap, pair-independent), and
// re-renders the actual ladder/detail/KPI flow only if not pinned and
// either `force` is set or the 20s interval has elapsed. Pin always wins —
// it means "don't change what I'm looking at" even across an explicit
// manual Refresh, not just the background poll.
function apply4gUpdate(payload, force){
  if(!payload || !(payload.data_4g_multi || payload.data_4g)) return;
  if(payload.data_4g_multi){
    DATA4G_MULTI = payload.data_4g_multi;
    DATA4G = DATA4G_MULTI[ltePairSel] || DATA4G_MULTI['1'] || DATA4G;
  } else {
    DATA4G = payload.data_4g;
  }
  if(payload.container_status_4g) CONTAINER_STATUS_4G = payload.container_status_4g;
  if(payload.kpi_history) KPI_HISTORY = payload.kpi_history;
  initLte4gLive();
  const due = Date.now() - lteLastRenderTs >= LTE_RENDER_INTERVAL_MS;
  if(!ltePinned && (force || due)){
    initLte4gFlow();
    lteLastRenderTs = Date.now();
  }
}

function applyData(events, meta, rrc, force4g){
  EVENTS=enrichEvents(events); META=meta;
  updateOverview(); buildLegend(); render(); renderMsgTable();
  if(rrc && rrc.rrc_twin) applyRrc(rrc.rrc_twin, rrc.rrc_trace, rrc.rrc_meta);
  if(rrc && rrc.signaling) applySignaling(rrc.signaling);
  // The 4G LTE tab used to only get fresh data on a full page reload, since
  // DATA4G was baked into the HTML at generation time and neither the 5s
  // live poll nor the Refresh button ever touched it after load.
  apply4gUpdate(rrc, !!force4g);
}

async function fetchData(url){
  const r=await fetch(url); if(!r.ok) throw new Error(r.statusText);
  return r.json();
}

async function pollLive(){
  if(!liveMode) return;
  try{
    const d=await fetchData('/api/data');
    if(d.message_count!==META.message_count || d.captured!==META.captured){
      applyData(d.events, d.meta, d);
    } else {
      // 5G state unchanged (5G stack likely isn't even running), but the
      // server now pulls fresh 4G logs on every /api/data call — refresh the
      // 4G tab independently so stopping/starting a UE shows up live instead
      // of being gated behind 5G activity that never happens. Not forced —
      // the ladder/detail/KPI flow still only updates every 20s (or stays
      // frozen if pinned); the pair-bar and histogram always update.
      apply4gUpdate(d, false);
    }
    document.getElementById('live-label').textContent='Live · updated '+new Date().toLocaleTimeString();
  }catch(e){}
}

async function pullRefresh(){
  const btn=document.getElementById('btn-refresh');
  btn.disabled=true; btn.textContent='Pulling…';
  try{
    const d=await fetchData('/api/refresh');
    applyData(d.events, d.meta, d, true);  // explicit user action — bypass the 20s interval (pin still wins)
    document.getElementById('live-label').textContent='Live · pulled '+new Date().toLocaleTimeString();
  }catch(e){ alert('Refresh failed: '+e.message); }
  btn.disabled=false; btn.textContent='Refresh';
}

async function initLive(){
  try{
    const d=await fetchData('/api/data');
    liveMode=true;
    document.getElementById('livebar').style.display='flex';
    applyData(d.events, d.meta, d);
    setInterval(pollLive, 5000);
    document.getElementById('btn-refresh').onclick=pullRefresh;
  }catch(e){
    updateOverview(); buildLegend(); render(); renderMsgTable();
  }
}

initLive();
window.addEventListener('resize',render);

/* --- RRC page --- */
let RRC_TWIN = __RRC_TWIN__;
let RRC_TRACE = __RRC_TRACE__;
let RRC_META = __RRC_META__;
let rrcSrc = 'twin', rrcSel = null;

function rrcList(){ return rrcSrc === 'trace' ? RRC_TRACE : RRC_TWIN; }

function initRrc(){
  const chSel = document.getElementById('rrc-ch');
  const channels = [...new Set(RRC_TWIN.map(m=>m.channel))].sort();
  channels.forEach(c=>{ const o=document.createElement('option'); o.value=c; o.textContent=c; chSel.appendChild(o); });
  document.getElementById('rrc-names').textContent =
    `srsTwin: ${RRC_META.twin_count} RRC (${RRC_META.has_json} JSON) · trace: ${RRC_META.trace_count}` +
    (RRC_META.trace_path ? ' · '+RRC_META.trace_path.split(/[/\\]/).pop() : '');
  document.getElementById('rrc-src-twin').onclick=()=>{ rrcSrc='twin'; document.getElementById('rrc-src-twin').classList.add('on');
    document.getElementById('rrc-src-trace').classList.remove('on'); renderRrc(); };
  document.getElementById('rrc-src-trace').onclick=()=>{ rrcSrc='trace'; document.getElementById('rrc-src-trace').classList.add('on');
    document.getElementById('rrc-src-twin').classList.remove('on'); renderRrc(); };
  document.getElementById('rrc-q').oninput=renderRrc;
  chSel.onchange=renderRrc;
  renderRrc();
}

function renderRrc(){
  const q=(document.getElementById('rrc-q').value||'').toLowerCase();
  const cf=document.getElementById('rrc-ch').value;
  const tb=document.getElementById('rrc-tbody'); tb.innerHTML='';
  rrcList().filter(m=>{
    if(cf && m.channel!==cf) return false;
    if(q && !(m.message_name+m.raw_name+m.channel).toLowerCase().includes(q)) return false;
    return true;
  }).forEach(m=>{
    const tr=document.createElement('tr');
    if(m.id===rrcSel) tr.className='sel';
    tr.onclick=()=>{ rrcSel=m.id; showRrc(m); renderRrc(); };
    const t = m.t !== undefined ? '+'+m.t.toFixed(3)+'s · ' : '';
    tr.innerHTML=`<td>${m.id+1}</td><td>${m.message_name}</td><td>${m.channel}</td><td>${m.direction}</td><td>${m.size_b}</td><td>${t}${m.source}</td>`;
    tb.appendChild(tr);
  });
}

function showRrc(m){
  document.getElementById('rt').textContent=m.message_name;
  document.getElementById('rmeta').innerHTML=
    `<span class="badge" style="background:${cssv('--RRC')}">RRC</span>`+
    `<div class="route">${m.pdu_type||'?'} · ${m.channel} · ${m.direction} · ${m.size_b} B · ${m.source}</div>`;
  const iq=m.iq||{};
  document.getElementById('riq').innerHTML=
    `<p class="info-lead">${iq.summary||''}</p>`+
    `<ul class="rrc-pipeline">${(iq.steps||[]).map(s=>'<li><b>'+s.layer+'</b> '+s.action+' — '+s.detail+'</li>').join('')}</ul>`+
    (iq.note?`<p style="font-size:12px;color:var(--muted)">${iq.note}</p>`:'')+
    (iq.zmq?`<p style="font-size:11px;color:var(--muted)">ZMQ: ${iq.zmq.samples_per_slot} samples/slot, ${iq.zmq.bytes_per_slot} B, REQ=${iq.zmq.request_byte}</p>`:'');
  const dec = m.trace_record ? JSON.stringify(m.trace_record, null, 2)
    : (m.decoded ? JSON.stringify(m.decoded, null, 2) : (m.json_raw || '(no JSON in log — hex only)'));
  document.getElementById('rjson').textContent=dec;
  document.getElementById('rhex').textContent=m.hex || '(no hex dump)';
}

function applyRrc(twin, trace, meta){
  RRC_TWIN=twin; RRC_TRACE=trace; RRC_META=meta; initRrc();
}

initRrc();

/* --- RRC · S1 · X2 signaling JSON --- */
let SIGNALING = __SIGNALING__;
let sigProto = 'RRC', sigLiveSel = null, sigRefSel = null;
let sigSourceEntries = [];
const traceSampleCache = new Map();

function applySignaling(data){
  SIGNALING = data || SIGNALING;
  renderSigMeta();
  renderSigLive();
  renderSigRef();
}

function renderSigMeta(){
  const m = SIGNALING.meta || {};
  const st = (SIGNALING.catalog && SIGNALING.catalog.status) || {};
  const parts = [
    `live: RRC ${m.live_rrc||0} · S1 ${m.live_s1||0} · X2 ${m.live_x2||0}`,
    `catalog: ${st.found||0}/${st.total||0} record_ids indexed`,
  ];
  if(st.trace_dir) parts.push(st.trace_dir.split(/[/\\]/).slice(-2).join('/'));
  if(st.building) parts.push('indexing traces…');
  if(st.error) parts.push('error: '+st.error);
  document.getElementById('sig-meta').textContent = parts.join(' · ');
}

document.querySelectorAll('#sig-proto button').forEach(btn=>{
  btn.onclick=()=>{
    document.querySelectorAll('#sig-proto button').forEach(b=>b.classList.remove('on'));
    btn.classList.add('on');
    sigProto = btn.dataset.p;
    sigLiveSel = null; sigRefSel = null;
    renderSigLive(); renderSigRef(); renderSigSources();
    document.getElementById('sig-live-json').textContent = 'Select a live message.';
    document.getElementById('sig-ref-json').textContent = 'Select a catalog entry or a live message to load the matching trace sample.';
  };
});

function sigLiveRows(){
  const by = (SIGNALING.live_by_protocol || {});
  return by[sigProto] || [];
}

function sigRefRows(){
  const by = (SIGNALING.catalog && SIGNALING.catalog.by_protocol) || {};
  return by[sigProto] || [];
}

function renderSigLive(){
  const tb = document.getElementById('sig-live-tbody');
  tb.innerHTML = '';
  sigLiveRows().forEach(m=>{
    const tr = document.createElement('tr');
    if(m.id === sigLiveSel) tr.className = 'sel';
    tr.onclick = ()=>{ sigLiveSel = m.id; showSigLive(m); renderSigLive(); };
    const t = m.t !== undefined ? '+'+Number(m.t).toFixed(3)+'s' : '';
    tr.innerHTML = `<td>${m.id+1}</td><td>${t}</td><td>${m.message_name}</td><td>${m.source||''}</td>`;
    tb.appendChild(tr);
  });
  if(!sigLiveRows().length){
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="4" style="color:var(--muted);cursor:default">No ${sigProto} messages in the current call flow.</td>`;
    tb.appendChild(tr);
  }
}

function renderSigRef(){
  const tb = document.getElementById('sig-ref-tbody');
  tb.innerHTML = '';
  const modeBy = Object.fromEntries(sigSourceEntries.map(e=>[e.message_name, e]));
  sigRefRows().forEach(e=>{
    const tr = document.createElement('tr');
    const key = e.record_id;
    if(key === sigRefSel) tr.className = 'sel';
    tr.onclick = ()=>{ sigRefSel = key; loadSigRef(key, e.message_name); renderSigRef(); };
    const found = e.found ? '<span class="sig-found">✓</span>' : '<span class="sig-miss">—</span>';
    const src = modeBy[e.message_name];
    const mode = src ? src.mode : '';
    tr.innerHTML = `<td>${e.record_id}</td><td>${e.message_name}${mode?` <span style="color:var(--muted)">(${mode})</span>`:''}</td><td>${found}</td>`;
    tb.appendChild(tr);
  });
}

async function loadSigSources(){
  const status = document.getElementById('sig-src-status');
  const tb = document.getElementById('sig-src-tbody');
  try{
    const r = await fetch('/api/message-sources');
    const data = await r.json();
    if(data.error){ status.textContent = data.error; return; }
    sigSourceEntries = data.entries || [];
    status.textContent = `${data.path||''}`.split(/[/\\]/).pop() || 'ok';
    renderSigSources();
  }catch(e){ status.textContent = 'offline'; }
}

function renderSigSources(){
  const tb = document.getElementById('sig-src-tbody');
  if(!tb) return;
  tb.innerHTML = '';
  sigSourceEntries.filter(e=>e.protocol===sigProto).forEach(e=>{
    const tr = document.createElement('tr');
    const sel = document.createElement('select');
    ['auto','trace','ml'].forEach(m=>{
      const o=document.createElement('option'); o.value=m; o.textContent=m;
      if(e.mode===m) o.selected=true; sel.appendChild(o);
    });
    sel.onchange=()=>{ e.mode=sel.value; };
    tr.innerHTML = `<td>${e.message_name}</td><td>${e.protocol}</td>
      <td class="${e.trace_sample?'sig-found':'sig-miss'}">${e.trace_sample?'✓':'—'}</td>
      <td class="${e.ml_available?'sig-found':'sig-miss'}">${e.ml_available?'✓':'—'}</td><td></td>`;
    tr.lastElementChild.appendChild(sel);
    tb.appendChild(tr);
  });
}

document.getElementById('sig-src-save').onclick = async ()=>{
  const sources = {};
  sigSourceEntries.forEach(e=>{ sources[e.message_name]=e.mode; });
  try{
    const r = await fetch('/api/message-sources',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({sources})});
    const data = await r.json();
    sigSourceEntries = data.entries || sigSourceEntries;
    renderSigSources(); renderSigRef();
    document.getElementById('sig-src-status').textContent = 'saved';
  }catch(e){ alert('Save failed: '+e.message); }
};

function showSigLive(m){
  document.getElementById('sig-live-json').textContent = JSON.stringify(m.message || {}, null, 2);
  const lookup = m.trace_lookup || m.message_name;
  const entries = sigRefRows();
  const hit = entries.find(e => e.message_name === lookup || e.message_name === m.message_name);
  if(hit){
    sigRefSel = hit.record_id;
    renderSigRef();
    loadSigRef(hit.record_id, hit.message_name);
  } else {
    fetchTraceSample(lookup).then(sample=>{
      if(sample) document.getElementById('sig-ref-json').textContent = JSON.stringify(sample, null, 2);
    });
  }
}

async function fetchTraceSample(key){
  if(!key) return null;
  const k = String(key);
  if(traceSampleCache.has(k)) return traceSampleCache.get(k);
  let sample = embeddedTraceSample(k);
  if(!sample){
    try{
      const r = await fetch('/api/trace-sample?record_id='+encodeURIComponent(k));
      if(r.ok){
        const data = await r.json();
        sample = data.message || null;
      } else {
        const r2 = await fetch('/api/trace-sample?name='+encodeURIComponent(k));
        if(r2.ok){ const data = await r2.json(); sample = data.message || null; }
      }
    }catch(e){ /* opened as file:// or server down */ }
  }
  traceSampleCache.set(k, sample);
  return sample;
}

async function loadSigRef(recordId, messageName){
  document.getElementById('sig-ref-json').textContent = 'Loading trace sample…';
  let sample = await fetchTraceSample(recordId);
  if(!sample && messageName) sample = await fetchTraceSample(messageName);
  if(sample){
    document.getElementById('sig-ref-json').textContent = JSON.stringify(sample, null, 2);
  } else {
    document.getElementById('sig-ref-json').textContent =
      '(no 22_decoded sample for this type — build the index from poc_StressTest, then regenerate the dashboard)';
  }
}

renderSigMeta();
renderSigLive();
renderSigRef();
loadSigSources();

/* =====================  4G LTE panels  ===================== */
let DATA4G_MULTI = __4G_DATA_MULTI__;
// trace_recs/per_templates/per_record_status are identical across every
// pair (same 22_decoded trace_dir) — kept here ONCE instead of embedded in
// each pair's own object, which used to triple ~7.7MB of JSON. aligned[]
// entries reference a record by `trace_idx` into DATA4G_SHARED.trace_recs
// rather than embedding a full copy — see resolveTrace() below.
let DATA4G_SHARED = __4G_DATA_SHARED__;
function resolveTrace(traceIdx){
  if(traceIdx === null || traceIdx === undefined) return null;
  return (DATA4G_SHARED.trace_recs || [])[traceIdx] || null;
}
let ltePairSel = Object.keys(DATA4G_MULTI)[0] || '1';
let DATA4G = DATA4G_MULTI[ltePairSel];
// Real docker-ps-derived running/stopped state, not log-derived — instant,
// unlike the KPI/outcome dot which lags behind srsRAN's own buffered file logger.
let CONTAINER_STATUS_4G = __4G_CONTAINER_STATUS__;
// Accumulated across every completed call flow demo3ue/live_cycler.py has
// recorded (independent of which single flow the ladder is showing).
let KPI_HISTORY = __4G_KPI_HISTORY__;
let lteSelIdx = null;
let ltetraceSelIdx = null;
// Pin freezes the ladder/detail/KPI panel on whatever flow is currently
// shown; the histogram (built from KPI_HISTORY, not the pinned flow) keeps
// updating regardless. The ladder otherwise only re-renders every 20s, not
// every 5s poll, so it isn't visually jarring mid-inspection.
let ltePinned = false;
let lteLastRenderTs = 0;
const LTE_RENDER_INTERVAL_MS = 20000;
let lteHistMetric = 'du_delay_ms';
// 'all' or a specific pair key (string, matches DATA4G_MULTI's keys) — lets
// you isolate one UE's accumulated history even after the others are
// stopped, since KPI_HISTORY is a permanent log, not tied to current
// container state.
let lteHistPairFilter = 'all';

const LTE_LAYER_COLORS = {
  PHY:'#a371f7', MAC:'#22d3ee', RRC:'#f9826c', NAS:'#3fb950',
  S1AP:'#ffd700', SCHED:'#8b949e'
};

// LTE lane indices — must match the header order in the HTML
const LTE_LANES = ['UE','ZMQ','eNB','EPC'];
const LTE_LANE_IDX = {'UE':0,'ZMQ':1,'eNB':2,'EPC':3};

// Map src/dst names from parse_4g to lane keys
function toLaneKey(s){
  if(!s) return 'UE';
  const u = s.toUpperCase();
  if(u==='UE'||u==='SRSUE') return 'UE';
  if(u==='ENB'||u==='SRSENB'||u==='GNB') return 'eNB';
  if(u==='EPC'||u==='SRSEPC'||u==='MME') return 'EPC';
  if(u==='ZMQ') return 'ZMQ';
  return 'UE';
}

function lteLayerColor(layer){
  return LTE_LAYER_COLORS[layer] || '#8b949e';
}

function templateStatusForTrace(rec){
  if(!rec) return 'none';
  return rec._template_status || 'none';
}

function statusColor(status){
  return {
    exact:'#3fb950',
    reconstructed:'#d29922',
    minimal:'#8b949e',
    encode_failed:'#f85149',
    none:'#8b949e'
  }[status || 'none'] || '#8b949e';
}

function statusBadge(status){
  const st = status || 'none';
  return '<span class="status-badge status-'+st+'">'+st+'</span>';
}

function renderLteInjectBar(){
  const bar = document.getElementById('lte-inject-bar');
  const meta = DATA4G.inject_meta || {};
  if(!meta.m_tmsi && !meta.cause){ bar.style.display='none'; return; }
  bar.style.display='block';
  bar.innerHTML = 'Trace injection active &mdash; '
    + (meta.m_tmsi ? '<b>m_tmsi</b>='+meta.m_tmsi+'&nbsp;&nbsp;':'')
    + (meta.cause  ? '<b>cause</b>=' +meta.cause +'&nbsp;&nbsp;':'')
    + (meta.source ? '<span style="color:var(--muted)">['+meta.source+']</span>':'');
}

// Build the SVG arrow ladder for the 4G signal flow
function buildLteLadderSvg(evs){
  const alignedByEv = new Map((DATA4G.aligned || [])
    .filter(a => a.trace_idx != null && a.ev_idx !== undefined)
    .map(a => [a.ev_idx, resolveTrace(a.trace_idx)]));
  const LANE_N   = 4;
  const ROW_H    = 42;   // px per event row
  const PAD_TOP  = 8;
  const LANE_W   = 120;  // px per lane column
  const LABEL_FONT = 11;
  const TIME_FONT  = 10;
  const TOTAL_W  = LANE_N * LANE_W;
  const TOTAL_H  = PAD_TOP + evs.length * ROW_H + 8;

  // lane centre X positions
  const lx = i => LANE_W * i + LANE_W / 2;

  let svgLines = [];

  // Vertical swim-lane lines
  for(let i=0;i<LANE_N;i++){
    const x = lx(i);
    const col = i===0?'#f9826c': i===2?'#58a6ff': i===3?'#ffd700':'#4a5568';
    svgLines.push(
      `<line x1="${x}" y1="0" x2="${x}" y2="${TOTAL_H}"
             stroke="${col}" stroke-width="1" stroke-dasharray="4 4" opacity="0.35"/>`
    );
  }

  evs.forEach((ev,i)=>{
    const traceRec = alignedByEv.get(i);
    const y = PAD_TOP + i * ROW_H + ROW_H / 2;
    const col = LTE_LAYER_COLORS[ev.layer] || '#8b949e';
    const st = templateStatusForTrace(traceRec);
    const lineCol = traceRec ? statusColor(st) : col;

    const srcKey = toLaneKey(ev.src);
    const dstKey = toLaneKey(ev.dst);
    const x1 = lx(LTE_LANE_IDX[srcKey] ?? 0);
    const x2 = lx(LTE_LANE_IDX[dstKey] ?? 0);

    const sameNode = x1 === x2;
    const dir = x2 > x1 ? 1 : -1;

    // Arrowhead marker id
    const mkId = 'ah'+i;
    svgLines.push(
      `<defs><marker id="${mkId}" markerWidth="7" markerHeight="7"
         refX="6" refY="3.5" orient="auto">
         <polygon points="0 0, 7 3.5, 0 7" fill="${lineCol}"/>
       </marker></defs>`
    );

    if(sameNode){
      // Self-loop (e.g. internal events)
      const rx = x1 + 24, ry = y - 12;
      svgLines.push(
        `<path d="M${x1},${y-10} C${rx},${ry} ${rx},${y+10} ${x1},${y+10}"
              stroke="${lineCol}" fill="none" stroke-width="${traceRec ? '2.4' : '1.5'}"
              marker-end="url(#${mkId})"/>`
      );
    } else {
      // via_zmq — pass through lane 1 (ZMQ) visually with a small dot
      const hasZmq = ev.via_zmq;
      const ex2 = hasZmq ? x2 : x2 - dir * 8;
      svgLines.push(
        `<line x1="${x1}" y1="${y}" x2="${ex2}" y2="${y}"
               stroke="${lineCol}" stroke-width="${traceRec ? '2.4' : '1.5'}"
               marker-end="url(#${mkId})"/>`
      );
      if(hasZmq){
        const zx = lx(1); // ZMQ lane
        svgLines.push(
          `<circle cx="${zx}" cy="${y}" r="3" fill="${col}" opacity="0.7"/>`
        );
      }
    }

    // Dot at source
    svgLines.push(
      `<circle cx="${x1}" cy="${y}" r="${traceRec ? '5' : '4'}" fill="${lineCol}"/>`
    );

    // Label text — centred between src and dst
    const tx = sameNode ? x1 + 30 : (x1 + x2) / 2;
    const shortLabel = ev.label.length > 32 ? ev.label.slice(0,30)+'\u2026' : ev.label;
    svgLines.push(
      `<text x="${tx}" y="${y-6}" text-anchor="middle" font-size="${LABEL_FONT}"
             fill="${lineCol}" font-family="ui-monospace,Consolas,monospace">${
               shortLabel.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
             }</text>`
    );
    if(traceRec){
      svgLines.push(
        `<text x="${tx}" y="${y+9}" text-anchor="middle" font-size="9"
               fill="${lineCol}" font-family="ui-monospace,Consolas,monospace">22_decoded · ${st}</text>`
      );
    }

    // Layer badge + time on far right; show 3GPP phase when present
    const timeStr = ev.short ? ev.short.split('T').pop().slice(0,12) : '';
    const phaseStr = ev.flow_phase ? ev.flow_phase.split('—').pop().trim() : '';
    svgLines.push(
      `<text x="${TOTAL_W - 2}" y="${y+4}" text-anchor="end" font-size="${TIME_FONT}"
             fill="#8b949e" font-family="ui-monospace,Consolas,monospace"
             >${ev.layer} ${timeStr}</text>`
    );
    // Delay since the previous displayed message (or a "carried in" note when
    // the log timestamp predates the actual over-the-air send, e.g. NAS
    // Attach Request — see lteFmtMs/ts_note).
    if(ev.delay_ms != null || ev.ts_note){
      const delayLabel = ev.ts_note ? '⚠ queued early' : '+' + lteFmtMs(ev.delay_ms);
      const delayFill = ev.ts_note ? '#d29922' : '#6e7681';
      svgLines.push(
        `<text x="${TOTAL_W - 2}" y="${y+15}" text-anchor="end" font-size="9"
               fill="${delayFill}" font-family="ui-monospace,Consolas,monospace"
               >${delayLabel}${ev.ts_note ? `<title>${ev.ts_note.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</title>` : ''}</text>`
      );
    }
    if(phaseStr){
      svgLines.push(
        `<text x="4" y="${y+4}" text-anchor="start" font-size="9"
               fill="#6e7681" font-family="ui-monospace,Consolas,monospace"
               >${phaseStr.replace(/&/g,'&amp;')}</text>`
      );
    }
  });

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${TOTAL_W}" height="${TOTAL_H}" viewBox="0 0 ${TOTAL_W} ${TOTAL_H}"
              style="display:block"
              id="lte-ladder-svg">
    ${svgLines.join('\n    ')}
  </svg>`;
}

function lteFmtMs(ms){
  if(ms == null) return '—';
  return ms < 1000 ? Math.round(ms) + 'ms' : (ms/1000).toFixed(2) + 's';
}

// Pull just the hex-dump lines out of a live log entry's detail text
// (the human-readable header line is dropped; only "0000: aa bb cc" rows remain).
function lteHexDump(detail){
  if(!detail) return null;
  const lines = detail.split('\n').slice(1)
    .filter(l => /^\s*[0-9a-fA-F]{4,}:\s+[0-9a-fA-F]{2}/.test(l));
  return lines.length ? lines.join('\n').trim() : null;
}

function lteInfoHtml(info, concise){
  const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  if(!info || !info.purpose){
    return '<p class="info-lead">No reference entry for this event.</p>';
  }
  let html = `<p class="info-lead">${esc(info.summary)}</p>`;
  if(concise){
    html += `<p>${esc(info.purpose)}</p>`;
  } else {
    // No PER/22_decoded reference to show alongside — spend the freed-up
    // space on the full purpose/protocol/structure/flow breakdown instead.
    html += `<dl>`
      + `<dt>Purpose</dt><dd>${esc(info.purpose)}</dd>`
      + `<dt>Protocol &amp; channel</dt><dd>${esc(info.protocol)}</dd>`
      + `<dt>Format &amp; structure</dt><dd>${esc(info.structure)}</dd>`
      + `<dt>In this flow</dt><dd>${esc(info.flow)}</dd>`
      + `</dl>`;
  }
  return html;
}

function renderLteDetailBody(ev, aligned){
  const body = document.getElementById('lte-detail-body');
  const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const trace = aligned && resolveTrace(aligned.trace_idx);
  const decodedMsg = trace && trace.decoded
    ? (trace.decoded.message !== undefined ? trace.decoded.message : trace.decoded)
    : null;
  const hex = lteHexDump(ev.detail);
  const hasEncoding = !!(decodedMsg || hex);

  let html = '';
  if(ev.ts_note){
    html += `<p style="color:#d29922;font-size:11px;margin:0 0 10px">`
      + `&#9888; ${esc(ev.ts_note)} — the timestamp above is when this layer `
      + `built the message locally, not when it went over the air.</p>`;
  } else if(ev.delay_ms != null){
    html += `<p style="color:var(--muted);font-size:11px;margin:0 0 10px">`
      + `+${lteFmtMs(ev.delay_ms)} since the previous message in this attempt</p>`;
  }
  html += lteInfoHtml(ev.info, hasEncoding);

  if(hasEncoding){
    html += `<div class="lte-enc">`
      + `<div><h4>Decoded message</h4>`
      + (decodedMsg ? `<pre>${esc(JSON.stringify(decodedMsg, null, 2))}</pre>`
                    : `<pre class="empty">no 22_decoded reference for this message</pre>`)
      + `</div>`
      + `<div><h4>PER-encoded bytes</h4>`
      + (hex ? `<pre>${esc(hex)}</pre>`
             : `<pre class="empty">no encoded bytes captured for this event</pre>`)
      + `</div>`
      + `</div>`;
  }

  if(trace){
    const recId = trace.record_id !== undefined ? trace.record_id : '?';
    html += `<details class="lte-raw-box">`
      + `<summary>Expand full 22_decoded record (#${esc(String(recId))})</summary>`
      + `<pre>${esc(JSON.stringify(trace, null, 2))}</pre>`
      + `</details>`;
  }

  body.innerHTML = html;
}

function renderLteEvList(){
  const list = document.getElementById('lte-ev-list');
  const evs = DATA4G.events || [];
  if(!evs.length){
    list.innerHTML = '<div style="padding:14px;color:var(--muted);font-size:12px">'
      + (DATA4G.has_live
          ? 'No 4G events parsed yet.'
          : 'Start the 4G stack:<br><code style="font-size:11px">docker compose -f docker-compose.4g.yml up</code>')
      + '</div>';
    return;
  }

  // SVG ladder
  const svgHtml = buildLteLadderSvg(evs);

  // Clickable row overlay (transparent absolute divs on top of SVG rows)
  const ROW_H = 42, PAD_TOP = 8;
  const rowDivs = evs.map((ev,i)=>{
    const top = PAD_TOP + i * ROW_H;
    const traceRec = (DATA4G.aligned||[]).find(a=>a.ev_idx===i && a.trace_idx != null);
    const cls = 'lte-sv-row' + (traceRec ? ' trace-backed' : '') + (i===lteSelIdx?' sel':'');
    const title = traceRec ? ev.label + ' (22_decoded trace available)' : ev.label;
    return `<div class="${cls}" data-i="${i}"
                style="position:absolute;left:0;right:0;top:${top}px;height:${ROW_H}px;
                       cursor:pointer;border-radius:4px;"
                title="${title.replace(/"/g,'&quot;')}"></div>`;
  }).join('');

  list.innerHTML = `<div style="position:relative;overflow-x:auto">
    ${svgHtml}
    <div style="position:absolute;inset:0;pointer-events:none">
      <div style="position:relative;pointer-events:auto">${rowDivs}</div>
    </div>
  </div>`;

  list.querySelectorAll('.lte-sv-row').forEach(el=>{
    el.addEventListener('click',()=>{
      lteSelIdx = +el.dataset.i;
      // highlight
      list.querySelectorAll('.lte-sv-row').forEach(r=>{
        r.style.background = r===el ? 'rgba(249,130,108,.15)' : '';
      });
      const ev = evs[lteSelIdx];
      const aligned = (DATA4G.aligned||[]).find(a=>a.ev_idx===lteSelIdx);
      document.getElementById('lte-detail-title').textContent =
        '[' + ev.layer + '] ' + ev.label + (aligned && aligned.trace_idx != null ? '  [22_decoded]' : '');
      renderLteDetailBody(ev, aligned);
    });
  });
}

function renderLtetraceTable(){
  const tbody = document.getElementById('ltetrace-tbody');
  const recs  = DATA4G_SHARED.trace_recs || [];
  const tmpl  = DATA4G_SHARED.per_templates || {};
  document.getElementById('ltetrace-count').textContent =
    recs.length + ' trace records · '
    + Object.keys(tmpl).length + ' PER templates';
  if(!recs.length){
    tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted);padding:14px">'
      + 'No 22_decoded records loaded — check RRC_TRACE_DIR / 22_decoded path.</td></tr>';
    return;
  }
  tbody.innerHTML = recs.map((r,i)=>{
    const dmeta = r.decoding_metadata || {};
    const choice = dmeta.decoded_message_choice || r.message_name || r.decoded_choice || '—';
    const hasPer = tmpl[choice] ? true : false;
    const templateStatus = r._template_status || (hasPer ? 'minimal' : 'none');
    const sem = r.semantic_fields || {};
    // For raw 22_decoded records, pull top-level m_tmsi as well
    const semParts = [];
    if(r.m_tmsi != null) semParts.push('m_tmsi='+r.m_tmsi);
    Object.entries(sem).forEach(([k,v])=>{ if(k!=='m_tmsi') semParts.push(k+'='+v); });
    const semStr = semParts.join('  ') || '—';
    return '<tr data-i="'+i+'">'
      + '<td>'+r.pdu_type+'</td>'
      + '<td>'+(r.direction||'—')+'</td>'
      + '<td>'+choice+'</td>'
      + '<td>'+(hasPer ? '<span class="per-badge">PER</span>' : '')+statusBadge(templateStatus)+'</td>'
      + '<td style="font-size:11px;color:var(--muted)">'+semStr+'</td>'
      + '</tr>';
  }).join('');
  tbody.querySelectorAll('tr').forEach(tr=>{
    tr.addEventListener('click',()=>{
      ltetraceSelIdx = +tr.dataset.i;
      tbody.querySelectorAll('tr').forEach(r=>r.classList.remove('sel'));
      tr.classList.add('sel');
      const rec  = recs[ltetraceSelIdx];
      const dmeta = rec.decoding_metadata || {};
      const choice = dmeta.decoded_message_choice || rec.message_name || rec.decoded_choice || '';
      const tmplEntry = tmpl[choice];
      document.getElementById('ltetrace-json').textContent =
        JSON.stringify(rec, null, 2);
      document.getElementById('per-template-json').textContent =
        tmplEntry
          ? JSON.stringify(tmplEntry, null, 2)
          : '(no PER template for "'+choice+'" — run encode_templates.py)';
    });
  });
}

function renderLteKpi(){
  const box = document.getElementById('lte-kpi');
  const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const kpi = DATA4G.kpis;
  if(!kpi || !kpi.phases || !kpi.phases.length){
    box.innerHTML = '<div class="lte-kpi-hdr"><b>Attach KPIs</b></div>'
      + '<p style="color:var(--muted);font-size:11px;margin:0">No attach procedure parsed yet.</p>';
    return;
  }
  const maxMs = Math.max(...kpi.phases.map(p=>p.duration_ms), 1);
  const rows = kpi.phases.map(p=>{
    const pct = Math.max(2, Math.round(p.duration_ms / maxMs * 100));
    return `<div class="lte-kpi-row">`
      + `<span class="ph" title="${esc(p.phase)}">${esc(p.phase)}</span>`
      + `<span class="bar"><i style="width:${pct}%"></i></span>`
      + `<span class="ms">${lteFmtMs(p.duration_ms)}</span>`
      + `</div>`;
  }).join('');
  // Attach (procedure latency) and session (idle/active hold time before
  // Release) are deliberately separate numbers — folding the hold time into
  // "attach duration" would make a call that sat connected for 30s look like
  // attach itself took 30s.
  const sessionPart = kpi.session_ms != null
    ? `Session held ${lteFmtMs(kpi.session_ms)}`
    : 'still connected (no release seen yet)';
  box.innerHTML = `<div class="lte-kpi-hdr"><b>Attach KPIs</b>`
    + `<span class="lte-kpi-badge ${esc(kpi.outcome)}">${esc(kpi.outcome.replace('_',' '))}</span></div>`
    + `<p class="lte-kpi-total">Attach ${lteFmtMs(kpi.attach_ms)} · ${sessionPart} · `
    + `${kpi.event_count} messages · most recent attempt</p>`
    + rows;
}

let ltePairBusy = new Set();   // pair keys with a start/stop request in flight

function renderLtePairBar(){
  const bar = document.getElementById('lte-pair-bar');
  bar.style.display = 'flex';
  const keys = Object.keys(DATA4G_MULTI);
  const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  let chips = '';
  if(keys.length > 1){
    chips = keys.map(k=>{
      const d = DATA4G_MULTI[k];
      const outcome = (d && d.kpis && d.kpis.outcome) || 'none';
      const on = k===ltePairSel ? ' on' : '';
      const cs = CONTAINER_STATUS_4G[k] || {};
      const ueUp = cs.ue === 'running';
      const stoppedTag = ueUp ? '' : ' (stopped)';
      const title = `UE container: ${cs.ue || 'unknown'} · eNB container: ${cs.enb || 'unknown'}`
        + (d && d.has_live ? ` · last known outcome: ${outcome}` : ' · no live logs for this pair');
      const busy = ltePairBusy.has(k);
      const action = ueUp ? 'stop' : 'start';
      const powerTitle = busy ? 'Working…' : (ueUp ? `Stop UE ${k} (and its eNB)` : `Start UE ${k} (and its eNB)`);
      const selectBtn = `<button type="button" class="lte-pair-btn${on}${ueUp ? '' : ' down'}" data-pair="${esc(k)}" title="${esc(title)}">`
        + `<span class="dot ${esc(outcome)}"></span>UE ${esc(k)}${stoppedTag}</button>`;
      const powerBtn = `<button type="button" class="lte-pair-power${busy ? ' busy' : ''}" data-pair="${esc(k)}" `
        + `data-action="${action}" title="${esc(powerTitle)}">${busy ? '…' : '⏻'}</button>`;
      return `<span class="lte-pair-chip">${selectBtn}${powerBtn}</span>`;
    }).join('');
  }
  const pinTitle = ltePinned
    ? 'Pinned — the ladder/detail/KPI panel is frozen on this flow. Click to resume live updates.'
    : 'Freeze the ladder/detail/KPI panel on the current flow. The histogram below keeps updating regardless.';
  const paramsOpen = document.getElementById('lte-params-panel').style.display !== 'none';
  bar.innerHTML = '<b>Viewing</b>' + chips
    + '<span style="flex:1"></span>'
    + `<button type="button" class="lte-params-toggle" id="lte-params-toggle">Parameters ${paramsOpen ? '▲' : '▾'}</button>`
    + `<button type="button" class="lte-pin-btn${ltePinned ? ' on' : ''}" id="lte-pin-btn" title="${esc(pinTitle)}">`
    + (ltePinned ? '📌 Pinned' : '📌 Pin') + '</button>';
  bar.querySelectorAll('.lte-pair-btn').forEach(btn=>{
    btn.onclick = () => selectLtePair(btn.dataset.pair);
  });
  bar.querySelectorAll('.lte-pair-power').forEach(btn=>{
    btn.onclick = () => togglePairPower(btn.dataset.pair, btn.dataset.action);
  });
  document.getElementById('lte-pin-btn').onclick = toggleLtePin;
  document.getElementById('lte-params-toggle').onclick = toggleLteParams;
}

function selectLtePair(key){
  if(!DATA4G_MULTI[key]) return;
  ltePairSel = key;
  DATA4G = DATA4G_MULTI[ltePairSel];
  lteSelIdx = null;
  initLte4g();
}

function toggleLtePin(){
  ltePinned = !ltePinned;
  renderLtePairBar();
}

function toggleLteParams(){
  const panel = document.getElementById('lte-params-panel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  renderLtePairBar();
}

async function togglePairPower(key, action){
  if(ltePairBusy.has(key)) return;
  ltePairBusy.add(key);
  renderLtePairBar();
  try{
    const r = await fetch(`/api/4g/pair/${encodeURIComponent(key)}/power`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action})
    });
    const d = await r.json();
    if(d && d.container_status_4g) CONTAINER_STATUS_4G = d.container_status_4g;
  }catch(e){
    console.error('pair power toggle failed', e);
  }finally{
    ltePairBusy.delete(key);
    renderLtePairBar();
  }
}

const LTE_HIST_METRICS = {
  du_delay_ms: { label: 'DU delay (Msg1→Msg2)', unit: 'ms' },
  attach_ms:   { label: 'Attach time',               unit: 'ms' },
  session_ms:  { label: 'Call duration',             unit: 's', divisor: 1000 },
};

function selectLteHistMetric(metric){
  lteHistMetric = metric;
  renderLteHist();
}

function selectLteHistPair(key){
  lteHistPairFilter = key;
  renderLteHist();
}

function renderLteHist(){
  const box = document.getElementById('lte-hist');
  if(!box) return;
  const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const metricBtns = Object.keys(LTE_HIST_METRICS).map(m=>{
    const on = m===lteHistMetric ? ' on' : '';
    return `<button type="button" class="lte-hist-metric-btn${on}" data-metric="${m}">${esc(LTE_HIST_METRICS[m].label)}</button>`;
  }).join('');

  // Pair filter options come from whatever pairs actually appear in the
  // history log, NOT from DATA4G_MULTI/current container state — a UE you
  // just stopped should still be selectable here, its past samples are
  // still valid history.
  const histPairKeys = [...new Set((KPI_HISTORY||[]).map(s => String(s.pair)))].sort();
  const pairFilterBtns = histPairKeys.length > 1
    ? ['all', ...histPairKeys].map(k=>{
        const on = k===lteHistPairFilter ? ' on' : '';
        const label = k === 'all' ? 'All UEs' : `UE ${k}`;
        return `<button type="button" class="lte-hist-metric-btn${on}" data-pairfilter="${esc(k)}">${esc(label)}</button>`;
      }).join('')
    : '';

  const def = LTE_HIST_METRICS[lteHistMetric] || LTE_HIST_METRICS.du_delay_ms;
  const divisor = def.divisor || 1;
  let samples = (KPI_HISTORY||[]).filter(s => s[lteHistMetric] != null);
  if(lteHistPairFilter !== 'all'){
    samples = samples.filter(s => String(s.pair) === lteHistPairFilter);
  }

  let bodyHtml;
  if(samples.length === 0){
    bodyHtml = lteHistPairFilter === 'all'
      ? '<p class="lte-hist-empty">No completed call flows recorded yet — run '
        + '<code>demo3ue/live_cycler.py</code> to generate live samples for this histogram.</p>'
      : `<p class="lte-hist-empty">No recorded samples for UE ${esc(lteHistPairFilter)} yet.</p>`;
  } else {
    const vals = samples.map(s => s[lteHistMetric] / divisor);
    const min = Math.min(...vals), max = Math.max(...vals);
    const nbins = Math.min(8, Math.max(3, Math.round(Math.sqrt(vals.length))));
    const span = Math.max(max - min, 0.001);
    const binW = span / nbins;
    const bins = new Array(nbins).fill(0);
    vals.forEach(v=>{
      let idx = Math.floor((v - min) / binW);
      if(idx >= nbins) idx = nbins - 1;
      if(idx < 0) idx = 0;
      bins[idx]++;
    });
    const maxCount = Math.max(...bins, 1);
    const rows = bins.map((c,i)=>{
      const lo = min + i*binW, hi = min + (i+1)*binW;
      const pct = Math.max(2, Math.round(c / maxCount * 100));
      return `<div class="lte-hist-bar-row">`
        + `<span class="bin">${lo.toFixed(1)}-${hi.toFixed(1)}${esc(def.unit)}</span>`
        + `<span class="bar"><i style="width:${pct}%"></i></span>`
        + `<span class="cnt">${c}</span>`
        + `</div>`;
    }).join('');

    const mean = vals.reduce((a,b)=>a+b,0) / vals.length;
    const perPair = {};
    samples.forEach(s=>{
      const p = s.pair;
      (perPair[p] = perPair[p] || []).push(s[lteHistMetric] / divisor);
    });
    // Redundant once already filtered to one pair — only show the
    // per-pair breakdown in the "All UEs" view.
    const perPairHtml = lteHistPairFilter === 'all'
      ? Object.keys(perPair).sort().map(p=>{
          const pv = perPair[p];
          const pmean = pv.reduce((a,b)=>a+b,0) / pv.length;
          return `<span>UE ${esc(p)}: ${pmean.toFixed(1)}${esc(def.unit)} avg (n=${pv.length})</span>`;
        }).join('')
      : '';

    bodyHtml = `<p class="lte-hist-summary">n=${vals.length} · mean ${mean.toFixed(1)}${esc(def.unit)} · `
      + `min ${min.toFixed(1)}${esc(def.unit)} · max ${max.toFixed(1)}${esc(def.unit)}</p>`
      + rows
      + (perPairHtml ? `<div class="lte-hist-per-pair">${perPairHtml}</div>` : '');
  }

  box.innerHTML = `<div class="lte-hist-hdr"><b>KPI History</b>${metricBtns}</div>`
    + (pairFilterBtns ? `<div class="lte-hist-hdr">${pairFilterBtns}</div>` : '')
    + bodyHtml;
  // Both button kinds share the .lte-hist-metric-btn class for styling —
  // select by data-attribute, not class, so wiring one kind never clobbers
  // the other's click handler.
  box.querySelectorAll('[data-pairfilter]').forEach(btn=>{
    btn.onclick = () => selectLteHistPair(btn.dataset.pairfilter);
  });
  box.querySelectorAll('[data-metric]').forEach(btn=>{
    btn.onclick = () => selectLteHistMetric(btn.dataset.metric);
  });
}

// "Live" parts are cheap and pair-independent (or instant container status) —
// these always refresh, pin or no pin, every poll.
function initLte4gLive(){
  renderLtePairBar();
  renderLteHist();
}

// The actual call-flow view (ladder/detail/KPI) — gated by pin + the 20s
// interval everywhere except here, where it's an explicit/immediate render
// (first load, or the user just clicked a different pair).
function initLte4gFlow(){
  renderLteInjectBar();
  renderLteEvList();
  renderLteKpi();
  renderLtetraceTable();
}

function initLte4g(){
  initLte4gLive();
  initLte4gFlow();
  lteLastRenderTs = Date.now();
}

/* Overview tab: animated 4G stack topology, click a node for its definition */
const LTE4G_TOPO_NODES = [
  {key:'ue',  label:'UE',  sub:'srsue4g',  icon:'__ICON_UE__',
   def:'The User Equipment — real srsRAN_4G <code>srsue</code> software simulating a phone. '
     + 'It runs the actual LTE UE protocol stack (PHY, MAC, RLC, PDCP, RRC, NAS) and produces '
     + 'byte-correct ASN.1 signaling, the same as a real device would.'},
  {key:'iq',  label:'ZMQ IQ', sub:'radio link', icon:'__ICON_IQ__',
   def:'A ZeroMQ socket pair standing in for the radio link. Instead of real RF hardware, '
     + '<code>srsue</code> and <code>srsenb</code> exchange raw IQ samples over ZeroMQ in lockstep '
     + '— one request/reply round trip per radio subframe — so every PRACH preamble and RRC '
     + 'message crossing this link is exactly what it would be over real air.'},
  {key:'enb', label:'eNB',  sub:'srsenb',  icon:'__ICON_ENB__',
   def:'The evolved Node B — the LTE base station, real srsRAN_4G <code>srsenb</code> software '
     + '(PHY, MAC scheduling, RLC, PDCP, RRC). It replies to a UE\'s PRACH attempt with a Random '
     + 'Access Response and terminates RRC signaling.'},
  {key:'epc', label:'EPC',  sub:'srsepc',  icon:'__ICON_EPC__',
   def:'The Evolved Packet Core — real srsRAN_4G <code>srsepc</code> software combining the MME '
     + '(mobility/signaling), HSS (subscriber database), and S/P-GW (data-plane gateway). It '
     + 'authenticates the UE, manages its security context, and sets up its data bearer.'},
];
let lte4gTopoSel = null;

function renderLte4gTopo(){
  const box = document.getElementById('lte4g-topo');
  const nodes = LTE4G_TOPO_NODES.map((n,i)=>{
    const link = i < LTE4G_TOPO_NODES.length - 1
      ? `<div class="lte4g-topo-link"><span class="lte4g-topo-pulse" style="animation-delay:${i*0.55}s"></span></div>`
      : '';
    return `<button type="button" class="lte4g-topo-node${n.key===lte4gTopoSel?' sel':''}" data-node="${n.key}">`
      + `<span class="lte4g-topo-icon">${n.icon}</span><b>${n.label}</b><small>${n.sub}</small></button>${link}`;
  }).join('');
  box.innerHTML = `<div class="lte4g-topo-row">${nodes}</div>`;
  box.querySelectorAll('.lte4g-topo-node').forEach(btn=>{
    btn.onclick = () => selectLte4gTopoNode(btn.dataset.node);
  });
  renderLte4gTopoDef();
}

function selectLte4gTopoNode(key){
  lte4gTopoSel = (lte4gTopoSel === key) ? null : key;
  renderLte4gTopo();
}

function renderLte4gTopoDef(){
  const def = document.getElementById('lte4g-topo-def');
  const n = LTE4G_TOPO_NODES.find(x=>x.key===lte4gTopoSel);
  if(!n){ def.style.display = 'none'; return; }
  def.style.display = 'block';
  def.innerHTML = `<b>${n.label} — ${n.sub}</b>${n.def}`;
}

initLte4g();
renderLte4gTopo();
if((DATA4G.events || []).length){
  activateTab('lte4g');
}
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("log_dir", nargs="?", default=os.path.join(here, "logs"))
    ap.add_argument("-o", "--out", default=os.path.join(here, "index.html"))
    a = ap.parse_args()

    events, meta = build(a.log_dir)
    rrc_twin, rrc_trace, rrc_meta = build_rrc(a.log_dir)
    signaling = build_signaling(rrc_twin, events)
    rrc_twin, events, signaling = apply_trace_replay(rrc_twin, events, signaling)
    n_tx = write_transmit_plan(signaling, default_transmit_plan_path(a.log_dir))
    trace_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", "..", "poc_StressTest", "22_decoded", "00")
    real_trace_dir = trace_dir if os.path.isdir(trace_dir) else None
    data_4g = build_4g(a.log_dir, real_trace_dir)
    data_4g_multi = {"1": data_4g}
    for key, sub in (("2", "pair2"), ("3", "pair3")):
        pair_dir = os.path.join(a.log_dir, sub)
        if os.path.isdir(pair_dir):
            data_4g_multi[key] = build_4g(pair_dir, real_trace_dir)
    html_out = render_html(events, meta, rrc_twin, rrc_trace, rrc_meta, signaling,
                           data_4g=data_4g, data_4g_multi=data_4g_multi)

    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html_out)

    # Legacy alias
    legacy = os.path.join(os.path.dirname(a.out), "callflow.html")
    if os.path.abspath(legacy) != os.path.abspath(a.out):
        with open(legacy, "w", encoding="utf-8") as f:
            f.write(html_out)

    print(f"Dashboard: {len(events)} messages -> {a.out}")
    if n_tx:
        print(f"Transmit plan: {n_tx} UL messages -> {default_transmit_plan_path(a.log_dir)}")
    if meta["by_layer"]:
        print("By layer:", ", ".join(f"{k}={v}" for k, v in sorted(meta["by_layer"].items())))
    n4g = len(data_4g.get("events", []))
    n_tr = len(data_4g.get("trace_recs", []))
    n_tmpl = len(data_4g.get("per_templates", {}))
    if n4g or n_tr:
        print(f"4G LTE: {n4g} live events, {n_tr} trace records, {n_tmpl} PER templates")


if __name__ == "__main__":
    main()
