# srsTwin — Virtual UE ⇄ Simulated DU (+ optional IQ Hub)

Virtual **srsUE** attaches to **OCUDU gNB/DU** and **Open5GS** over a ZeroMQ IQ link.
By default the UE talks **directly** to the gNB (hub bypass). Enable the **IQ hub**
when you need multiple UEs on one cell.

## Radio paths

**Direct (default — recommended for 1 UE):**

```
 ┌────────────┐    ZMQ IQ     ┌────────────┐    N2/N3    ┌────────────┐
 │  srsUE     │ ◀──────────▶  │  OCUDU     │ ◀────────▶  │  Open5GS   │
 └────────────┘               │  gNB / DU  │             └────────────┘
   tun_srsue                   ZMQ :2000/:2100
```

**Via IQ hub (multi-UE):**

```
 ┌────────────┐              ┌─────────────┐              ┌────────────┐
 │  srsUE (N) │ ◀── IQ ───▶  │  IQ hub     │ ◀── IQ ───▶  │  OCUDU     │ ◀──▶ Open5GS
 └────────────┘              │  10.53.1.4  │              │  gNB / DU  │
                             └─────────────┘              └────────────┘
```

| Mode | Config files | Compose |
|------|----------------|---------|
| **Direct** | `gnb_zmq.direct.yml`, `ue_zmq.direct.conf` | `docker-compose.yml` |
| **Hub** | `gnb_zmq.hub.yml`, `ue_zmq.hub.conf` | `+ docker-compose.hub.yml` |

Real PHY IQ (PRACH, PDCCH, PDSCH/PUSCH) uses srsRAN ZMQ REQ/REP lockstep.

## Components

| File | Purpose |
|------|---------|
| `gnb_zmq.direct.yml` | gNB ZMQ — peers with srsUE directly (**default**) |
| `ue_zmq.direct.conf` | srsUE — peers with gNB directly (**default**) |
| `gnb_zmq.hub.yml` | gNB ZMQ — peers with IQ hub |
| `ue_zmq.hub.conf` | srsUE slot 0 via hub `:3000` |
| `hub/iqhub.py` | IQ hub: DL fan-out, UL sum (hub mode only) |
| `ue_zmq2.conf` | srsUE slot 1 via hub `:3001` (multi-UE) |
| `compose-up.sh` | Staged bring-up for direct or hub |
| `verify.sh` | End-to-end checks (`RADIO_MODE=direct\|hub`) |

## Run it — direct (1 UE, hub bypass)

```bash
cd integration
bash compose-up.sh
# or manually:
docker compose build
docker compose up -d 5gc gnb srsue
RADIO_MODE=direct bash verify.sh
```

Startup order: `5gc` → `gnb` → `srsue`

## Run it — via IQ hub (1 UE)

```bash
cd integration
bash compose-up.sh hub
RADIO_MODE=hub bash verify.sh
```

Startup order: `5gc` → `gnb` → `hub` → `srsue`

## Run it — via IQ hub (2 UEs)

```bash
bash compose-up.sh hub multi
EXPECTED_UES=2 RADIO_MODE=hub bash verify.sh
```

## Gotchas (direct and hub)

1. **`continuous_tx = yes` on the UE** — keeps gNB ZMQ RX fed before RACH.
2. **Balanced 0 dB gains** on UE and gNB.
3. **`coreset0_index: 6`** — SSB where srsUE searches.
4. **srsUE PDCCH limits** — common SS2 + fallback DCI only.
5. **Clean restarts** — use `docker compose down -v` if ZMQ desyncs; don't partial-restart mid-link.

## Signaling storm (many UEs, configurable patterns)

To simulate a **signaling storm** — a variable number of UEs arriving under a
chosen pattern (burst / outage-recovery / Poisson / ramp / periodic) — see
[`storm/README.md`](storm/README.md). It adds two layers on top of this testbed:
a **bounded RF-real srsUE pool** through the IQ hub (genuine PRACH collisions,
near-far/capture) and a **UERANSIM scale layer** (hundreds of PHY-abstract UEs
straight to the AMF). A storm is one `storm/scenario.yml`:

```bash
python storm/generate.py storm/scenario.yml   # render artifacts
python storm/orchestrate.py --build            # play the arrival timeline
python storm/metrics.py                        # attach CDF, RACH/RRC contention
```

## Dashboard

**Localhost (recommended):**

```bash
cd integration/dashboard
python serve_dashboard.py              # http://127.0.0.1:8765/
python serve_dashboard.py --mode hub   # hub overlay for log pull
python serve_dashboard.py --pull       # pull container logs on start
```

Windows: `.\serve_dashboard.ps1`

The server auto-opens your browser, polls `/api/data` every 5s, and exposes **Pull logs & refresh** to copy live logs from Docker.

Static export (no server): open `dashboard/index.html` after `bash dashboard/gen_dashboard.sh`.

## What "working" looks like

* gNB: `NG setup procedure completed`, cell up on ZMQ.
* srsUE: `PDU Session Establishment successful. IP: 10.45.1.x`.
* `ping -I tun_srsue 10.45.0.1` succeeds inside the UE container.
