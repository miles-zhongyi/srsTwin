"""
parse_lw5g.py  –  Lightweight 5G Twin log parser  (DU test-mode edition)
=========================================================================
OCUDU gnb runs in test_mode with ru_dummy.  The phantom UEs cycle at the
DU level — the gNB DU injects RRC Setup messages to each phantom UE and
manages attach/run/release/guard cycles entirely within the gnb process.

Actual log lines (from the [DU] logger, produced every ~10 s per cycle):

  [DU      ] [I] TEST_MODE: Injected F1 Setup Response
  [DU      ] [I] TEST_MODE rnti=0x4444: Injected DL RRC Message (rrcSetup)
  [DU      ] [I] TEST_MODE cell=0: All 3 UE(s) established. Running for 8000 ms.
  [DU      ] [I] TEST_MODE cell=0: Attach/detach duration elapsed. Releasing 3 UE(s).
  [DU      ] [I] TEST_MODE cell=0: All UE(s) released. Entering guard period.
  [DU      ] [I] TEST_MODE cell=0: Guard period elapsed. Starting new creation cycle.

NG Setup (between gnb and Open5GS AMF) happens once at startup:
  [NGAP    ] [I] Tx PDU: NGSetupRequest
  [NGAP    ] [I] Rx PDU: NGSetupResponse

The dashboard ladder shows 2 lanes: Phantom UE ← OCUDU gNB.
KPIs: cycle rate, setup latency (first rrcSetup → All established), active UEs.
"""
from __future__ import annotations

import os
import re
import statistics
import subprocess
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Log tailing
# ---------------------------------------------------------------------------
# We grep for only DU TEST_MODE + NGAP lines to avoid the huge PHY/MAC noise.
# Keep last N matching lines (each cycle produces ~6 DU lines; this covers >8 h).
LOG_GREP_LINES = 3_000

_TS_PAT    = r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)"
_RNTI_PAT  = r"rnti=(0x[0-9a-fA-F]+)"

# ---- NG Setup (one-shot at gnb startup) ----
_NG_SETUP_REQ  = re.compile(_TS_PAT + r".*\[NGAP\s*\].*Tx PDU: NGSetupRequest")
_NG_SETUP_RSP  = re.compile(_TS_PAT + r".*\[NGAP\s*\].*Rx PDU: NGSetupResponse")

# ---- DU test-mode cycle events ----
_F1_SETUP      = re.compile(_TS_PAT + r".*\[DU\s*\].*TEST_MODE: Injected F1 Setup Response")
_RRC_SETUP     = re.compile(_TS_PAT + r".*\[DU\s*\].*TEST_MODE\s+" + _RNTI_PAT +
                             r": Injected DL RRC Message \(rrcSetup\)")
_ALL_ESTAB     = re.compile(_TS_PAT + r".*\[DU\s*\].*TEST_MODE cell=\d+: All (\d+) UE\(s\) established\."
                             r" Running for (\d+) ms")
_RELEASING     = re.compile(_TS_PAT + r".*\[DU\s*\].*TEST_MODE cell=\d+: Attach/detach duration elapsed\."
                             r" Releasing (\d+) UE\(s\)\.")
_ALL_RELEASED  = re.compile(_TS_PAT + r".*\[DU\s*\].*TEST_MODE cell=\d+: All UE\(s\) released\."
                             r" Entering guard period")
_GUARD_ELAPSED = re.compile(_TS_PAT + r".*\[DU\s*\].*TEST_MODE cell=\d+: Guard period elapsed\."
                             r" Starting new creation cycle")


def _parse_ts(ts_str: str) -> float:
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


# Grep pattern that matches only the lines we care about (avoids PHY/MAC noise).
# The log produces ~150k PHY/MAC lines per minute; tailing blindly would give
# only ~20s of history.  Grepping for TEST_MODE + NGAP lines is fast and
# gives us the full history with minimal data.
_GREP_PAT = r"\[DU[[:space:]]*\].*TEST_MODE|\[NGAP[[:space:]]*\]"


# Tail this many raw lines before grepping.  At ~150k PHY/MAC lines/min this
# covers ~80 s of log — enough for 8+ cycles — while keeping grep fast because
# `tail` seeks from the file end instead of scanning from the beginning.
_TAIL_LINES = 200_000


def _grep_log(path: str, n: int) -> list[str]:
    """Grep the tail of a local log file for TEST_MODE/NGAP lines, keep last n."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Read the last ~30 MB to avoid loading multi-GB files
            chunk = min(size, 30 * 1024 * 1024)
            f.seek(size - chunk)
            raw = f.read().decode("utf-8", errors="replace")
        pat = re.compile(_GREP_PAT)
        lines = [l + "\n" for l in raw.splitlines() if pat.search(l)]
        return lines[-n:]
    except Exception:
        return []


def _docker_grep(container: str, src_path: str, n: int) -> list[str]:
    """Grep the tail of src_path inside a running container, keep last n matching lines.

    Uses ``tail | grep`` instead of ``grep`` on the full file so the command
    completes in <1 s even after hours of log accumulation.
    """
    try:
        result = subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             f"tail -n {_TAIL_LINES} {src_path} | grep -E '{_GREP_PAT}' | tail -n {n}"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.splitlines(keepends=True)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def build_lw5g(log_path: Optional[str] = None,
               container: str = "srstwin_gnb") -> dict:
    """
    Parse gnb.log and return dashboard data for the Lightweight 5G Twin.

    log_path: direct filesystem path (host-side); if None (or file absent),
              tails the log from inside the running container via docker exec.
    """
    if log_path and os.path.isfile(log_path):
        lines = _grep_log(log_path, LOG_GREP_LINES)
    else:
        lines = _docker_grep(container, "/tmp/gnb.log", LOG_GREP_LINES)

    if not lines:
        return _empty_result()

    events: list[dict] = []
    ng_setup_done = False
    gnb_start_ts: Optional[float] = None

    # Per-cycle state
    cycle_start_ts: Optional[float] = None   # ts of first rrcSetup in current cycle
    cycle_rnti_seen: set[str] = set()
    cycles: list[dict] = []   # [{start_ts, estab_ts, release_ts, released_ts, nof_ues}]
    _cur_cycle: dict = {}

    # Per-RNTI stats
    rnti_cycles: dict[str, int] = {}     # rnti → count of rrcSetup injections
    rnti_last_setup: dict[str, float] = {}

    active_ues = 0

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        # ---- NG Setup ----
        m = _NG_SETUP_REQ.match(line)
        if m:
            ts_str, ts = m.group(1), _parse_ts(m.group(1))
            gnb_start_ts = gnb_start_ts or ts
            events.append({"ts": ts, "ts_str": ts_str,
                            "rnti": None, "src": "gNB", "dst": "gNB",
                            "label": "NG Setup Request → AMF",
                            "phase": "setup", "raw_line": line})
            continue

        m = _NG_SETUP_RSP.match(line)
        if m:
            ts_str, ts = m.group(1), _parse_ts(m.group(1))
            gnb_start_ts = gnb_start_ts or ts
            ng_setup_done = True
            events.append({"ts": ts, "ts_str": ts_str,
                            "rnti": None, "src": "gNB", "dst": "gNB",
                            "label": "NG Setup OK ← AMF",
                            "phase": "setup", "raw_line": line})
            continue

        # ---- F1 Setup (once) ----
        m = _F1_SETUP.match(line)
        if m:
            ts_str, ts = m.group(1), _parse_ts(m.group(1))
            gnb_start_ts = gnb_start_ts or ts
            events.append({"ts": ts, "ts_str": ts_str,
                            "rnti": None, "src": "gNB", "dst": "gNB",
                            "label": "F1 Setup (test mode)",
                            "phase": "setup", "raw_line": line})
            continue

        # ---- Per-UE rrcSetup injection ----
        m = _RRC_SETUP.match(line)
        if m:
            ts_str, ts = m.group(1), _parse_ts(m.group(1))
            rnti = m.group(2)
            gnb_start_ts = gnb_start_ts or ts
            if rnti not in cycle_rnti_seen:
                cycle_rnti_seen.add(rnti)
                if cycle_start_ts is None:
                    cycle_start_ts = ts
                    _cur_cycle = {"start_ts": ts}
            rnti_cycles[rnti] = rnti_cycles.get(rnti, 0) + 1
            rnti_last_setup[rnti] = ts
            events.append({"ts": ts, "ts_str": ts_str,
                            "rnti": rnti, "src": "gNB", "dst": "UE",
                            "label": f"RRC Setup [{rnti}]",
                            "phase": "attach", "raw_line": line})
            continue

        # ---- All UEs established ----
        m = _ALL_ESTAB.match(line)
        if m:
            ts_str, ts = m.group(1), _parse_ts(m.group(1))
            nof_ues = int(m.group(2))
            run_ms  = int(m.group(3))
            active_ues = nof_ues
            setup_lat_ms = None
            if cycle_start_ts is not None:
                setup_lat_ms = round((ts - cycle_start_ts) * 1000, 1)
                _cur_cycle["estab_ts"] = ts
                _cur_cycle["setup_lat_ms"] = setup_lat_ms
                _cur_cycle["nof_ues"] = nof_ues
            events.append({"ts": ts, "ts_str": ts_str,
                            "rnti": None, "src": "gNB", "dst": "gNB",
                            "label": f"All {nof_ues} UE(s) established · run {run_ms} ms"
                                     + (f" · setup {setup_lat_ms:.0f} ms" if setup_lat_ms else ""),
                            "phase": "nas", "raw_line": line})
            cycle_rnti_seen = set()
            continue

        # ---- Releasing ----
        m = _RELEASING.match(line)
        if m:
            ts_str, ts = m.group(1), _parse_ts(m.group(1))
            nof = int(m.group(2))
            _cur_cycle["release_ts"] = ts
            events.append({"ts": ts, "ts_str": ts_str,
                            "rnti": None, "src": "gNB", "dst": "gNB",
                            "label": f"Releasing {nof} UE(s)",
                            "phase": "release", "raw_line": line})
            continue

        # ---- All released ----
        m = _ALL_RELEASED.match(line)
        if m:
            ts_str, ts = m.group(1), _parse_ts(m.group(1))
            active_ues = 0
            _cur_cycle["released_ts"] = ts
            events.append({"ts": ts, "ts_str": ts_str,
                            "rnti": None, "src": "gNB", "dst": "gNB",
                            "label": "All UEs released · guard period",
                            "phase": "release", "raw_line": line})
            continue

        # ---- Guard elapsed → new cycle ----
        m = _GUARD_ELAPSED.match(line)
        if m:
            ts_str, ts = m.group(1), _parse_ts(m.group(1))
            _cur_cycle["guard_end_ts"] = ts
            if _cur_cycle.get("start_ts"):
                cycles.append(dict(_cur_cycle))
            _cur_cycle = {}
            cycle_start_ts = None
            events.append({"ts": ts, "ts_str": ts_str,
                            "rnti": None, "src": "gNB", "dst": "gNB",
                            "label": "Guard elapsed · new cycle",
                            "phase": "cycle", "raw_line": line})
            continue

    # ---- Build KPIs ----
    # Per-RNTI
    ue_kpis: dict[str, dict] = {}
    for rnti, cnt in rnti_cycles.items():
        ue_kpis[rnti] = {"cycles": cnt, "rnti": rnti}

    # Aggregate from completed cycles
    setup_lats = [c["setup_lat_ms"] for c in cycles if c.get("setup_lat_ms") is not None]
    total_cycles_completed = len(cycles)

    elapsed_min = 0.0
    if events and gnb_start_ts:
        elapsed_min = max((events[-1]["ts"] - gnb_start_ts) / 60.0, 0.01)

    summary = {
        "ng_setup_done": ng_setup_done,
        "active_ues": active_ues,
        "total_cycles": total_cycles_completed,
        "nof_ues_seen": len(rnti_cycles),
        "reg_rate_per_min": round(total_cycles_completed / elapsed_min, 2) if elapsed_min > 0 else 0.0,
        "median_setup_ms": round(statistics.median(setup_lats), 1) if setup_lats else None,
        "p90_setup_ms": (round(sorted(setup_lats)[int(len(setup_lats) * 0.9)], 1)
                         if len(setup_lats) >= 5 else None),
        "elapsed_min": round(elapsed_min, 2),
    }

    # Keep last 3 complete cycles + the one-time startup events.
    # Use label matching (not phase) so that recurring "Guard elapsed · new cycle"
    # events (phase="cycle") are subject to the cutoff, while the genuine
    # one-shot NG Setup / F1 Setup lines are always kept at the top.
    KEEP_CYCLES = 3
    _STARTUP_LABELS = {"NG Setup Request → AMF", "NG Setup OK ← AMF", "F1 Setup (test mode)"}
    startup_evs = [e for e in events if e["label"] in _STARTUP_LABELS]
    cutoff_ts = None
    if len(cycles) >= KEEP_CYCLES:
        cutoff_ts = cycles[-KEEP_CYCLES]["start_ts"]
    if cutoff_ts:
        recent_evs = [e for e in events if e["ts"] >= cutoff_ts and e["label"] not in _STARTUP_LABELS]
    else:
        recent_evs = [e for e in events if e["label"] not in _STARTUP_LABELS]
    trimmed = sorted(startup_evs + recent_evs, key=lambda e: e["ts"])

    return {
        "events": trimmed,
        "ue_kpis": ue_kpis,
        "summary": summary,
        "has_live": bool(rnti_cycles),
    }


def _empty_result() -> dict:
    return {
        "events": [],
        "ue_kpis": {},
        "summary": {
            "ng_setup_done": False,
            "active_ues": 0,
            "total_cycles": 0,
            "nof_ues_seen": 0,
            "reg_rate_per_min": 0.0,
            "median_setup_ms": None,
            "p90_setup_ms": None,
            "elapsed_min": 0.0,
        },
        "has_live": False,
    }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json, sys

    log = sys.argv[1] if len(sys.argv) > 1 else None
    result = build_lw5g(log_path=log)
    s = result["summary"]
    print(f"Events:          {len(result['events'])}")
    print(f"UEs seen:        {s['nof_ues_seen']}  (RNTIs: {list(result['ue_kpis'])})")
    print(f"Completed cycles:{s['total_cycles']}")
    print(f"Active UEs now:  {s['active_ues']}")
    print(f"Cycle rate:      {s['reg_rate_per_min']} /min")
    print(f"Median setup:    {s['median_setup_ms']} ms")
    print(f"P90 setup:       {s['p90_setup_ms']} ms")
    print(f"NG Setup done:   {s['ng_setup_done']}")
    if result["events"]:
        print("\nLast 8 events:")
        for ev in result["events"][-8:]:
            print(f"  [{ev['ts_str'][11:23]}] {ev['src']}→{ev['dst']}: {ev['label']}")
