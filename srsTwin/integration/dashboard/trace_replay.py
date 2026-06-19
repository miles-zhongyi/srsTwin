#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""Replace srsRAN-native decoded JSON with direct 22_decoded trace replays when available."""

from __future__ import annotations

import copy
import re

from parse_signaling import NGAP_RE, NGAP_TO_S1, NR_RRC_TO_LTE

NGAP_LABEL_RE = re.compile(r"PDU[:\s]+(\w+)", re.I)


def trace_name_for_rrc(raw_name: str | None) -> str | None:
    if not raw_name:
        return None
    return NR_RRC_TO_LTE.get(raw_name, raw_name)


def trace_name_for_ngap(detail: str, label: str = "") -> str | None:
    m = NGAP_RE.search(detail or "") or NGAP_RE.search(label or "")
    if m:
        return NGAP_TO_S1.get(m.group(1), m.group(1))
    m2 = NGAP_LABEL_RE.search(label or "")
    if m2:
        return NGAP_TO_S1.get(m2.group(1), m2.group(1))
    return None


def _samples_from_signaling(signaling: dict | None) -> dict[str, dict]:
    if not signaling:
        return {}
    cat = signaling.get("catalog") or {}
    by_name = dict(cat.get("trace_samples_by_name") or {})
    for rid, sample in (cat.get("trace_samples") or {}).items():
        name = (sample or {}).get("message_name")
        if name and name not in by_name:
            by_name[name] = sample
    return by_name


def _replay(sample: dict | None) -> dict | None:
    if not sample:
        return None
    return copy.deepcopy(sample)


def enrich_rrc_twin(rrc_twin: list[dict], by_name: dict[str, dict]) -> None:
    for m in rrc_twin:
        lookup = trace_name_for_rrc(m.get("raw_name") or m.get("message_name"))
        m["trace_lookup"] = lookup
        sample = _replay(by_name.get(lookup)) if lookup else None
        if sample:
            m["trace_record"] = sample
            m["decoded"] = sample
            m["trace_source"] = "22_decoded"
            if m.get("json_raw"):
                m["srsran_decoded"] = m.get("json_raw")


def enrich_events(events: list[dict], rrc_twin: list[dict], by_name: dict[str, dict]) -> None:
    rrc_by_ts = {m["ts"]: m for m in rrc_twin if m.get("ts")}
    for e in events:
        layer = e.get("layer")
        lookup = None
        sample = None
        if layer == "RRC":
            hit = rrc_by_ts.get(e.get("ts"))
            if hit:
                lookup = hit.get("trace_lookup")
                sample = hit.get("trace_record")
            if not sample:
                lookup = trace_name_for_rrc(_rrc_raw_from_detail(e.get("detail", "")))
                sample = _replay(by_name.get(lookup)) if lookup else None
        elif layer == "NGAP":
            lookup = trace_name_for_ngap(e.get("detail", ""), e.get("label", ""))
            sample = _replay(by_name.get(lookup)) if lookup else None
        if lookup:
            e["trace_lookup"] = lookup
        if sample:
            e["decoded_trace"] = sample
            e["trace_source"] = "22_decoded"


def enrich_signaling_live(signaling: dict | None, by_name: dict[str, dict]) -> None:
    if not signaling:
        return
    for row in signaling.get("live") or []:
        lookup = row.get("trace_lookup") or row.get("message_name")
        sample = _replay(by_name.get(lookup)) if lookup else None
        if sample:
            row["message"] = sample
            row["source"] = "trace replay"
            row["trace_record"] = sample


def _rrc_raw_from_detail(detail: str) -> str | None:
    m = re.search(
        r"(?:Tx|Rx)\s+(?:\w+\s+)?(?:CCCH\s+)?(?:UL|DL\s+)?(\w+)\s*\(",
        detail or "",
    )
    return m.group(1) if m else None


def apply_trace_replay(
    rrc_twin: list[dict],
    events: list[dict],
    signaling: dict | None,
    *,
    ue_id: str = "srsTwin-ue",
    cell: str = "cell-1",
) -> tuple[list[dict], list[dict], dict | None]:
    """Attach 22_decoded-shaped records and wire UL messages into the transmit path."""
    by_name = _samples_from_signaling(signaling)
    if by_name:
        enrich_rrc_twin(rrc_twin, by_name)
        enrich_events(events, rrc_twin, by_name)
        enrich_signaling_live(signaling, by_name)

    from trace_transmit import wire_trace_transmit

    return wire_trace_transmit(rrc_twin, events, signaling, ue_id=ue_id, cell=cell)
