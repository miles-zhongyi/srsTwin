#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Continuous background cycler — keeps the 4G pairs attaching/releasing
indefinitely so the dashboard has a steady stream of fresh call flows to
show live and to build KPI histograms from. This is what "make the UEs
live" means in practice: nothing about the live dashboard panel works
without something actually generating repeated call flows.

Reuses the exact recreate -> wait-attach -> wait-release pattern already
validated in run_cycles.py (recreating eNB+UE together every round — UE-only
recreate against a running eNB causes a RACH retry storm, see RESULTS.md).

Each round, for every pair: extract {du_delay_ms, attach_ms, session_ms,
outcome} (same fields as analyze.py) and append one JSON line to
dashboard/logs/kpi_history.jsonl. The dashboard reads that file directly —
this script and the dashboard server are independent processes; either can
be restarted without affecting the other.

This actively recreates containers in a loop — run it deliberately, not as
a background-forever habit. Stop with Ctrl+C.

Usage (run from integration/):
  python3 demo3ue/live_cycler.py --pairs 1,2,3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

from parse_4g import build_4g  # noqa: E402
from run_cycles import PAIR_SERVICES, recreate, wait_attach, wait_release  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(HERE, "..", "dashboard", "logs", "kpi_history.jsonl")


def pull_pair_logs(pair: int, dst_dir: str) -> None:
    import subprocess
    _, _, enb_c, ue_c = PAIR_SERVICES[pair]
    subprocess.run(["docker", "cp", f"{enb_c}:/tmp/enb.log", os.path.join(dst_dir, "enb.log")], check=True)
    subprocess.run(["docker", "cp", f"{ue_c}:/tmp/ue4g.log", os.path.join(dst_dir, "ue4g.log")], check=True)


def extract_kpi(pair: int) -> dict | None:
    with tempfile.TemporaryDirectory() as td:
        try:
            pull_pair_logs(pair, td)
        except Exception:
            return None
        data = build_4g(td, None)

    msg2 = next((e for e in data["events"] if e["label"] == "Random Access Response (Msg2)"), None)
    kpis = data["kpis"]
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pair": pair,
        "du_delay_ms": msg2["delay_ms"] if msg2 else None,
        "attach_ms": kpis.get("attach_ms"),
        "session_ms": kpis.get("session_ms"),
        "outcome": kpis.get("outcome"),
        "event_count": len(data["events"]),
    }


def append_history(samples: list[dict]) -> None:
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", default="1,2,3", help='comma-separated pair indices, e.g. "1" or "1,2,3"')
    ap.add_argument("--rounds", type=int, default=0, help="0 = run forever (Ctrl+C to stop)")
    ap.add_argument("--release-timeout-s", type=float, default=90.0)
    ap.add_argument("--attach-timeout-s", type=float, default=60.0)
    args = ap.parse_args()

    pairs = [int(x) for x in args.pairs.split(",")]
    print(f"Live cycler: pairs={pairs} -> {HISTORY_PATH}")
    print("Ctrl+C to stop. This recreates eNB+UE containers every round — "
          "don't run it if you need those pairs to stay untouched.")

    round_n = 0
    try:
        while args.rounds == 0 or round_n < args.rounds:
            round_n += 1
            t0 = time.time()
            print(f"\n=== round {round_n} (pairs={pairs}) ===")
            recreate(pairs)
            timed_out = wait_attach(pairs, args.attach_timeout_s)
            attached = [p for p in pairs if p not in timed_out]
            if timed_out:
                print(f"  pairs {sorted(timed_out)} did not attach in time")
            not_released = wait_release(attached, args.release_timeout_s)
            if not_released:
                print(f"  pairs {sorted(not_released)} did not release in time")

            samples = [s for p in attached if (s := extract_kpi(p)) is not None]
            append_history(samples)
            for s in samples:
                print(f"  pair{s['pair']}: du_delay={s['du_delay_ms']}ms "
                      f"attach={s['attach_ms']}ms session={s['session_ms']}ms outcome={s['outcome']}")
            print(f"  round wall time: {time.time() - t0:.1f}s")
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
