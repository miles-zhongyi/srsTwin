#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
M0: generate N distinct subscriber identities for the multi-UE realizer.

One source of truth, two consumers:
  - srsEPC's `user_db.csv` (this script's output)
  - the realizer's N `usim_base` instances at startup (M1+, reads the same JSON)

This does NOT touch the existing single-UE `4g_configs/subscribers.csv` —
that file stays authoritative for num_ues=1 (the unchanged legacy path).
This generator is new tooling that only matters once num_ues>1.

Usage:
  python3 gen_user_db.py --count 8 --out subscribers.multi.csv --out-json subscribers.multi.json
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

# Matches integration/4g_configs/subscribers.csv exactly.
_HEADER = "# Name,Auth,IMSI,Key,OP_Type,OP/OPc,AMF,SQN,QCI,IP_alloc"

# TELUS test PLMN used throughout this twin (302/221). IMSIs are sequential
# off the same base the single-UE config already uses, so a generated UE 0
# is byte-identical to today's lone subscriber when --count 1 is requested.
_IMSI_BASE = 302221000000001
_AMF = "9001"
_SQN0 = "000000000000"   # fresh subscriber, never authenticated — not the
                         # in-memory SQN srsEPC tracks after first auth
_QCI = "7"


def _hexkey(rng: random.Random, nbytes: int = 16) -> str:
    return "".join(f"{rng.randint(0, 255):02x}" for _ in range(nbytes))


def generate_subscribers(count: int, seed: int = 0) -> list[dict]:
    """Deterministic for a given (count, seed) — same inputs, same identities,
    so re-provisioning srsEPC and the realizer from the same call always agree."""
    if count < 1:
        raise ValueError("count must be >= 1")
    rng = random.Random(seed)
    subs = []
    for i in range(count):
        subs.append({
            "name":    f"telus_ue{i + 1}",
            "auth":    "mil",
            "imsi":    str(_IMSI_BASE + i),
            "key":     _hexkey(rng),
            "op_type": "opc",
            "opc":     _hexkey(rng),
            "amf":     _AMF,
            "sqn":     _SQN0,
            "qci":     _QCI,
            "ip_alloc": "dynamic",
        })
    return subs


def write_user_db_csv(subs: list[dict], out_path: str) -> None:
    lines = [_HEADER]
    for s in subs:
        lines.append(",".join([
            s["name"], s["auth"], s["imsi"], s["key"], s["op_type"],
            s["opc"], s["amf"], s["sqn"], s["qci"], s["ip_alloc"],
        ]))
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_usim_json(subs: list[dict], out_path: str) -> None:
    """Realizer-side source: one usim credential block per logical UE,
    keyed by a stable ue_id (index) so M1's UeContext provisioning and
    srsEPC's user_db.csv never drift apart."""
    out = [
        {
            "ue_id": i,
            "imsi":  s["imsi"],
            "k":     s["key"],
            "opc":   s["opc"],
            "imei":  f"35349006987{3310 + i:04d}",
        }
        for i, s in enumerate(subs)
    ]
    Path(out_path).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, required=True, help="number of logical UEs (num_ues)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="subscribers.multi.csv", help="srsEPC user_db.csv output")
    ap.add_argument("--out-json", default="subscribers.multi.json", help="realizer usim source output")
    args = ap.parse_args()

    subs = generate_subscribers(args.count, args.seed)
    write_user_db_csv(subs, args.out)
    write_usim_json(subs, args.out_json)

    print(f"Generated {len(subs)} subscribers (seed={args.seed})")
    print(f"  srsEPC user_db -> {args.out}")
    print(f"  realizer usim source -> {args.out_json}")
    for s in subs:
        print(f"    ue_id={subs.index(s)} imsi={s['imsi']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
