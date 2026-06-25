#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""M0 test: TransmitIntent/DownlinkEvent shape and validation."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from interfaces import DownlinkEvent, TransmitIntent, validate_event  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {PASS if cond else FAIL}  {name}")
    if not cond:
        failures.append(name)


def main() -> int:
    print("=== interfaces ===")

    intent = TransmitIntent(ue_id="ue-0", channel="SRB0", procedure_tag="rrc_conn_request")
    check("TransmitIntent.per_encoded_bytes defaults to None (M1 native-encoder path)",
          intent.per_encoded_bytes is None)

    intent_m5 = TransmitIntent(ue_id="ue-0", channel="SRB0", procedure_tag="rrc_conn_request",
                                per_encoded_bytes=b"\x50\x00\x00")
    check("TransmitIntent accepts per_encoded_bytes for the future M5 path",
          intent_m5.per_encoded_bytes == b"\x50\x00\x00")

    ev = DownlinkEvent(ue_id="ue-0", rnti=0x46, event_type="attach_complete", ts=1.0)
    check("valid event has no problems", validate_event(ev) == [])

    bad_rnti = DownlinkEvent(ue_id="ue-0", rnti=0, event_type="paging")
    check("rnti=0 flagged as out of range", any("rnti" in p for p in validate_event(bad_rnti)))

    empty_ue = DownlinkEvent(ue_id="", rnti=5, event_type="paging")
    check("empty ue_id flagged", any("ue_id" in p for p in validate_event(empty_ue)))

    unknown_type = DownlinkEvent(ue_id="ue-0", rnti=5, event_type="something_new")
    check("unknown event_type is informational, not fatal (only this one problem)",
          len(validate_event(unknown_type)) == 1)

    print(f"\n{'='*40}")
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
