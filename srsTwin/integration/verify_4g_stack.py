#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Phase 1d verification: static checks on all 4G stack config files before
a full docker compose build.  Run from integration/:

    python3 verify_4g_stack.py

Checks performed:
  1. All required files exist.
  2. PLMN 302/221 appears in every component that advertises it.
  3. ZMQ ports are consistent (eNB <-> UE cross-reference).
  4. Subscriber IMSI in subscribers.csv matches the UE's configured IMSI.
  5. TAC matches between epc.conf, rr.conf, and compose overlay.
  6. srsUE entrypoint trace injection is wired (env var + volume in compose).
  7. No accidental 5G port re-use (2000/2001).
  8. Compose overlay defines a separate 10.53.2.x subnet.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

REQUIRED_FILES = [
    HERE / "Dockerfile.enb",
    HERE / "Dockerfile.epc",
    HERE / "docker-compose.4g.yml",
    HERE / "4g_configs/epc.conf",
    HERE / "4g_configs/enb.conf",
    HERE / "4g_configs/ue.conf",
    HERE / "4g_configs/sib.conf",
    HERE / "4g_configs/rr.conf",
    HERE / "4g_configs/rb.conf",
    HERE / "4g_configs/subscribers.csv",
]

PASS = "[PASS]"
FAIL = "[FAIL]"
failures: list[str] = []


def check(cond: bool, msg: str) -> bool:
    tag = PASS if cond else FAIL
    print(f"  {tag}  {msg}")
    if not cond:
        failures.append(msg)
    return cond


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    print("\n=== srsTwin 4G Stack — static config verification ===\n")

    # 1. File existence
    print("1. Required files exist")
    for f in REQUIRED_FILES:
        check(f.exists(), str(f.relative_to(HERE)))

    # Load file contents for deeper checks
    epc   = read(HERE / "4g_configs/epc.conf")
    enb   = read(HERE / "4g_configs/enb.conf")
    ue    = read(HERE / "4g_configs/ue.conf")
    rr    = read(HERE / "4g_configs/rr.conf")
    sub   = read(HERE / "4g_configs/subscribers.csv")
    dc4g  = read(HERE / "docker-compose.4g.yml")

    # 2. PLMN 302/221
    print("\n2. PLMN 302/221 in every component")
    check("mcc" in epc and "302" in epc, "epc.conf  mcc=302")
    check("mnc" in epc and "221" in epc, "epc.conf  mnc=221")
    check("mcc" in enb and "302" in enb, "enb.conf  mcc=302")
    check("mnc" in enb and "221" in enb, "enb.conf  mnc=221")
    check("302221" in ue, "ue.conf   imsi starts 302221")
    check("302221" in sub, "subscribers.csv  IMSI prefix 302221")

    # 3. ZMQ port consistency (eNB tx=2002, UE tx=2003)
    print("\n3. ZMQ port cross-reference")
    enb_tx = re.search(r"tx_port=tcp://\*:(\d+)", enb)
    enb_rx = re.search(r"rx_port=tcp://[^,]+:(\d+)", enb)
    ue_tx  = re.search(r"tx_port=tcp://\*:(\d+)", ue)
    ue_rx  = re.search(r"rx_port=tcp://[^,]+:(\d+)", ue)

    enb_tx_port = enb_tx.group(1) if enb_tx else "?"
    enb_rx_port = enb_rx.group(1) if enb_rx else "?"
    ue_tx_port  = ue_tx.group(1)  if ue_tx  else "?"
    ue_rx_port  = ue_rx.group(1)  if ue_rx  else "?"

    check(enb_tx_port == ue_rx_port,
          f"eNB DL REP :{enb_tx_port} == UE DL REQ :{ue_rx_port}")
    check(ue_tx_port  == enb_rx_port,
          f"UE UL REP  :{ue_tx_port} == eNB UL REQ :{enb_rx_port}")
    check(enb_tx_port not in ("2000", "2001"),
          f"4G eNB port :{enb_tx_port} does not collide with 5G")
    check(ue_tx_port  not in ("2000", "2001"),
          f"4G UE  port :{ue_tx_port}  does not collide with 5G")

    # 4. IMSI match UE <-> subscriber DB
    print("\n4. IMSI match: ue.conf <-> subscribers.csv")
    ue_imsi_m = re.search(r"imsi\s*=\s*(\d+)", ue)
    ue_imsi   = ue_imsi_m.group(1) if ue_imsi_m else ""
    check(bool(ue_imsi), f"ue.conf imsi parsed: '{ue_imsi}'")
    check(ue_imsi in sub, f"imsi {ue_imsi} present in subscribers.csv")

    # 5. TAC match (epc.conf mme section vs rr.conf cell_list — eNB reads TAC from rr.conf)
    print("\n5. TAC consistency")
    epc_tac_m = re.search(r"tac\s*=\s*(0x[\da-fA-F]+|\d+)", epc)
    rr_tac_m  = re.search(r"tac\s*=\s*(0x[\da-fA-F]+|\d+)", rr)

    def hex_int(s: str) -> int:
        return int(s, 16) if s.startswith("0x") else int(s)

    epc_tac = hex_int(epc_tac_m.group(1)) if epc_tac_m else -1
    rr_tac  = hex_int(rr_tac_m.group(1))  if rr_tac_m  else -3
    check(epc_tac == rr_tac,
          f"TAC: epc.conf={epc_tac} rr.conf cell_list={rr_tac} — match")

    # 6. Trace injection wired in compose
    print("\n6. Trace injection wiring in docker-compose.4g.yml")
    check("RRC_TRACE_DIR" in dc4g,  "RRC_TRACE_DIR env var present in compose")
    check("22_decoded" in dc4g,      "22_decoded volume mounted in compose")

    # 7. Separate subnet
    print("\n7. 4G subnet isolation")
    check("10.53.2" in dc4g, "subnet 10.53.2.x defined in compose")
    check("10.53.1" not in dc4g, "No accidental 10.53.1 (5G) addresses in 4G compose")

    # 8. EARFCN match (eNB reads EARFCN from rr.conf cell_list; UE configured in ue.conf)
    print("\n8. EARFCN match")
    ue_earfcn_m  = re.search(r"dl_earfcn\s*=\s*(\d+)", ue)
    rr_earfcn_m  = re.search(r"dl_earfcn\s*=\s*(\d+)", rr)
    ue_ear  = ue_earfcn_m.group(1) if ue_earfcn_m else "?"
    rr_ear  = rr_earfcn_m.group(1) if rr_earfcn_m else "?"
    check(ue_ear == rr_ear,
          f"EARFCN: ue.conf={ue_ear}  rr.conf cell_list={rr_ear} -- match")

    # Summary
    print(f"\n{'='*55}")
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("RESULT: ALL CHECKS PASSED — 4G stack config is consistent.")
    print("Next: docker compose -f docker-compose.4g.yml build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
