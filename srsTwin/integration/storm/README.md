# srsTwin Signaling Storm

Simulate a configurable surge of UEs — a **signaling storm** — on top of the
single-UE srsTwin testbed, as RF-realistically as one host allows.

Two layers share one Open5GS core:

```
 Layer A  RF-REAL, bounded     srsUE pool ─IQ─▶ IQ hub ─IQ─▶ OCUDU gNB ─┐
   real PRACH collisions,             (per-UE channel model)            │ N2/N3
   near-far/capture, real RRC/NAS                                       ├─▶ Open5GS
 Layer B  PHY-ABSTRACT, scale  UERANSIM gNB + N UEs ──────NGAP/NAS──────┘   (one SIM DB)
   hundreds of cheap NAS regs              (SCTP straight to the AMF)
```

* **Layer A** runs real software modems (full PHY) over the existing ZeroMQ IQ
  hub. Because the hub **sums** uplink IQ and **broadcasts** downlink, two UEs
  RACHing in the same window genuinely collide, and the per-UE channel model
  (near-far `ul_gain` + fading) makes the collision resolve by *capture* the way it
  would on a real cell. **Keep the pool small (2–4).** The limit is *not* CPU
  cores — all N UEs + the gNB share **one ZMQ lockstep**, so the cell's virtual
  sample clock slows roughly linearly with N. It stays PHY-correct, but above ~4
  UEs the clock crawls and attach times balloon. The expensive channel ops (CFO,
  DL AWGN) are **off by default** for this reason (they cratered throughput ~20×);
  re-enable them via the `*_heavy` profiles only with a 2–3 UE pool.
* **Layer B** uses [UERANSIM](https://github.com/aligungr/UERANSIM): UEs that
  speak NAS/RRC over NGAP straight to the AMF with **no PHY**, so hundreds cost
  almost nothing. This is the scale knob for *core-network* signaling load.

A storm is **one `scenario.yml`** → `generate.py` renders artifacts →
`orchestrate.py` plays the arrival timeline → `metrics.py` measures the result.

## Quick start

```bash
cd integration

# 1. Describe the storm (edit storm/scenario.yml — see below)
# 2. Render configs / compose / subscribers / manifest
python storm/generate.py storm/scenario.yml

# 3. Build images once (heavy), then play the storm
python storm/orchestrate.py --build          # subsequent runs: drop --build

# 4. Measure it
python storm/metrics.py
```

Tear down: `python storm/orchestrate.py --down` (or
`docker compose -p srstwin_storm -f docker-compose.storm.yml down -v`).

> Bring the plain `docker-compose.yml` / hub stack **down** first — the storm
> reuses subnet `10.53.1.0/24` and the same container names.

## scenario.yml

```yaml
name: stadium-flash-crowd
duration_s: 120          # storm timeline length
seed: 1                  # reproducible arrivals + RF realizations

layer_a:                 # RF-real pool (CPU-bounded)
  pool_size: 8           # concurrent srsUE slots = the CPU knob (~1 core each)
  total_arrivals: 40     # arrival EVENTS recycled across the pool
  rf_profile_mix:        # fractions over {near, mid, edge, ideal}
    near: 0.25
    mid: 0.45
    edge: 0.30
  behavior:              # each UE: attach -> ping -> detach, then the slot frees
    ping_count: 5
    attach_timeout_s: 90
    idle_after_s: 0

layer_b:                 # UERANSIM scale
  total_ues: 0           # set >0 to enable (needs the UERANSIM image, see below)

pattern:
  type: burst            # burst | outage_recovery | poisson | ramp | periodic
  params:
    start_s: 10
    window_s: 8
```

### Key idea: arrivals vs slots

`total_arrivals` (how many UEs *show up*) is decoupled from `pool_size` (how many
run *at once*). The orchestrator assigns each arrival a free slot and frees it on
detach, so a small pool serves many arrival events. When arrivals outrun the pool
they **queue** — which is exactly the admission/backoff behavior of an overloaded
cell. `metrics.py` reports that queueing as the "admission delay".

### Arrival patterns (`storm/patterns.py`)

| type | models | params |
|------|--------|--------|
| `burst` | flash-crowd / paging flood | `start_s`, `window_s` |
| `outage_recovery` | mass reconnect after power restored | `start_s`, `tau_s` (decay) |
| `poisson` | independent steady arrivals | — (uniform at `n/duration`) |
| `ramp` | linearly rising load | `rate_start`, `rate_end` |
| `periodic` | diurnal / periodic IoT check-ins | `period_s`, `amplitude`, `phase` |

### RF profiles (`storm/rf_profiles.py`)

`near` / `mid` / `edge` set per-UE uplink gain, downlink SNR, fading
(Rician/Rayleigh), CFO and propagation delay; `ideal` is identity passthrough.
Values are deliberately conservative — even `edge` stays decodable (the verified
ZMQ link needs balanced ~0 dB gains), but the near-far *spread* is what drives the
PRACH capture effect. RF degradation is applied **inside the hub** (`hub_core.py`),
never by changing UE/gNB gains, so the baseline link stays healthy.

## What gets generated (`storm/gen/`)

| file | what |
|------|------|
| `../docker-compose.storm.yml` | self-contained stack (written to `integration/`) |
| `subscribers.storm.csv` | Open5GS SIM DB for both layers (sequential IMSIs) |
| `ue_NN.conf` | one srsUE config per Layer-A slot |
| `ueransim_gnb.yaml`, `ueransim_ue.yaml` | Layer-B configs (when `total_ues>0`) |
| `manifest.json` | everything `orchestrate.py` consumes |
| `events.csv`, `metrics.json` | produced by the run + `metrics.py` |

## Layer B (UERANSIM) — enabling scale

1. Set `layer_b.total_ues` > 0 and `python storm/generate.py`.
2. `python storm/orchestrate.py --build` builds the UERANSIM image
   (`integration/Dockerfile.ueransim`, pinned to `v3.2.6`). If the source build is
   too slow, swap in a community prebuilt image by editing the `ueransim` service
   `build:` → `image:` in the generated compose (or in `generate.py`).
3. The orchestrator starts `nr-gnb` (a second NGAP association into the same AMF)
   and fires `nr-ue` batches along the **same arrival pattern**, so the AMF sees
   the combined Layer-A + Layer-B registration surge.

UERANSIM UEs need `/dev/net/tun` + `NET_ADMIN` (already set in the service).

## Measuring the storm (`storm/metrics.py`)

Reads `events.csv` (Layer-A attach outcomes) and the live gNB log, and reports:

* attach success rate and **latency CDF** (p50/p90/max) — overall and **per RF
  profile** (watch `edge` attach worse / later than `near`),
* admission/queue delay (pool saturation),
* gNB **RACH events**, RRC setup requests vs completions (a contention proxy), and
  NGAP registrations,
* a `metrics.json` timeline (1 s buckets of arrivals/attaches) for plotting.

## Tests (no Docker needed)

```bash
python storm/tests/test_patterns.py     # arrival-process shapes
python storm/tests/test_channel.py      # channel math + identity baseline
python hub/tests/test_hub_core.py        # UL summation (still byte-exact)
python hub/tests/test_lockstep.py        # hub transport over real ZMQ
```

## How it stays true to the verified baseline

* With no channel registered, `hub_core` ops are the strict identity — the
  1-UE/2-UE link and its tests are untouched.
* The hub gained dynamic **join while others are active** and slot **recycle on
  leave** (UL-miss timeout), but the DL fan-out / UL sum / lockstep are unchanged.
* All the documented ZMQ gotchas (`continuous_tx=yes`, 0 dB gains,
  `coreset0_index 6`, common SS2 + fallback DCI, clean ordered bring-up) are
  preserved in the generated configs.
