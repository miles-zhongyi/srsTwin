#!/usr/bin/env python3
"""Verify trace replay is wired into SignalingDispatcher transmit path."""
from __future__ import annotations

import json
import sys

from parse_callflow import build
from parse_rrc import build_rrc
from parse_signaling import build_signaling
from trace_replay import apply_trace_replay


def main() -> int:
    log_dir = sys.argv[1] if len(sys.argv) > 1 else "logs"
    events, _meta = build(log_dir)
    rrc_twin, _rrc_trace, _rrc_meta = build_rrc(log_dir)
    signaling = build_signaling(rrc_twin, events)
    rrc_twin, events, signaling = apply_trace_replay(rrc_twin, events, signaling)

    plan = signaling.get("transmit_plan", {})
    meta_tx = plan.get("meta", {})
    msgs = plan.get("messages", [])
    print(f"transmit_wired: {meta_tx.get('wired')}")
    print(f"transmit_count: {meta_tx.get('count')}")
    if meta_tx.get("wired", 0) <= 0:
        print("FAIL: expected UL messages wired")
        return 1

    for m in msgs:
        rec = m["record"]
        assert "decoded" in rec, m["logical"]
        assert "_twin" in rec, m["logical"]
        assert rec["_twin"].get("signaling_source"), m["logical"]
        assert "<<" not in json.dumps(rec), f"unfilled tokens in {m['logical']}"

    tx_rrc = [m for m in rrc_twin if m.get("transmit_record")]
    print(f"rrc_twin with transmit_record: {len(tx_rrc)}")
    if not tx_rrc:
        print("FAIL: expected transmit_record on UL RRC rows")
        return 1

    live_tx = [r for r in signaling["live"] if r.get("source") == "trace transmit"]
    print(f"signaling live trace transmit: {len(live_tx)}")
    if not live_tx:
        print("FAIL: expected trace transmit on live signaling rows")
        return 1

    html = open("index.html", encoding="utf-8").read()
    tabs = html.split('<nav class="tabs">')[1].split("</nav>")[0]
    if "Overview</button>" not in tabs or not tabs.strip().endswith("Overview</button>"):
        print("FAIL: Overview tab should be rightmost")
        return 1
    if 'class="panel on" id="panel-callflow"' not in html:
        print("FAIL: Signaling flow should be default active tab")
        return 1
    print("tab order: OK")
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
