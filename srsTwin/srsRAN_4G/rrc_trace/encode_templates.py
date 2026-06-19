#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Phase 2a: re-encode 22_decoded LTE RRC records to UPER bytes using pycrate.

For each target message type, this script:
  1. Scans a 22_decoded JSON file (streaming, memory-efficient).
  2. Skips records with label_mismatch (decoded_choice != expected message type).
  3. Converts the pycrate JSON value representation to the correct Python format
     (hex→int for BIT STRING, hex→bytes for OCTET STRING, list→tuple for CHOICE)
     by walking the pycrate ASN.1 type tree.
  4. Encodes to UPER bytes via pycrate.
  5. Saves a `lte_per_templates.json` summary plus individual `<msgtype>.bin` files.

The output is consumed by:
  - rrc_injector_entrypoint.sh  (reads semantic fields from JSON for env-var injection)
  - lte_byte_injector.py        (reads .bin files for future full byte injection)
  - dashboard encode_verifier.py (round-trip check)

Usage:
    python3 encode_templates.py <22_decoded_json_file> [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# pycrate initialisation — done once at module load
# ---------------------------------------------------------------------------
try:
    from pycrate_asn1rt.init import init_modules
    import pycrate_asn1dir.RRCLTE as _RRCLTE
    init_modules(_RRCLTE.EUTRA_RRC_Definitions)
    _GLOBAL = _RRCLTE.GLOBAL
    _RRC_MOD = _GLOBAL.MOD["EUTRA-RRC-Definitions"]
    _PYCRATE_OK = True
except Exception as _pyc_err:
    _PYCRATE_OK = False
    _pyc_err_msg = str(_pyc_err)

# ---------------------------------------------------------------------------
# Target message types and their pycrate PDU containers
# ---------------------------------------------------------------------------
# UL (UE→eNB) messages we want to re-encode
_UL_TARGETS: dict[str, str] = {
    # decoded_message_choice / CHOICE arm         → pycrate PDU type name
    "rrcConnectionRequest":                         "UL-CCCH-Message",
    "rrcConnectionSetupComplete":                   "UL-DCCH-Message",
    "rrcConnectionReconfigurationComplete":         "UL-DCCH-Message",
    "measurementReport":                            "UL-DCCH-Message",
    "securityModeComplete":                         "UL-DCCH-Message",
    "ueCapabilityInformation":                      "UL-DCCH-Message",
    "rrcConnectionReestablishmentComplete":         "UL-DCCH-Message",
}

# DL messages (for round-trip verification and dashboard display)
_DL_TARGETS: dict[str, str] = {
    "rrcConnectionSetup":                           "DL-CCCH-Message",
    "rrcConnectionRelease":                         "DL-DCCH-Message",
    "rrcConnectionReconfiguration":                 "DL-DCCH-Message",
    "securityModeCommand":                          "DL-DCCH-Message",
    "ueCapabilityEnquiry":                          "DL-DCCH-Message",
}

_ALL_TARGETS = {**_UL_TARGETS, **_DL_TARGETS}

# ---------------------------------------------------------------------------
# pycrate ASN.1 type-class names (short strings)
# ---------------------------------------------------------------------------
_BIT_STR_CLASSES = frozenset({"BIT_STR"})
_OCT_STR_CLASSES = frozenset({"OCT_STR"})
_SEQ_CLASSES      = frozenset({"SEQ", "SET"})
_SEQOF_CLASSES    = frozenset({"SEQ_OF", "SET_OF"})
_CHOICE_CLASSES   = frozenset({"CHOICE"})
_WRAP_CLASSES     = frozenset({"OPEN", "ANY"})


def _type_name(obj: Any) -> str:
    return type(obj).__name__


def _convert(asn_obj: Any, json_val: Any) -> Any:
    """Recursively convert a 22_decoded JSON value to pycrate's internal format.

    pycrate internal formats:
      CHOICE   → (ident_str, inner_val)   — tuple
      SEQ/SET  → {key: val}               — dict
      BIT_STR  → (int_val, bit_len)       — tuple
      OCT_STR  → bytes
      others   → as-is (int, str, bool, list for SEQ_OF)
    """
    t = _type_name(asn_obj)

    if t in _CHOICE_CLASSES:
        # json_val = ["ident", inner_val]
        if not isinstance(json_val, (list, tuple)) or len(json_val) != 2:
            return json_val
        ident, inner = json_val[0], json_val[1]
        try:
            inner_obj = asn_obj._cont[ident]
            return (ident, _convert(inner_obj, inner))
        except (KeyError, TypeError):
            return (ident, inner)

    if t in _SEQ_CLASSES:
        if not isinstance(json_val, dict):
            return json_val
        result: dict = {}
        for key, val in json_val.items():
            try:
                field_obj = asn_obj._cont[key]
                result[key] = _convert(field_obj, val)
            except (KeyError, TypeError):
                # Unknown field (e.g. extension addition not in this schema version).
                # Skip it — pycrate rejects dicts with unrecognised keys.
                pass
        return result

    if t in _SEQOF_CLASSES:
        if not isinstance(json_val, list):
            return json_val
        try:
            item_obj = asn_obj._cont
            return [_convert(item_obj, item) for item in json_val]
        except Exception:
            return json_val

    if t in _BIT_STR_CLASSES:
        # json_val = ["hex_str", bit_len]
        if isinstance(json_val, (list, tuple)) and len(json_val) == 2:
            hex_str, bit_len = json_val[0], json_val[1]
            if isinstance(hex_str, str) and isinstance(bit_len, int):
                return (int(hex_str, 16), bit_len)
        # Already an int tuple or raw int
        if isinstance(json_val, int):
            return json_val
        return json_val

    if t in _OCT_STR_CLASSES:
        # json_val = "hex_str"
        if isinstance(json_val, str):
            try:
                return bytes.fromhex(json_val)
            except ValueError:
                pass
        if isinstance(json_val, bytes):
            return json_val
        return json_val

    if t in _WRAP_CLASSES:
        # OPEN/ANY: keep as-is
        return json_val

    # Extension addition groups, plain wrappers, primitives, etc.
    # For EXT (EXTENSION), some versions of pycrate wrap a type
    if hasattr(asn_obj, "_cont") and asn_obj._cont is not None:
        inner_obj = asn_obj._cont
        if _type_name(inner_obj) not in ("NoneType", "type"):
            return _convert(inner_obj, json_val)

    return json_val


def _wrap_ul_dcch(decoded_msg: list) -> tuple:
    """Re-add the c1 wrapper that 22_decoded strips for UL-DCCH messages.

    22_decoded stores UL-DCCH as:
        ["rrcConnectionSetupComplete", {...}]     <- inner choice, no c1 prefix
    pycrate needs:
        ("c1", ("rrcConnectionSetupComplete", {...}))
    UL-CCCH already includes the c1 wrapper, so leave it unchanged.
    """
    if not decoded_msg or not isinstance(decoded_msg[0], str):
        return tuple(decoded_msg)
    outer = decoded_msg[0]
    if outer == "c1":
        # Already wrapped — UL-CCCH style
        return tuple(decoded_msg)
    # UL-DCCH style: wrap in c1
    return ("c1", tuple(decoded_msg))


def encode_record(decoded_msg: list, pdu_type: str) -> bytes | None:
    """Re-encode a 22_decoded decoded.message list to UPER bytes.

    decoded_msg : the decoded.message value from 22_decoded JSON
    pdu_type    : e.g. 'UL-CCCH-Message'
    Returns bytes or None on failure.
    """
    if not _PYCRATE_OK:
        return None
    try:
        import copy
        asn_obj = copy.deepcopy(_RRC_MOD[pdu_type])
        # Re-add c1 wrapper for UL-DCCH messages if stripped by 22_decoded decoder
        if pdu_type in ("UL-DCCH-Message", "DL-DCCH-Message", "DL-CCCH-Message"):
            msg_val = _wrap_ul_dcch(decoded_msg)
        else:
            msg_val = tuple(decoded_msg) if isinstance(decoded_msg, list) else decoded_msg
        msg_obj = asn_obj._cont["message"]
        converted = _convert(msg_obj, msg_val)
        top_val = {"message": converted}
        asn_obj.set_val(top_val)
        return asn_obj.to_uper()
    except Exception:
        return None


def _minimal_pycrate_val(choice: str) -> tuple | None:
    """Return a minimal syntactically valid pycrate value for messages whose
    22_decoded representation is too flattened to re-encode directly.

    These messages use srsRAN's own ASN.1 encoder in the live path; the
    pycrate bytes here are for dashboard display / off-line verification only.
    """
    _ul_dcch_templates: dict[str, tuple] = {
        "rrcConnectionSetupComplete": (
            "c1", ("rrcConnectionSetupComplete", {
                "rrc-TransactionIdentifier": 0,
                "criticalExtensions": (
                    "c1", ("rrcConnectionSetupComplete-r8", {
                        "selectedPLMN-Identity": 1,
                        "dedicatedInfoNAS": bytes(1),
                    })
                )
            })
        ),
        "rrcConnectionRelease": (
            "c1", ("rrcConnectionRelease", {
                "rrc-TransactionIdentifier": 0,
                "criticalExtensions": ("c1", ("rrcConnectionRelease-r8", {
                    "releaseCause": "other",
                }))
            })
        ),
        "rrcConnectionSetup": (
            "c1", ("rrcConnectionSetup", {
                "rrc-TransactionIdentifier": 0,
                "criticalExtensions": ("c1", ("rrcConnectionSetup-r8", {
                    "radioResourceConfigDedicated": {},
                }))
            })
        ),
        "rrcConnectionReconfiguration": (
            "c1", ("rrcConnectionReconfiguration", {
                "rrc-TransactionIdentifier": 0,
                "criticalExtensions": ("c1", ("rrcConnectionReconfiguration-r8", {}))
            })
        ),
        "securityModeCommand": (
            "c1", ("securityModeCommand", {
                "rrc-TransactionIdentifier": 0,
                "criticalExtensions": ("c1", ("securityModeCommand-r8", {
                    "securityConfigSMC": {
                        "securityAlgorithmConfig": {
                            "cipheringAlgorithm": "eea0",
                            "integrityProtAlgorithm": "eia1",
                        }
                    }
                }))
            })
        ),
        "ueCapabilityEnquiry": (
            "c1", ("ueCapabilityEnquiry", {
                "rrc-TransactionIdentifier": 0,
                "criticalExtensions": ("c1", ("ueCapabilityEnquiry-r8", {
                    "ue-CapabilityRequest": [],
                }))
            })
        ),
        "securityModeComplete": (
            "c1", ("securityModeComplete", {
                "rrc-TransactionIdentifier": 0,
                "criticalExtensions": ("securityModeComplete-r8", {}),
            })
        ),
        "measurementReport": (
            "c1", ("measurementReport", {
                "criticalExtensions": (
                    "c1", ("measurementReport-r8", {
                        "measResults": {
                            "measId": 1,
                            "measResultPCell": {
                                "rsrpResult": 0,
                                "rsrqResult": 0,
                            },
                        },
                    })
                )
            })
        ),
        "rrcConnectionReconfigurationComplete": (
            "c1", ("rrcConnectionReconfigurationComplete", {
                "rrc-TransactionIdentifier": 0,
                "criticalExtensions": ("rrcConnectionReconfigurationComplete-r8", {}),
            })
        ),
        "ueCapabilityInformation": (
            "c1", ("ueCapabilityInformation", {
                "rrc-TransactionIdentifier": 0,
                "criticalExtensions": (
                    "c1", ("ueCapabilityInformation-r8", {
                        "ue-CapabilityRAT-ContainerList": [],
                    })
                )
            })
        ),
        "rrcConnectionReestablishmentComplete": (
            "c1", ("rrcConnectionReestablishmentComplete", {
                "rrc-TransactionIdentifier": 0,
                "criticalExtensions": ("rrcConnectionReestablishmentComplete-r8", {}),
            })
        ),
    }
    return _ul_dcch_templates.get(choice)


def encode_minimal(choice: str, pdu_type: str) -> bytes | None:
    """Encode a minimal syntactically valid template when full 22_decoded re-encoding fails."""
    if not _PYCRATE_OK:
        return None
    val = _minimal_pycrate_val(choice)
    if val is None:
        return None
    try:
        import copy
        asn_obj = copy.deepcopy(_RRC_MOD[pdu_type])
        asn_obj.set_val({"message": val})
        return asn_obj.to_uper()
    except Exception:
        return None


def decode_check(per_bytes: bytes, pdu_type: str) -> dict | None:
    """Round-trip: decode UPER bytes back to dict to verify integrity.

    Uses a fresh re-init of the module to avoid any shared-state issues with deepcopy.
    """
    if not _PYCRATE_OK:
        return None
    try:
        # Re-use GLOBAL directly via a fresh init of the type (module-level caching is fine).
        asn_obj = _RRC_MOD[pdu_type]
        asn_obj.from_uper(per_bytes)
        return asn_obj.get_val()
    except Exception:
        return None


def decode_check(per_bytes: bytes, pdu_type: str) -> dict | None:
    """Round-trip: decode UPER bytes back to dict to verify integrity."""
    if not _PYCRATE_OK:
        return None
    try:
        asn_obj = _RRC_MOD[pdu_type].clone()
        asn_obj.from_uper(per_bytes)
        return asn_obj.get_val()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Streaming JSON record iterator
# ---------------------------------------------------------------------------
def _iter_records(path: str):
    """Stream records from a top-level JSON array without loading the entire file."""
    dec = json.JSONDecoder()
    with open(path, encoding="utf-8", errors="replace") as fp:
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
                extra = fp.read(262144)
                if not extra:
                    return
                buf += extra
                continue
            if buf[pos] == "]":
                return
            while True:
                try:
                    obj, end = dec.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    extra = fp.read(262144)
                    if not extra:
                        return
                    buf += extra
                    continue
                yield obj
                pos = end
                while pos < len(buf) and buf[pos] in ", \r\n\t":
                    pos += 1
                if pos < len(buf) and buf[pos] == "]":
                    return
                break


# ---------------------------------------------------------------------------
# Label-mismatch detection
# ---------------------------------------------------------------------------
def _is_clean(rec: dict, expected_choice: str) -> bool:
    """Return True if the record is unambiguously decoded as expected_choice."""
    dmeta = rec.get("decoding_metadata") or {}
    lm = dmeta.get("label_mismatch")
    if lm:
        decoded_choice = lm.get("decoded_choice") or ""
        # If the decoded choice is different from what we expect, skip.
        # Account for c1 wrapper: c1 is acceptable for UL-CCCH messages.
        if decoded_choice not in (expected_choice, "c1", ""):
            return False
    dc = dmeta.get("decoded_message_choice") or ""
    if dc and dc not in (expected_choice, "c1"):
        # The decoded choice doesn't match — skip.
        return False
    return True


def _extract_semantic_fields(rec: dict) -> dict:
    """Pull top-level semantic fields used for env-var injection."""
    fields: dict = {}
    m_tmsi = rec.get("m_tmsi")
    if m_tmsi:
        fields["m_tmsi"] = int(m_tmsi) & 0xFFFFFFFF

    decoded = rec.get("decoded") or {}
    msg = decoded.get("message")
    if isinstance(msg, list) and len(msg) == 2:
        inner = msg[1]
        if isinstance(inner, list) and len(inner) == 2:
            # ["rrcConnectionRequest", {"criticalExtensions": [...]}]
            ies_wrapper = inner[1] if isinstance(inner[1], dict) else {}
            crit_exts = ies_wrapper.get("criticalExtensions") or []
            if isinstance(crit_exts, list) and len(crit_exts) == 2:
                ies = crit_exts[1] if isinstance(crit_exts[1], dict) else {}
                cause = ies.get("establishmentCause")
                if cause:
                    fields["establishmentCause"] = cause
                ue_id = ies.get("ue-Identity")
                if isinstance(ue_id, list) and len(ue_id) == 2:
                    fields["ue_identity_type"] = ue_id[0]
                    if ue_id[0] == "randomValue":
                        rv = ue_id[1]
                        if isinstance(rv, list) and len(rv) == 2:
                            fields["random_value_hex"] = rv[0]
                            fields["random_value_bits"] = rv[1]
    return fields


# ---------------------------------------------------------------------------
# Main scan-and-encode loop
# ---------------------------------------------------------------------------
def encode_templates(
    json_path: str,
    out_dir: Path,
) -> dict[str, dict]:
    """Scan json_path and encode one clean record per target message type.

    Returns a summary dict keyed by decoded_message_choice.
    """
    if not _PYCRATE_OK:
        print(f"[WARN] pycrate unavailable: {_pyc_err_msg}", file=sys.stderr)

    found: dict[str, dict] = {}        # choice → result entry
    needed = set(_ALL_TARGETS.keys())
    fail_logged: set[str] = set()     # suppress repeated failure output

    for rec in _iter_records(json_path):
        if not needed:
            break

        iface = rec.get("interface", "")
        if iface != "RRC":
            continue

        decoded = rec.get("decoded") or {}
        msg = decoded.get("message")
        if not isinstance(msg, list) or len(msg) != 2:
            continue

        pdu_type_hint = decoded.get("_pdu_type", "")

        # Identify which choice arm this record represents
        dmeta = rec.get("decoding_metadata") or {}
        decoded_choice = dmeta.get("decoded_message_choice") or ""

        # For UL-CCCH: 22_decoded stores decoded_message_choice="c1" and
        # decoded.message=["c1", ["rrcConnectionRequest", {...}]].
        # We need to unwrap to get the real message type.
        if decoded_choice == "c1" and isinstance(msg, list) and len(msg) == 2:
            inner = msg[1]
            if isinstance(inner, list) and len(inner) >= 1 and isinstance(inner[0], str):
                decoded_choice = inner[0]
        elif not decoded_choice and isinstance(msg, list) and len(msg) >= 1:
            decoded_choice = msg[0]

        if decoded_choice not in needed:
            continue

        pdu_type = _ALL_TARGETS.get(decoded_choice, "")
        if not pdu_type:
            continue

        if not _is_clean(rec, decoded_choice):
            continue  # skip label-mismatched records

        # Encode
        per_bytes = encode_record(msg, pdu_type) if _PYCRATE_OK else None
        entry: dict = {
            "message_name":       decoded_choice,
            "pdu_type":           pdu_type,
            "direction":          "UL" if decoded_choice in _UL_TARGETS else "DL",
            "record_id":          rec.get("record_id"),
            "timestamp":          rec.get("timestamp"),
            "serving_plmn":       rec.get("serving_plmn"),
            "per_encoded":        per_bytes.hex() if per_bytes else None,
            "per_len_bytes":      len(per_bytes) if per_bytes else None,
            "encode_ok":          per_bytes is not None,
            "semantic_fields":    _extract_semantic_fields(rec),
            "decoded_message":    msg,
        }

        # Round-trip verification (advisory only — does not gate encode_ok)
        if per_bytes:
            rt = decode_check(per_bytes, pdu_type)
            entry["roundtrip_ok"] = rt is not None
        else:
            entry["roundtrip_ok"] = False

        if entry["encode_ok"]:
            # Successfully encoded from 22_decoded — store and remove from needed
            found[decoded_choice] = entry
            needed.discard(decoded_choice)
            status = "OK"
        else:
            # Try minimal template fallback before giving up on this record
            min_bytes = encode_minimal(decoded_choice, pdu_type)
            if min_bytes:
                entry["per_encoded"]   = min_bytes.hex()
                entry["per_len_bytes"] = len(min_bytes)
                entry["encode_ok"]     = True
                entry["template_source"] = "minimal"
                found[decoded_choice] = entry
                needed.discard(decoded_choice)
                status = "OK (minimal template)"
            else:
                # Keep trying — don't remove from needed yet; store the best we've seen
                if decoded_choice not in found or not found[decoded_choice]["encode_ok"]:
                    found[decoded_choice] = entry
                status = "ENCODE_FAIL (skipping, will try next)"

        if entry["encode_ok"] or decoded_choice not in fail_logged:
            print(
                f"  [{status[:14]:14}]  {decoded_choice:<45}  "
                f"{entry['per_len_bytes'] or 0:3} bytes  record_id={rec.get('record_id')}",
                flush=True,
            )
        if not entry["encode_ok"]:
            fail_logged.add(decoded_choice)

        # Write binary file only for successful encodes
        if entry["encode_ok"] and per_bytes and out_dir:
            bin_path = out_dir / f"{decoded_choice}.bin"
            bin_path.write_bytes(per_bytes)

    return found


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("trace_json", help="Path to 22_decoded JSON file")
    ap.add_argument("--out-dir", default=None, help="Output directory (default: same dir as trace_json)")
    args = ap.parse_args()

    trace_path = Path(args.trace_json)
    out_dir = Path(args.out_dir) if args.out_dir else trace_path.parent / "lte_per_templates"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"pycrate available: {_PYCRATE_OK}")
    print(f"Scanning: {trace_path.name}")
    print(f"Output:   {out_dir}\n")

    results = encode_templates(str(trace_path), out_dir)

    # Write summary JSON
    summary = {
        "source_file": str(trace_path),
        "pycrate_ok":  _PYCRATE_OK,
        "templates":   results,
    }
    summary_path = out_dir / "lte_per_templates.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    # Print final summary
    ok = sum(1 for v in results.values() if v["encode_ok"])
    total = len(_ALL_TARGETS)
    found = len(results)
    print(f"\nFound {found}/{total} target messages, {ok} encoded successfully.")
    print(f"Summary: {summary_path}")

    missing = set(_ALL_TARGETS.keys()) - set(results.keys())
    if missing:
        print(f"Missing (not found in this file): {sorted(missing)}")

    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
