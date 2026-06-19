# F1AP Conformance Tester — Architecture & ML Strategy

Architectural plan for the next phase of the digital twin, based on the
June 8 2026 team meeting and subsequent design discussions. Supersedes the
srsRAN/srsUE direction in `UE_SRSRAN_SIMULATION.md` for the immediate
next milestone.

---

## Use Cases (from June 8 2026 meeting)

| # | Name | Goal | Status |
|---|---|---|---|
| 1 | RF Simulator | Train a NN to adapt the Samsung Korean propagation model to the Canadian RF environment. Predict cell coverage and signal penetration across a continuous mixture of bands (not discrete near/medium/far profiles like Viavi). | Secondary — future work |
| 2 | Conformance Tester | Simulate a signaling storm against a DU. Leverage F1 link data to test DU behaviour under duress. | **Immediate next step** |

Use Case 2 is the focus of this document.

---

## Full O-RAN Stack Context

```
UE
 ↕  (Uu — air interface: PRACH, PDSCH, PUSCH, PDCCH, PUCCH)
RU   — RF conversion, antenna, lower PHY (FFT, PRACH filtering)
 ↕  (eCPRI / O-RAN 7.2x fronthaul)
DU   — upper PHY (channel coding / LDPC), MAC, RLC
 ↕  (F1AP over SCTP — this is the target interface)
CU   — PDCP, RRC, SDAP
 ↕  (S1AP / NGAP)
Core — MME/AMF, SMF, UPF  (e.g. Open5GS)
```

The existing ML model (SessionTransformer) was trained on S1AP traces, which
sit at the **CU ↔ Core** boundary. The conformance tester targets the
**DU ↔ CU** boundary (F1AP), one layer lower.

---

## Why F1AP, Not the Air Interface

The device under test is the DU. To stress it there are two approaches:

| Approach | Attack vector | PHY required? | Scalability |
|---|---|---|---|
| UE emulator (srsUE / Amarisoft) | Uu air interface — from below the DU | Yes — full DSP | ~30 concurrent UEs (srsUE); ~1000 (Amarisoft, commercial) |
| CU emulator (custom code) | F1AP — from above the DU | No | Limited only by SCTP connections and process resources |

Key constraints on the UE emulator path:
- srsRAN Project was archived in early 2026 (successor: OCUDU at `gitlab.com/ocudu/ocudu`)
- srsUE hits ZMQ buffer saturation beyond ~3 concurrent transmitting UEs
- Pushing to ~30 UEs requires staggered PRACH timing and still produces gridlock

The F1AP CU emulator path avoids all of this. "1000 virtual UEs" is 1000 tracked
UE context IDs in one process — no ZMQ, no IQ samples, no per-UE processes.

The team is also acquiring **F1 link captures** from partners as the primary data
source. That data lives at F1AP by definition, making the CU emulator path the
natural fit.

---

## Conformance Tester Architecture

```
ML CU model (generates session sequences)
        ↓
F1AP encoder — pycrate ASN.1 APER + SCTP socket
        ↓  SCTP port 38472
DU under test (OCUDU in dev; real production DU in production)
```

No RU. No air interface. No UE processes.

### Wire format requirements

| Layer | Detail |
|---|---|
| Transport | **SCTP** (not TCP, not UDP — 3GPP mandates it for F1AP). Linux supports natively via kernel SCTP module. |
| Encoding | **ASN.1 Aligned Packed Encoding Rules (APER)** — binary format F1AP PDUs are serialised into. Raw JSON or dicts will not be accepted. |
| Port | **38472** (F1AP standard port) — the DU connects outbound to the CU emulator. Your code listens; OCUDU/real DU dials in. |
| ASN.1 library | **pycrate** — ships with 3GPP NR ASN.1 schemas pre-compiled including F1AP (TS 38.473). |

### Required conversation sequence

F1 Setup must complete before any UE messages:

```
DU  →  F1 Setup Request          (DU announces ID, cells, PLMNs)
CU  ←  F1 Setup Response         (CU accepts)
─── F1 link up ───
DU  →  Initial UL RRC Msg        (simulates a UE attaching / PRACH)
CU  ←  UE Context Setup Request  (CU assigns bearers)
DU  →  UE Context Setup Response
CU  ←  UE Context Release Cmd    (CU tears down UE)
DU  →  UE Context Release Cmpl
```

### Signaling storm behaviours

| Storm type | F1AP representation |
|---|---|
| PRACH overload | Flood of `Initial UL RRC Message Transfer` — each represents a new UE attaching. Creates DU internal state per message. |
| RRC state thrashing | Rapid `UE Context Setup Request` → `UE Context Release Command` cycles |
| PCAP replay | Decode real F1AP PCAPs with pycrate; re-send as SCTP payloads directly |

### Implementation sketch

```python
import sctp          # pip install pysctp
import pycrate_mobile.TS38473_F1AP as f1ap

# CU emulator listens — DU dials in
sock = sctp.sctpsocket_tcp(socket.AF_INET)
sock.bind(('0.0.0.0', 38472))
sock.listen(1)
conn, addr = sock.accept()

# F1 Setup handshake
raw = conn.recv(4096)
pdu = f1ap.F1AP_PDU()
pdu.from_aper(raw)                     # decode APER → Python dict
conn.send(build_f1_setup_response(pdu).to_aper())

# Storm loop driven by ML model
for msg_name, ue_id in ml_model.generate_storm():
    pdu = build_f1ap_pdu(msg_name, ue_id)   # template lookup + IE fill
    conn.send(pdu.to_aper())
```

`build_f1ap_pdu()` is where the ML pipeline connects: takes a message name
token, looks up the template for that message type (derived from training
captures), fills dynamic fields (UE IDs, C-RNTI, cell ID, RRC container),
returns an encodable pycrate object.

### Information Element (IE) population

The ML model predicts *which message* to send next — it does not determine IE
values. Those require a small protocol state layer alongside the model:

- **gNB-CU-UE-F1AP-ID**: your counter, incremented per new UE
- **gNB-DU-UE-F1AP-ID**: echoed back from the DU's Initial UL RRC message
- **SpCell-ID**: from the F1 Setup negotiation (known at handshake time)
- **RRC containers**: can be minimal / zero-padded for storm testing
- **DRB templates**: single minimal DRB sufficient for conformance testing

For storm/conformance purposes IE values need to be structurally valid (correct
APER encoding, mandatory fields present, correct types) but not semantically
precise. Templates derived from training captures satisfy this.

---

## Two ML Models

The end goal is two independently trained and deployable models:

```
Production F1AP captures (bidirectional)
        ↓
┌─────────────────────┐    ┌─────────────────────┐
│    CU model         │    │    DU model          │
│  generates CU-side  │    │  generates DU-side   │
│  messages and storm │    │  responses including │
│  sequences          │    │  overload behaviour  │
└─────────────────────┘    └─────────────────────┘
        ↕  F1AP (SCTP / APER)
Either: real production DU (Phase 2)
Or:     ML DU model (Phase 3 — fully synthetic twin)
```

Two separate models (rather than one model with role conditioning) is preferred
because:
- DU firmware updates affect only DU behaviour → retrain only the DU model
- CU logic changes affect only CU behaviour → retrain only the CU model
- Each model can be trained on data from different sources or time windows

### Bidirectional token vocabulary

Both models are trained on the **same full bidirectional F1AP sequences** — not
split by direction. Direction is encoded into each token:

```
# Old (S1AP, direction=2 for all — dataset quirk)
INITIAL_UL_RRC_MESSAGE_TRANSFER
UE_CONTEXT_SETUP_REQUEST
UE_CONTEXT_SETUP_RESPONSE

# New (F1AP, direction in token name)
DU:INITIAL_UL_RRC_MESSAGE_TRANSFER
CU:UE_CONTEXT_SETUP_REQUEST
DU:UE_CONTEXT_SETUP_RESPONSE
CU:UE_CONTEXT_RELEASE_COMMAND
```

This preserves cause-and-effect across the F1 interface — the CU model learns
that `DU:INITIAL_UL_RRC` causes `CU:UE_CONTEXT_SETUP_REQUEST`, not just that
those messages exist independently.

At inference, role masking is applied: the CU model's output logits for all
`DU:` tokens are zeroed before sampling (and vice versa for the DU model),
so each model only generates its own role's messages.

### Pipeline changes required

| File | Change |
|---|---|
| `reconstruct_sessions.py` | Preserve direction field from F1AP captures; tag each event as `DU:` or `CU:` |
| `tokenize_sessions.py` | Prefix each token with direction |
| `vocab.json` | Expanded vocabulary — both directions of every F1AP message type |
| `generate_logs.py` | At inference, apply role mask to logits before sampling |
| `train.py` | Unchanged — same transformer architecture, larger vocabulary |
| `anomaly_detection.py` | Unchanged — perplexity scoring works on direction-prefixed tokens |

---

## Pre-training and Fine-tuning Strategy

The model is developed in two stages (transfer learning):

### Stage 1 — Pre-training on OCUDU (unlimited, free data)

OCUDU runs the full O-RAN disaggregated stack with F1AP. Capturing traces from
OCUDU under various conditions gives unlimited spec-compliant F1AP data for free.

Pre-training teaches the model:
- F1AP message grammar (which messages exist, which sequences are valid)
- Normal procedure flows (F1 Setup, UE Context Setup/Modify/Release, Handover)
- Spec-compliant error responses (failure causes, error indications)

**Important**: OCUDU's responses will not match the production DU's vendor-specific
behaviour. Do not use OCUDU traces to train the DU model's device-specific
characteristics — use them only to bootstrap the general F1AP foundation.

### Stage 2 — Fine-tuning on production traces (limited, high-value data)

Fine-tuning adapts the pre-trained base to a specific device:

```
Base model (pre-trained on OCUDU)
        ↓ fine-tune, lower learning rate
Device model (trained on production DU traces)
  - Normal traces  → adapts baseline behaviour to this specific DU
  - Overload traces → learns how THIS DU responds under stress
```

A lower learning rate during fine-tuning prevents catastrophic forgetting — the
model nudges toward the production distribution without erasing the F1AP
foundation learned during pre-training.

### Multi-device scalability

```
Base model (pre-trained on OCUDU)
    ├── fine-tune → Samsung DU model
    ├── fine-tune → Ericsson DU model
    └── fine-tune → Nokia DU model
```

One base, many device-specific checkpoints. A vendor firmware update triggers
a fine-tune of that vendor's checkpoint only — not a full retrain.

---

## Overload Regime: A Special Training Requirement

A model trained only on normal F1AP traffic will generate normal responses
even when the CU is generating storm-level load. Under real overload the DU:

- Rejects `UE Context Setup Requests` with cause `radio-resources-not-available`
- Sends `F1AP Error Indication` messages
- Initiates `UE Context Release Requests` (DU-initiated, giving up on UEs)
- Delays or drops responses as queues fill
- May trigger `F1 Reset` to recover

None of these appear in normal traces. The model has zero probability for them
and will keep generating successful responses regardless of simulated load.

**Solution**: the fine-tuning dataset must include overload-scenario traces:

```
OCUDU pre-training
  → learns F1AP grammar and normal flows

Fine-tune: production normal traces
  → learns device-specific normal deviations

Fine-tune: production overload traces    ← required for storm fidelity
  → learns how this DU behaves under stress
```

Overload traces are captured during Phase 2 by running the CU emulator against
the production DU under increasing load and capturing the F1AP exchange
throughout. The same Phase 2 session that validates the CU emulator also
produces the overload training data.

### Asymmetry between CU and DU models

| Model | Overload training needed? | Reason |
|---|---|---|
| CU storm generator | No | Generating storms is amplified normal CU behaviour — same message types, higher rate |
| DU response model | Yes | DU overload responses are qualitatively different from normal — new message types, failure causes, silences not present in normal traces |

---

## Data Drift Detection

The existing `anomaly_detection.py` perplexity scorer doubles as a model
calibration monitor — no additional tooling required:

- Run live production F1AP traces through either model on a schedule
- Rising average perplexity = model is surprised by new patterns = drift
- Spike concentrated in specific message types = targeted retraining signal
  (e.g. only DU handover responses drifted, CU side still calibrated)

When perplexity crosses a configured threshold, pull fresh traces and fine-tune
the affected model checkpoint. The base pre-trained model is never retouched.

---

## Phased Roadmap

### Phase 1 — OCUDU (development scaffolding)

```
Custom CU emulator → OCUDU DU
```

Purpose: validate SCTP transport, APER encoding, F1 Setup procedure, and
message structure. OCUDU confirms messages are structurally valid, not that
they reflect production behaviour. Pre-train both base models on OCUDU traces.

Deliverables:
- CU emulator skeleton (SCTP listener, F1 Setup handler, message templates)
- F1AP token vocabulary + updated tokenizer
- Base model checkpoints (CU + DU) pre-trained on OCUDU traces

### Phase 2 — Production DU (calibration)

```
Custom CU emulator → real production DU
Capture bidirectional F1AP traces: normal load + deliberate overload
```

Purpose: validate CU emulator against real device; build the fine-tuning dataset
for both models (normal and overload regimes).

Deliverables:
- CU model fine-tuned on production CU-side traces
- DU model fine-tuned on production DU-side traces (normal + overload)
- Validated storm sequences that produce measurable DU degradation

### Phase 3 — Fully independent twin

```
ML CU model  ↔  ML DU model
```

No production DU required at runtime. Both models trained on real production
traces, running against each other in a closed loop. Retrain only on drift.

Deliverables:
- Closed-loop synthetic F1AP twin
- Drift monitoring pipeline (scheduled perplexity scoring on live traces)
- Per-vendor fine-tune checkpoints

---

## Relationship to Existing Files

| File | Relationship |
|---|---|
| `POC_SCOPE_AND_ROADMAP.md` | This document addresses Scenario 2 (DU/CU validation) at a lower layer (F1AP instead of S1AP). Milestone 2 in that doc maps to Phase 2 here. |
| `UE_SRSRAN_SIMULATION.md` | Superseded for the conformance tester use case. srsUE/srsRAN approach remains valid if Uu-layer (PHY/MAC scheduler) testing is needed in the future. |
| `NEXT_STEPS.md` | The LLM→srsRAN translation gap described there is no longer on the critical path for this use case. |
| `anomaly_detection.py` | Reused as drift detector. No changes required. |
| `train.py` / `model.py` | Architecture unchanged. Vocabulary expansion and direction-prefixed tokens are the only required modifications. |
| `tokenize_sessions.py` | Needs direction tagging when processing F1AP captures. |
