# POC Scope and Roadmap

What the current ML model proves, how it maps onto the digital twin POC
scenarios, and what additional work is required to reach the full vision.

---

## What the current model proves

The central question the POC leaves open is whether ML can learn real
eNB-specific behaviour from production traces, or whether a deterministic
model would have to be manually tuned to match it.

The model answers that question with quantifiable results:

- Learned, with no manual configuration, that this specific eNB fails S1
  handovers 100% of the time — behaviour that would not appear in any
  spec-based implementation
- Assigns anomaly scores 8–157× higher than normal sessions to every known
  anomalous session type, with **100% recall** on all flagged categories
- Converged in under 2 minutes on a single GPU
- Generates synthetic traces at arrival rates that match real per-cell
  traffic measured in the production data

The core claim this supports: **the data-driven behavioural learning
approach works, and works on real production data.**

---

## Mapping to POC scenarios

### Scenario 3 — Production issue reproduction
**Status: functionally complete at the signalling level.**

The pipeline can ingest real incident traces, reconstruct the session
sequences, score them for anomalousness, and reproduce the failure pattern.
The S1 handover failure cluster is a concrete demonstration: the model
identifies every failure session without being told what to look for.

What is not yet built: the parametric fix step — automatically suggesting
which configuration change resolves the issue. The detection and
reproduction are real; the automated remediation is future work.

srsRAN/OAI is not required for this scenario.

### Scenario 2 — DU/CU integration and configuration validation
**Status: Layer 1 built, Layer 2 (O-RAN interface) is the next milestone.**

The ML model is the scenario engine — it provides realistic session
patterns, arrival rates, and failure signatures to drive an RU emulator.
srsRAN Project and OpenAirInterface both implement the full O-RAN 7.2x
fronthaul stack (eCPRI, M-Plane, S-Plane) and can connect to a real DU/CU
as a software RU.

With that integration, a DU/CU team can connect their pre-production
instance to the twin and validate configuration changes against
ML-calibrated, production-realistic session behaviour. This is buildable.

### Scenario 1 — Cluster-level traffic and capacity simulation
**Status: achievable with important caveats.**

srsRAN can generate traffic toward a DU at load. The ML model parameterises
session arrival rates per cell and session mix. Basic load testing, handover
testing, and admission control validation are achievable with this
combination.

The caveat is RF calibration — see the remaining gaps section below.

---

## What more production data enables

| Capability | Current state | With more data |
|---|---|---|
| Behavioural model per eNB | One site, 3 hours | Multi-site, generalizable |
| Anomaly detection coverage | 3 known anomaly types | Broader failure mode library |
| Handover failure recall | 100% on S1 HO failures | More failure types covered |
| Session diversity | Single traffic mix | Time-of-day, event-driven patterns |
| 5G NR support | LTE only (RRC/S1AP) | Needs NR traces (NR-RRC/NGAP) |
| Per-eNB fine-tuning | Demonstrated on one site | Scalable across estate |

More data from the same eNB over a longer period, or from multiple eNBs,
makes the model more general without changing the architecture. The pipeline
— reconstruct sessions, tokenise, train, fit arrival model — is the same
regardless of scale.

---

## Remaining gaps

### 1. RF calibration to specific sites
srsRAN implements generic channel models (AWGN, Rayleigh, EPA, ETU) that
are correct for LTE/5G in general but are not calibrated to any specific
site's geography, building layout, interference environment, or antenna
pattern.

The POC's Scenario 1 targets "cell-edge conditions, RSRP/RSRQ dynamics,
interference scenarios" — those values need to come from real measurements.
The production traces used here contain only control-plane signalling, not
RF measurements.

**What fills this gap:** MDT (Minimisation of Drive Tests) data, drive test
measurements, or crowd-sourced RF data (e.g. Opensignal) tied to the
specific cell locations. This was identified in the original POC as a data
source; it would need to be incorporated into the training and scenario
parameterisation pipeline.

Without it, the twin's RF layer is a plausible generic simulation, not a
replica of the specific site's radio environment.

### 2. Real-time dynamic reaction to DU scheduling
The POC states the twin "reacts dynamically to DU/CU control messages,
adjusting its simulated RF and traffic behaviour."

srsRAN handles the millisecond frame loop and does react to DU scheduler
decisions at that timescale. The ML model operates at session timescale
(seconds to minutes). The model can tell srsRAN "there should be 40 active
UEs on cell 71 with a mix of NAS-heavy and short sessions," but it cannot
dynamically adjust per-UE SINR in response to individual DU scheduler
grants at 1ms granularity.

These two layers complement each other but the interface between them —
how ML-level session decisions translate into frame-level srsRAN parameters
— requires engineering work that is not yet designed.

### 3. 5G NR
The current model is trained on LTE traces (RRC/S1AP). 5G uses different
protocols: NR-RRC, NGAP, F1AP. The architecture and pipeline transfer
directly; the trained model does not. A 5G twin requires 5G NR production
traces as training input.

LTE serves as a valid stepping stone — the approach is proven, and the
model migrates when 5G traces become available.

---

## Honest capability summary

| Layer | Achievable | What is needed |
|---|---|---|
| Control-plane behavioural model | Yes | More production data, more sites |
| Anomaly detection in production | Yes | More data, deploy the pipeline |
| Scenario 3 — issue reproduction | Yes | Largely complete |
| Scenario 2 — DU/CU validation | Yes | srsRAN/OAI integration |
| Scenario 1 — load and HO testing | Mostly | srsRAN + session parameterisation |
| Site-specific RF behaviour | Partial | MDT / drive test data also needed |
| Real-time ms-level RF reaction | Not yet | Fundamental timescale mismatch |
| Full 5G NR twin | Not yet | Requires 5G NR trace data |

The ML model + srsRAN/OAI combination reaches a **high-fidelity
control-plane and load-level twin** — covering Scenarios 2 and 3 fully,
and Scenario 1 for most practical validation purposes.

The part of the POC that describes real-time RF-level dynamic reaction to
DU scheduler decisions is out of reach without either a physics-based
channel model calibrated to specific sites or PHY-level measurement data
not currently available.

---

## Recommended next milestones

**Milestone 1 — Production pipeline (immediate)**
Apply the current model to a larger, multi-site production dataset.
Validate that anomaly detection generalises across sites and that
session diversity improves.

**Milestone 2 — srsRAN integration (next)**
Connect srsRAN/OAI as the RU emulator layer. Drive it with ML session
parameters. Demonstrate Scenario 2: a DU/CU team connects to the twin
and validates a configuration change.

**Milestone 3 — RF data integration**
Incorporate MDT or drive test data to calibrate srsRAN's channel model
to specific cell sites. This upgrades Scenario 1 from generic load
testing to site-specific capacity simulation.

**Milestone 4 — 5G NR**
Collect 5G NR traces (NR-RRC/NGAP), retrain the model on the new
protocol vocabulary, and migrate the twin to 5G.
