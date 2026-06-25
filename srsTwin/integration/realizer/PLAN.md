# Shared Realizer — PLAN.md

Status: **DRAFT — awaiting review, no code written yet.**

Goal: let N>1 logical UEs share one PHY/radio pipeline against srsENB+srsEPC,
so the network sees N distinct, independently-attaching UEs instead of one.
First concrete step toward scaling the 4G LTE twin. Per-UE behavioral models,
TRC-driven correlation, storm triggers, and anything 5G/NR are explicitly out
of scope — this plan only builds the shared channel and leaves clean hooks.

---

## 0. Confirmed facts (recap)

Established and accepted in review before this plan was written:

| Fact | Value |
|---|---|
| RF backend | ZMQ (`device_name = zmq`), not SDR |
| srsRAN version | srsRAN_4G **25.10.0** (vanilla, + the `rrc_trace/` injection patch only) |
| Live message injection today | `rrc.cc` `std::getenv("RRC_TRACE_LTE_M_TMSI"/"_CAUSE")` — 2 fields, 1 message (Msg3), 1 UE, srsUE's native encoder builds the bytes |
| pycrate PER path | **Offline only** (`encode_templates.py` → `lte_per_templates.json`, dashboard reference display). No live byte-injection exists. `lte_byte_injector.py` is named in a docstring as future work and does not exist. |
| Single-UE config | One `[usim]` block per `ue.conf`, one static IMSI/Ki/OPc, one ZMQ port pair |
| Hub (5G, prior art) | `integration/hub/`: ZMQ REQ/REP relay, sums uplink IQ for N srsUE against one OCUDU gNB. **Structural ceiling**, not a bug: `_gather_ul()` does one serial REQ/REP per UE per slot tick, so the cell's sample clock slows ~linearly with N. Documented (`storm/README.md`) to crawl above ~4 UEs. |

Decisions carried into this plan from review:
- **Option A** (extend srsUE to host N L2/L3 contexts over one shared PHY worker), not Option B.
- M1 uses srsUE's **native encoder** — no byte injection. Live pycrate byte-injection is **M5**, after multi-UE is solid.
- N subscribers provisioned in srsEPC `user_db.csv` with distinct IMSI/Ki/OPc; each UE context gets its own USIM credentials.
- Enable existing MAC PCAP (RNTI-tagged) as the acceptance signal.
- **srsENB is not touched.** All work is on the srsUE/realizer side.

---

## 1. Architecture decision: Option A

### Why Option A, not Option B

Option B is, structurally, "rebuild the Hub for 4G": N independent `srsue`
processes, IQ summed at a ZMQ boundary. The Hub already proves what that
costs — not a fixable inefficiency, a structural one. Every UE adds one
cross-process ZMQ REQ/REP round trip to the **critical path of every slot
tick**, because the gNB/eNB can't produce its next DL until all ULs are in.
Real-time slot timing then forces the per-tick budget to be shared serially
across N processes. The Hub's own data point — usable at 2-4 UEs, "clock
crawls" beyond that — is exactly the failure mode this task needs to avoid
to ever reach "hundreds of UEs."

Option A removes the cross-process round trip entirely: N UE contexts run
in one process, share one PHY worker, and the "summing" happens *inside*
one shared resource grid before a single IFFT — not across a socket.

### The physical-layer justification (why this isn't an approximation)

LTE uplink is SC-FDMA: the eNB scheduler grants each UE **disjoint PRBs**
(frequency-domain regions) per TTI. Two UEs transmitting on disjoint PRBs in
the same TTI combine, physically, by frequency-domain superposition — this
is exactly what a real eNB receives from N physically separate phones. So
"N logical UEs fill their granted PRBs into one shared frequency-domain
grid, then one IFFT" is not a shortcut model of multi-UE uplink — it *is*
multi-UE uplink, done more cheaply than N independent IFFTs summed in time
domain (which would also be correct, just more compute for the same
result). Downlink needs no special handling at all: LTE DL is OFDMA, the
eNB already broadcasts one shared grid today, and every UE already only
decodes its own resource blocks out of one received buffer — this is true
whether there's 1 logical UE behind the receiver or N.

The one place real RF contention is unavoidable and *should* stay modeled,
not engineered away: **PRACH**. Two logical UEs choosing the same preamble
index in the same RACH occasion physically collide (this is real 3GPP
behavior, not a simulator artifact) — the realizer should sum colliding
PRACH attempts rather than special-case around them. This is what M3's
acceptance criteria ("no collisions beyond modeled RACH contention") is
asking for.

### How invasive Option A actually is (precise, not hand-wavy)

Read `srsue/hdr/stack/ue_stack_lte.h` and the MAC headers to ground this in
the actual class boundaries rather than guessing:

- `ue_stack_lte` owns **one of each** by value/unique_ptr: `mac`, `rlc`
  (`srsran::rlc`), `pdcp`, `rrc`, `nas`, `usim` (`unique_ptr<usim_base>`).
  This is the wiring layer that must change from "owns one" to "owns N,"
  one per logical UE.
- **Better news than expected**: `srsue::ue_rnti` (`mac_common.h`) is
  *already* a clean, self-contained "one UE's RNTI state" object
  (crnti/rar_rnti/temp_rnti/tpc_rnti/sps_rnti + its own mutex), not a
  scattering of ad-hoc scalars. `dl_harq_entity::init(ue_rnti* rntis, ...)`
  already takes one of these by pointer. `mux`/`demux` are LCID-keyed
  internally but that's fine *as long as each logical UE gets its own mux
  and demux instance* — LCID 0 (CCCH) for UE A and LCID 0 for UE B are
  different channels, but they live in different `mux`/`demux` objects, not
  one shared table. **The classes that hold per-UE state are mostly already
  shaped correctly for "one instance per UE" — they're just currently wired
  as singletons.**
- The actual new work is concentrated in three places:
  1. **Top-level wiring** (`ue_stack_lte`-equivalent): own `vector<UeContext>`
     (each bundling mac/rlc/pdcp/rrc/nas/usim) instead of one of each, plus
     one shared `phy`.
  2. **The dispatch layer** (new code, doesn't exist today): each TTI, blind
     PDCCH-search the one received subframe against *every* UE context's
     RNTI set (the PDCCH search primitives in `lib/src/phy/ue/ue_dl.c`
     already take `rnti` as a parameter — they're reusable as-is, just
     called N times), then route each found grant to the correct UE
     context's HARQ/mux. **A bug here mis-routes one UE's grant or data to
     another UE — this is the highest-risk new code, not a modification of
     existing logic.**
  3. **Shared uplink grid construction**: gather whichever UE contexts have
     a UL grant this TTI, place each one's modulated symbols at its
     granted PRBs in one shared frequency-domain buffer, one IFFT, one ZMQ
     TX — replacing what is today "one UE always owns the whole grid."
- `rrc.cc`/`nas.cc`/`usim.cc` themselves should need **no internal
  changes** — they already only know about "my own state," and that
  continues to be true when there are N instances of them, each closed over
  its own `UeContext`.

### Recommendation

**Option A.** Effort is concentrated and identifiable (wiring + dispatch +
grid construction), not a PHY rewrite. Real-time feasibility is *better*
than Option B because it removes the Hub's proven per-tick serial-handshake
ceiling. Scaling to "hundreds of UEs" later is a question of CPU budget
inside one process (blind-search cost, HARQ memory) rather than a hard
architectural wall — Option B hits its wall at single digits regardless of
hardware.

---

## 2. Component design

### 2.1 `UeContext` (new)

One per logical UE. Bundles what `ue_stack_lte` owns today:

```
UeContext:
  ue_id            # stable logical identifier (not RNTI — RNTI changes across RA)
  rnti: ue_rnti    # existing class, instantiated per-UE instead of singleton
  mac, rlc, pdcp, rrc, nas: instances of the existing classes (unchanged)
  usim: usim_base  # own IMSI/Ki/OPc, no longer read from a single [usim] block
  ra_state: proc_ra instance (own preamble, power-ramp counter, backoff/contention timers)
  harq: { dl_harq_entity, ul_harq_entity } per CC (CC count = 1 for this twin)
```

### 2.2 Shared PHY worker (modified `lte::sf_worker` / `phy_common`)

Per TTI:
1. Pull one received IQ buffer (existing single ZMQ RX) → one FFT (unchanged).
2. **Dispatch** (new): for each `UeContext`, blind-search PDCCH for its RNTI
   set in the one decoded subframe; route hits to that context's HARQ.
3. For each `UeContext` with a UL grant this TTI: build its transport block
   via its own mac/harq (existing per-UE logic, unchanged), modulate via
   existing PHY primitives.
4. **Grid placement** (new): place each UE's modulated symbols at its
   granted PRBs in one shared frequency-domain buffer. PRACH attempts are
   summed directly (modeled collision) rather than grid-placed.
5. One IFFT, one ZMQ TX (existing single socket, unchanged).

### 2.3 Realizer external interface (per task spec, defined now even though M1 doesn't use the byte-injection half)

```
TransmitIntent:
  ue_id: str
  channel: str          # "SRB0" | "SRB1" | ... (CCCH for Msg3, DCCH later)
  per_encoded_bytes: bytes | None   # None in M1 (native encoder); populated from M5
  procedure_tag: str    # e.g. "rrc_conn_request", for logging/correlation only
```

```
DownlinkEvent (new — the hook for the future state-machine layer):
  ue_id: str
  rnti: int
  event_type: str        # "paging" | "rrc_reconfig" | "contention_resolution" | ...
  payload: dict
  ts: float
```

Both are intentionally shaped like the dashboard's existing `mk_ev()` event
schema (`parse_4g.py`) for consistency with the rest of the project — the
state-machine layer that eventually consumes `DownlinkEvent` should feel
like the same data model the dashboard already renders.

These interfaces are the **only** thing the future state-machine layer
touches. It does not get access to `UeContext` internals, HARQ, or PHY.

### 2.4 Subscriber provisioning (new, small, low-risk)

One CSV (IMSI, Ki, OPc, ...) is the single source of truth for N
identities, consumed by two generators:
- `gen_user_db.py` → srsEPC's `user_db.csv` format (matches
  `4g_configs/subscribers.csv` schema already in use).
- Realizer startup → N `usim_base` instances, one per `UeContext`.

This avoids the two sides drifting out of sync. Modeled on the existing
sequential-IMSI pattern already used for the (5G/storm) subscriber CSV
generator — same idea, new schema, because srsEPC's `user_db.csv` columns
differ from Open5GS's.

---

## 3. Risk register

| # | Risk | Where | Impact if it goes wrong | Detection / mitigation |
|---|---|---|---|---|
| 1 | Grant misrouting — a UE's DCI grant or decoded data handed to the wrong `UeContext` | New dispatch layer (2.2 step 2) | Silent cross-UE data corruption: UE A's NAS bytes delivered to UE B's RRC, PDCP COUNT desync, decryption failure | Per-UE PCAP + dashboard-style log assertions: every decoded message's RNTI must match its `UeContext`'s current `ue_rnti.crnti`. Add an explicit unit test that asserts no two `UeContext`s ever observe the same TC-RNTI/C-RNTI simultaneously. This is the #1 thing M3 exists to catch. |
| 2 | proc_ra timer collisions | `proc_ra` per UE, contention-resolution / backoff timers | One UE's RA timer fires for/cancels another's | Each `UeContext` must get its own `srsran::timer_handler` timer IDs, never a shared pool indexed only by timer-type. Unit test: start RA on 2 contexts with overlapping timer durations, assert independent expiry. |
| 3 | PDCCH blind-search budget | Shared PHY worker, step 2 | Real LTE UEs have a bounded number of blind decode attempts per subframe (3GPP search-space limits, same ceiling the Hub's gotchas note calls out for the 5G side). Searching N RNTI sets per subframe multiplies this; at some N it silently degrades detection probability rather than crashing — easy to miss until load-tested. | M2 (8 UEs) is the first empirical checkpoint. Track per-UE PDCCH miss rate as a metric from the start, not just attach success/fail. |
| 4 | Per-TTI real-time deadline | Shared PHY worker overall | LTE numerology gives ~1ms per TTI. N-context dispatch + grid placement adds work inside that budget; past some N the worker misses deadlines and the whole cell desyncs (same failure *symptom* as the Hub's crawl, different cause — compute-bound here vs. handshake-bound there). | Instrument per-TTI worker wall-clock time from M1 onward; alarm if it approaches the 1ms budget well before N is large enough to actually blow it, so we have headroom data before M2/M3 run. |
| 5 | HARQ soft-buffer memory scaling | `dl_harq_entity`/`ul_harq_entity` × N × 8 processes (FDD) | Fine at N=2/8; a real concern noted now for when "hundreds" is revisited later (out of scope for this plan, but the memory model should be measured at M2 so the hundreds-of-UEs question has real numbers instead of guesses) | Measure RSS at M2 (8 UEs) and extrapolate; flag in the M2 report rather than discovering it later. |
| 6 | N=1 regression | Top-level wiring change | Any refactor from scalar members to indexed/looped collections risks subtly changing single-UE timing/ordering even when N=1 | Save a byte-for-byte baseline of the *current* single-UE attach (enb.log signaling sequence + timestamps-to-the-extent-deterministic) before touching any code. After the M1 refactor, re-run N=1 and diff against the baseline — this is a hard gate, not a nice-to-have, per the "N=1 must still work exactly as today" constraint. |
| 7 | USIM credential plumbing | Config layer | Single `[usim]` block doesn't generalize; an easy mistake is to let N UE contexts default to the *same* credentials if the new config path isn't wired correctly, causing silent IMSI collisions that look like attach success but aren't actually N distinct subscribers | M1's acceptance check explicitly verifies N distinct IMSIs appear in srsEPC's logs, not just N distinct RNTIs (RNTI is assigned post-attach and doesn't by itself prove distinct subscriber identity). |

---

## 4. Milestones

### M0 — Scaffolding (no PHY/MAC changes)
- Define `UeContext`, `TransmitIntent`, `DownlinkEvent` (interfaces only).
- `gen_user_db.py`: generate N-subscriber `user_db.csv` for srsEPC from one
  source CSV.
- Save the N=1 baseline (enb.log signaling sequence) for later regression diff.
- Exit: config/provisioning lands and is reviewed independently of any
  srsue source change — de-risks the rest by getting the non-PHY plumbing
  right first.

### M1 — 2 logical UEs, native encoder, full cycle
- `ue_stack_lte`-equivalent owns 2 `UeContext`s over 1 shared `phy`.
- Dispatch layer (risk #1) and shared uplink grid (risk #4 path) land here —
  this is where most of the engineering risk in this plan lives.
- 2 UEs concurrently complete attach → default bearer → some signaling →
  detach against **unmodified** srsENB + srsEPC.
- Enable MAC PCAP.
- Exit / acceptance: 2 distinct C-RNTIs in srsENB logs, 2 distinct IMSIs in
  srsEPC logs, PCAP shows 2 separate per-UE NAS/RRC flows, N=1 baseline
  diff is clean.

### M2 — Parameterize, verify at 8
- `num_ues` config-driven (default 1 — N=1 path untouched).
- Re-run M1's acceptance criteria at N=8.
- Collect risk #3/#4/#5 metrics (PDCCH miss rate, per-TTI worker time, HARQ
  RSS) as real numbers, not estimates.

### M3 — Correct shared scheduling
- Targeted tests for risk #1 specifically: assert grant-to-UE routing is
  never ambiguous; assert no two contexts are ever placed on overlapping
  PRBs in the same TTI (a placement bug, not real contention).
- Confirm PRACH collisions only occur when modeled (same preamble chosen),
  never as a side effect of dispatch bugs.

### M4 — Load harness
- Spin up N UEs, report per-UE attach success rate and timing.
- Reuse, don't reinvent: this is close to the attach-phase KPI work already
  in `dashboard/parse_4g.py` (`compute_attach_kpis`, `attach_ms`/`session_ms`
  split) — extend that to aggregate across N concurrent UE contexts rather
  than building a separate metrics pipeline.

### M5 — Live pycrate byte-injection (deferred from M1 by design)
- Build the `lte_byte_injector.py`-equivalent: reuse `encode_templates.py`'s
  ASN.1-tree-walking/UPER-encode routines as a library, wire it to actually
  populate `TransmitIntent.per_encoded_bytes` and have the realizer transmit
  those bytes instead of srsUE's native encoder output, for at least one
  message per UE.
- Only attempted once M1-M4 are solid, per review decision — multi-UE
  bring-up and byte-injection bring-up are two separate unknowns and should
  not be debugged simultaneously.

---

## 5. Test strategy

- **Unit**: new dispatch-layer routing logic (risk #1) gets a dedicated
  test with synthetic DCI grants across ≥3 `UeContext`s, asserting correct
  routing and zero cross-contamination. Timer independence test (risk #2).
- **Regression**: N=1 baseline diff (risk #6) — hard gate before any
  milestone is considered done.
- **Integration**: extend the existing `verify_4g_stack.py` /
  `verify_4g_dashboard.py` pattern already used in this repo rather than
  inventing a new harness — add N-UE-aware checks (distinct RNTIs, distinct
  IMSIs) alongside what's already verified for the single-UE path.
- **Acceptance** (per milestone, as specified by the task and reflected in
  section 4 above): distinct C-RNTIs + distinct IMSIs in logs, PCAP with
  separable per-UE flows, N=1 unchanged.
- **Load** (M4): per-UE attach success rate and timing distribution across
  N concurrent UEs, building on the dashboard's existing KPI computation.

---

## 6. Explicit assumptions (flag if any of these are wrong)

1. **Target hardware is a single workstation** (consistent with everything
   else built in this repo so far, including the Hub's own documented
   ceiling of "~10-20 UEs on a standard workstation" for the N-process
   approach). If the real target is a dedicated multi-core server, the
   real-time-deadline risk (#4) and blind-search-budget risk (#3) headroom
   numbers from M2 should be read accordingly.
2. **CC count = 1** (no carrier aggregation) for this twin — simplifies
   `dl_harq_entity`/`ul_harq_entity` indexing to one dimension (per-UE) for
   now rather than two (per-UE, per-CC). If CA is ever needed later, the
   existing per-CC indexing pattern generalizes cleanly.
3. **PCAP RNTI-tagging is sufficient evidence** for "separate per-UE
   NAS/RRC flows" in the acceptance criteria — srsENB's MAC PCAP already
   tags packets by RNTI today, so no new PCAP-splitting code is needed,
   just enabling what exists (`[pcap] enable = none` → `mac`).
4. **The N=1 baseline is captured from the current live containers**
   (the same srsue4g/srsenb/srsepc this session has already verified
   end-to-end), not re-derived from a clean build — so the regression
   diff is against real, already-validated behavior.

---

## 7. Out of scope (unchanged from task brief, restated for the record)

Per-UE behavioral/Markov state machines, TRC-data-driven timing/field
correlation, signaling-storm trigger/backoff modeling, anything 5G/NR. This
plan leaves the `DownlinkEvent`/`TransmitIntent` hooks (section 2.3) for a
future layer to plug into, but does not build that layer.

---

## Next step

Pausing here for review, per process. Once approved, implementation starts
at M0 (lowest risk, no srsue source changes) and proceeds milestone by
milestone with a test at each step before moving on, per the agreed
process. Any srsRAN-internals refactor beyond what's scoped in section 2
will be flagged before being written, not after.
