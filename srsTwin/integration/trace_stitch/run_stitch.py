#!/usr/bin/env python3
"""
Build per-UE stitched call flows from 22_decoded trace files.

Examples:
  python run_stitch.py --list-ues --max-files 5
  python run_stitch.py --ue random:1bdd227cd8 --print-flow
  python run_stitch.py --export-dir ../../22_decoded_stitched --max-files 20
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from stitch_engine import StitchEngine, default_trace_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="Stitch 22_decoded records into per-UE call flows.")
    ap.add_argument("--trace-dir", type=Path, default=None)
    ap.add_argument("--max-files", type=int, default=0, help="0 = scan all trace files")
    ap.add_argument("--list-ues", action="store_true", help="Print UE index summary")
    ap.add_argument("--ue", type=str, default=None, help="UE key or alias (e.g. random:1bdd227cd8)")
    ap.add_argument("--print-flow", action="store_true", help="Print stitched message flow for --ue")
    ap.add_argument("--export-dir", type=Path, default=None, help="Export all UE timelines as JSON")
    ap.add_argument("--export-ue", type=Path, default=None, help="Export one UE timeline to JSON")
    ap.add_argument("--include-raw", action="store_true", help="Include full 22_decoded records in export")
    args = ap.parse_args()

    trace_dir = args.trace_dir or default_trace_dir()
    engine = StitchEngine(trace_dir=trace_dir, max_files=args.max_files)

    if args.list_ues or (not args.ue and not args.export_dir and not args.export_ue):
        rows = engine.list_ues()
        print(json.dumps({"trace_dir": str(trace_dir), "ue_count": len(rows), "ues": rows}, indent=2))
        if not args.ue and not args.export_dir and not args.export_ue:
            return 0

    if args.ue and args.print_flow:
        tl = engine.get_ue(args.ue)
        if not tl:
            print(f"UE not found: {args.ue}")
            return 1
        for r in tl.records:
            print(f"{r.timestamp}  [{r.flow_phase}]  {r.label}  ({r.interface})  {r.source_file}")
        print(f"\n{len(tl.records)} records, {len(tl.sessions)} sessions, key={tl.ue_key}")

    if args.export_ue:
        if not args.ue:
            ap.error("--export-ue requires --ue")
        engine.export_ue(args.ue, args.export_ue, include_raw=args.include_raw)
        print(f"exported {args.ue} -> {args.export_ue}")

    if args.export_dir:
        summary = engine.export_all(args.export_dir, include_raw=args.include_raw)
        print(json.dumps(summary, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
