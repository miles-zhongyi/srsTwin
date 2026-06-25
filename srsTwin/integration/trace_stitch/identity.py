"""Extract UE identity fields from 22_decoded records."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_IMSI_RE = re.compile(r"(?:^|[^0-9a-fA-F])(3[0-9]{4}[0-9a-fA-F]{10,20})(?:[^0-9a-fA-F]|$)")


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _hex_bits(value: Any) -> str | None:
    if isinstance(value, list) and value and isinstance(value[0], str):
        return str(value[0]).lower()
    if isinstance(value, str):
        return value.lower()
    return None


def _choice(rec: dict) -> str:
    meta = rec.get("decoding_metadata") or {}
    choice = meta.get("decoded_message_choice") or rec.get("message_name") or ""
    if choice == "c1":
        msg = (rec.get("decoded") or {}).get("message")
        if isinstance(msg, list) and len(msg) >= 2:
            inner = msg[1]
            if isinstance(inner, list) and inner and isinstance(inner[0], str):
                return inner[0]
    return str(choice)


def _rrc_random_value(rec: dict) -> str | None:
    msg = (rec.get("decoded") or {}).get("message")
    if not isinstance(msg, list):
        return None
    for node in _walk(msg):
        if "ue-Identity" in node:
            ident = node["ue-Identity"]
            if isinstance(ident, list) and len(ident) >= 2 and ident[0] == "randomValue":
                return _hex_bits(ident[1])
        if "randomValue" in node:
            return _hex_bits(node["randomValue"])
    return None


def _rrc_stmsi(rec: dict) -> tuple[int | None, int | None]:
    msg = (rec.get("decoded") or {}).get("message")
    if not isinstance(msg, list):
        return None, None
    for node in _walk(msg):
        if "ue-Identity" not in node:
            continue
        ident = node["ue-Identity"]
        if not isinstance(ident, list) or len(ident) < 2:
            continue
        if ident[0] != "s-TMSI":
            continue
        body = ident[1]
        if not isinstance(body, dict):
            continue
        mmec = body.get("mmec")
        mtmsi = body.get("m-TMSI")
        mmec_val = int(mmec[0], 16) if isinstance(mmec, list) and mmec else None
        mtmsi_val = int(mtmsi[0], 16) if isinstance(mtmsi, list) and mtmsi else None
        return mmec_val, mtmsi_val
    return None, None


def _nas_hex_blobs(rec: dict) -> list[str]:
    out: list[str] = []
    decoded = rec.get("decoded") or {}
    for node in _walk(decoded):
        for key, val in node.items() if isinstance(node, dict) else []:
            if key in ("value", "nas-PDU", "nasPDU") and isinstance(val, str) and len(val) >= 8:
                out.append(val.lower())
    return out


def _imsi_from_nas_hex(hex_str: str) -> str | None:
    # BCD IMSI often appears as hex in attach request (e.g. 30221...)
    for m in _IMSI_RE.finditer(hex_str):
        digits = m.group(1)
        if len(digits) >= 14:
            return digits[:15]
    return None


@dataclass
class UeIdentity:
    """Stable UE key material gathered from one or more records."""

    ue_key: str
    key_type: str  # random | stmsi | imsi | s1ap | mtmsi | unknown
    random_value: str | None = None
    imsi: str | None = None
    m_tmsi: int | None = None
    mmec: int | None = None
    enb_ue_s1ap_id: int | None = None
    mme_ue_s1ap_id: int | None = None
    serving_plmn: str | None = None
    enb_id: str | None = None
    cell_id: int | None = None
    aliases: set[str] = field(default_factory=set)

    def merge(self, other: UeIdentity) -> None:
        if not self.random_value and other.random_value:
            self.random_value = other.random_value
        if not self.imsi and other.imsi:
            self.imsi = other.imsi
        if self.m_tmsi is None and other.m_tmsi is not None:
            self.m_tmsi = other.m_tmsi
        if self.mmec is None and other.mmec is not None:
            self.mmec = other.mmec
        if self.enb_ue_s1ap_id is None and other.enb_ue_s1ap_id is not None:
            self.enb_ue_s1ap_id = other.enb_ue_s1ap_id
        if self.mme_ue_s1ap_id is None and other.mme_ue_s1ap_id is not None:
            self.mme_ue_s1ap_id = other.mme_ue_s1ap_id
        if not self.serving_plmn and other.serving_plmn:
            self.serving_plmn = other.serving_plmn
        if not self.enb_id and other.enb_id:
            self.enb_id = other.enb_id
        if self.cell_id is None and other.cell_id is not None:
            self.cell_id = other.cell_id
        self.aliases.update(other.aliases)


def extract_identity(rec: dict) -> UeIdentity:
    """Derive identity hints from a single 22_decoded record."""
    choice = _choice(rec)
    random_value = _rrc_random_value(rec) if "rrcconnectionrequest" in choice.lower() else None
    mmec, stmsi = _rrc_stmsi(rec) if "rrcconnectionrequest" in choice.lower() else (None, None)

    imsi = None
    for blob in _nas_hex_blobs(rec):
        imsi = _imsi_from_nas_hex(blob)
        if imsi:
            break

    m_tmsi = rec.get("m_tmsi")
    if m_tmsi is not None:
        m_tmsi = int(m_tmsi)

    enb_id = rec.get("enb_ue_s1ap_id")
    mme_id = rec.get("mme_ue_s1ap_id")

    if imsi:
        key, ktype = f"imsi:{imsi}", "imsi"
    elif random_value:
        key, ktype = f"random:{random_value}", "random"
    elif stmsi is not None:
        key, ktype = f"stmsi:{mmec or 0}:{stmsi:08x}", "stmsi"
    elif mme_id is not None and enb_id is not None:
        key, ktype = f"s1ap:{mme_id}:{enb_id}", "s1ap"
    elif enb_id is not None:
        key, ktype = f"enb:{enb_id}", "enb"
    elif m_tmsi is not None and int(m_tmsi) != 1048575:
        key, ktype = f"mtmsi:{m_tmsi}", "mtmsi"
    else:
        key, ktype = "unknown:anonymous", "unknown"

    ident = UeIdentity(
        ue_key=key,
        key_type=ktype,
        random_value=random_value,
        imsi=imsi,
        m_tmsi=m_tmsi,
        mmec=mmec,
        enb_ue_s1ap_id=int(enb_id) if enb_id is not None else None,
        mme_ue_s1ap_id=int(mme_id) if mme_id is not None else None,
        serving_plmn=rec.get("serving_plmn"),
        enb_id=str(rec.get("enb_id")) if rec.get("enb_id") is not None else None,
        cell_id=int(rec["cell_id"]) if rec.get("cell_id") is not None else None,
    )
    if not key.startswith("unknown:"):
        ident.aliases.add(key)
        if random_value:
            ident.aliases.add(f"random:{random_value}")
        if imsi:
            ident.aliases.add(f"imsi:{imsi}")
        if enb_id is not None:
            ident.aliases.add(f"enb:{enb_id}")
    return ident


def pick_canonical_key(idents: list[UeIdentity]) -> UeIdentity:
    """Choose the best stable UE key from session identity observations."""
    merged = UeIdentity(ue_key="unknown:anonymous", key_type="unknown")
    for ident in idents:
        merged.merge(ident)

    if merged.imsi:
        merged.ue_key = f"imsi:{merged.imsi}"
        merged.key_type = "imsi"
    elif merged.random_value:
        merged.ue_key = f"random:{merged.random_value}"
        merged.key_type = "random"
    elif merged.m_tmsi is not None and merged.m_tmsi != 1048575:
        merged.ue_key = f"mtmsi:{merged.m_tmsi}"
        merged.key_type = "mtmsi"
    elif merged.enb_ue_s1ap_id is not None:
        merged.ue_key = f"enb:{merged.enb_ue_s1ap_id}"
        merged.key_type = "enb"
    return merged
