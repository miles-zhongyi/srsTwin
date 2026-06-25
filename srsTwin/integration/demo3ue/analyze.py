#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Extract DU (eNB) processing delay and call duration from the captured
attach/release cycles, for both the 1-pair-alone and 3-pairs-concurrent
scenarios, and plot histograms.

DU processing delay = PRACH preamble (Msg1) -> Random Access Response (Msg2)
turnaround. Pure eNB-side: no EPC round-trip involved, the standard RACH
response time KPI. This is the delay_ms already computed on the Msg2 event
by parse_4g.py's _annotate_delays().

Call duration = attach_ms's complement -- specifically compute_attach_kpis()'s
session_ms: NAS Attach Complete -> the eNB's inactivity-triggered release
starting. Real measured quantity here because run_cycles.py --wait-release
polled for the actual release instead of guessing a fixed sleep.

Usage (run from integration/):
  python3 demo3ue/analyze.py
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))
from parse_4g import build_4g  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def find_cycles(log_dir: str) -> list[tuple[int, int]]:
    """Return sorted (pair, cycle) tuples found in log_dir."""
    found = set()
    for f in glob.glob(os.path.join(log_dir, "pair*_cycle*_enb.log")):
        m = re.search(r"pair(\d+)_cycle(\d+)_enb\.log$", os.path.basename(f))
        if m:
            found.add((int(m.group(1)), int(m.group(2))))
    return sorted(found)


def analyze_cycle(log_dir: str, pair: int, cycle: int) -> dict:
    """Copy this cycle's pair{P}_cycle{N}_{enb,ue}.log into a temp dir under
    the filenames build_4g() expects, then reuse the already-validated
    parsing/KPI pipeline as-is."""
    with tempfile.TemporaryDirectory() as td:
        shutil.copy(os.path.join(log_dir, f"pair{pair}_cycle{cycle}_enb.log"),
                    os.path.join(td, "enb.log"))
        shutil.copy(os.path.join(log_dir, f"pair{pair}_cycle{cycle}_ue.log"),
                    os.path.join(td, "ue4g.log"))
        data = build_4g(td, None)

    msg2 = next((e for e in data["events"] if e["label"] == "Random Access Response (Msg2)"), None)
    du_delay_ms = msg2["delay_ms"] if msg2 else None

    kpis = data["kpis"]
    return {
        "pair": pair, "cycle": cycle,
        "du_delay_ms": du_delay_ms,
        "attach_ms": kpis.get("attach_ms"),
        "session_ms": kpis.get("session_ms"),
        "outcome": kpis.get("outcome"),
        "event_count": len(data["events"]),
    }


def analyze_scenario(log_dir: str, name: str) -> list[dict]:
    cycles = find_cycles(log_dir)
    print(f"\n=== {name}: {len(cycles)} (pair, cycle) samples in {log_dir} ===")
    rows = []
    for pair, cycle in cycles:
        r = analyze_cycle(log_dir, pair, cycle)
        rows.append(r)
        print(f"  pair{pair} cycle{cycle}: DU delay={r['du_delay_ms']}ms  "
              f"attach={r['attach_ms']}ms  session={r['session_ms']}ms  "
              f"outcome={r['outcome']}  events={r['event_count']}")
    return rows


def summarize(rows: list[dict], field: str) -> dict:
    vals = [r[field] for r in rows if r[field] is not None]
    if not vals:
        return {"n": 0, "mean": None, "min": None, "max": None}
    return {"n": len(vals), "mean": sum(vals) / len(vals), "min": min(vals), "max": max(vals)}


def main() -> int:
    one = analyze_scenario(os.path.join(HERE, "logs_1pair"), "1 pair alone")
    three = analyze_scenario(os.path.join(HERE, "logs_3pair"), "3 pairs concurrent")

    print("\n=== Summary ===")
    for name, rows in [("1 pair alone", one), ("3 pairs concurrent", three)]:
        du = summarize(rows, "du_delay_ms")
        sess = summarize(rows, "session_ms")
        print(f"\n{name}:")
        print(f"  DU processing delay (Msg1->Msg2): n={du['n']}  "
              f"mean={du['mean']:.2f}ms  min={du['min']:.2f}ms  max={du['max']:.2f}ms"
              if du["n"] else f"  DU processing delay: no samples")
        print(f"  Call duration (attach->release):  n={sess['n']}  "
              f"mean={sess['mean']:.1f}ms  min={sess['min']:.1f}ms  max={sess['max']:.1f}ms"
              if sess["n"] else f"  Call duration: no samples")

    import json
    out = {"one_pair": one, "three_pair": three}
    out_path = os.path.join(HERE, "results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved raw results -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
