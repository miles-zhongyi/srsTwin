# Demo notes: poc_StressTest + srsTwin

Audience: technical, not digital-twin specialists. Assume they know
software/systems engineering, sockets, Docker, async — don't assume they
know 3GPP jargon. Define terms once, briefly, inline; don't dwell.

These are talking points to speak *from*, not a script to read. `[DEMO: ...]`
marks exactly when to switch screens and what to click. Target ~15-20 min
total; trim the architecture detail first if you're short on time, keep the
live demos and the numbers.

---

## 0. The one-sentence frame (say this near the top, it's the spine of the talk)

> "Both of these are software stand-ins for a cellular radio network, so we
> can test against them instead of real towers and real subscribers. They
> sit at opposite ends of the same trade-off: **fidelity vs. scale**."

- **poc_StressTest** — low fidelity, huge scale. Thousands of UEs, one process.
- **srsTwin** — high fidelity, real protocol stacks. One UE, rock solid;
  three UEs is where it starts to cost you, and that cost is exactly what
  we measured live this week.

Both are validated against the **same real-world data**: decoded call
traces from a real TELUS LTE network (`22_decoded/`). That's not a
coincidence — it's the throughline of the whole talk.

---

## 1. poc_StressTest — "5G RU Digital Twin, Stress Test PoC" (~5 min)

**What it is, in one breath:** a software 5G cluster — 1 DU (scheduler),
3 RU sites each with 3 sector cells (9 cells, 250 PRBs* each), and a UE
simulator running as many UEs as you want as lightweight async tasks in a
single process. No real radio, no real ASN.1 — message *names* mirror real
RRC/F1AP procedures, but payloads are simplified JSON. That trade is
deliberate: it's what lets one process run thousands of UEs.

*(PRB = Physical Resource Block — the basic unit of radio capacity a
scheduler hands out. "250 PRBs" is roughly one 5G cell's worth of spectrum.)*

**The one architecture idea worth explaining:** connection multiplexing is
*different at each hop*, on purpose:
- UE↔RU: one TCP socket *per UE*. A UE's handover is just "redirect this
  socket to a different RU" — no migrating state across processes.
- RU↔DU: exactly **one** multiplexed connection per RU, carrying every UE's
  signaling, correlated by a transaction id. This mirrors how real F1
  works and keeps the DU's connection count at "one per tower," not "one
  per phone" — that's *the* reason this scales to thousands of UEs on one
  laptop.

**What it's for:** capacity and admission-control testing — PRB exhaustion,
handover correctness (A3-style hysteresis between 9 cells / 3 sites),
VoIP vs. broadband traffic profiles, "what happens at 500 concurrent
users." Also replays real call-trace *timing* (attach/measurement/release
moments from real traces) to drive synthetic load with realistic arrival
patterns instead of pure randomness.

**[DEMO: open http://localhost:9090]**
- Point out the UE-count slider — live-scale from 1 to hundreds, no restart.
- Point out PRB utilization bars filling up per cell as load increases.
- Point out the live call-flow ladder (one locked UE, RRC→S1AP sequence).
- One good live moment: bump the UE slider way up and watch PRBs fill /
  cells go red — this is the "stress test" payoff in 10 seconds.

---

## 2. srsTwin — 4G LTE digital twin (~8-10 min, this is the meat)

**What it is, in one breath:** this is *not* a simplified stand-in — it's
the **real** open-source cellular software (srsRAN_4G) running as Docker
containers: a real UE, a real eNodeB, a real EPC core, talking real RRC /
NAS / S1AP ASN.1-encoded messages over a ZeroMQ-emulated radio link instead
of real RF hardware. Same software a real base station could run.

**Why that matters:** we can take a real captured attach from a real TELUS
subscriber (`22_decoded/`) and **inject that subscriber's real identity**
into the live simulated UE's first RRC message — the live signaling we get
back is byte-comparable to what a real phone produced on a real network.
That's the fidelity poc_StressTest's JSON stand-ins can't give you.

**The cost of that fidelity:** real protocol stacks assume *one* UE per
process (a UE is, after all, normally one physical phone). Scaling that is
genuinely hard — there's no free "just add more UEs" knob like
poc_StressTest's asyncio tasks.

**[DEMO: open http://localhost:8765, 4G LTE tab]**
- Walk the signaling ladder: PRACH → RACH response → RRC setup → NAS
  Attach Request → auth → security → bearer setup → Attach Complete →
  release. Click a message, show the side panel: plain-English explanation
  + the decoded message next to its actual PER-encoded bytes.
- Point out the KPI panel: attach time vs. call/session duration, computed
  from real log timestamps, not estimates.

### The finding (this is your best concrete moment — lead with the numbers)

We don't have a way to share one radio pipeline across UEs yet, so today
"more UEs" literally means more independent eNB+UE container pairs
fighting for the same CPU. We measured exactly what that costs:

| | 1 UE alone | 3 UEs concurrent | |
|---|---|---|---|
| eNB processing delay (PRACH→response) | 28.4 ms | 30.9 ms | **+8.7%** |
| Call duration | 35.4 s | 38.4 s | **+8.6%** |

That's not a guess — that's `n=5` measured cycles per scenario, same
hardware, only the concurrency changed. It's real contention cost, and now
it's a number instead of a hunch.

**[DEMO: bottom-left KPI histogram panel]**
- Show the live histogram, built from a background process that
  continuously cycles real attach/release flows and logs every one.
- Click the UE1/UE2/UE3 filter — explain it's a *permanent log*, so even a
  UE you've since stopped still shows its history.
- Click 📌 **Pin** — freezes the ladder on whatever flow you're inspecting
  while the histogram keeps accumulating underneath. Good "we thought
  about actually using this under pressure" detail.

**Where this is headed (one sentence, don't over-promise):** there's a
reviewed, paused architecture plan to let one shared radio pipeline serve
N logical UEs properly — same idea as poc_StressTest's "one socket per UE,
one connection per tower," just one layer lower, at the real PHY/MAC level
instead of the application layer.

---

## 3. Where the two meet (~2 min)

- Same real-world data source feeds both (`22_decoded/` TELUS traces) —
  poc_StressTest replays its *timing*, srsTwin injects its *identity*.
- Same underlying problem (scale vs. fidelity) solved at two different
  altitudes: poc_StressTest multiplexes at the *application* layer;
  srsTwin's next step multiplexes at the *radio* layer.
- Natural two-tier strategy: srsTwin for protocol-exact validation at small
  scale (does this *exact* message sequence behave correctly), poc_StressTest
  for capacity/admission behavior at large scale (does the *system* hold up
  at 500 users) — you don't need one project to do both jobs.

---

## 4. Close

> "Two different answers to the same question: how do you test a cellular
> network without a cellular network. One trades fidelity for scale, one
> trades scale for fidelity — and now we have real numbers for what that
> trade actually costs."

If there's time for one closing beat: re-open whichever dashboard got the
best reaction and let people ask questions while it's still on screen live.

---

## Logistics checklist (do this 10 min before the talk, not during)

```bash
# poc_StressTest
cd poc_StressTest && docker compose ps        # expect du, ru, ru2, ru3, ue-sim, dashboard = Up
# http://localhost:9090

# srsTwin
cd srsTwin/integration && docker compose -f docker-compose.4g.yml -f docker-compose.3ue.yml ps
# http://localhost:8765  (if the server isn't running: python dashboard/serve_dashboard.py --pull)
```

Both confirmed live and responding as of drafting these notes. The srsTwin
KPI histogram currently has 9 real samples from earlier testing — enough to
show bars, but run `demo3ue/live_cycler.py --pairs 1,2,3` for a few minutes
beforehand if you want a fuller-looking histogram for the demo (Ctrl+C it
before you go live — it actively recreates containers the whole time it runs).
