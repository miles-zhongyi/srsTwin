#!/usr/bin/env python3
"""Verify per-message 22_decoded grouped export output."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def default_out_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "22_decoded_grouped" / "by_decoded_choice"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(("OK   " if ok else "FAIL ") + name + (f" ({detail})" if detail else ""))
    return ok


def load_first_record(group_file: Path) -> dict[str, Any] | None:
    data = json.loads(group_file.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify grouped 22_decoded export.")
    ap.add_argument("--out-dir", type=Path, default=default_out_dir())
    ap.add_argument("--require", action="append", default=[
        "rrcConnectionRequest",
        "rrcConnectionSetupComplete",
    ], help="group key expected to exist; can be repeated")
    args = ap.parse_args()

    out_dir = args.out_dir.resolve()
    idx_path = out_dir / "_index.json"
    failures = 0

    failures += not check("output directory exists", out_dir.is_dir(), str(out_dir))
    failures += not check("_index.json exists", idx_path.is_file(), str(idx_path))
    if failures:
        return 1

    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    groups = idx.get("groups") or []
    total_records = int(idx.get("total_records") or 0)
    failures += not check("index has groups", bool(groups), f"count={len(groups)}")
    failures += not check("index has records", total_records > 0, f"total={total_records}")

    group_by_key = {g.get("key"): g for g in groups if isinstance(g, dict)}
    for key in args.require:
        entry = group_by_key.get(key)
        failures += not check(f"required group {key}", entry is not None)
        if not entry:
            continue
        group_file = out_dir / str(entry.get("file"))
        failures += not check(f"{key} file exists", group_file.is_file(), group_file.name)
        if group_file.is_file():
            rec = load_first_record(group_file)
            failures += not check(f"{key} file contains records", rec is not None)
            if rec:
                failures += not check(f"{key} record has _group_key", rec.get("_group_key") == key,
                                      f"got={rec.get('_group_key')}")

    # Count files excluding the index; this should match the group count.
    json_files = [p for p in out_dir.glob("*.json") if p.name != "_index.json"]
    failures += not check("group file count matches index", len(json_files) == len(groups),
                          f"files={len(json_files)} groups={len(groups)}")

    print(f"\n{len(groups)} groups, {total_records} total records")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
