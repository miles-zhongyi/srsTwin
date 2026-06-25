#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
M0: capture the current single-UE (N=1) attach signaling sequence as a
regression baseline.

Per PLAN.md risk #6 ("N=1 regression"): any refactor that turns scalar MAC
members into N-indexed collections risks subtly changing single-UE
behavior even when N=1. This script freezes what N=1 looks like *today*
(before any srsue source is touched) so check_n1_baseline.py can later
prove M1's refactor left it unchanged.

Compares the ordered (layer, label) sequence of the most recent complete
attach cycle — not exact timestamps, which are never bit-identical across
runs, but message identity and order, which must be.

Usage (run against the live srstwin_ue4g/srstwin_enb/srstwin_epc containers):
  python3 capture_n1_baseline.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASHBOARD = HERE.parent / "dashboard"
sys.path.insert(0, str(DASHBOARD))

from parse_4g import build_4g, split_attach_procedures  # noqa: E402

BASELINE_PATH = HERE / "baselines" / "n1_attach_baseline.json"


def pull_logs(log_dir: Path) -> None:
    log_dir.mkdir(exist_ok=True)
    for svc, src, dst in [
        ("srstwin_ue4g", "/tmp/ue4g.log", "ue4g.log"),
        ("srstwin_enb",  "/tmp/enb.log",  "enb.log"),
    ]:
        subprocess.run(
            ["docker", "cp", f"{svc}:{src}", str(log_dir / dst)],
            check=True, capture_output=True, text=True,
        )


def capture(log_dir: Path) -> dict:
    data = build_4g(str(log_dir), None)
    events = data["events"]
    groups = split_attach_procedures(events)
    if not groups:
        raise RuntimeError("no attach procedure found in current logs — "
                            "make sure the 4G stack has completed at least one attach")

    last = groups[-1]
    ordered = sorted(last, key=lambda e: (e.get("flow_rank", 8000), e.get("ts", "")))
    sequence = [{"layer": e["layer"], "label": e["label"]} for e in ordered]

    outcome = data["kpis"]["outcome"]
    if outcome != "attached":
        print(f"WARNING: most recent attach cycle outcome is {outcome!r}, not 'attached'. "
              f"Capturing it anyway, but you may want a cleaner run for the baseline.",
              file=sys.stderr)

    return {
        "outcome": outcome,
        "event_count": len(sequence),
        "sequence": sequence,
    }


def main() -> int:
    log_dir = HERE / "_capture_logs"
    print("Pulling fresh logs from srstwin_ue4g / srstwin_enb ...")
    pull_logs(log_dir)

    print("Parsing attach sequence ...")
    baseline = capture(log_dir)

    BASELINE_PATH.parent.mkdir(exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2), encoding="utf-8")

    print(f"Captured {baseline['event_count']} events, outcome={baseline['outcome']!r}")
    print(f"Saved baseline -> {BASELINE_PATH}")
    for ev in baseline["sequence"]:
        print(f"  {ev['layer']:<6} {ev['label']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
