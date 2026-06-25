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


def render_html(events, meta, rrc_twin, rrc_trace, rrc_meta, signaling=None,
                data_4g=None, data_4g_multi=None, container_status_4g=None):
    if signaling is None:
        signaling = build_signaling(rrc_twin, events)
    if data_4g is None:
        data_4g = dict(_EMPTY_4G)
    if data_4g_multi is None:
        data_4g_multi = {"1": data_4g}
    if container_status_4g is None:
        container_status_4g = {}
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
            .replace("__4G_CONTAINER_STATUS__", json.dumps(container_status_4g)))


HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>srsTwin Dashboard</title>
<style>
:root{
  --bg:#0a0e14; --panel:#121820; --panel2:#1a2230; --line:#2d3640; --muted:#8b949e;
  --txt:#e6edf3; --ok:#3fb950; --warn:#d29922; --bad:#f85149;
  --PHY:#a371f7; --MAC:#22d3ee; --RRC:#58a6ff; --NAS:#3fb950; --NGAP:#f0883e;
}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--txt)}
header{padding:8px 18px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#101620,#0a0e14);
  display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:40px;flex-shrink:0}
header h1{margin:0;font-size:15px;font-weight:600;white-space:nowrap}
.livebar{display:none;align-items:center;gap:8px;font-size:12px;margin:0;flex-shrink:0}
.livebar .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.livebar button{padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--panel2);
  color:var(--txt);cursor:pointer;font-size:11px}.livebar button:hover{border-color:var(--RRC)}
.tabs{display:flex;gap:4px;padding:6px 18px 0;border-bottom:1px solid var(--line);background:var(--panel);flex-shrink:0}
.tab{padding:8px 14px;border:none;background:transparent;color:var(--muted);cursor:pointer;font-size:12px;
  border-bottom:2px solid transparent;margin-bottom:-1px}
.tab:hover{color:var(--txt)}
.tab.on{color:var(--txt);border-bottom-color:var(--RRC);font-weight:600}
.panel{display:none;height:calc(100vh - 76px);overflow:hidden;min-height:0}
.panel.on{display:flex;flex-direction:column}
/* --- overview --- */
.overview{padding:20px 22px;overflow:auto;flex:1}
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
.lte-wrap{display:flex;flex:1;overflow:hidden}
/* Fixed to the SVG ladder's native width (4 lanes x 120px, see buildLteLadderSvg)
   plus padding — sizing this to content (not flex:1) is what frees up the rest
   of the panel for .lte-detail instead of leaving it empty. */
.lte-ladder{flex:0 0 520px;overflow:auto;border-right:1px solid var(--line);display:flex;flex-direction:column}
.lte-lanes{display:flex;gap:0;padding:8px 14px;border-bottom:2px solid #f9826c;background:var(--panel2)}
.lte-lanehdr{flex:1;text-align:center;font-size:12px;font-weight:600;color:#f9826c;padding:4px 0}
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
  <h1>srsTwin · 5G SA Digital Twin</h1>
  <div class="livebar" id="livebar">
    <span class="dot"></span><span id="live-label">Live</span>
    <button type="button" id="btn-refresh">Refresh</button>
  </div>
</header>
<nav class="tabs">
  <button class="tab on" data-tab="callflow">Signaling flow</button>
  <button class="tab" data-tab="messages">Messages</button>
  <button class="tab" data-tab="rrc">RRC → IQ</button>
  <button class="tab" data-tab="signaling">RRC · S1 · X2</button>
  <button class="tab tab-4g" data-tab="lte4g">4G LTE</button>
  <button class="tab tab-4g" data-tab="lte4gtrace">4G Trace</button>
  <button class="tab" data-tab="overview">Overview</button>
</nav>

<section class="panel" id="panel-lte4g">
  <div class="lte-pair-bar" id="lte-pair-bar"><b>Viewing</b></div>
  <div class="lte-wrap">
    <div class="lte-ladder">
      <div class="lte-lanes">
        <div class="lte-lanehdr">srsUE 4G</div>
        <div class="lte-lanehdr">ZMQ IQ</div>
        <div class="lte-lanehdr">srseNB</div>
        <div class="lte-lanehdr">srsEPC</div>
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
      <div class="lte-kpi" id="lte-kpi"></div>
    </div>
  </div>
</section>

<section class="panel" id="panel-lte4gtrace">
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

<section class="panel" id="panel-overview">
  <div class="overview">
    <div class="cards" id="cards"></div>
    <div class="topo">
      <h3 style="margin:0 0 12px;font-size:14px">Topology · <span id="topo-mode">direct</span></h3>
      <div id="topo-svg"></div>
    </div>
    <h3 style="font-size:14px;margin:0 0 8px">Messages by layer</h3>
    <div class="layer-bars" id="layer-bars"></div>
  </div>
</section>

<section class="panel on" id="panel-callflow">
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

<section class="panel" id="panel-messages">
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

<section class="panel" id="panel-rrc">
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

<section class="panel" id="panel-signaling">
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

function applyData(events, meta, rrc){
  EVENTS=enrichEvents(events); META=meta;
  updateOverview(); buildLegend(); render(); renderMsgTable();
  if(rrc && rrc.rrc_twin) applyRrc(rrc.rrc_twin, rrc.rrc_trace, rrc.rrc_meta);
  if(rrc && rrc.signaling) applySignaling(rrc.signaling);
  // The 4G LTE tab used to only get fresh data on a full page reload, since
  // DATA4G was baked into the HTML at generation time and neither the 5s
  // live poll nor the Refresh button ever touched it after load.
  if(rrc && rrc.data_4g_multi){
    DATA4G_MULTI = rrc.data_4g_multi;
    DATA4G = DATA4G_MULTI[ltePairSel] || DATA4G_MULTI['1'] || DATA4G;
    if(rrc.container_status_4g) CONTAINER_STATUS_4G = rrc.container_status_4g;
    initLte4g();
  } else if(rrc && rrc.data_4g){
    DATA4G = rrc.data_4g;
    if(rrc.container_status_4g) CONTAINER_STATUS_4G = rrc.container_status_4g;
    initLte4g();
  }
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
    } else if(d.data_4g_multi){
      // 5G state unchanged (5G stack likely isn't even running), but the
      // server now pulls fresh 4G logs on every /api/data call — refresh the
      // 4G tab independently so stopping/starting a UE shows up within one
      // poll interval instead of being gated behind 5G activity that never happens.
      DATA4G_MULTI = d.data_4g_multi;
      DATA4G = DATA4G_MULTI[ltePairSel] || DATA4G_MULTI['1'] || DATA4G;
      if(d.container_status_4g) CONTAINER_STATUS_4G = d.container_status_4g;
      initLte4g();
    }
    document.getElementById('live-label').textContent='Live · updated '+new Date().toLocaleTimeString();
  }catch(e){}
}

async function pullRefresh(){
  const btn=document.getElementById('btn-refresh');
  btn.disabled=true; btn.textContent='Pulling…';
  try{
    const d=await fetchData('/api/refresh');
    applyData(d.events, d.meta, d);
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
let ltePairSel = Object.keys(DATA4G_MULTI)[0] || '1';
let DATA4G = DATA4G_MULTI[ltePairSel];
// Real docker-ps-derived running/stopped state, not log-derived — instant,
// unlike the KPI/outcome dot which lags behind srsRAN's own buffered file logger.
let CONTAINER_STATUS_4G = __4G_CONTAINER_STATUS__;
let lteSelIdx = null;
let ltetraceSelIdx = null;

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
    .filter(a => a.trace && a.ev_idx !== undefined)
    .map(a => [a.ev_idx, a.trace]));
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
  const trace = aligned && aligned.trace;
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
    const traceRec = (DATA4G.aligned||[]).find(a=>a.ev_idx===i && a.trace);
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
        '[' + ev.layer + '] ' + ev.label + (aligned && aligned.trace ? '  [22_decoded]' : '');
      renderLteDetailBody(ev, aligned);
    });
  });
}

function renderLtetraceTable(){
  const tbody = document.getElementById('ltetrace-tbody');
  const recs  = DATA4G.trace_recs || [];
  const tmpl  = DATA4G.per_templates || {};
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

function renderLtePairBar(){
  const bar = document.getElementById('lte-pair-bar');
  const keys = Object.keys(DATA4G_MULTI);
  if(keys.length <= 1){ bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const buttons = keys.map(k=>{
    const d = DATA4G_MULTI[k];
    const outcome = (d && d.kpis && d.kpis.outcome) || 'none';
    const on = k===ltePairSel ? ' on' : '';
    const cs = CONTAINER_STATUS_4G[k] || {};
    const ueUp = cs.ue === 'running';
    const stoppedTag = ueUp ? '' : ' (stopped)';
    const title = `UE container: ${cs.ue || 'unknown'} · eNB container: ${cs.enb || 'unknown'}`
      + (d && d.has_live ? ` · last known outcome: ${outcome}` : ' · no live logs for this pair');
    return `<button type="button" class="lte-pair-btn${on}${ueUp ? '' : ' down'}" data-pair="${esc(k)}" title="${esc(title)}">`
      + `<span class="dot ${esc(outcome)}"></span>UE ${esc(k)}${stoppedTag}</button>`;
  }).join('');
  bar.innerHTML = '<b>Viewing</b>' + buttons;
  bar.querySelectorAll('.lte-pair-btn').forEach(btn=>{
    btn.onclick = () => selectLtePair(btn.dataset.pair);
  });
}

function selectLtePair(key){
  if(!DATA4G_MULTI[key]) return;
  ltePairSel = key;
  DATA4G = DATA4G_MULTI[ltePairSel];
  lteSelIdx = null;
  initLte4g();
}

function initLte4g(){
  renderLtePairBar();
  renderLteInjectBar();
  renderLteEvList();
  renderLteKpi();
  renderLtetraceTable();
}

initLte4g();
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
