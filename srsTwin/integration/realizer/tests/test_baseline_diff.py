#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""M0 test: the N=1 regression diff logic, in isolation from docker/live containers."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from check_n1_baseline import diff_sequences  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {PASS if cond else FAIL}  {name}")
    if not cond:
        failures.append(name)


def main() -> int:
    print("=== check_n1_baseline.diff_sequences ===")

    seq = [{"layer": "RRC", "label": "SIB1"}, {"layer": "PHY", "label": "PRACH preamble (Msg1)"}]

    ok, lines = diff_sequences(seq, list(seq))
    check("identical sequences -> ok, no diff lines", ok is True and lines == [])

    reordered = [seq[1], seq[0]]
    ok, lines = diff_sequences(seq, reordered)
    check("reordered sequence -> not ok", ok is False)
    check("reordered sequence -> diff lines mention both positions", len(lines) == 4)

    truncated = seq[:1]
    ok, lines = diff_sequences(seq, truncated)
    check("shorter current sequence -> not ok (missing event detected)", ok is False)
    check("missing trailing event shows current=None", any("current:  None" in l for l in lines))

    extra = seq + [{"layer": "NAS", "label": "NAS Attach Request"}]
    ok, lines = diff_sequences(seq, extra)
    check("longer current sequence -> not ok (extra event detected)", ok is False)
    check("extra leading baseline shows baseline=None", any("baseline: None" in l for l in lines))

    relabeled = [seq[0], {"layer": "PHY", "label": "PRACH preamble (Msg1) RETRY"}]
    ok, lines = diff_sequences(seq, relabeled)
    check("relabeled event -> not ok", ok is False)
    check("relabeled event -> exactly one position differs", len(lines) == 2)

    print(f"\n{'='*40}")
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
