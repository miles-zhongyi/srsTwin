# Training Guide

How to train a new model from scratch, update the existing model with new
data, or fine-tune for a specific eNB.

---

## Pipeline overview

Every training run goes through the same four steps in order:

```
New trace files (.json)
        │
        ▼
reconstruct_sessions.py   →  sessions.json
        │
        ▼
tokenize_sessions.py      →  vocab.json + train/val/test_tokens.json
        │
        ▼
train.py                  →  checkpoints/best_model.pt
        │
        ▼
arrival_model.py          →  arrival_model.json
```

---

## Scenario A — Full retrain from new data

Use this when you have trace files from a new eNB, a new time period, or
a significantly larger dataset.

**Step 1: Put your new decoded JSON trace files in a directory.**

The files must be in the same format as the existing ones — each file a
JSON array of event objects with at least these fields:

```json
{
  "interface":        "S1",
  "message_name":     "S1_INITIAL_UE_MESSAGE",
  "timestamp":        "2026-01-22T00:00:17.305000+00:00",
  "cell_id":          71,
  "direction":        2,
  "enb_ue_s1ap_id":   878,
  "mme_ue_s1ap_id":   69208340
}
```

**Step 2: Point the pipeline at your trace files.**

Paths are resolved by `config.py` (no per-script edits needed):

```powershell
# Optional — decoded trace JSON tree (defaults to srsTwin/22_decoded)
$env:DIGITAL_TWIN_TRACE_DIR = "d:\DodonaData\DigitalTwins\poc_StressTest\22_decoded"

# Optional — model artifacts directory (defaults to srsTwin/digital-twin)
$env:DIGITAL_TWIN_DATA_DIR = "d:\DodonaData\DigitalTwins\srsTwin\digital-twin"
```

Or copy new trace files into `srsTwin/digital-twin/merged/` (gitignored).

**Step 3: Run the pipeline:**

```powershell
cd d:\DodonaData\DigitalTwins\srsTwin\digital-twin
.\.venv\Scripts\Activate.ps1

python reconstruct_sessions.py   # when available / notebook export
python tokenize_sessions.py
python train.py                  # or train_model_local.ipynb
python arrival_model.py
```

Each step must complete before the next. Total time on your GPU for the
existing dataset size (~18k sessions) is under 2 minutes.

**Step 4: Validate:**

```bash
.venv/bin/python validate.py
.venv/bin/python anomaly_detection.py
```

Check that:
- Val perplexity decreases and stabilises (see `training_curve.png`)
- HO failure recall stays at 100%
- Anomaly score distribution makes sense for the new data

---

## Scenario B — Incremental update (adding new trace files)

Use this when you have new trace files to add to an existing trained model.
No need to start from scratch — just merge and retrain.

```powershell
Copy-Item path\to\new\*.json merged\
python reconstruct_sessions.py   # rebuilds sessions.json
python tokenize_sessions.py      # rebuilds vocab + splits
python train.py                  # retrains from scratch on merged data
python arrival_model.py          # refit arrival rates
```

The vocabulary may grow if new message types appear in the new data, which
is why a full retrain (rather than fine-tuning the checkpoint) is the
safest approach here.

---

## Scenario C — Fine-tuning for a specific eNB

Use this when you want a model that closely reflects one particular eNB's
behaviour, starting from a base model trained on multiple sites.

**Step 1: Train a base model** on data from all available eNBs (Scenario A
or B with the combined dataset).

**Step 2: Prepare eNB-specific data.**

Filter `sessions.json` to only sessions from the target eNB:

```python
import json

with open("sessions.json") as f:
    sessions = json.load(f)

# All sessions in this dataset are from enb_id 499557
# For multi-eNB data, filter here:
target_enb = "499557"
enb_sessions = [s for s in sessions if s.get("enb_id") == target_enb]

with open("sessions_enb499557.json", "w") as f:
    json.dump(enb_sessions, f)
```

**Step 3: Fine-tune.** Edit `train.py` to:
1. Load the base model checkpoint at the start of training instead of
   initialising from scratch:

```python
# In train.py, after creating the model, add:
base_ckpt = torch.load("checkpoints/best_model.pt", map_location=device,
                       weights_only=True)
model.load_state_dict(base_ckpt["model_state"])
print("Loaded base model for fine-tuning")
```

2. Use a lower learning rate (the model is already near convergence):

```python
CFG = {
    ...
    "lr":     3e-5,    # was 3e-4 — 10x lower for fine-tuning
    "epochs": 10,      # fewer epochs needed
    ...
}
```

3. Use only the eNB-specific sessions for tokenisation (`tokenize_sessions.py`
   pointing at `sessions_enb499557.json`).

**Step 4:** Run `tokenize_sessions.py` and `train.py` with the eNB-specific
data and the modified settings above.

---

## Tuning training hyperparameters

The defaults in `train.py` work well for the current dataset size. If you
significantly change the data volume or are seeing problems, adjust these:

| Parameter | Default | When to change |
|---|---|---|
| `epochs` | 30 | Increase if val loss is still decreasing at epoch 30 |
| `lr` | 3e-4 | Lower if val loss is unstable or diverging |
| `batch_size` | 64 | Lower if GPU runs out of memory |
| `d_model` | 128 | Increase to 256 for >50k sessions |
| `n_layers` | 4 | Increase to 6 for larger datasets |
| `dropout` | 0.1 | Increase to 0.2 if training perplexity << val perplexity |

**Signs of overfitting** (training perplexity much lower than val):
- Increase `dropout`
- Reduce `epochs`
- Add more training data

**Signs of underfitting** (val perplexity not decreasing after epoch 10):
- Increase `d_model` or `n_layers`
- Increase `epochs`
- Lower `lr`

---

## Reading the training output

```
Epoch  17/30 | train_loss=0.352  ppl=1.42 | val_loss=0.303  ppl=1.35 | lr=1.4e-04 | 0.9s
  -> saved best model (val_loss=0.303)
```

- **train_loss / val_loss**: cross-entropy. Lower is better. Gap between
  train and val indicates overfitting if large.
- **ppl (perplexity)**: exp(loss). A perplexity of 1.35 means the model
  is, on average, fairly confident about the next token. For protocol
  sequences with a small vocabulary (~74 tokens) this is good.
- **`-> saved best model`**: checkpoint saved only when val_loss improves.
  The final checkpoint at `checkpoints/best_model.pt` is always the best
  val loss seen, not the last epoch.

After training, check `training_curve.png` to visually confirm:
- Both curves decrease
- Val curve does not start increasing while train continues decreasing
  (that is overfitting)

---

## What changes in the model when trained on new data

The model learns two things:

1. **Transition probabilities** — which message is likely to follow which.
   Adding data from a site where S1 HOs succeed will lower the perplexity
   assigned to successful HO sequences.

2. **Timing patterns** — the time delta bucket distribution per message pair.
   A site with faster MME response times will produce different timing tokens.

The arrival model (`arrival_model.json`) learns separately and only captures
session volume and burstiness — it does not affect message sequence generation.

---

## File inventory after training

```
22_decoded/
├── merged/                   raw trace JSON files (input)
├── sessions.json             reconstructed sessions
├── vocab.json                token → index mapping
├── train_tokens.json         encoded training sequences
├── val_tokens.json           encoded validation sequences
├── test_tokens.json          encoded test sequences
├── token_samples.txt         human-readable sample sessions
├── arrival_model.json        Hawkes parameters per cell
├── training_log.json         per-epoch loss and perplexity
├── training_curve.png        training/val perplexity plot
├── checkpoints/
│   ├── best_model.pt         best val-loss checkpoint  ← used by all scripts
│   └── final_model.pt        last-epoch checkpoint
└── validation_report.json    fidelity metrics from validate.py
```

All downstream scripts (`anomaly_detection.py`, `generate_logs.py`,
`compare_ho_paths.py`, `validate.py`) load from `checkpoints/best_model.pt`.
Replacing that file with a retrained checkpoint is all that's needed to
update the whole system.
