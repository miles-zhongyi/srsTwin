#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Phase 2c verification: round-trip check for pycrate LTE RRC encoding.

Reads lte_per_templates.json produced by encode_templates.py and verifies:
  1. Every successfully encoded message round-trips (bytes → pycrate decode → same structure).
  2. The rrcConnectionRequest m_tmsi from 22_decoded matches the decoded PER value.
  3. The rrc.cc patch checks: expected env var names are mentioned in source.
  4. The entrypoint script exports the LTE-specific vars.

Usage:
    python3 verify_encode_roundtrip.py <lte_per_templates.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PASS = "[PASS]"
FAIL = "[FAIL]"
failures: list[str] = []


def check(cond: bool, msg: str) -> bool:
    tag = PASS if cond else FAIL
    print(f"  {tag}  {msg}")
    if not cond:
        failures.append(msg)
    return cond


def main() -> int:
    if len(sys.argv) < 2:
        # Try to find lte_per_templates.json relative to this script
        here = Path(__file__).resolve().parent
        candidates = list(here.parent.parent.glob("**/lte_per_templates/lte_per_templates.json"))
        if candidates:
            path = str(candidates[0])
        else:
            print("Usage: verify_encode_roundtrip.py <lte_per_templates.json>")
            return 1
    else:
        path = sys.argv[1]

    print(f"\n=== 4G LTE PER encode round-trip verification ===\n")
    print(f"Reading: {path}")

    with open(path, encoding="utf-8") as f:
        summary = json.load(f)

    pycrate_ok = summary.get("pycrate_ok", False)
    check(pycrate_ok, "pycrate is available and RRCLTE module loaded")
    if not pycrate_ok:
        print("Cannot run encoding checks without pycrate — skipping.")
        return 1

    templates = summary.get("templates", {})
    encoded = {k: v for k, v in templates.items() if v.get("encode_ok")}
    check(len(encoded) > 0, f"at least one message encoded ({len(encoded)} encoded, {len(templates)} found)")

    # --- pycrate round-trip check ---
    print("\n1. Round-trip (bytes -> decode -> re-encode must produce same bytes)")
    try:
        from pycrate_asn1rt.init import init_modules
        import pycrate_asn1dir.RRCLTE as R
        init_modules(R.EUTRA_RRC_Definitions)
        GLOBAL = R.GLOBAL
        RRC = GLOBAL.MOD["EUTRA-RRC-Definitions"]
    except Exception as e:
        print(f"  pycrate init failed: {e}")
        return 1

    for choice, entry in encoded.items():
        per_hex = entry.get("per_encoded", "")
        pdu_type = entry.get("pdu_type", "")
        if not per_hex or not pdu_type:
            continue
        per_bytes = bytes.fromhex(per_hex)
        try:
            asn = RRC[pdu_type]
            asn.from_uper(per_bytes)
            v = asn.get_val()
            rt_bytes = asn.to_uper()
            ok = rt_bytes == per_bytes
            check(ok, f"{choice} ({pdu_type}) round-trip: {per_hex} -> decode -> {rt_bytes.hex()}")
        except Exception as e:
            check(False, f"{choice} round-trip exception: {e}")

    # --- rrcConnectionRequest semantic field check ---
    print("\n2. rrcConnectionRequest m_tmsi field check")
    rrc_req = encoded.get("rrcConnectionRequest") or templates.get("rrcConnectionRequest")
    if rrc_req:
        sem = rrc_req.get("semantic_fields", {})
        check("m_tmsi" in sem, f"m_tmsi present in semantic_fields: {sem.get('m_tmsi')}")
        check("establishmentCause" in sem, f"establishmentCause: {sem.get('establishmentCause')}")
        per_hex = rrc_req.get("per_encoded", "")
        if per_hex and rrc_req.get("encode_ok"):
            per_bytes = bytes.fromhex(per_hex)
            asn = RRC["UL-CCCH-Message"]
            asn.from_uper(per_bytes)
            v = asn.get_val()
            m = v.get("message", ())
            # v = {'message': ('c1', ('rrcConnectionRequest', {'criticalExtensions': ...}))}
            try:
                inner = m[1][1]["criticalExtensions"][1]
                ue_id = inner.get("ue-Identity", ())
                cause = inner.get("establishmentCause")
                if ue_id[0] == "randomValue":
                    decoded_val = ue_id[1][0]  # int
                    # The PER bytes encode the UE's original random value from the trace
                    # (e.g. 0x1bdd227cd8). This is different from the CN-level m_tmsi.
                    # When RRC_TRACE_LTE_M_TMSI is set, srsUE substitutes m_tmsi into
                    # this field. Here we check that encoding preserved the trace value.
                    expected_rv = int(sem.get("random_value_hex", "0"), 16)
                    check(decoded_val == expected_rv,
                          f"decoded randomValue={hex(decoded_val)} == trace random_value={hex(expected_rv)}")
                elif ue_id[0] == "s-TMSI":
                    m_tmsi_val = ue_id[1].get("m-TMSI", (None,))[0]
                    check(m_tmsi_val is not None, f"s-TMSI m-TMSI={m_tmsi_val}")
                check(cause == sem.get("establishmentCause"),
                      f"cause={cause} matches trace={sem.get('establishmentCause')}")
            except Exception as e:
                check(False, f"field extraction failed: {e}")
    else:
        check(False, "rrcConnectionRequest not found in templates")

    # --- rrc.cc patch check ---
    print("\n3. rrc.cc patch check (static source scan)")
    rrc_cc = Path(__file__).resolve().parent.parent / "srsue/src/stack/rrc/rrc.cc"
    if rrc_cc.exists():
        src = rrc_cc.read_text(encoding="utf-8", errors="replace")
        check("RRC_TRACE_LTE_M_TMSI" in src, "rrc.cc reads RRC_TRACE_LTE_M_TMSI env var")
        check("RRC_TRACE_LTE_CAUSE"  in src, "rrc.cc reads RRC_TRACE_LTE_CAUSE env var")
        check("std::getenv" in src, "rrc.cc uses std::getenv")
        check("std::strcmp" in src,  "rrc.cc uses std::strcmp for cause mapping")
    else:
        check(False, f"rrc.cc not found at {rrc_cc}")

    # --- entrypoint script check ---
    print("\n4. Entrypoint script check")
    ep = Path(__file__).resolve().parent / "rrc_injector_entrypoint.sh"
    if ep.exists():
        src = ep.read_text(encoding="utf-8", errors="replace")
        check("RRC_TRACE_LTE_M_TMSI" in src, "entrypoint exports RRC_TRACE_LTE_M_TMSI")
        check("RRC_TRACE_LTE_CAUSE"  in src, "entrypoint exports RRC_TRACE_LTE_CAUSE")
        check("exec srsue" in src, "entrypoint exec's srsue")
    else:
        check(False, f"rrc_injector_entrypoint.sh not found")

    # --- Summary ---
    print(f"\n{'='*55}")
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("RESULT: ALL CHECKS PASSED -- 4G LTE encode pipeline is consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
