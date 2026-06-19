#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Encode/reconstruct grouped 22_decoded S1AP records.

Without raw TELUS S1AP PER bytes, exact S1AP recovery is generally not possible:
22_decoded stores simplified IE values while S1AP PER uses procedure-specific
open types. This script therefore marks direct trace encoding as failed and
builds a conservative reconstructed procedure shell when pycrate has the
corresponding S1AP elementary procedure.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

try:
    from pycrate_asn1rt.init import init_modules
    import pycrate_asn1dir.S1AP as _S1AP

    init_modules(_S1AP.S1AP_PDU_Descriptions)
    _S1_MOD = _S1AP.GLOBAL.MOD["S1AP-PDU-Descriptions"]
    _PYCRATE_S1AP_OK = True
    _PYCRATE_S1AP_ERR = ""
except Exception as exc:  # pragma: no cover - environment dependent
    _PYCRATE_S1AP_OK = False
    _PYCRATE_S1AP_ERR = str(exc)


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_RECON_CACHE: dict[str, tuple[bytes | None, dict[str, Any] | None, bool]] = {}

_S1_GROUP_TO_EP = {
    "S1_INITIAL_UE_MESSAGE": "initialUEMessage",
    "S1_DOWNLINK_NAS_TRANSPORT": "downlinkNASTransport",
    "S1_UPLINK_NAS_TRANSPORT": "uplinkNASTransport",
    "S1_INITIAL_CONTEXT_SETUP_REQUEST": "initialContextSetup",
    "S1_INITIAL_CONTEXT_SETUP_RESPONSE": "initialContextSetup",
    "S1_UE_CONTEXT_RELEASE_COMMAND": "uEContextRelease",
    "S1_UE_CONTEXT_RELEASE_COMPLETE": "uEContextRelease",
    "S1_UE_CONTEXT_RELEASE_REQUEST": "uEContextReleaseRequest",
    "S1_ERAB_SETUP_REQUEST": "e-RABSetup",
    "S1_ERAB_SETUP_RESPONSE": "e-RABSetup",
    "S1_ERAB_RELEASE_COMMAND": "e-RABRelease",
    "S1_ERAB_RELEASE_RESPONSE": "e-RABRelease",
    "S1_UE_CAPABILITY_INDICATION": "uECapabilityInfoIndication",
    "S1_CELL_TRAFFIC_TRACE": "traceStart",
    "S1_LOCATION_REPORTING_CONTROL": "locationReportingControl",
    "S1_LOCATION_REPORT": "locationReport",
    "S1_HANDOVER_REQUIRED": "handoverPreparation",
    "S1_HANDOVER_REQUEST": "handoverResourceAllocation",
    "S1_HANDOVER_REQUEST_ACKNOWLEDGE": "handoverResourceAllocation",
    "S1_HANDOVER_NOTIFY": "handoverNotification",
    "S1_HANDOVER_CANCEL": "handoverCancel",
    "S1_HANDOVER_CANCEL_ACKNOWLEDGE": "handoverCancel",
    "S1_PATH_SWITCH_REQUEST": "pathSwitchRequest",
    "S1_PATH_SWITCH_REQUEST_ACKNOWLEDGE": "pathSwitchRequest",
    "S1_MME_STATUS_TRANSFER": "mMEStatusTransfer",
    "S1_ENB_STATUS_TRANSFER": "eNBStatusTransfer",
}


def safe_filename(key: str, max_len: int = 140) -> str:
    out = _SAFE.sub("_", key).strip("._-")
    return (out or "unknown")[:max_len]


def default_group_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "22_decoded_grouped" / "by_decoded_choice"


def default_out_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "22_decoded_per_records" / "s1ap_by_decoded_choice"


def iter_json_array(path: Path) -> Iterator[dict[str, Any]]:
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


def json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"_bytes_hex": value.hex()}
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    return value


def message_kind(decoded: dict[str, Any]) -> str | None:
    for key in ("initiatingMessage", "successfulOutcome", "unsuccessfulOutcome"):
        if key in decoded:
            return key
    return None


def procedure_shell(group_key: str, rec: dict[str, Any]) -> tuple | None:
    decoded = rec.get("decoded") or {}
    kind = message_kind(decoded)
    if not kind:
        return None
    body = decoded.get(kind) or {}
    ep_name = _S1_GROUP_TO_EP.get(group_key)
    if not ep_name or ep_name not in _S1_MOD:
        return None
    ep = _S1_MOD[ep_name].get_val()

    open_field = {
        "initiatingMessage": "InitiatingMessage",
        "successfulOutcome": "SuccessfulOutcome",
        "unsuccessfulOutcome": "UnsuccessfulOutcome",
    }[kind]
    open_type = ep.get(open_field)
    if open_type is None:
        return None

    procedure_code = int(body.get("procedureCode", ep.get("procedureCode", 0)))
    criticality = body.get("criticality", ep.get("criticality", "ignore"))
    open_name = str(open_type._typeref.called[1]) if getattr(open_type, "_typeref", None) else open_field

    return (
        kind,
        {
            "procedureCode": procedure_code,
            "criticality": criticality,
            "value": (open_name, {"protocolIEs": []}),
        },
    )


def encode_reconstructed(val: tuple) -> tuple[bytes | None, dict[str, Any] | None, bool]:
    if not _PYCRATE_S1AP_OK:
        return None, None, False
    cache_key = json.dumps(json_safe(val), sort_keys=True, separators=(",", ":"))
    if cache_key in _RECON_CACHE:
        return _RECON_CACHE[cache_key]
    try:
        import copy

        obj = copy.deepcopy(_S1_MOD["S1AP-PDU"])
        obj.set_val(val)
        per_bytes = obj.to_aper()

        dec = copy.deepcopy(_S1_MOD["S1AP-PDU"])
        dec.from_aper(per_bytes)
        decoded_val = dec.get_val()

        enc = copy.deepcopy(_S1_MOD["S1AP-PDU"])
        enc.set_val(decoded_val)
        result = (per_bytes, json_safe(decoded_val), enc.to_aper() == per_bytes)
        _RECON_CACHE[cache_key] = result
        return result
    except Exception:
        _RECON_CACHE[cache_key] = (None, None, False)
        return None, None, False


def encode_one(group_key: str, rec: dict[str, Any]) -> dict[str, Any]:
    decoded = rec.get("decoded") or {}
    meta = rec.get("decoding_metadata") or {}
    base = {
        "status": "encode_failed",
        "message_name": group_key,
        "record_id": rec.get("record_id"),
        "timestamp": rec.get("timestamp"),
        "source_file": rec.get("_source_file"),
        "source_path": rec.get("_source_path"),
        "serving_plmn": rec.get("serving_plmn"),
        "interface": rec.get("interface"),
        "protocol": rec.get("protocol"),
        "semantic_fields": decoded.get("semantic_fields") or {},
        "decoded": decoded,
        "procedure_code": (meta.get("protocol_metadata") or {}).get("procedure_code"),
    }
    if rec.get("interface") != "S1":
        base["reason"] = "unsupported_interface"
        return base
    if not _PYCRATE_S1AP_OK:
        base["reason"] = f"pycrate_s1ap_unavailable: {_PYCRATE_S1AP_ERR}"
        return base

    val = procedure_shell(group_key, rec)
    if val is None:
        base["reason"] = "unsupported_s1ap_procedure"
        return base
    per_bytes, decoded_val, ok = encode_reconstructed(val)
    if not per_bytes:
        base["reason"] = "reconstruction_encode_failed"
        return base
    base.update({
        "status": "reconstructed",
        "template_source": "s1ap_reconstructed",
        "truth_level": "reconstructed",
        "per_rule": "APER",
        "per_encoded": per_bytes.hex(),
        "per_len_bytes": len(per_bytes),
        "roundtrip_ok": ok,
        "roundtrip_decoded": decoded_val,
        "reconstruction_note": "S1AP procedure shell reconstructed with empty protocolIEs; original IE open-type values require raw PER or deeper IE mapping.",
    })
    return base


def write_array(path: Path, entries: Iterator[dict[str, Any]]) -> tuple[int, Counter[str]]:
    count = 0
    statuses: Counter[str] = Counter()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fp:
        fp.write("[\n")
        for entry in entries:
            if count:
                fp.write(",\n")
            fp.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            count += 1
            statuses[entry.get("status", "unknown")] += 1
        fp.write("\n]\n")
    return count, statuses


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--group-dir", type=Path, default=default_group_dir())
    ap.add_argument("--out-dir", type=Path, default=default_out_dir())
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--limit-per-group", type=int, default=0)
    args = ap.parse_args()

    group_dir = args.group_dir.resolve()
    out_dir = args.out_dir.resolve()
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    idx = json.loads((group_dir / "_index.json").read_text(encoding="utf-8"))
    wanted = set(args.only)
    summary_groups = []
    total = Counter()
    total_count = 0
    for group in idx.get("groups", []):
        key = str(group.get("key") or "")
        if wanted and key not in wanted:
            continue
        interfaces = group.get("interfaces") or {}
        if "S1" not in interfaces:
            continue
        src = group_dir / str(group.get("file"))
        dst = out_dir / f"{safe_filename(key)}.s1ap_per_records.json"
        print(f"encoding {key} ({group.get('count')} records) -> {dst.name}", flush=True)

        def entries() -> Iterator[dict[str, Any]]:
            for i, rec in enumerate(iter_json_array(src), 1):
                if args.limit_per_group and i > args.limit_per_group:
                    break
                yield encode_one(key, rec)

        count, statuses = write_array(dst, entries())
        total.update(statuses)
        total_count += count
        summary_groups.append({
            "key": key,
            "source_file": src.name,
            "result_file": dst.name,
            "input_count": group.get("count"),
            "processed_count": count,
            "statuses": dict(sorted(statuses.items())),
        })

    report = {
        "group_dir": str(group_dir),
        "out_dir": str(out_dir),
        "pycrate_s1ap_ok": _PYCRATE_S1AP_OK,
        "processed_group_count": len(summary_groups),
        "processed_record_count": total_count,
        "statuses": dict(sorted(total.items())),
        "groups": summary_groups,
    }
    (out_dir / "_s1ap_per_record_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"done: {total_count} records across {len(summary_groups)} groups")
    print(f"statuses: {dict(sorted(total.items()))}")
    print(f"report: {out_dir / '_s1ap_per_record_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
