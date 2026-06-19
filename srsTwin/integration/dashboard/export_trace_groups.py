#!/usr/bin/env python3
"""
Group 22_decoded records into one JSON file per message type.

The 22_decoded files can be very large, so this exporter streams records instead
of loading the whole trace into memory. By default it groups by decoded ASN.1
choice, which is the most stable template identifier for dashboard/template use.

Examples:
  python export_trace_groups.py
  python export_trace_groups.py --trace-dir ..\\..\\22_decoded --clean
  python export_trace_groups.py --key-by record_id
  python export_trace_groups.py --max-files 1 --pretty
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterator


def iter_trace_records(path: Path) -> Iterator[dict[str, Any]]:
    """Stream objects from a top-level JSON array."""
    dec = json.JSONDecoder()
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        buf = ""
        while "[" not in buf:
            extra = fp.read(65536)
            if not extra:
                return
            buf += extra
        pos = buf.index("[") + 1

        while True:
            buf = buf[pos:].lstrip()
            pos = 0
            if not buf:
                extra = fp.read(65536)
                if not extra:
                    return
                buf = extra
                continue
            if buf[pos] == "]":
                return

            while True:
                try:
                    obj, end = dec.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    extra = fp.read(262144)
                    if not extra:
                        raise
                    buf += extra
                    continue

                if isinstance(obj, dict):
                    yield obj
                pos = end
                while pos < len(buf) and buf[pos] in ", \r\n\t":
                    pos += 1
                if pos < len(buf) and buf[pos] == "]":
                    return
                break


def _choice_from_decoded_message(rec: dict[str, Any]) -> str:
    msg = (rec.get("decoded") or {}).get("message")
    if not isinstance(msg, list) or not msg:
        return ""

    first = msg[0]
    if first == "c1" and len(msg) >= 2:
        inner = msg[1]
        if isinstance(inner, list) and inner and isinstance(inner[0], str):
            return inner[0]
    if isinstance(first, str):
        return first
    return ""


def decoded_choice(rec: dict[str, Any]) -> str:
    meta = rec.get("decoding_metadata") or {}
    choice = (
        meta.get("decoded_message_choice")
        or meta.get("normalized_label")
        or _choice_from_decoded_message(rec)
        or rec.get("message_name")
        or "unknown"
    )
    if choice == "c1":
        choice = _choice_from_decoded_message(rec) or choice
    return str(choice)


def group_key(rec: dict[str, Any], key_by: str) -> str:
    choice = decoded_choice(rec)
    rid = rec.get("record_id")
    if key_by == "record_id":
        return f"record_{rid}" if rid is not None else "record_unknown"
    if key_by == "message_name":
        return str(rec.get("message_name") or choice or "unknown")
    if key_by == "message_record":
        rid_part = f"record_{rid}" if rid is not None else "record_unknown"
        return f"{rid_part}__{choice}"
    return choice


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(key: str, max_len: int = 140) -> str:
    out = _SAFE.sub("_", key).strip("._-")
    return (out or "unknown")[:max_len]


def default_trace_dir() -> Path:
    here = Path(__file__).resolve()
    srs_root = here.parents[2]
    repo_root = srs_root.parent
    for candidate in (srs_root / "22_decoded", repo_root / "poc_StressTest" / "22_decoded"):
        if candidate.is_dir():
            return candidate
    return srs_root / "22_decoded"


def default_out_dir(key_by: str) -> Path:
    srs_root = Path(__file__).resolve().parents[2]
    return srs_root / "22_decoded_grouped" / f"by_{key_by}"


def finalize_jsonl(jsonl_path: Path, json_path: Path, pretty: bool) -> int:
    count = 0
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("r", encoding="utf-8") as src, json_path.open("w", encoding="utf-8", newline="\n") as dst:
        dst.write("[\n")
        for line in src:
            line = line.rstrip("\n")
            if not line:
                continue
            if count:
                dst.write(",\n")
            if pretty:
                dst.write(json.dumps(json.loads(line), indent=2, ensure_ascii=False))
            else:
                dst.write(line)
            count += 1
        dst.write("\n]\n")
    return count


def export_groups(trace_dir: Path, out_dir: Path, key_by: str, max_files: int, clean: bool, pretty: bool) -> dict[str, Any]:
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tmp_dir = out_dir / "_tmp_jsonl"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    files = sorted(trace_dir.rglob("*_TRC_decoded.json"))
    if max_files:
        files = files[:max_files]
    if not files:
        raise FileNotFoundError(f"no *_TRC_decoded.json files under {trace_dir}")

    counts: Counter[str] = Counter()
    interfaces: dict[str, Counter[str]] = {}
    first_record: dict[str, dict[str, Any]] = {}
    source_files: Counter[str] = Counter()

    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] scanning {path}", flush=True)
        for rec in iter_trace_records(path):
            key = group_key(rec, key_by)
            fname = safe_filename(key)
            payload = dict(rec)
            payload["_source_file"] = path.name
            payload["_source_path"] = str(path)
            payload["_group_key"] = key

            with (tmp_dir / f"{fname}.jsonl").open("a", encoding="utf-8", newline="\n") as fp:
                fp.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

            counts[key] += 1
            source_files[path.name] += 1
            iface = str(rec.get("interface") or rec.get("protocol") or "unknown")
            interfaces.setdefault(key, Counter())[iface] += 1
            first_record.setdefault(key, {
                "record_id": rec.get("record_id"),
                "message_name": rec.get("message_name"),
                "decoded_choice": decoded_choice(rec),
                "interface": rec.get("interface"),
                "timestamp": rec.get("timestamp"),
                "source_file": path.name,
            })

    groups: list[dict[str, Any]] = []
    for key in sorted(counts):
        fname = safe_filename(key)
        jsonl_path = tmp_dir / f"{fname}.jsonl"
        json_path = out_dir / f"{fname}.json"
        finalized_count = finalize_jsonl(jsonl_path, json_path, pretty)
        groups.append({
            "key": key,
            "file": json_path.name,
            "count": finalized_count,
            "interfaces": dict(sorted(interfaces.get(key, Counter()).items())),
            "first_record": first_record.get(key, {}),
        })

    shutil.rmtree(tmp_dir)

    summary = {
        "trace_dir": str(trace_dir),
        "out_dir": str(out_dir),
        "key_by": key_by,
        "files_scanned": len(files),
        "total_records": sum(counts.values()),
        "group_count": len(groups),
        "groups": groups,
        "source_files": dict(source_files),
    }
    (out_dir / "_index.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Export all 22_decoded messages into per-type JSON files.")
    ap.add_argument("--trace-dir", type=Path, default=default_trace_dir())
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--key-by", choices=("decoded_choice", "message_name", "record_id", "message_record"),
                    default="decoded_choice")
    ap.add_argument("--max-files", type=int, default=0, help="0 = all files")
    ap.add_argument("--clean", action="store_true", help="delete output folder before exporting")
    ap.add_argument("--pretty", action="store_true", help="pretty-print records; slower and larger")
    args = ap.parse_args()

    out_dir = args.out_dir or default_out_dir(args.key_by)
    summary = export_groups(args.trace_dir.resolve(), out_dir.resolve(), args.key_by, args.max_files, args.clean, args.pretty)
    print(
        f"done: {summary['total_records']} records -> {summary['group_count']} groups "
        f"under {summary['out_dir']}"
    )
    print(f"index: {Path(summary['out_dir']) / '_index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
