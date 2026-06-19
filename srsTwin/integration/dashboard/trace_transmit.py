#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""Wire trace replay into the SignalingDispatcher transmit path (22_decoded-shaped PDUs)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from parse_signaling import NGAP_TO_S1, NR_RRC_TO_LTE
from signaling_sources import get_signaling_dispatcher

_DEFAULT_UE = "srsTwin-ue"
_DEFAULT_CELL = "cell-1"


def _lte_to_logical(dispatcher) -> dict[str, str]:
    return {wire: logical for logical, wire in dispatcher.MESSAGE_NAMES.items()}


def logical_for_trace_name(trace_name: str, lte_to_logical: dict[str, str]) -> str | None:
    return lte_to_logical.get(trace_name)


def logical_for_rrc(raw_name: str | None, lte_to_logical: dict[str, str]) -> str | None:
    if not raw_name:
        return None
    lte = NR_RRC_TO_LTE.get(raw_name, raw_name)
    return logical_for_trace_name(lte, lte_to_logical)


def logical_for_ngap(raw_name: str | None, lte_to_logical: dict[str, str]) -> str | None:
    if not raw_name:
        return None
    lte = NGAP_TO_S1.get(raw_name, raw_name)
    return logical_for_trace_name(lte, lte_to_logical)


def _step_name(_dispatcher, logical: str) -> str | None:
    try:
        from common.signaling import procedures as proc  # noqa: WPS433
    except ImportError:
        return None
    step = proc.STEP_BY_UPLINK.get(logical)
    return step.name if step else None


def build_transmit_record(
    dispatcher,
    logical: str,
    *,
    ue_id: str = _DEFAULT_UE,
    cell: str = _DEFAULT_CELL,
    source: str | None = None,
) -> dict:
    """Build a dispatcher transmit record — same path as poc_StressTest ue_sim."""
    kwargs: dict = {"ue_id": ue_id, "cell": cell}
    step = _step_name(dispatcher, logical)
    if step:
        kwargs["step"] = step
    if source is not None:
        kwargs["source"] = source
    return dispatcher.build(logical, **kwargs)


def _is_ue_ul(m: dict) -> bool:
    if m.get("direction") != "Tx":
        return False
    side = (m.get("side") or "").upper()
    source = (m.get("source") or "").lower()
    return side == "UE" or "srsue" in source or source == "ue"


def _plan_entry(m: dict, logical: str, record: dict) -> dict:
    twin = record.get("_twin") or {}
    return {
        "ts": m.get("ts"),
        "t": m.get("t"),
        "logical": logical,
        "message_name": record.get("message_name"),
        "record_id": record.get("record_id"),
        "direction": "UL",
        "channel": m.get("channel"),
        "signaling_source": twin.get("signaling_source"),
        "record": record,
    }


def wire_trace_transmit(
    rrc_twin: list[dict],
    events: list[dict],
    signaling: dict | None,
    *,
    ue_id: str = _DEFAULT_UE,
    cell: str = _DEFAULT_CELL,
) -> tuple[list[dict], list[dict], dict | None]:
    """Replace catalog-only replays with dispatcher-built transmit records for UL messages."""
    if signaling is None:
        signaling = {}

    dispatcher, err = get_signaling_dispatcher()
    plan: list[dict] = []
    meta: dict = {
        "dispatcher": "SignalingDispatcher",
        "ue_id": ue_id,
        "cell": cell,
        "wired": 0,
    }

    if dispatcher is None:
        meta["error"] = err
        signaling["transmit_plan"] = {"meta": meta, "messages": plan}
        return rrc_twin, events, signaling

    lte_to_logical = _lte_to_logical(dispatcher)
    by_ts: dict[str, dict] = {}

    for m in rrc_twin:
        if not _is_ue_ul(m):
            continue
        lookup = m.get("trace_lookup") or NR_RRC_TO_LTE.get(
            m.get("raw_name") or m.get("message_name") or "", ""
        )
        logical = logical_for_rrc(m.get("raw_name") or m.get("message_name"), lte_to_logical)
        if not logical:
            continue
        record = build_transmit_record(dispatcher, logical, ue_id=ue_id, cell=cell)
        m["transmit_record"] = record
        m["trace_record"] = copy.deepcopy(record)
        m["decoded"] = record
        m["trace_source"] = "trace_transmit"
        m["trace_lookup"] = lookup or record.get("message_name")
        if m.get("json_raw") and not m.get("srsran_decoded"):
            m["srsran_decoded"] = m.get("json_raw")
        if m.get("ts"):
            by_ts[m["ts"]] = m
        plan.append(_plan_entry(m, logical, record))
        meta["wired"] += 1

    for e in events:
        if e.get("layer") != "RRC" or e.get("src") != "UE":
            continue
        hit = by_ts.get(e.get("ts", ""))
        if hit and hit.get("transmit_record"):
            e["transmit_record"] = hit["transmit_record"]
            e["decoded_trace"] = hit["transmit_record"]
            e["trace_source"] = "trace_transmit"
            continue
        raw = _rrc_raw_from_detail(e.get("detail", ""))
        logical = logical_for_rrc(raw, lte_to_logical)
        if not logical:
            continue
        record = build_transmit_record(dispatcher, logical, ue_id=ue_id, cell=cell)
        e["transmit_record"] = record
        e["decoded_trace"] = record
        e["trace_source"] = "trace_transmit"
        e["trace_lookup"] = NR_RRC_TO_LTE.get(raw, raw) if raw else record.get("message_name")
        plan.append(_plan_entry(e, logical, record))
        meta["wired"] += 1

    for row in signaling.get("live") or []:
        if row.get("protocol") != "RRC":
            continue
        if row.get("direction") not in ("Tx", "UE→DU", "UL"):
            continue
        transmit = None
        if row.get("ts") in by_ts:
            transmit = by_ts[row["ts"]].get("transmit_record")
        if not transmit:
            logical = logical_for_rrc(row.get("raw_name") or row.get("message_name"), lte_to_logical)
            if logical:
                transmit = build_transmit_record(dispatcher, logical, ue_id=ue_id, cell=cell)
        if not transmit:
            continue
        row["transmit_record"] = transmit
        row["message"] = transmit
        row["source"] = "trace transmit"
        row["trace_record"] = copy.deepcopy(transmit)
        twin = transmit.get("_twin") or {}
        row["signaling_source"] = twin.get("signaling_source")

    plan.sort(key=lambda x: (x.get("ts") or "", x.get("logical") or ""))
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for entry in plan:
        key = (entry.get("ts") or "", entry.get("logical") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    plan = deduped
    meta["wired"] = len(plan)
    meta["count"] = len(plan)
    signaling["transmit_plan"] = {"meta": meta, "messages": plan}
    signaling.setdefault("meta", {})["transmit_wired"] = meta["wired"]
    return rrc_twin, events, signaling


def _rrc_raw_from_detail(detail: str) -> str | None:
    import re

    m = re.search(
        r"(?:Tx|Rx)\s+(?:\w+\s+)?(?:CCCH\s+)?(?:UL|DL\s+)?(\w+)\s*\(",
        detail or "",
    )
    return m.group(1) if m else None


def write_transmit_plan(signaling: dict | None, path: str | Path) -> int:
    """Persist transmit plan as JSONL (one framed PDU per line). Returns lines written."""
    if not signaling:
        return 0
    plan = signaling.get("transmit_plan") or {}
    messages = plan.get("messages") or []
    if not messages:
        return 0
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for entry in messages:
            record = entry.get("record")
            if not record:
                continue
            line = {
                "ts": entry.get("ts"),
                "t": entry.get("t"),
                "logical": entry.get("logical"),
                "message_name": entry.get("message_name"),
                "record_id": entry.get("record_id"),
                "signaling_source": entry.get("signaling_source"),
                "message": record,
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return len(messages)


def default_transmit_plan_path(log_dir: str | None = None) -> Path:
    env = os.environ.get("TRACE_TRANSMIT_PATH", "").strip()
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    base = Path(log_dir) if log_dir else here / "logs"
    return base / "transmit_plan.jsonl"
