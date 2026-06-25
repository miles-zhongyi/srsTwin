#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
M0: the realizer's external interface — defined now, implemented M1+.

These two types are the ONLY thing a future state-machine layer touches.
It never sees UeContext internals, HARQ state, or PHY — it sends
TransmitIntents and receives DownlinkEvents, routed by ue_id/rnti.

Shaped to match the dashboard's existing live-event schema
(dashboard/parse_4g.py: mk_ev() -> {ts, src, dst, layer, label, detail, ...})
on purpose, so a state-machine layer consuming realizer events feels like
the same data model the dashboard already renders for the single-UE path.

Status: interface only. Nothing in M1-M4 constructs these from a live
process yet — M1 uses srsUE's native ASN.1 encoder directly, so
per_encoded_bytes stays None until M5 wires up the pycrate byte-injector
(see PLAN.md section 1, "M1 uses srsUE's native encoder").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TransmitIntent:
    """One per-UE uplink message request, handed to the realizer.

    M1-M4: only ue_id, channel, and procedure_tag are populated — the
    realizer's per-UE mac/rrc/nas (srsUE's own stack) builds the actual
    bytes with its native encoder, same as the single-UE path today.

    M5: per_encoded_bytes becomes populated (pycrate UPER-encoded, reusing
    encode_templates.py's ASN.1-tree-walking logic as a library) and the
    realizer transmits those bytes instead of asking the native encoder to
    build them.
    """
    ue_id: str
    channel: str                          # "SRB0" | "SRB1" | "SRB2" | "DTCH"
    procedure_tag: str                    # e.g. "rrc_conn_request" — logging/correlation only, not parsed
    per_encoded_bytes: bytes | None = None  # None until M5


@dataclass
class DownlinkEvent:
    """One per-UE downlink occurrence, surfaced out of the realizer.

    Generated wherever srsUE's RRC/MAC already logs the equivalent event
    today (paging, RRC reconfiguration, contention resolution, ...) — M1's
    job is routing these by RNTI to the correct UeContext, not inventing
    new protocol events. A future state-machine layer reacts to these; it
    is not built as part of this plan.
    """
    ue_id: str
    rnti: int
    event_type: str                       # "paging" | "rrc_reconfig" | "contention_resolution" | ...
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0


# Event types this plan's milestones are expected to emit by M3. Not
# exhaustive — a future state-machine layer should not assume this list is
# closed, but every milestone test in this plan checks against it so a
# silently-missing event type is caught early rather than discovered later.
KNOWN_EVENT_TYPES = frozenset({
    "paging",
    "rrc_reconfig",
    "contention_resolution",
    "rach_collision",       # two UE contexts chose the same preamble in the same occasion (modeled, not a bug)
    "attach_complete",
    "detach",
})


def validate_event(ev: DownlinkEvent) -> list[str]:
    """Cheap shape/sanity checks. Returns a list of problems (empty == ok)."""
    problems = []
    if not ev.ue_id:
        problems.append("ue_id is empty")
    if ev.rnti <= 0 or ev.rnti > 0xFFFF:
        problems.append(f"rnti {ev.rnti} out of valid 16-bit range")
    if ev.event_type not in KNOWN_EVENT_TYPES:
        problems.append(f"event_type {ev.event_type!r} not in KNOWN_EVENT_TYPES (informational, not fatal)")
    return problems
