#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Encode every grouped 22_decoded RRC record into per-record PER templates.

Reads the output of integration/dashboard/export_trace_groups.py and writes one
result JSON file per message type. Each input record is marked as:

  exact         - 22_decoded JSON encoded to UPER and decoded/re-encoded to
                  the same bytes.
  reconstructed - exact encode failed/skipped, but a standard ASN.1 shape was
                  rebuilt using trace fields plus safe defaults.
  minimal       - reconstruction failed, but a minimal syntactically
                  valid fallback exists for that message type.
  encode_failed - neither exact nor minimal encoding is available.

This script is intentionally separate from encode_templates.py, which keeps only
one representative template per message type for dashboard display.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import encode_templates as enc  # noqa: E402


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MINIMAL_CACHE: dict[tuple[str, str], dict[str, Any] | None] = {}
_RECON_CACHE: dict[tuple[str, str, str], dict[str, Any] | None] = {}


def safe_filename(key: str, max_len: int = 140) -> str:
    out = _SAFE.sub("_", key).strip("._-")
    return (out or "unknown")[:max_len]


def default_group_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "22_decoded_grouped" / "by_decoded_choice"


def default_out_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "22_decoded_per_records" / "by_decoded_choice"


def iter_json_array(path: Path) -> Iterator[dict[str, Any]]:
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


def decoded_choice(rec: dict[str, Any], fallback: str = "") -> str:
    meta = rec.get("decoding_metadata") or {}
    choice = meta.get("decoded_message_choice") or rec.get("_group_key") or fallback
    msg = (rec.get("decoded") or {}).get("message")
    if choice == "c1" and isinstance(msg, list) and len(msg) >= 2:
        inner = msg[1]
        if isinstance(inner, list) and inner and isinstance(inner[0], str):
            choice = inner[0]
    return str(choice or "")


def pdu_type_for(choice: str, rec: dict[str, Any]) -> str | None:
    return enc._ALL_TARGETS.get(choice)


def roundtrip_same(per_bytes: bytes, pdu_type: str) -> tuple[bool, dict[str, Any] | None]:
    """Decode then re-encode and require byte-for-byte equality."""
    if not enc._PYCRATE_OK:
        return False, None
    try:
        import copy

        dec_obj = copy.deepcopy(enc._RRC_MOD[pdu_type])
        dec_obj.from_uper(per_bytes)
        val = dec_obj.get_val()

        enc_obj = copy.deepcopy(enc._RRC_MOD[pdu_type])
        enc_obj.set_val(val)
        return enc_obj.to_uper() == per_bytes, val
    except Exception:
        return False, None


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


def minimal_entry(choice: str, pdu_type: str) -> dict[str, Any] | None:
    key = (choice, pdu_type)
    if key in _MINIMAL_CACHE:
        return _MINIMAL_CACHE[key]
    min_bytes = enc.encode_minimal(choice, pdu_type)
    if not min_bytes:
        _MINIMAL_CACHE[key] = None
        return None
    min_ok, min_decoded = roundtrip_same(min_bytes, pdu_type)
    entry = {
        "status": "minimal",
        "template_source": "minimal",
        "per_encoded": min_bytes.hex(),
        "per_len_bytes": len(min_bytes),
        "minimal_roundtrip_ok": min_ok,
        "minimal_roundtrip_decoded": json_safe(min_decoded),
    }
    _MINIMAL_CACHE[key] = entry
    return entry


def _message_body(msg: Any, choice: str) -> dict[str, Any]:
    if not isinstance(msg, list) or len(msg) < 2:
        return {}
    if msg[0] == choice and isinstance(msg[1], dict):
        return msg[1]
    if msg[0] == "c1" and isinstance(msg[1], list) and len(msg[1]) >= 2:
        inner = msg[1]
        if inner[0] == choice and isinstance(inner[1], dict):
            return inner[1]
    return {}


def _transaction_id(body: dict[str, Any]) -> int:
    try:
        return int(body.get("rrc-TransactionIdentifier", 0)) & 3
    except (TypeError, ValueError):
        return 0


def _reconstructed_val(choice: str, rec: dict[str, Any]) -> tuple | None:
    """Build a standard ASN.1 value from flattened 22_decoded fields.

    This is intentionally not marked exact: missing mandatory fields are filled
    with safe defaults so pycrate can produce valid PER.
    """
    msg = (rec.get("decoded") or {}).get("message")
    body = _message_body(msg, choice)
    tx = _transaction_id(body)

    if choice == "rrcConnectionSetupComplete":
        return (
            "c1", ("rrcConnectionSetupComplete", {
                "rrc-TransactionIdentifier": tx,
                "criticalExtensions": (
                    "c1", ("rrcConnectionSetupComplete-r8", {
                        "selectedPLMN-Identity": int(body.get("selectedPLMN-Identity", 1) or 1),
                        "dedicatedInfoNAS": bytes(1),
                    })
                ),
            })
        )

    if choice == "measurementReport":
        meas_id = body.get("measId", body.get("measurementIdentity", 1))
        try:
            meas_id = max(1, min(int(meas_id), 32))
        except (TypeError, ValueError):
            meas_id = 1
        return (
            "c1", ("measurementReport", {
                "criticalExtensions": (
                    "c1", ("measurementReport-r8", {
                        "measResults": {
                            "measId": meas_id,
                            "measResultPCell": {
                                "rsrpResult": 0,
                                "rsrqResult": 0,
                            },
                        },
                    })
                )
            })
        )

    if choice == "securityModeComplete":
        return (
            "c1", ("securityModeComplete", {
                "rrc-TransactionIdentifier": tx,
                "criticalExtensions": ("securityModeComplete-r8", {}),
            })
        )

    if choice == "rrcConnectionReconfigurationComplete":
        return (
            "c1", ("rrcConnectionReconfigurationComplete", {
                "rrc-TransactionIdentifier": tx,
                "criticalExtensions": ("rrcConnectionReconfigurationComplete-r8", {}),
            })
        )

    if choice == "ueCapabilityInformation":
        return (
            "c1", ("ueCapabilityInformation", {
                "rrc-TransactionIdentifier": tx,
                "criticalExtensions": (
                    "c1", ("ueCapabilityInformation-r8", {
                        "ue-CapabilityRAT-ContainerList": [],
                    })
                ),
            })
        )

    if choice == "rrcConnectionReestablishmentComplete":
        return (
            "c1", ("rrcConnectionReestablishmentComplete", {
                "rrc-TransactionIdentifier": tx,
                "criticalExtensions": ("rrcConnectionReestablishmentComplete-r8", {}),
            })
        )

    if choice == "rrcConnectionRelease":
        release_cause = body.get("releaseCause", "other")
        return (
            "c1", ("rrcConnectionRelease", {
                "rrc-TransactionIdentifier": tx,
                "criticalExtensions": ("c1", ("rrcConnectionRelease-r8", {
                    "releaseCause": release_cause,
                })),
            })
        )

    if choice == "rrcConnectionSetup":
        return (
            "c1", ("rrcConnectionSetup", {
                "rrc-TransactionIdentifier": tx,
                "criticalExtensions": ("c1", ("rrcConnectionSetup-r8", {
                    "radioResourceConfigDedicated": {},
                })),
            })
        )

    if choice == "rrcConnectionReconfiguration":
        return (
            "c1", ("rrcConnectionReconfiguration", {
                "rrc-TransactionIdentifier": tx,
                "criticalExtensions": ("c1", ("rrcConnectionReconfiguration-r8", {})),
            })
        )

    if choice == "securityModeCommand":
        return (
            "c1", ("securityModeCommand", {
                "rrc-TransactionIdentifier": tx,
                "criticalExtensions": ("c1", ("securityModeCommand-r8", {
                    "securityConfigSMC": {
                        "securityAlgorithmConfig": {
                            "cipheringAlgorithm": "eea0",
                            "integrityProtAlgorithm": "eia1",
                        }
                    }
                })),
            })
        )

    if choice == "ueCapabilityEnquiry":
        return (
            "c1", ("ueCapabilityEnquiry", {
                "rrc-TransactionIdentifier": tx,
                "criticalExtensions": ("c1", ("ueCapabilityEnquiry-r8", {
                    "ue-CapabilityRequest": [],
                })),
            })
        )

    return None


def encode_val(choice: str, pdu_type: str, val: tuple) -> bytes | None:
    if not enc._PYCRATE_OK:
        return None
    try:
        import copy

        asn_obj = copy.deepcopy(enc._RRC_MOD[pdu_type])
        asn_obj.set_val({"message": val})
        return asn_obj.to_uper()
    except Exception:
        return None


def reconstructed_entry(choice: str, pdu_type: str, rec: dict[str, Any]) -> dict[str, Any] | None:
    val = _reconstructed_val(choice, rec)
    if val is None:
        return None
    cache_key = (choice, pdu_type, json.dumps(json_safe(val), sort_keys=True, separators=(",", ":")))
    if cache_key in _RECON_CACHE:
        cached = _RECON_CACHE[cache_key]
        return dict(cached) if cached else None
    per_bytes = encode_val(choice, pdu_type, val)
    if not per_bytes:
        _RECON_CACHE[cache_key] = None
        return None
    ok, decoded = roundtrip_same(per_bytes, pdu_type)
    entry = {
        "status": "reconstructed",
        "template_source": "reconstructed",
        "per_encoded": per_bytes.hex(),
        "per_len_bytes": len(per_bytes),
        "roundtrip_ok": ok,
        "roundtrip_decoded": json_safe(decoded),
        "reconstruction_note": "standard ASN.1 wrapper restored; missing mandatory fields filled with defaults; not exact trace PER",
    }
    _RECON_CACHE[cache_key] = entry
    return dict(entry)


def encode_one(rec: dict[str, Any], choice: str) -> dict[str, Any]:
    msg = (rec.get("decoded") or {}).get("message")
    pdu_type = pdu_type_for(choice, rec)
    base = {
        "status": "encode_failed",
        "message_name": choice,
        "record_id": rec.get("record_id"),
        "timestamp": rec.get("timestamp"),
        "source_file": rec.get("_source_file"),
        "source_path": rec.get("_source_path"),
        "serving_plmn": rec.get("serving_plmn"),
        "interface": rec.get("interface"),
        "pdu_type": pdu_type,
        "semantic_fields": enc._extract_semantic_fields(rec),
        "decoded_message": msg,
    }

    if rec.get("interface") != "RRC":
        base["reason"] = "unsupported_interface"
        return base
    if not pdu_type:
        base["reason"] = "unsupported_message_type"
        return base
    if not isinstance(msg, list):
        base["reason"] = "missing_decoded_message"
        return base
    if not enc._PYCRATE_OK:
        base["reason"] = f"pycrate_unavailable: {getattr(enc, '_pyc_err_msg', '')}"
        return base

    dmeta = rec.get("decoding_metadata") or {}
    # Many 22_decoded groups are label-mismatched and flattened, so an exact
    # trace PER cannot be recovered. RRC Connection Request is the exception:
    # its decoded UL-CCCH payload is complete enough to round-trip exactly even
    # when the trace label metadata is noisy.
    if dmeta.get("label_mismatch") and choice != "rrcConnectionRequest":
        base["roundtrip_ok"] = False
        base["reason"] = "exact_skipped_label_mismatch"
        recon_info = reconstructed_entry(choice, pdu_type, rec)
        if recon_info:
            base.update(recon_info)
            return base
        min_info = minimal_entry(choice, pdu_type)
        if min_info:
            base.update(min_info)
        return base

    per_bytes = enc.encode_record(msg, pdu_type)
    if per_bytes:
        ok, decoded_val = roundtrip_same(per_bytes, pdu_type)
        if ok:
            base.update({
                "status": "exact",
                "template_source": "trace",
                "per_encoded": per_bytes.hex(),
                "per_len_bytes": len(per_bytes),
                "roundtrip_ok": True,
                "roundtrip_decoded": json_safe(decoded_val),
            })
            return base
        base["exact_per_encoded"] = per_bytes.hex()
        base["exact_per_len_bytes"] = len(per_bytes)
        base["roundtrip_ok"] = False
        base["reason"] = "exact_roundtrip_failed"
    else:
        base["roundtrip_ok"] = False
        base["reason"] = "exact_encode_failed"

    recon_info = reconstructed_entry(choice, pdu_type, rec)
    if recon_info:
        base.update(recon_info)
        return base

    min_info = minimal_entry(choice, pdu_type)
    if min_info:
        base.update(min_info)
        return base

    return base


def write_result_array(results_path: Path, entries: Iterator[dict[str, Any]]) -> tuple[int, Counter[str]]:
    count = 0
    statuses: Counter[str] = Counter()
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("w", encoding="utf-8", newline="\n") as fp:
        fp.write("[\n")
        for entry in entries:
            if count:
                fp.write(",\n")
            fp.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            count += 1
            statuses[entry.get("status", "unknown")] += 1
        fp.write("\n]\n")
    return count, statuses


def iter_group_entries(group_file: Path, key: str, limit: int = 0) -> Iterator[dict[str, Any]]:
    for i, rec in enumerate(iter_json_array(group_file), 1):
        if limit and i > limit:
            break
        yield encode_one(rec, decoded_choice(rec, key) or key)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--group-dir", type=Path, default=default_group_dir())
    ap.add_argument("--out-dir", type=Path, default=default_out_dir())
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--include-unsupported", action="store_true",
                    help="also emit encode_failed files for non-RRC/unsupported grouped files")
    ap.add_argument("--only", action="append", default=[],
                    help="only encode this group key; can be repeated")
    ap.add_argument("--limit-per-group", type=int, default=0,
                    help="debug/testing limit; 0 = all records")
    args = ap.parse_args()

    group_dir = args.group_dir.resolve()
    out_dir = args.out_dir.resolve()
    idx_path = group_dir / "_index.json"
    if not idx_path.is_file():
        raise FileNotFoundError(f"group index not found: {idx_path}")
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    group_index = json.loads(idx_path.read_text(encoding="utf-8"))
    groups = group_index.get("groups") or []
    wanted = set(args.only)

    summary_groups: list[dict[str, Any]] = []
    total_statuses: Counter[str] = Counter()
    processed_records = 0

    for group in groups:
        key = str(group.get("key") or "")
        if wanted and key not in wanted:
            continue
        group_file = group_dir / str(group.get("file"))
        if not group_file.is_file():
            continue
        supported = key in enc._ALL_TARGETS or any(iface == "RRC" for iface in (group.get("interfaces") or {}))
        if not supported and not args.include_unsupported:
            continue

        out_file = out_dir / f"{safe_filename(key)}.per_records.json"
        print(f"encoding {key} ({group.get('count')} records) -> {out_file.name}", flush=True)
        count, statuses = write_result_array(out_file, iter_group_entries(group_file, key, args.limit_per_group))
        processed_records += count
        total_statuses.update(statuses)
        summary_groups.append({
            "key": key,
            "source_file": group_file.name,
            "result_file": out_file.name,
            "input_count": group.get("count"),
            "processed_count": count,
            "statuses": dict(sorted(statuses.items())),
            "interfaces": group.get("interfaces") or {},
        })

    summary = {
        "group_dir": str(group_dir),
        "out_dir": str(out_dir),
        "pycrate_ok": enc._PYCRATE_OK,
        "processed_group_count": len(summary_groups),
        "processed_record_count": processed_records,
        "statuses": dict(sorted(total_statuses.items())),
        "groups": summary_groups,
    }
    report_path = out_dir / "_per_record_report.json"
    report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    print(f"done: {processed_records} records across {len(summary_groups)} groups")
    print(f"statuses: {dict(sorted(total_statuses.items()))}")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
