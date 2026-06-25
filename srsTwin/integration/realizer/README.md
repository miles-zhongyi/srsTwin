# Shared Realizer

Multi-UE 4G LTE: N logical UEs sharing one PHY/radio pipeline against
srsENB + srsEPC, so the network sees N distinct UEs instead of one. See
[`PLAN.md`](PLAN.md) for the full architecture decision, risk register, and
milestones.

## Status: M0 complete, M1 not started

M0 is scaffolding only — **no srsue source has been touched.** Everything
below is new tooling/specs that M1 (the actual srsue MAC/PHY dispatch work)
builds against.

| File | What |
|---|---|
| `gen_user_db.py` | Generates N distinct subscriber identities (IMSI/Ki/OPc): srsEPC's `user_db.csv` format + a JSON source for the realizer's per-UE USIM provisioning. Does **not** replace `4g_configs/subscribers.csv` — that stays authoritative for the unchanged num_ues=1 path. |
| `interfaces.py` | `TransmitIntent` / `DownlinkEvent` — the only two types a future state-machine layer touches. `per_encoded_bytes` stays `None` until M5; M1-M4 use srsUE's native encoder. |
| `ue_context.h` | C++ interface skeleton for the per-UE bundle (`mac`/`rlc`/`pdcp`/`rrc`/`nas`/`usim`/RA state). Forward declarations only, not included by any build target — M1 implements this for real inside `srsue/hdr/stack/ue_stack_lte.h`. Syntax-checked with the container's `g++ -std=c++17 -fsyntax-only`, nothing more. |
| `capture_n1_baseline.py` | Captures the current N=1 attach signaling sequence (layer+label, order-sensitive, not timestamp-sensitive) from the live containers. |
| `check_n1_baseline.py` | Diffs current N=1 behavior against the captured baseline. **Hard regression gate** — must pass before any M1+ change is considered done (constraint: "N=1 must still work exactly as today"). |
| `baselines/n1_attach_baseline.json` | The frozen baseline — 34 events, full attach → bearer setup → release, captured against `srstwin_ue4g`/`srstwin_enb`/`srstwin_epc` before any realizer code exists. |

## Running the M0 tests

```bash
cd integration/realizer
python3 tests/test_gen_user_db.py
python3 tests/test_interfaces.py
python3 tests/test_baseline_diff.py     # pure diff-logic unit tests, no docker needed
python3 check_n1_baseline.py            # live regression check against the running 4G stack
```

`check_n1_baseline.py` and `capture_n1_baseline.py` need the live
`srstwin_ue4g` / `srstwin_enb` / `srstwin_epc` containers up (the existing
4G stack — `docker compose -f ../docker-compose.4g.yml up -d` from
`integration/`) and at least one completed attach in their logs.

## Next: M1

2 logical UEs, native encoder (no byte injection — that's M5), full
attach → bearer → signaling → detach against **unmodified** srsENB/srsEPC.
This is where the actual srsue source changes start: top-level wiring in
`ue_stack_lte` to own N `UeContext`s, the PDCCH-dispatch-by-RNTI layer, and
shared uplink resource-grid placement. See PLAN.md sections 1-3 for the
full design and risk register before starting — risk #1 (grant misrouting)
is the one to design around most carefully.
