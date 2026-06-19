#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Measure a srsTwin signaling storm after (or during) a run.

Sources:
  storm/gen/events.csv     per-arrival outcomes from orchestrate.py (Layer A)
  gNB log (/tmp/gnb.log)   RACH / RRC / NGAP signalling on the real cell
                           (pulled from the srstwin_gnb container by default)

Outputs:
  - a console summary (attach success, latency p50/p90/max, per-profile, RACH
    attempts vs RRC completions as a contention proxy)
  - storm/gen/metrics.json  machine-readable summary + 1 s timeline buckets
    (arrivals, attaches, RRC setups) for the dashboard / plotting

Run:
  python storm/metrics.py                       # events.csv + gNB log via docker
  python storm/metrics.py --gnb-log gnb.log     # use a copied log file
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "gen")

# srsRAN-Project gNB log signatures (control-plane of the storm).
RE_RACH = re.compile(r"(RACH|PRACH|Detected preamble|RA-RNTI)", re.I)
RE_RRC_REQ = re.compile(r"RRC (Setup Request|Connection Request|Reestablishment)", re.I)
RE_RRC_DONE = re.compile(r"RRC (Setup Complete|Connection Reconfiguration Complete)", re.I)
RE_NGAP_REG = re.compile(r"(Initial UE Message|Registration (request|complete)|InitialContextSetup)", re.I)


def read_events():
    path = os.path.join(GEN, "events.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_gnb_log(arg):
    if arg:
        with open(arg, encoding="utf-8", errors="replace") as f:
            return f.read()
    # default: pull from the running gNB container
    try:
        out = subprocess.run(
            ["docker", "exec", "srstwin_gnb", "sh", "-c", "cat /tmp/gnb.log"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            return out.stdout
        print("  (could not read gNB log from container; skip RACH/RRC metrics)")
    except Exception as e:  # noqa: BLE001
        print(f"  (gNB log unavailable: {e}; skip RACH/RRC metrics)")
    return ""


def summarize_events(events):
    n = len(events)
    ok = [e for e in events if e["result"] == "attached"]
    fails = [e for e in events if e["result"].startswith("failed")]
    lat = sorted(float(e["attach_s"]) for e in ok if e["attach_s"])

    by_profile = {}
    for e in events:
        p = by_profile.setdefault(e["profile"], {"n": 0, "ok": 0})
        p["n"] += 1
        if e["result"] == "attached":
            p["ok"] += 1

    queue_delays = [float(e["launch_s"]) - float(e["sched_s"])
                    for e in events if e["sched_s"] and e["launch_s"]]

    def pct(v, q):
        return v[min(len(v) - 1, int(len(v) * q))] if v else 0.0

    return {
        "arrivals": n,
        "attached": len(ok),
        "failed": len(fails),
        "attach_rate": round(len(ok) / n, 3) if n else 0.0,
        "latency_p50": round(pct(lat, 0.5), 1),
        "latency_p90": round(pct(lat, 0.9), 1),
        "latency_max": round(lat[-1], 1) if lat else 0.0,
        "queue_delay_max": round(max(queue_delays), 1) if queue_delays else 0.0,
        "by_profile": by_profile,
        "fail_reasons": _tally(e["result"] for e in fails),
    }


def _tally(items):
    out = {}
    for it in items:
        out[it] = out.get(it, 0) + 1
    return out


def summarize_gnb(log):
    if not log:
        return {}
    return {
        "rach_events": len(RE_RACH.findall(log)),
        "rrc_setup_requests": len(RE_RRC_REQ.findall(log)),
        "rrc_setup_completes": len(RE_RRC_DONE.findall(log)),
        "ngap_registrations": len(RE_NGAP_REG.findall(log)),
    }


def timeline_buckets(events, duration_hint=0):
    """1-second buckets of arrivals and attach completions (for plotting)."""
    if not events:
        return []
    end = duration_hint
    for e in events:
        for k in ("sched_s", "launch_s", "end_s"):
            if e[k]:
                end = max(end, float(e[k]))
    buckets = [{"t": s, "arrivals": 0, "attaches": 0} for s in range(int(end) + 2)]
    for e in events:
        if e["sched_s"]:
            buckets[int(float(e["sched_s"]))]["arrivals"] += 1
        if e["result"] == "attached" and e["end_s"]:
            buckets[int(float(e["end_s"]))]["attaches"] += 1
    return buckets


def ascii_hist(values, width=40, bins=10):
    if not values:
        return "  (no data)"
    lo, hi = min(values), max(values)
    if hi == lo:
        hi = lo + 1
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int((v - lo) / (hi - lo) * bins))] += 1
    top = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        edge = lo + (hi - lo) * i / bins
        bar = "#" * int(c / top * width)
        lines.append(f"  {edge:6.1f}s |{bar} {c}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Measure a srsTwin signaling storm.")
    ap.add_argument("--gnb-log", help="path to a copied gNB log (else read via docker)")
    args = ap.parse_args()

    events = read_events()
    if not events:
        print("No events.csv — run `python storm/orchestrate.py` first.")
    ev_sum = summarize_events(events) if events else {}

    print("== gNB control-plane (RACH / RRC / NGAP) ==")
    gnb_sum = summarize_gnb(read_gnb_log(args.gnb_log))
    for k, v in gnb_sum.items():
        print(f"  {k:22s} {v}")
    if gnb_sum.get("rrc_setup_requests"):
        contention = 1 - gnb_sum.get("rrc_setup_completes", 0) / gnb_sum["rrc_setup_requests"]
        print(f"  RRC failure/contention proxy: {contention*100:.0f}% "
              f"(requests not completed — collisions/backoff)")

    if events:
        print("\n== Layer A attach outcomes ==")
        print(f"  arrivals={ev_sum['arrivals']}  attached={ev_sum['attached']}  "
              f"failed={ev_sum['failed']}  rate={ev_sum['attach_rate']*100:.0f}%")
        print(f"  attach latency: p50={ev_sum['latency_p50']}s "
              f"p90={ev_sum['latency_p90']}s max={ev_sum['latency_max']}s")
        print(f"  max queue (admission) delay: {ev_sum['queue_delay_max']}s")
        print("  per RF profile:")
        for p, d in sorted(ev_sum["by_profile"].items()):
            print(f"    {p:6s} {d['ok']}/{d['n']} attached")
        if ev_sum["fail_reasons"]:
            print(f"  failures: {ev_sum['fail_reasons']}")
        print("\n== Attach-latency distribution ==")
        print(ascii_hist(sorted(float(e["attach_s"]) for e in events if e["attach_s"])))

    out = {
        "events": ev_sum,
        "gnb": gnb_sum,
        "timeline": timeline_buckets(events),
    }
    path = os.path.join(GEN, "metrics.json")
    os.makedirs(GEN, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
