#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Force N clean attach/release cycles for one or more eNB+UE pairs, saving
per-cycle logs for DU-processing-delay / call-duration analysis.

Recreates BOTH the eNB and UE together for each cycle (not just the UE) —
testing this empirically showed that recreating the UE alone against an
already-running eNB causes a RACH retry storm (multiple simultaneous
preamble detections that never settle into a clean attach). Full
force-recreate of both sides every cycle is the reliable pattern; it's
also exactly what integration/README.md's gotchas section already warns:
"don't partial-restart mid-link."

Usage (run from integration/):
  python3 demo3ue/run_cycles.py --pairs 1 --cycles 8 --out demo3ue/logs_1pair
  python3 demo3ue/run_cycles.py --pairs 1,2,3 --cycles 8 --out demo3ue/logs_3pair
"""
from __future__ import annotations

import argparse
import os
import subprocess
import time

# pair index -> (enb service, ue service, enb container, ue container)
PAIR_SERVICES = {
    1: ("srsenb",  "srsue4g",  "srstwin_enb",  "srstwin_ue4g"),
    2: ("srsenb2", "srsue4g2", "srstwin_enb2", "srstwin_ue4g2"),
    3: ("srsenb3", "srsue4g3", "srstwin_enb3", "srstwin_ue4g3"),
}

COMPOSE = ["docker", "compose", "-f", "docker-compose.4g.yml", "-f", "docker-compose.3ue.yml"]


def recreate(pairs: list[int]) -> None:
    svcs = []
    for p in pairs:
        enb_svc, ue_svc, _, _ = PAIR_SERVICES[p]
        svcs += [enb_svc, ue_svc]
    subprocess.run(COMPOSE + ["up", "-d", "--force-recreate"] + svcs, check=True,
                   capture_output=True, text=True)


def wait_attach(pairs: list[int], timeout_s: float = 30.0) -> set[int]:
    """Poll each pair's UE stdout for 'Network attach successful'.

    Returns the set of pairs that did NOT attach within timeout_s.
    """
    deadline = time.time() + timeout_s
    pending = set(pairs)
    while pending and time.time() < deadline:
        for p in list(pending):
            _, _, _, ue_c = PAIR_SERVICES[p]
            out = subprocess.run(["docker", "logs", ue_c], capture_output=True, text=True).stdout
            if "Network attach successful" in out:
                pending.discard(p)
        if pending:
            time.sleep(1)
    return pending


def wait_release(pairs: list[int], timeout_s: float = 50.0) -> set[int]:
    """Poll each pair's eNB stdout for the inactivity-triggered release
    ("Disconnecting rnti=...") so call duration (attach -> release) is a
    real measured quantity, not a guessed fixed sleep. srsenb's default
    inactivity timer is 30s; timeout_s should clear that with margin.

    Returns the set of pairs that did NOT release within timeout_s.
    """
    deadline = time.time() + timeout_s
    pending = set(pairs)
    while pending and time.time() < deadline:
        for p in list(pending):
            _, _, enb_c, _ = PAIR_SERVICES[p]
            out = subprocess.run(["docker", "logs", enb_c], capture_output=True, text=True).stdout
            if "Disconnecting rnti=" in out:
                pending.discard(p)
        if pending:
            time.sleep(1)
    return pending


def pull_logs(pairs: list[int], out_dir: str, cycle: int) -> None:
    for p in pairs:
        _, _, enb_c, ue_c = PAIR_SERVICES[p]
        subprocess.run(["docker", "cp", f"{enb_c}:/tmp/enb.log",
                         os.path.join(out_dir, f"pair{p}_cycle{cycle}_enb.log")], check=True)
        subprocess.run(["docker", "cp", f"{ue_c}:/tmp/ue4g.log",
                         os.path.join(out_dir, f"pair{p}_cycle{cycle}_ue.log")], check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", required=True, help='comma-separated pair indices, e.g. "1" or "1,2,3"')
    ap.add_argument("--cycles", type=int, default=8)
    ap.add_argument("--attach-timeout-s", type=float, default=30.0)
    ap.add_argument("--wait-release", action="store_true",
                     help="poll for the eNB's inactivity-triggered release instead of a "
                          "fixed idle sleep, so call duration is a real measured quantity")
    ap.add_argument("--release-timeout-s", type=float, default=50.0,
                     help="srsenb's default inactivity timer is 30s; needs margin above that")
    ap.add_argument("--idle-s", type=float, default=5.0,
                     help="fixed hold time before pulling logs, used only without --wait-release")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    pairs = [int(x) for x in args.pairs.split(",")]
    os.makedirs(args.out, exist_ok=True)

    results = []
    for cycle in range(1, args.cycles + 1):
        t0 = time.time()
        print(f"=== cycle {cycle}/{args.cycles} (pairs={pairs}) ===")
        recreate(pairs)
        timed_out = wait_attach(pairs, args.attach_timeout_s)
        attach_wall_s = time.time() - t0
        if timed_out:
            print(f"  WARNING: pairs {sorted(timed_out)} did not attach within "
                  f"{args.attach_timeout_s}s (wall {attach_wall_s:.1f}s)")
        else:
            print(f"  all pairs attached (wall {attach_wall_s:.1f}s)")

        if args.wait_release:
            attached_pairs = [p for p in pairs if p not in timed_out]
            t1 = time.time()
            not_released = wait_release(attached_pairs, args.release_timeout_s)
            release_wall_s = time.time() - t1
            if not_released:
                print(f"  WARNING: pairs {sorted(not_released)} did not release within "
                      f"{args.release_timeout_s}s (wall {release_wall_s:.1f}s)")
            else:
                print(f"  all pairs released (wall {release_wall_s:.1f}s)")
        else:
            time.sleep(args.idle_s)

        pull_logs(pairs, args.out, cycle)
        results.append({"cycle": cycle, "pairs": pairs, "timed_out": sorted(timed_out),
                         "attach_wall_s": round(attach_wall_s, 1)})

    print("\nDone.")
    for r in results:
        flag = " <-- TIMED OUT" if r["timed_out"] else ""
        print(f"  cycle {r['cycle']}: attach_wall_s={r['attach_wall_s']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
