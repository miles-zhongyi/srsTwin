# 3-UE Demo Results

Quick demo, not the M-plan (see `../realizer/PLAN.md` for the real multi-UE
architecture work — this is a faster, lower-fidelity stand-in for a same-day
demo): 3 independent eNB+UE pairs, each its own cell, sharing one srsEPC.
No shared PHY/radio pipeline — that's Option A in the M-plan, not built.

## What was measured

- **DU processing delay** = PRACH preamble (Msg1) → Random Access Response
  (Msg2) turnaround. Pure eNB-side, no EPC round-trip — the standard RACH
  response-time KPI. 4G has no separate DU (eNB is monolithic); this is the
  eNB's equivalent.
- **Call duration** = NAS Attach Complete → the eNB's inactivity-triggered
  release starting (srsenb's default inactivity timer is 30s). Both UE and
  eNB were force-recreated together each cycle (see `run_cycles.py` — UE-only
  recreate against a running eNB caused a RACH retry storm that never
  settled), and the harness polled for the actual release rather than
  guessing a fixed sleep, so this is a real measured quantity.

5 cycles per scenario, all `outcome=attached`, no failures.

## Results

| Metric | 1 pair alone | 3 pairs concurrent | Δ |
|---|---|---|---|
| DU delay (Msg1→Msg2), mean | 28.40 ms | 30.86 ms | **+8.7%** |
| DU delay, range | 23.4–33.1 ms | 26.0–38.5 ms | |
| Call duration, mean | 35.41 s | 38.45 s | **+8.6%** |
| Call duration, range | 34.78–35.79 s | 37.56–39.76 s | |

See `histograms.png` — both distributions visibly shift right under
concurrency, distributions don't fully separate (this is host CPU
contention, not a hard wall) but the means are unambiguous.

## Why the effect is real, not noise

srsenb's ZMQ link runs in lockstep: it must finish processing a subframe
within its ~1ms TTI budget to stay synchronized. Running 3 eNB processes on
the same physical cores means the OS scheduler can't always give each one
its CPU slice promptly — that shows up directly as slower RACH response
time and (compounding slightly) longer time-to-detect-inactivity.

## Reproducing

```bash
cd integration
docker compose -f docker-compose.4g.yml -f docker-compose.3ue.yml up -d --build
# wait for srsenb2/3 + srsue4g2/3 to attach, then:

python3 demo3ue/run_cycles.py --pairs 1       --cycles 5 --wait-release --out demo3ue/logs_1pair
python3 demo3ue/run_cycles.py --pairs 1,2,3   --cycles 5 --wait-release --out demo3ue/logs_3pair
python3 demo3ue/analyze.py
python3 demo3ue/plot_histograms.py
```

## Known issues hit and fixed along the way

1. **`docker restart` / UE-only recreate desyncs the ZMQ link.** Recreating
   only the UE container against an already-running eNB produced a RACH
   retry storm (multiple simultaneous preamble detections that never
   resolved into a clean attach). Fix: always force-recreate the eNB and UE
   together. Matches `integration/README.md`'s existing gotcha #5 ("don't
   partial-restart mid-link").
2. **CRLF-corrupted entrypoint script.** `rrc_trace/rrc_injector_entrypoint.sh`
   had been silently rewritten to CRLF line endings by this repo's
   `core.autocrlf=true` git setting, which breaks its `#!/bin/sh` shebang
   inside Linux containers (`exec /entrypoint.sh: no such file or
   directory`). Fixed the file and added `.gitattributes` (`*.sh text
   eol=lf`) at the repo root so it doesn't recur for this or other scripts.
3. **EPC subscriber sync.** srsEPC loads `user_db.csv` once at container
   startup; the 2 new subscriber rows added for UE2/UE3 weren't visible
   until srsEPC was recreated. If you add more subscribers later, recreate
   `srsepc` too, not just the new UEs.
