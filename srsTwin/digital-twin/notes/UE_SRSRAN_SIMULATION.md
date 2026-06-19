# UE Simulation with srsRAN Integration

Extends the existing S1AP digital twin to drive real UE software (`srsue`) against a
live srsRAN eNodeB/EPC stack, producing genuine S1AP traffic from synthetic session
profiles.

---

## Background

### What the current system does

`generate_logs.py` is a pure offline simulator:

1. **Hawkes process** (`arrival_model.py`) → realistic UE arrival times per cell
2. **SessionTransformer** (`model.py`) → S1AP message sequences per session
3. **Output** → JSON file of S1AP events (post-hoc logs, no live radio)

### The abstraction gap

The generated JSON cannot be fed directly into srsRAN. srsRAN's eNB produces S1AP
traffic as a *side-effect* of UEs connecting over the radio interface. To get real
S1AP traffic out of srsRAN you must drive actual `srsue` processes.

**The mapping:**  
session token sequences → behavioral scripts for `srsue` processes →
`srsue` connects to `srsenb` → `srsenb` generates real S1AP to `srsepc`

---

## Target Architecture

```
┌──────────────────────────────────────────────────────────┐
│                   Session Profile Layer                   │
│                                                          │
│  arrival_model.py  ──►  arrival times (Hawkes process)   │
│  model.py          ──►  session token sequences          │
│  state_machine.py  ──►  protocol-valid transition guard  │
└────────────────────────────┬─────────────────────────────┘
                             │  UEPlan objects
                             ▼
┌──────────────────────────────────────────────────────────┐
│               UE Orchestrator  (ue_simulator.py)          │
│                                                          │
│  - IMSI pool manager (assigns IMSIs from user_db.csv)    │
│  - Real-time scheduler (wall-clock or compressed time)   │
│  - srsue process launcher / teardown                     │
│  - Token → lifecycle action translator                   │
│  - Traffic injector (ping / iperf per UE netns)          │
└──────┬─────────────────────────────────────┬─────────────┘
       │  ZMQ RF (unique port pair per UE)   │
       ▼                                     ▼
┌─────────────┐                   ┌──────────────────────┐
│  srsue #1   │◄──────────────────►│                      │
│  srsue #2   │◄──────────────────►│  srsenb  (ZMQ mode)  │
│  srsue #N   │◄──────────────────►│                      │
└─────────────┘                   └──────────┬───────────┘
                                             │ S1AP / GTP-U
                                             ▼
                                   ┌──────────────────────┐
                                   │  srsepc              │
                                   │  (MME + SGW + PGW)   │
                                   │  user_db.csv         │
                                   └──────────────────────┘
```

---

## Component Breakdown

### 1. Session Token → UE Behavior Mapping

The existing `state_machine.py` states map cleanly onto `srsue` lifecycle events:

| S1AP state transition | Token(s) | `srsue` action |
|---|---|---|
| `IDLE → CONNECTING` | `S1_INITIAL_UE_MESSAGE` | Launch `srsue` process (auto-attaches on start) |
| `CONNECTING → CONTEXT_ACTIVE` | `S1_INITIAL_CONTEXT_SETUP_RESPONSE` | Wait for attach confirmation in srsue log |
| `CONTEXT_ACTIVE` | `S1_ERAB_SETUP_REQUEST/RESPONSE` | Start traffic generator (`ping` or `iperf3`) |
| `CONTEXT_ACTIVE` | `S1_UPLINK/DOWNLINK_NAS_TRANSPORT` | Idle NAS keep-alive (no explicit action needed) |
| `CONTEXT_ACTIVE → HO_PENDING` | `S1_HANDOVER_REQUIRED` | Reconfigure UE for target cell (future work) |
| `RELEASING → DONE` | `S1_UE_CONTEXT_RELEASE_COMPLETE` | `SIGTERM` to `srsue` process |

The timing between actions is derived from the token's bucket index
(`T0`–`T6` → `BUCKET_MIDPOINTS` in `generate_logs.py`).

---

### 2. New File: `ue_simulator.py`

**Responsibilities:**

- Load Hawkes arrival params and sample arrival times (reuses `sample_hawkes`)
- For each arrival, generate a session token sequence (reuses `model.generate`)
- Translate token sequence into a `UEPlan` dataclass:
  ```
  UEPlan:
    cell_id          int
    imsi             str        # allocated from IMSI pool
    ki               str        # auth key matching user_db.csv
    arrival_time_s   float      # when to launch srsue
    session_dur_s    float      # how long before detach
    has_data         bool       # whether ERAB setup was in token seq
    has_handover     bool       # whether HO_REQUIRED was in token seq
    zmq_tx_port      int        # assigned at launch time
    zmq_rx_port      int        # assigned at launch time
  ```
- Real-time scheduler loop: sleep until next arrival, launch `srsue`, schedule detach
- Process manager: track running `srsue` PIDs, log stdout, clean up on exit
- Optional: compressed-time mode where 1 real second = N simulated seconds

**Key design decisions:**

- Each `srsue` is a separate OS process — clean isolation, no shared state
- ZMQ port pairs are drawn from a pre-allocated pool (e.g. base port 2000, +2 per UE)
- The IMSI pool is pre-registered in `user_db.csv` so `srsepc` accepts any of them
- srsue config is generated from a Jinja2/string template per UE at launch time

---

### 3. New Directory: `srsran_config/`

```
srsran_config/
├── enb.conf              # srsenb config (ZMQ, N UE ports)
├── epc.conf              # srsepc config
├── ue_template.conf      # srsue config template (IMSI/port vars substituted)
├── user_db.csv           # pre-provisioned IMSI pool (generated by gen_user_db.py)
├── gen_user_db.py        # script to generate IMSI pool + user_db.csv
└── rr.conf               # srsenb radio resource config (cell params)
```

#### `enb.conf` (ZMQ key section)

```ini
[rf]
device_name = zmq
device_args = fail_on_disconnect=true,\
              tx_port0=tcp://*:2000,rx_port0=tcp://localhost:2001,\
              tx_port1=tcp://*:2002,rx_port1=tcp://localhost:2003,\
              ...

[enb]
enb_id = 0x19C2A5      # matches ENB_ID = "499557" in generate_logs.py
mcc = 302
mnc = 221              # matches PLMN = "302 221"
```

#### `ue_template.conf` (substitution variables: `{imsi}`, `{ki}`, `{tx_port}`, `{rx_port}`)

```ini
[usim]
mode = soft
algo = milenage
imsi = {imsi}
imei = 353490069873319
k    = {ki}
opc  = 63BFA50EE6523365FF14C1F45F88737D

[rf]
device_name = zmq
device_args = tx_port=tcp://localhost:{tx_port},rx_port=tcp://localhost:{rx_port},\
              id=ue,base_srate=23.04e6

[rat.eutra]
dl_earfcn = 3400
nof_carriers = 1
```

#### `user_db.csv` format (srsEPC subscriber DB)

```csv
# Name, Auth, IMSI, Key, OP_Type, OPC, AMF, SQN, QCI, IP_alloc
ue001,mil,001010123456001,00112233445566778899aabbccddeeff,opc,63BFA50EE6523365FF14C1F45F88737D,9000,000000001489,9,dynamic
ue002,mil,001010123456002,00112233445566778899aabbccddeeff,opc,...
```

---

### 4. Multi-UE ZMQ Port Assignment

srsRAN 4G's `srsenb` supports multiple ZMQ UE ports when compiled with ZMQ support.
Each UE gets an exclusive TX/RX port pair:

```
UE index 0:  tx=2000, rx=2001
UE index 1:  tx=2002, rx=2003
UE index 2:  tx=2004, rx=2005
...
UE index N:  tx=2000+(N*2), rx=2001+(N*2)
```

The orchestrator maintains a `PortPool` that checks out/returns port pairs as UEs
attach and detach. The `enb.conf` must be pre-configured with enough port pairs for
`max_concurrent_ues`.

---

## Practical Constraints

| Concern | Detail |
|---|---|
| **Scale** | Each `srsue` is a full LTE stack process (~200 MB RAM, 1 CPU core). Realistic ceiling is ~10–20 concurrent UEs on a standard workstation. |
| **srsRAN version** | Target **srsRAN 4G** (`srsenb` / `srsue` / `srsepc`). srsRAN Project (5G NR) has less mature multi-UE ZMQ support. |
| **No SDR hardware** | `device_name=zmq` runs entirely in software. |
| **srsEPC deprecation** | srsEPC is deprecated upstream but fully functional for this use case. Alternative: Open5GS as the EPC. |
| **OS requirement** | Linux only (UE network namespace isolation, TUN/TAP). |
| **Timing fidelity** | Compressed-time mode is approximate — the Hawkes interarrival times are real, but srsue attach latency (~1–2 s) adds jitter to session start times. |

---

## Prerequisites

### Install srsRAN 4G

```bash
# Ubuntu/Debian
sudo apt install cmake libfftw3-dev libmbedtls-dev libboost-program-options-dev \
                 libconfig++-dev libsctp-dev libzmq3-dev

git clone https://github.com/srsran/srsRAN_4G.git
cd srsRAN_4G
mkdir build && cd build
cmake .. -DENABLE_ZMQ=ON
make -j$(nproc)
sudo make install
sudo ldconfig
```

### Verify ZMQ support

```bash
srsue --version   # should mention ZMQ in RF device list
srsenb --version
srsepc --version
```

### Python dependencies (add to `requirements.txt`)

```
psutil       # process monitoring
jinja2       # UE config template rendering
```

---

## Startup Sequence

```
1. gen_user_db.py          → generates srsran_config/user_db.csv (IMSI pool)
2. srsepc -c epc.conf      → start core network (background)
3. srsenb -c enb.conf      → start eNodeB in ZMQ mode (background)
4. ue_simulator.py         → start orchestrator
       ├── loads Hawkes arrival params
       ├── loads SessionTransformer checkpoint
       ├── samples arrival schedule
       └── real-time loop:
             wait → launch srsue → wait session_dur_s → SIGTERM srsue
```

---

## Files to Create

| File | Purpose |
|---|---|
| `ue_simulator.py` | Main orchestrator (new) |
| `srsran_config/enb.conf` | srsenb config with ZMQ multi-UE ports |
| `srsran_config/epc.conf` | srsepc config |
| `srsran_config/rr.conf` | Radio resource config (cell params matching dataset) |
| `srsran_config/ue_template.conf` | Per-UE srsue config template |
| `srsran_config/gen_user_db.py` | IMSI pool + user_db.csv generator |

### Files unchanged

`generate_logs.py`, `arrival_model.py`, `model.py`, `state_machine.py`,
`tokenize_sessions.py`, `train.py`, `validate.py` — all stay as-is.

---

## Future Extensions

- **Multi-cell handover simulation**: when `S1_HANDOVER_REQUIRED` appears in a token
  sequence, reconfigure the UE's EARFCN to point at a second `srsenb` instance
- **Traffic shaping**: vary `iperf3` bitrate based on ERAB count in session tokens
- **Log correlation**: capture `srsepc` S1AP PCAP and compare message distribution
  against `generate_logs.py` output to measure fidelity
- **Open5GS EPC**: drop-in replacement for `srsepc` with better multi-UE scalability
- **srsRAN Project (5G NR)**: extend to NR once multi-UE ZMQ matures upstream
