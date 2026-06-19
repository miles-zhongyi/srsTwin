# Use Cases: Anomaly Detection & Synthetic Log Generation

This document covers two practical uses of the trained SessionTransformer:

1. **Anomaly detection** — score real sessions to find unusual behaviour
2. **Synthetic log generation** — produce realistic S1AP trace files

Both use the model trained in `train.py`. Run that first if you haven't already.

---

## What the model actually does (quick recap)

The model learned the statistical patterns of S1AP signaling sessions from
real LTE traces. Given a sequence of protocol messages, it assigns a
probability to each one — how likely is this next message given everything
before it?

- **Low probability** assigned to a message = the model finds it surprising
- **High probability** = normal, expected behaviour

This is what makes both use cases possible.

---

## Use Case 2 — Anomaly Detection

### What it does

Every session gets a **perplexity score**: the geometric mean of how
"surprised" the model was at each message in the session.

```
perplexity ≈ 1.0   → completely expected sequence
perplexity = 5.0   → moderately unusual
perplexity = 50+   → highly anomalous (the model has rarely or never
                      seen this sequence pattern in training)
```

In this dataset, the known anomalies are:
- 76 S1 handover failures (`S1_HO_ATTEMPT`) — perplexity 8–157
- 8 ERAB release procedures (`ERAB_RELEASE`) — perplexity ~8.5
- 1 X2 handover (`X2_HO`) — perplexity ~19

Normal sessions cluster below perplexity 3.0. The default threshold is
the 95th percentile of all scores.

### Running it

**Score all sessions in the dataset:**
```bash
.venv/bin/python anomaly_detection.py
```

**Score sessions from a new trace file:**
```bash
# First reconstruct sessions from the new data
.venv/bin/python reconstruct_sessions.py   # point DATA_DIR at new files first

# Then score
.venv/bin/python anomaly_detection.py --input /path/to/new/sessions.json
```

**Use a fixed threshold instead of percentile:**
```bash
.venv/bin/python anomaly_detection.py --threshold 5.0
```

**Show more anomalous sessions:**
```bash
.venv/bin/python anomaly_detection.py --top-n 50
```

### Output files

| File | Contents |
|---|---|
| `anomaly_scores.json` | Perplexity score and label for every session |
| `anomaly_scores.png` | Histogram of scores by session type + scatter plot |

### Interpreting results

The 95th percentile threshold gives you a starting point — it is not
a fixed truth. Adjust based on your operational tolerance:

- **Lower threshold** (e.g. 90th percentile) → catches more anomalies,
  more false positives
- **Higher threshold** (e.g. 99th percentile) → fewer alerts, higher
  confidence that flagged sessions are genuinely unusual

Sessions flagged with perplexity above 50 are almost certainly anomalous —
in this dataset that is exclusively S1 HO failures. Sessions between 5–50
may be rare-but-valid procedures or genuine misconfiguration issues.

### What to look for when investigating a flagged session

The output prints the full message sequence for each flagged session.
Ask:
- Does the sequence start a procedure (e.g. `HANDOVER_REQUIRED`) that
  never completes normally?
- Are messages appearing in an unexpected order?
- Are messages associated with the wrong cell than you'd expect?
- Is the session unusually long or short compared to similar sessions?

### Applying to new production data

To use the anomaly detector on live data:

1. Export S1AP traces from the eNB/probe in the same JSON format
2. Run `reconstruct_sessions.py` with `DATA_DIR` updated to point at the
   new files
3. Run `anomaly_detection.py --input new_sessions.json`
4. Sessions above the threshold are candidates for investigation

The model was trained on a single 3-hour window from one eNB. For
production use across multiple eNBs or time periods, retrain on a broader
dataset first (see `TRAINING_GUIDE.md`).

---

## Use Case 3 — Synthetic Log Generation

### What it does

Generates a realistic stream of synthetic S1AP session traces by combining:

1. **Hawkes arrival model** (from `arrival_model.py`) — decides *when*
   sessions arrive at each cell, matching the burstiness of real traffic
2. **SessionTransformer** — decides *what messages* each session contains,
   conditioned on the cell

The output is a JSON file in the same format as the original trace files,
with a `"synthetic": true` flag on each event so downstream tools can
filter them.

### Running it

**Generate 5 minutes of synthetic traffic across all cells:**
```bash
.venv/bin/python generate_logs.py --duration 300 --output synthetic_traces.json
```

**Generate 1 hour:**
```bash
.venv/bin/python generate_logs.py --duration 3600 --output hour_simulation.json
```

**Generate traffic for a single cell only:**
```bash
.venv/bin/python generate_logs.py --duration 300 --cell 71 --output cell71.json
```

**Increase diversity of generated sessions** (higher temperature = more
variation, lower = more conservative/repetitive):
```bash
.venv/bin/python generate_logs.py --duration 300 --temperature 1.1 --output diverse.json
```

**Fix the random seed for reproducible output:**
```bash
.venv/bin/python generate_logs.py --duration 300 --seed 1234 --output replay.json
```

### Output format

Each event in the output JSON looks like this:

```json
{
  "interface":        "S1",
  "message_name":     "S1_INITIAL_CONTEXT_SETUP_REQUEST",
  "timestamp":        "2026-01-22T04:00:00.043+00:00",
  "serving_plmn":     "302 221",
  "enb_id":           "499557",
  "cell_id":          71,
  "direction":        2,
  "m_tmsi":           1048575,
  "enb_ue_s1ap_id":   1001,
  "mme_ue_s1ap_id":   51001,
  "protocol":         "S1AP",
  "synthetic":        true
}
```

The `synthetic: true` field lets any consumer distinguish generated events
from real ones.

### What the generated data is realistic for

**Good for:**
- Testing OSS/BSS tools that parse S1AP logs — the message names, cell IDs,
  timing patterns, and session structure all match real data statistics
- Load testing tools that consume S1AP event streams — arrival rates and
  session volumes match real traffic
- Generating training data for downstream classifiers (e.g. alarm
  correlation tools)
- Demonstrating the network's typical session mix to stakeholders

**Not realistic for:**
- Anything that inspects the decoded message payload (the `decoded` field
  from the original traces) — those are not generated
- Testing tools that validate IEs inside S1AP messages — the model only
  generates message names, not message content
- Radio-layer simulation — no RSRP, MCS, CQI, or PHY content is generated

### Verifying the output

After generating, spot-check a few things:

1. **Session volume matches expectation**: for a 60-second window, cell 71
   should have roughly 40–50 sessions (matching its 0.72/s real rate)
2. **Message mix looks right**: dominant messages should be
   `UE_CONTEXT_RELEASE_COMMAND`, `UPLINK/DOWNLINK_NAS_TRANSPORT`,
   `INITIAL_CONTEXT_SETUP_REQUEST/RESPONSE`
3. **Timestamps are monotonically increasing** within each session
4. **No impossible sequences**: the state machine constraint prevents
   protocol-invalid orderings

```bash
# Quick check: count events per message type in generated output
python3 -c "
import json
with open('synthetic_traces.json') as f:
    data = json.load(f)
from collections import Counter
c = Counter(e['message_name'] for e in data['events'])
for msg, cnt in c.most_common(10):
    print(f'{cnt:5d}  {msg}')
"
```

---

## Notes on limitations

Both use cases inherit the constraints of the training data:

- **Single eNB, single day**: the model learned one site's behaviour. Anomaly
  scores and generated sessions reflect that specific site's patterns.
- **Control plane only**: no user-plane or RF content.
- **Direction field**: all events show `direction=2` (a trace export quirk
  in this dataset). With richer production data this would carry real
  information.
- **Rare events are underrepresented**: with only 76 HO failures in 18k
  sessions, the model will rarely generate HO events spontaneously in the
  synthetic output. The anomaly detector handles them well because they
  have high perplexity, but the generator needs a seeded prefix to produce
  them on demand.

For production use, retrain on a larger, multi-site dataset. See
`TRAINING_GUIDE.md` for how to do that.
