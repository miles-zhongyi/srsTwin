"""
Stitch 22_decoded records into per-UE timelines across trace files and sessions.

Pipeline:
  1. Stream records from *_TRC_decoded.json files
  2. Segment into UE sessions (S1AP id anchor + time/procedure correlation)
  3. Resolve a canonical UE key per session (randomValue / IMSI / S-TMSI)
  4. Merge sessions for the same UE across files
  5. Order messages by 3GPP attach phase + timestamp
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

try:
    from .identity import UeIdentity, extract_identity, pick_canonical_key, _choice
except ImportError:
    from identity import UeIdentity, extract_identity, pick_canonical_key, _choice

# Reuse streaming parser from dashboard export tool
import sys

_DASH = Path(__file__).resolve().parents[1] / "dashboard"
if str(_DASH) not in sys.path:
    sys.path.insert(0, str(_DASH))
from export_trace_groups import iter_trace_records  # noqa: E402

# 3GPP-ish ordering for decoded_choice / message_name substrings
_FLOW: list[tuple[str, int, str]] = [
    ("sib1", 110, "1 — Cell acquisition"),
    ("systeminformation", 120, "1 — Cell acquisition"),
    ("prach", 200, "2 — Random access"),
    ("random access", 210, "2 — Random access"),
    ("rrcconnectionrequest", 220, "2 — Random access"),
    ("rrcconnectionsetup", 230, "2 — Random access"),
    ("rrcconnectionreject", 235, "2 — Random access"),
    ("rrcconnectionsetupcomplete", 300, "3 — Setup complete"),
    ("attachrequest", 305, "3 — Setup complete"),
    ("s1_initial_ue_message", 320, "3 — Setup complete"),
    ("initialuemessage", 320, "3 — Setup complete"),
    ("authenticationrequest", 400, "4 — NAS auth"),
    ("authenticationresponse", 410, "4 — NAS auth"),
    ("securitymodecommand", 420, "4 — NAS auth"),
    ("securitymodecomplete", 430, "4 — NAS auth"),
    ("s1_downlink_nas_transport", 440, "4 — NAS auth"),
    ("s1_uplink_nas_transport", 450, "4 — NAS auth"),
    ("uecapabilityenquiry", 520, "5 — Bearer setup"),
    ("uecapabilityinformation", 530, "5 — Bearer setup"),
    ("s1_initial_context_setup_request", 540, "5 — Bearer setup"),
    ("initialcontextsetuprequest", 540, "5 — Bearer setup"),
    ("rrcconnectionreconfiguration", 550, "5 — Bearer setup"),
    ("attachaccept", 560, "5 — Bearer setup"),
    ("s1_initial_context_setup_response", 570, "5 — Bearer setup"),
    ("rrcconnectionreconfigurationcomplete", 580, "5 — Bearer setup"),
    ("attachcomplete", 600, "6 — Attach complete"),
    ("measurementreport", 700, "Connected mode"),
    ("countercheck", 710, "Connected mode"),
    ("paging", 720, "Connected mode"),
    ("rrcconnectionrelease", 900, "Release"),
    ("s1_ue_context_release", 910, "Release"),
    ("uecontextrelease", 910, "Release"),
]

_SESSION_START = re.compile(
    r"rrcconnectionrequest|s1_initial_ue_message|attachrequest",
    re.I,
)
_SESSION_END = re.compile(
    r"rrcconnectionrelease|s1_ue_context_release|uecontextrelease|"
    r"rrcconnectionreject|signallingconnectionrelease",
    re.I,
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def flow_rank(label: str) -> tuple[int, str]:
    n = _norm(label)
    for needle, rank, phase in _FLOW:
        if needle in n:
            return rank, phase
    return 8000, "Other"


def parse_ts(ts: str) -> float:
    try:
        t = ts.replace("Z", "+00:00")
        if "." in t:
            base, frac = t.split(".", 1)
            tz = ""
            if "+" in frac:
                frac, tz = frac.split("+", 1)
                tz = "+" + tz
            elif frac.count("-") > 0 and frac.rfind("-") > 6:
                idx = frac.rfind("-")
                tz = frac[idx:]
                frac = frac[:idx]
            dt = datetime.fromisoformat(f"{base}.{frac[:6]}{tz}")
        else:
            dt = datetime.fromisoformat(t)
        return dt.timestamp()
    except Exception:
        return 0.0


def default_trace_dir() -> Path:
    root = Path(__file__).resolve().parents[2]
    repo = root.parent
    for candidate in (root / "22_decoded", repo / "poc_StressTest" / "22_decoded"):
        if candidate.is_dir():
            return candidate
    return root / "22_decoded"


@dataclass
class StitchedRecord:
    timestamp: str
    ts_sort: float
    label: str
    flow_rank: int
    flow_phase: str
    interface: str
    record_id: int | None
    procedure_id: int | None
    direction: int | None
    source_file: str
    session_id: str
    raw: dict


@dataclass
class UeSession:
    session_id: str
    source_files: set[str] = field(default_factory=set)
    enb_ue_s1ap_id: int | None = None
    mme_ue_s1ap_id: int | None = None
    identity: UeIdentity | None = None
    records: list[dict] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0

    def absorb(self, rec: dict, source_file: str) -> None:
        self.records.append(rec)
        self.source_files.add(source_file)
        ts = parse_ts(str(rec.get("timestamp") or ""))
        if not self.start_ts or ts < self.start_ts:
            self.start_ts = ts
        if ts > self.end_ts:
            self.end_ts = ts
        if rec.get("enb_ue_s1ap_id") is not None:
            self.enb_ue_s1ap_id = int(rec["enb_ue_s1ap_id"])
        if rec.get("mme_ue_s1ap_id") is not None:
            self.mme_ue_s1ap_id = int(rec["mme_ue_s1ap_id"])


@dataclass
class UeTimeline:
    ue_key: str
    key_type: str
    identity: UeIdentity
    sessions: list[UeSession]
    records: list[StitchedRecord]
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ue_key": self.ue_key,
            "key_type": self.key_type,
            "identity": {
                "random_value": self.identity.random_value,
                "imsi": self.identity.imsi,
                "m_tmsi": self.identity.m_tmsi,
                "serving_plmn": self.identity.serving_plmn,
                "enb_id": self.identity.enb_id,
                "cell_id": self.identity.cell_id,
                "aliases": sorted(self.identity.aliases),
            },
            "stats": self.stats,
            "sessions": [
                {
                    "session_id": s.session_id,
                    "source_files": sorted(s.source_files),
                    "enb_ue_s1ap_id": s.enb_ue_s1ap_id,
                    "mme_ue_s1ap_id": s.mme_ue_s1ap_id,
                    "start_ts": s.start_ts,
                    "end_ts": s.end_ts,
                    "record_count": len(s.records),
                }
                for s in self.sessions
            ],
            "flow": [
                {
                    "timestamp": r.timestamp,
                    "label": r.label,
                    "flow_phase": r.flow_phase,
                    "flow_rank": r.flow_rank,
                    "interface": r.interface,
                    "record_id": r.record_id,
                    "procedure_id": r.procedure_id,
                    "direction": r.direction,
                    "source_file": r.source_file,
                    "session_id": r.session_id,
                }
                for r in self.records
            ],
        }


class StitchEngine:
    def __init__(self, trace_dir: Path | str | None = None, max_files: int = 0):
        self.trace_dir = Path(trace_dir) if trace_dir else default_trace_dir()
        self.max_files = max_files
        self._sessions: list[UeSession] = []
        self._ue_index: dict[str, UeTimeline] = {}

    def iter_files(self) -> list[Path]:
        files = sorted(self.trace_dir.rglob("*_TRC_decoded.json"))
        if self.max_files:
            files = files[: self.max_files]
        return files

    def iter_enriched(self) -> Iterator[tuple[dict, str]]:
        for path in self.iter_files():
            for rec in iter_trace_records(path):
                payload = dict(rec)
                payload["_source_file"] = path.name
                payload["_source_path"] = str(path)
                payload["_label"] = _choice(rec)
                yield payload, path.name

    def _session_key_for(self, rec: dict) -> str | None:
        enb = rec.get("enb_ue_s1ap_id")
        mme = rec.get("mme_ue_s1ap_id")
        if mme is not None and enb is not None:
            return f"s1:{mme}:{enb}"
        if enb is not None:
            return f"enb:{enb}"
        return None

    def segment_sessions(self) -> list[UeSession]:
        """Group records into per-connection sessions."""
        by_key: dict[str, UeSession] = {}
        orphans: list[tuple[dict, str]] = []

        for rec, src in self.iter_enriched():
            key = self._session_key_for(rec)
            if key:
                sess = by_key.setdefault(key, UeSession(session_id=key))
                sess.absorb(rec, src)
            else:
                orphans.append((rec, src))

        # Attach RRC-only records to sessions by m_tmsi + time overlap
        for rec, src in orphans:
            ts = parse_ts(str(rec.get("timestamp") or ""))
            mtmsi = rec.get("m_tmsi")
            best: UeSession | None = None
            best_gap = 999999.0
            for sess in by_key.values():
                if mtmsi is not None and any(r.get("m_tmsi") == mtmsi for r in sess.records):
                    if sess.start_ts - 5 <= ts <= sess.end_ts + 5:
                        gap = min(abs(ts - sess.start_ts), abs(ts - sess.end_ts))
                        if gap < best_gap:
                            best_gap = gap
                            best = sess
            if best:
                best.absorb(rec, src)
                continue

            # Standalone RRC session (e.g. connection request before S1 id assigned)
            ident = extract_identity(rec)
            if ident.random_value or _SESSION_START.search(rec.get("_label", "")):
                sid = f"rrc:{ident.random_value or rec.get('procedure_id')}"
                sess = by_key.setdefault(sid, UeSession(session_id=sid))
                sess.absorb(rec, src)
            else:
                sid = f"orphan:{rec.get('procedure_id')}:{src}"
                sess = by_key.setdefault(sid, UeSession(session_id=sid))
                sess.absorb(rec, src)

        sessions = sorted(by_key.values(), key=lambda s: s.start_ts)
        self._sessions = sessions
        return sessions

    def _link_sessions(self, sessions: list[UeSession]) -> dict[str, list[UeSession]]:
        """Group sessions by UE; merge only on stable keys (IMSI, randomValue)."""
        buckets: dict[str, list[UeSession]] = defaultdict(list)
        random_to_key: dict[str, str] = {}
        imsi_to_key: dict[str, str] = {}

        for sess in sessions:
            idents = [extract_identity(r) for r in sess.records]
            canon = pick_canonical_key(idents)
            sess.identity = canon
            buckets[canon.ue_key].append(sess)
            if canon.random_value:
                random_to_key.setdefault(canon.random_value, canon.ue_key)
            if canon.imsi:
                imsi_to_key.setdefault(canon.imsi, canon.ue_key)

        def resolve_key(key: str, identity: UeIdentity | None) -> str:
            if identity:
                if identity.imsi and identity.imsi in imsi_to_key:
                    return imsi_to_key[identity.imsi]
                if identity.random_value and identity.random_value in random_to_key:
                    return random_to_key[identity.random_value]
            return key

        merged: dict[str, list[UeSession]] = defaultdict(list)
        for key, group in buckets.items():
            root = resolve_key(key, group[0].identity if group else None)
            merged[root].extend(group)

        for key in merged:
            seen: set[str] = set()
            uniq: list[UeSession] = []
            for s in sorted(merged[key], key=lambda x: x.start_ts):
                if s.session_id in seen:
                    continue
                seen.add(s.session_id)
                uniq.append(s)
            merged[key] = uniq
        return dict(merged)

    def _build_timeline(self, ue_key: str, sessions: list[UeSession]) -> UeTimeline:
        idents = [s.identity for s in sessions if s.identity]
        identity = pick_canonical_key(idents) if idents else UeIdentity(ue_key=ue_key, key_type="unknown")

        stitched: list[StitchedRecord] = []
        for sess in sessions:
            for rec in sess.records:
                label = rec.get("_label") or _choice(rec)
                rank, phase = flow_rank(label)
                stitched.append(
                    StitchedRecord(
                        timestamp=str(rec.get("timestamp") or ""),
                        ts_sort=parse_ts(str(rec.get("timestamp") or "")),
                        label=label,
                        flow_rank=rank,
                        flow_phase=phase,
                        interface=str(rec.get("interface") or rec.get("protocol") or ""),
                        record_id=rec.get("record_id"),
                        procedure_id=rec.get("procedure_id"),
                        direction=rec.get("direction"),
                        source_file=str(rec.get("_source_file") or ""),
                        session_id=sess.session_id,
                        raw=rec,
                    )
                )

        stitched.sort(key=lambda r: (r.ts_sort, r.flow_rank, r.procedure_id or 0))

        phases = defaultdict(int)
        for r in stitched:
            phases[r.flow_phase] += 1

        return UeTimeline(
            ue_key=ue_key,
            key_type=identity.key_type,
            identity=identity,
            sessions=sessions,
            records=stitched,
            stats={
                "session_count": len(sessions),
                "record_count": len(stitched),
                "source_files": sorted({f for s in sessions for f in s.source_files}),
                "phase_counts": dict(phases),
                "time_start": min((r.timestamp for r in stitched), default=""),
                "time_end": max((r.timestamp for r in stitched), default=""),
            },
        )

    def build(self) -> dict[str, UeTimeline]:
        sessions = self.segment_sessions()
        linked = self._link_sessions(sessions)
        self._ue_index = {k: self._build_timeline(k, v) for k, v in linked.items()}
        return self._ue_index

    def list_ues(self) -> list[dict[str, Any]]:
        if not self._ue_index:
            self.build()
        rows = []
        for key, tl in sorted(self._ue_index.items(), key=lambda kv: -len(kv[1].records)):
            rows.append({
                "ue_key": key,
                "key_type": tl.key_type,
                "sessions": len(tl.sessions),
                "records": len(tl.records),
                "random_value": tl.identity.random_value,
                "imsi": tl.identity.imsi,
                "source_files": len(tl.stats.get("source_files", [])),
            })
        return rows

    def get_ue(self, ue_key: str) -> UeTimeline | None:
        if not self._ue_index:
            self.build()
        if ue_key in self._ue_index:
            return self._ue_index[ue_key]
        for key, tl in self._ue_index.items():
            if ue_key in tl.identity.aliases:
                return tl
        return None

    def export_index(self, path: Path) -> None:
        if not self._ue_index:
            self.build()
        payload = {
            "trace_dir": str(self.trace_dir),
            "files_scanned": len(self.iter_files()),
            "ue_count": len(self._ue_index),
            "ues": self.list_ues(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def export_ue(self, ue_key: str, path: Path, include_raw: bool = False) -> None:
        tl = self.get_ue(ue_key)
        if not tl:
            raise KeyError(f"UE not found: {ue_key}")
        payload = tl.to_dict()
        if include_raw:
            payload["records_raw"] = [r.raw for r in tl.records]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def export_all(self, out_dir: Path, include_raw: bool = False) -> dict[str, Any]:
        if not self._ue_index:
            self.build()
        out_dir.mkdir(parents=True, exist_ok=True)
        for key, tl in self._ue_index.items():
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:120]
            self.export_ue(key, out_dir / f"{safe}.json", include_raw=include_raw)
        index_path = out_dir / "_ue_index.json"
        self.export_index(index_path)
        return {
            "out_dir": str(out_dir),
            "ue_count": len(self._ue_index),
            "index": str(index_path),
        }
