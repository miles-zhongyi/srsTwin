#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
M0 tooling, used starting at M1: diff the current N=1 attach sequence
against the frozen baseline (capture_n1_baseline.py).

This is the hard regression gate for PLAN.md risk #6 — run it after any
change to srsue's MAC/PHY wiring, with num_ues=1, before considering that
change done. A clean diff is required, not optional.

Usage:
  python3 check_n1_baseline.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from capture_n1_baseline import HERE, BASELINE_PATH, capture, pull_logs


def diff_sequences(base_seq: list[dict], cur_seq: list[dict]) -> tuple[bool, list[str]]:
    """Pure comparison, no docker/filesystem — testable without live containers.

    Returns (ok, diff_lines). ok is True iff the sequences are identical.
    """
    if base_seq == cur_seq:
        return True, []
    lines = []
    max_len = max(len(base_seq), len(cur_seq))
    for i in range(max_len):
        b = base_seq[i] if i < len(base_seq) else None
        c = cur_seq[i] if i < len(cur_seq) else None
        if b == c:
            continue
        lines.append(f"  [{i}] baseline: {b}")
        lines.append(f"  [{i}] current:  {c}")
    return False, lines


def main() -> int:
    if not BASELINE_PATH.exists():
        print(f"No baseline at {BASELINE_PATH} — run capture_n1_baseline.py first.", file=sys.stderr)
        return 2

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    log_dir = HERE / "_capture_logs"
    print("Pulling fresh logs ...")
    pull_logs(log_dir)
    print("Parsing current attach sequence ...")
    current = capture(log_dir)

    ok, diff_lines = diff_sequences(baseline["sequence"], current["sequence"])

    if ok:
        print(f"PASS — N=1 attach sequence unchanged ({len(current['sequence'])} events, "
              f"outcome={current['outcome']!r})")
        return 0

    print("FAIL — N=1 attach sequence differs from baseline:\n")
    print("\n".join(diff_lines))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
