#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""M0 test: subscriber generator produces N distinct, srsEPC-compatible identities."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gen_user_db import generate_subscribers, write_user_db_csv, write_usim_json  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {PASS if cond else FAIL}  {name}")
    if not cond:
        failures.append(name)


def main() -> int:
    print("=== gen_user_db ===")

    subs8 = generate_subscribers(8, seed=42)
    check("count == 8", len(subs8) == 8)
    check("IMSIs are unique", len({s["imsi"] for s in subs8}) == 8)
    check("Ki are unique", len({s["key"] for s in subs8}) == 8)
    check("OPc are unique", len({s["opc"] for s in subs8}) == 8)
    check("Ki is 32 hex chars", all(len(s["key"]) == 32 for s in subs8))
    check("OPc is 32 hex chars", all(len(s["opc"]) == 32 for s in subs8))
    check("IMSIs sequential from base", [int(s["imsi"]) for s in subs8] ==
          [302221000000001 + i for i in range(8)])

    subs8_again = generate_subscribers(8, seed=42)
    check("same seed -> identical identities (reproducible provisioning)",
          subs8 == subs8_again)

    subs8_other_seed = generate_subscribers(8, seed=43)
    check("different seed -> different keys", subs8[0]["key"] != subs8_other_seed[0]["key"])

    sub1 = generate_subscribers(1, seed=0)
    check("count=1 imsi matches the existing single-UE subscriber",
          sub1[0]["imsi"] == "302221000000001")

    with tempfile.TemporaryDirectory() as td:
        csv_path = os.path.join(td, "user_db.csv")
        json_path = os.path.join(td, "usims.json")
        write_user_db_csv(subs8, csv_path)
        write_usim_json(subs8, json_path)

        with open(csv_path, encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f if l.strip()]
        check("csv header matches existing 4g_configs/subscribers.csv format",
              lines[0] == "# Name,Auth,IMSI,Key,OP_Type,OP/OPc,AMF,SQN,QCI,IP_alloc")
        check("csv has 1 header + 8 data rows", len(lines) == 9)
        check("csv rows have 10 comma-separated fields",
              all(len(l.split(",")) == 10 for l in lines[1:]))

        import json as jsonlib
        with open(json_path, encoding="utf-8") as f:
            usims = jsonlib.load(f)
        check("usim json has 8 entries", len(usims) == 8)
        check("usim json ue_id is 0..7", [u["ue_id"] for u in usims] == list(range(8)))
        check("usim json imsi matches csv-side imsi",
              [u["imsi"] for u in usims] == [s["imsi"] for s in subs8])

    print(f"\n{'='*40}")
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print(f"{len(subs8) + 8} checks passed, ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
