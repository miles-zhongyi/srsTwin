#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""Parse NR/LTE RRC messages from srsTwin logs and optional decoded trace JSON."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

TS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T(?P<short>\d{2}:\d{2}:\d{2}\.\d+))\s+"
    r"\[(?P<layer>[^\]]+)\]\s+\[(?P<lvl>[IDWE])\]\s+(?P<txt>.*)$"
)

UE_RRC = re.compile(
    r"^(?P<ch>BCCH-DLSCH|SRB\d)\s*-\s*(?P<dir>Tx|Rx)\s+(?P<name>\w+)\s*\((?P<sz>\d+)\s*B\)"
)
GNB_RRC = re.compile(
    r"^(?:ue=\d+\s+c-rnti=0x[0-9a-f]+:\s*)?"
    r"(?P<dir>Tx|Rx)\s+SRB\d\s+CCCH\s+(?P<uldl>UL|DL)\s+(?P<name>\w+)\s*\((?P<sz>\d+)\s*B\)"
)
GNB_SRB = re.compile(
    r"^(?:ue=\d+\s+c-rnti=0x[0-9a-f]+:\s*)?"
    r"(?P<dir>Tx|Rx)\s+SRB(?P<srb>\d+)\s+(?:DCCH\s+)?(?P<uldl>UL|DL)?\s*(?P<name>\w+)\s*\((?P<sz>\d+)\s*B\)"
)
HEX_LINE = re.compile(r"^\s*[0-9a-fA-F]{4}:\s+[0-9a-fA-F\s]+$")

SAMPLES_PER_SLOT = 11520
SAMPLE_RATE_MHZ = 11.52
BYTES_PER_SLOT = SAMPLES_PER_SLOT * 8


def _read_raw_lines(path: str) -> list[str]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return [ln.rstrip("\n") for ln in f]


def _collect_following(lines: list[str], start: int) -> tuple[list[str], int]:
    extra: list[str] = []
    i = start
    while i < len(lines):
        if TS_RE.match(lines[i]):
            break
        extra.append(lines[i])
        i += 1
    return extra, i


def _parse_json_from_lines(extra: list[str]) -> tuple[str | None, dict | list | None]:
    blob: list[str] = []
    started = False
    for line in extra:
        if not started:
            if "Content:[" in line:
                started = True
                blob.append(line[line.index("[", line.index("Content:")):])
            elif re.search(r"Containerized\s+\w+:\s*\[", line):
                started = True
                blob.append(line[line.index("[", line.index("Containerized")):])
            elif line.strip().startswith("["):
                started = True
                blob.append(line.strip())
            continue
        blob.append(line)
        try:
            parsed = json.loads("\n".join(blob))
            return "\n".join(blob), parsed
        except json.JSONDecodeError:
            continue
    return None, None


def _parse_hex_block(extra: list[str]) -> str | None:
    rows = []
    for line in extra:
        if HEX_LINE.match(line):
            rows.append(line.strip())
        elif "Content:" in line or "Containerized" in line:
            break
    return "\n".join(rows) if rows else None


def _pdu_type(decoded) -> str | None:
    if isinstance(decoded, list) and decoded and isinstance(decoded[0], dict):
        return next(iter(decoded[0].keys()), None)
    if isinstance(decoded, dict):
        if "message" in decoded and isinstance(decoded["message"], list):
            inner = decoded["message"]
            if inner and isinstance(inner[0], str):
                return inner[0]
        return decoded.get("_pdu_type")
    return None


def _message_name_from_decoded(decoded, fallback: str) -> str:
    if isinstance(decoded, dict) and "message" in decoded:
        msg = decoded["message"]
        if isinstance(msg, list) and msg and isinstance(msg[0], str):
            return msg[0]
    if isinstance(decoded, list) and decoded and isinstance(decoded[0], dict):
        root = decoded[0]
        for pdu in ("UL-CCCH-Message", "DL-CCCH-Message", "UL-DCCH-Message",
                    "DL-DCCH-Message", "BCCH-DL-SCH-Message"):
            if pdu in root:
                try:
                    c1 = root[pdu]["message"]["c1"]
                    return next(iter(c1.keys()))
                except (KeyError, StopIteration, TypeError):
                    pass
    return fallback


def iq_pipeline(channel: str, direction: str, message_name: str, size_b: int) -> dict:
    is_bcch = channel == "BCCH-DLSCH"
    is_ccch = channel == "SRB0"
    dl = direction == "Rx"

    phy_ch = "PDSCH (SI-RNTI)" if is_bcch else (
        "PDSCH (RA-RNTI/C-RNTI)" if is_ccch and dl else
        "PUSCH (Msg3/C-RNTI)" if is_ccch else
        "PDSCH" if dl else "PUSCH"
    )

    steps = [
        {"layer": "RRC", "action": f"ASN.1 PER → {message_name}",
         "detail": f"{size_b} B on {channel} ({direction})."},
        {"layer": "PDCP", "action": "Transparent (SRB0/BCCH) or secured SRB1+",
         "detail": "Adds SN/cipher for DCCH after security mode."},
        {"layer": "RLC", "action": "TM or AM SDU",
         "detail": "LCID 0 = CCCH/SRB0, LCID 1 = SRB1."},
        {"layer": "MAC", "action": "Multiplex + HARQ",
         "detail": "Msg3/4 on CCCH during RA; dedicated grants after C-RNTI."},
        {"layer": "PHY", "action": f"Modulate {phy_ch}",
         "detail": "QPSK typical for control; 1 slot = 1 ms @ 15 kHz SCS."},
        {"layer": "RF/ZMQ", "action": "Full-slot IQ over TCP",
         "detail": f"REQ 0xff + {SAMPLES_PER_SLOT}×complex64 ({BYTES_PER_SLOT} B/slot)."},
    ]
    return {
        "summary": f"{message_name}: {channel} → PHY → ZMQ slot(s); not 1 byte = 1 sample.",
        "steps": steps,
        "zmq": {
            "format": "complex64 LE (re, im float32)",
            "samples_per_slot": SAMPLES_PER_SLOT,
            "bytes_per_slot": BYTES_PER_SLOT,
            "sample_rate_msps": SAMPLE_RATE_MHZ,
            "request_byte": "0xff",
        },
        "note": "Twin exchanges whole slots; RRC PDU is a small part of the PDSCH/PUSCH TB.",
    }


def _mk_msg(e, source, side, channel, direction, name, size_b, hex_dump, json_raw, decoded):
    display = _message_name_from_decoded(decoded, name) if decoded else name
    return {
        "ts": e["ts"], "short": e["short"], "source": source, "side": side,
        "channel": channel, "direction": direction,
        "message_name": display, "raw_name": name, "size_b": size_b,
        "pdu_type": _pdu_type(decoded), "hex": hex_dump,
        "json_raw": json_raw, "decoded": decoded,
        "iq": iq_pipeline(channel, direction, display, size_b),
    }


def _attach_json(out: list[dict], json_raw, decoded) -> None:
    if not out or not decoded:
        return
    m = out[-1]
    if m.get("decoded"):
        return
    m["json_raw"] = json_raw
    m["decoded"] = decoded
    m["message_name"] = _message_name_from_decoded(decoded, m["raw_name"])
    m["pdu_type"] = _pdu_type(decoded)
    m["iq"] = iq_pipeline(m["channel"], m["direction"], m["message_name"], m["size_b"])


def parse_ue_rrc_lines(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    i = 0
    while i < len(lines):
        m_ts = TS_RE.match(lines[i])
        if not m_ts:
            i += 1
            continue
        layer = m_ts.group("layer").strip()
        txt = m_ts.group("txt")

        if layer in ("RRC-NR", "RRC") and "Content:[" in txt:
            extra, i = _collect_following(lines, i + 1)
            json_raw, decoded = _parse_json_from_lines([txt] + extra)
            _attach_json(out, json_raw, decoded)
            continue

        m = UE_RRC.search(txt) if layer in ("RRC-NR", "RRC") else None
        if not m:
            i += 1
            continue
        extra, i = _collect_following(lines, i + 1)
        ch, direction, name, sz = m.group("ch", "dir", "name", "sz")
        e = {"ts": m_ts.group("ts"), "short": m_ts.group("short")}
        out.append(_mk_msg(e, "srsUE", "UE", ch, direction, name, int(sz),
                           _parse_hex_block(extra), None, None))
    return out


def parse_gnb_rrc_lines(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    i = 0
    while i < len(lines):
        m_ts = TS_RE.match(lines[i])
        if not m_ts:
            i += 1
            continue
        layer = m_ts.group("layer").strip()
        txt = m_ts.group("txt")
        if layer != "RRC":
            i += 1
            continue

        if "Containerized" in txt and ": [" in txt:
            extra, i = _collect_following(lines, i + 1)
            frag = txt.split(":", 1)[1].strip()
            json_raw, decoded = _parse_json_from_lines([frag] + extra)
            _attach_json(out, json_raw, decoded)
            continue

        m = GNB_RRC.search(txt) or GNB_SRB.search(txt)
        if not m:
            i += 1
            continue
        extra, i = _collect_following(lines, i + 1)
        direction = m.group("dir")
        name = m.group("name")
        sz = int(m.group("sz"))
        ch = "SRB0" if "CCCH" in txt else f"SRB{m.group('srb')}" if m.groupdict().get("srb") else "SRB1"
        ue_dir = "Rx" if direction == "Tx" else "Tx"
        e = {"ts": m_ts.group("ts"), "short": m_ts.group("short")}
        out.append(_mk_msg(e, "ocudu gNB", "DU", ch, ue_dir, name, sz,
                           _parse_hex_block(extra), None, None))
    return out


def parse_trace_json(path: str, limit: int | None = None) -> list[dict]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    out = []
    for rec in data:
        if rec.get("interface") != "RRC" and rec.get("protocol") != "RRC":
            continue
        decoded = rec.get("decoded")
        name = rec.get("message_name") or _message_name_from_decoded(decoded, "?")
        direction = "Tx" if rec.get("direction") == 2 else "Rx"
        size_b = len(json.dumps(decoded)) if decoded else 0
        out.append({
            "ts": rec.get("timestamp", ""), "short": (rec.get("timestamp") or "")[11:23],
            "source": "field trace", "side": "UE", "channel": "LTE-DCCH",
            "direction": direction, "message_name": name, "raw_name": name,
            "size_b": size_b,
            "pdu_type": decoded.get("_pdu_type") if isinstance(decoded, dict) else None,
            "hex": None,
            "json_raw": json.dumps(decoded, indent=2) if decoded else None,
            "decoded": decoded,
            "iq": iq_pipeline("SRB1", direction, name, max(size_b, 1)),
            "trace_meta": {
                "record_id": rec.get("record_id"),
                "cell_id": rec.get("cell_id"),
                "plmn": rec.get("serving_plmn"),
            },
        })
        if limit and len(out) >= limit:
            break
    return out


def _default_trace_path() -> str | None:
    root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cand = os.path.join(root, "22_decoded", "00",
                        "A20260122.000000+0000-20260122.000020+0000_2887343578_TRC_decoded.json")
    return cand if os.path.isfile(cand) else None


def build_rrc(log_dir: str, trace_path: str | None = None, trace_limit: int = 100):
    msgs: list[dict] = []
    ue_path = os.path.join(log_dir, "ue.log")
    gnb_path = os.path.join(log_dir, "gnb.log")
    if os.path.isfile(ue_path):
        msgs.extend(parse_ue_rrc_lines(_read_raw_lines(ue_path)))
    if os.path.isfile(gnb_path):
        msgs.extend(parse_gnb_rrc_lines(_read_raw_lines(gnb_path)))

    twin: list[dict] = []
    seen: set[tuple] = set()
    for m in sorted(msgs, key=lambda x: (x["ts"], x["source"])):
        key = (m["ts"], m["raw_name"], m["direction"], m["source"])
        if key in seen:
            continue
        seen.add(key)
        twin.append(m)

    if trace_path is None:
        trace_path = _default_trace_path()
    trace = parse_trace_json(trace_path, trace_limit) if trace_path else []

    if twin:
        t0 = datetime.fromisoformat(twin[0]["ts"])
        for m in twin:
            m["t"] = round((datetime.fromisoformat(m["ts"]) - t0).total_seconds(), 3)
    for i, m in enumerate(twin):
        m["id"] = i
    for i, m in enumerate(trace):
        m["id"] = i

    meta = {
        "twin_count": len(twin),
        "trace_count": len(trace),
        "message_names": sorted({m["message_name"] for m in twin}),
        "has_json": sum(1 for m in twin if m.get("decoded")),
        "trace_path": trace_path if trace else None,
    }
    return twin, trace, meta


if __name__ == "__main__":
    import argparse
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("log_dir", nargs="?", default=os.path.join(here, "logs"))
    ap.add_argument("--trace", default=None)
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    twin, trace, meta = build_rrc(args.log_dir, args.trace, args.limit)
    print(f"twin: {meta['twin_count']} msgs, {meta['has_json']} with JSON")
    print(f"trace: {meta['trace_count']} from {meta.get('trace_path') or 'n/a'}")
