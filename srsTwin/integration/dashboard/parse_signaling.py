#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""Build live call-flow signaling JSON (RRC / S1 / X2) and trace catalog metadata."""

from __future__ import annotations

import json
import re
from datetime import datetime

from trace_catalog import get_catalog, protocol_of_record_id

# Map srsTwin NR RRC / NGAP names → LTE trace message_name for catalog lookup.
NR_RRC_TO_LTE = {
    "rrcSetupRequest": "RRC_RRC_CONNECTION_REQUEST",
    "rrcSetup": "RRC_RRC_CONNECTION_SETUP",
    "rrcSetupComplete": "RRC_RRC_CONNECTION_SETUP_COMPLETE",
    "securityModeCommand": "RRC_SECURITY_MODE_COMMAND",
    "securityModeComplete": "RRC_SECURITY_MODE_COMPLETE",
    "ueCapabilityEnquiry": "RRC_UE_CAPABILITY_INFORMATION",
    "ueCapabilityInformation": "RRC_UE_CAPABILITY_INFORMATION",
    "rrcReconfiguration": "RRC_RRC_CONNECTION_RECONFIGURATION_COMPLETE",
    "rrcReconfigurationComplete": "RRC_RRC_CONNECTION_RECONFIGURATION_COMPLETE",
    "rrcRelease": "RRC_RRC_CONNECTION_RELEASE",
    "dlInformationTransfer": "RRC_DL_INFORMATION_TRANSFER",
    "ulInformationTransfer": "RRC_UL_INFORMATION_TRANSFER",
}

NGAP_TO_S1 = {
    "InitialUEMessage": "S1_INITIAL_UE_MESSAGE",
    "DownlinkNASTransport": "S1_DOWNLINK_NAS_TRANSPORT",
    "UplinkNASTransport": "S1_UPLINK_NAS_TRANSPORT",
    "InitialContextSetupRequest": "S1_INITIAL_CONTEXT_SETUP_REQUEST",
    "InitialContextSetupResponse": "S1_INITIAL_CONTEXT_SETUP_RESPONSE",
    "UEContextReleaseCommand": "S1_UE_CONTEXT_RELEASE_COMMAND",
    "UEContextReleaseComplete": "S1_UE_CONTEXT_RELEASE_COMPLETE",
    "UEContextReleaseRequest": "S1_UE_CONTEXT_RELEASE_REQUEST",
    "UERadioCapabilityInfoIndication": "S1_UE_CAPABILITY_INDICATION",
    "PDUSessionResourceSetupRequest": "S1_ERAB_SETUP_REQUEST",
    "PDUSessionResourceSetupResponse": "S1_ERAB_SETUP_RESPONSE",
}

NGAP_RE = re.compile(
    r"PDU(?:\s+ue=\d+\s+ran_ue=\d+(?:\s+amf_ue=\d+)?:)?[:\s]+(\w+)"
)


def _json_from_detail(detail: str) -> dict | list | None:
    if not detail:
        return None
    start = detail.find("{")
    if start < 0:
        start = detail.find("[")
    if start < 0:
        return None
    blob = detail[start:]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def _trace_lookup_name(protocol: str, message_name: str, raw_name: str | None = None) -> str:
    if protocol == "RRC":
        return NR_RRC_TO_LTE.get(raw_name or message_name, message_name)
    if protocol == "S1":
        return NGAP_TO_S1.get(raw_name or message_name, message_name)
    return message_name


def _envelope(protocol: str, message_name: str, ts: str, t: float, direction: str,
              source: str, message: dict, **extra) -> dict:
    row = {
        "protocol": protocol,
        "interface": protocol,
        "message_name": message_name,
        "timestamp": ts,
        "t": t,
        "direction": direction,
        "source": source,
        "message": message,
        "trace_lookup": _trace_lookup_name(protocol, message_name, extra.get("raw_name")),
    }
    row.update({k: v for k, v in extra.items() if k != "raw_name"})
    return row


def live_from_rrc(rrc_twin: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in rrc_twin:
        decoded = m.get("decoded")
        if not decoded and m.get("json_raw"):
            try:
                decoded = json.loads(m["json_raw"])
            except json.JSONDecodeError:
                decoded = None
        if not decoded:
            continue
        name = m.get("message_name") or m.get("raw_name") or "?"
        out.append(_envelope(
            "RRC", name, m.get("ts", ""), m.get("t", 0.0),
            m.get("direction", "?"), m.get("source", "srsTwin"),
            decoded,
            raw_name=m.get("raw_name"),
            channel=m.get("channel"),
            pdu_type=m.get("pdu_type"),
            size_b=m.get("size_b"),
        ))
    for i, row in enumerate(out):
        row["id"] = i
    return out


def live_from_events(events: list[dict]) -> list[dict]:
    """NGAP PDUs as S1-tab JSON envelopes (5G N2; LTE S1AP reference on the right)."""
    out: list[dict] = []
    for e in events:
        if e.get("layer") != "NGAP":
            continue
        detail = e.get("detail", "") or ""
        m = NGAP_RE.search(detail) or NGAP_RE.search(e.get("label", ""))
        raw = m.group(1) if m else re.sub(r"\s*\(.*", "", e.get("label", ""))
        trace_name = NGAP_TO_S1.get(raw, raw)
        decoded = _json_from_detail(detail)
        message = decoded if decoded is not None else {
            "pdu": raw,
            "layer": "NGAP",
            "note": "5G NGAP on N2 — compare with LTE S1AP reference (right)",
            "route": f"{e.get('src')} → {e.get('dst')}",
            "log_excerpt": detail,
        }
        out.append(_envelope(
            "S1", trace_name, e.get("ts", ""), e.get("t", 0.0),
            f"{e.get('src')}→{e.get('dst')}", "srsTwin gNB",
            message,
            raw_name=raw,
            ngap_pdu=raw,
        ))
    return out


def build_live(rrc_twin: list[dict], events: list[dict]) -> list[dict]:
    live = live_from_rrc(rrc_twin) + live_from_events(events)
    live.sort(key=lambda x: (x.get("timestamp", ""), x.get("protocol", "")))
    for i, row in enumerate(live):
        row["id"] = i
    return live


def build_catalog_summary() -> dict:
    cat, err = get_catalog()
    if cat is None:
        return {
            "entries": [],
            "status": {"error": err, "ready": False, "found": 0, "total": 0},
            "by_protocol": {"RRC": [], "S1": [], "X2": []},
            "trace_samples": {},
            "trace_samples_by_name": {},
        }
    entries = cat.list_entries()
    by_protocol: dict[str, list] = {"RRC": [], "S1": [], "X2": []}
    trace_samples: dict[str, dict] = {}
    trace_samples_by_name: dict[str, dict] = {}
    seen_ids: set[int] = set()
    for e in entries:
        proto = e.get("protocol") or protocol_of_record_id(e["record_id"])
        by_protocol.setdefault(proto, []).append(e)
        rid = e["record_id"]
        if rid in seen_ids:
            continue
        sample = cat.get_by_record_id(rid)
        if sample and sample.get("message"):
            trace_samples[str(rid)] = sample["message"]
            trace_samples_by_name[e["message_name"]] = sample["message"]
            seen_ids.add(rid)
    return {
        "entries": entries,
        "status": cat.status(),
        "by_protocol": by_protocol,
        "trace_samples": trace_samples,
        "trace_samples_by_name": trace_samples_by_name,
    }


def build_signaling(rrc_twin: list[dict], events: list[dict]) -> dict:
    live = build_live(rrc_twin, events)
    catalog = build_catalog_summary()
    by_protocol = {"RRC": [], "S1": [], "X2": []}
    for m in live:
        by_protocol.setdefault(m["protocol"], []).append(m)
    return {
        "live": live,
        "live_by_protocol": by_protocol,
        "catalog": catalog,
        "meta": {
            "live_count": len(live),
            "live_rrc": len(by_protocol["RRC"]),
            "live_s1": len(by_protocol["S1"]),
            "live_x2": len(by_protocol["X2"]),
            "catalog_entries": len(catalog.get("entries") or []),
            "catalog_found": (catalog.get("status") or {}).get("found", 0),
        },
    }
