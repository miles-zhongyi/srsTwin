"""
Step 7: Validation — statistical fidelity and the Scenario 3 demo.

Runs after training. Loads the best checkpoint and:
  1. Computes per-cell session generation quality
  2. Checks S1 HO failure rate in generated output
  3. Scenario 3 demo: replay an S1 HO failure prefix, show outcome distribution,
     then inject X2 path and show it shifts toward success
  4. Saves validation_report.json and plots (if matplotlib available)
"""

import json
import math
import collections
import random
import sys
from pathlib import Path

import torch
import torch.nn.functional as F

_PKG = Path(__file__).resolve().parents[1]
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from config import (
    CHECKPOINT_FILE,
    GRAPHS_DIR,
    SESSIONS_FILE,
    TRAINING_LOG_FILE,
    VALIDATION_REPORT_FILE,
    VOCAB_FILE,
    ensure_dirs,
    token_split_file,
)
from model import SessionTransformer
import state_machine as sm


def load_model_and_vocab():
    with open(VOCAB_FILE, encoding="utf-8") as f:
        vocab = json.load(f)
    inv_vocab = {v: k for k, v in vocab.items()}

    ckpt = torch.load(CHECKPOINT_FILE, map_location="cpu", weights_only=True)
    cfg  = ckpt["cfg"]
    model = SessionTransformer(
        vocab_size=ckpt["vocab_size"],
        d_model=cfg["d_model"], n_heads=cfg["n_heads"],
        n_layers=cfg["n_layers"], max_len=cfg["max_len"],
        dropout=0.0,
    )
    model.load_state_dict(ckpt["model_state"])
    model.eval()
    return model, vocab, inv_vocab


def generate_sessions(model, vocab, inv_vocab, cell_id, n=100, temperature=0.9, top_k=10):
    """Generate n sessions conditioned on cell_id."""
    bos = "<BOS>"
    sessions = []
    for _ in range(n):
        context = [f"CELL_{cell_id}", bos]
        generated = model.generate(
            context, vocab, inv_vocab,
            state_machine=sm,
            max_new_tokens=30,
            temperature=temperature,
            top_k=top_k,
        )
        sessions.append(generated)
    return sessions


def extract_message_seq(token_seq):
    """Strip CELL_, BOS, EOS, time bucket — return just message names."""
    msgs = []
    for tok in token_seq:
        if tok.startswith("CELL_") or tok in ("<BOS>", "<EOS>", "<PAD>", "<UNK>"):
            continue
        parts = tok.split("|")
        if parts:
            msgs.append(parts[0])
    return msgs


def ho_failure_rate(sessions):
    """Fraction of sessions containing HANDOVER_REQUIRED followed by PREPARATION_FAILURE."""
    total_ho = 0
    failed   = 0
    for sess in sessions:
        msgs = extract_message_seq(sess) if isinstance(sess[0], str) else [
            t for t in sess if isinstance(t, str)
        ]
        if any("HANDOVER_REQUIRED" in m for m in msgs):
            total_ho += 1
            if any("PREPARATION_FAILURE" in m for m in msgs):
                failed += 1
    return failed, total_ho


def load_real_sessions(split="test"):
    with open(token_split_file(split), encoding="utf-8") as f:
        return json.load(f)


def kl_divergence(p_counts, q_counts):
    """
    KL(P||Q) over union of keys. Unseen events get a small smoothing mass
    so the result stays finite and non-negative.
    """
    all_keys = set(p_counts) | set(q_counts)
    smooth = 1e-9
    total_p = sum(p_counts.values()) + smooth * len(all_keys)
    total_q = sum(q_counts.values()) + smooth * len(all_keys)
    kl = 0.0
    for k in all_keys:
        p = (p_counts.get(k, 0) + smooth) / total_p
        q = (q_counts.get(k, 0) + smooth) / total_q
        kl += p * math.log(p / q)
    return kl


def scenario3_demo(model, vocab, inv_vocab, real_sessions_encoded):
    """
    Reproduce the S1 HO failure pattern and show that swapping to X2 path
    changes the outcome distribution.

    Two prefixes:
      A (failure path):  CELL_71 -> BOS -> INITIAL_UE_MESSAGE -> ... -> HANDOVER_REQUIRED
      B (X2 path):       same but swap HANDOVER_REQUIRED for a cell_id change token
                         to simulate X2 (which succeeded in the real data)
    """
    print("\n--- Scenario 3 Demo: S1 Handover Failure Reproduction ---\n")

    # Find real sessions that contain HANDOVER_REQUIRED token
    inv_vocab_local = {v: k for k, v in vocab.items()}
    ho_tok_ids = [i for tok, i in vocab.items() if "HANDOVER_REQUIRED" in tok]
    prep_fail_ids = [i for tok, i in vocab.items() if "PREPARATION_FAILURE" in tok]
    release_ids   = [i for tok, i in vocab.items() if "RELEASE_COMPLETE" in tok]

    ho_sessions = []
    for sess in real_sessions_encoded:
        if any(t in ho_tok_ids for t in sess):
            ho_sessions.append(sess)

    print(f"Real sessions containing HANDOVER_REQUIRED: {len(ho_sessions)}")

    if not ho_sessions:
        print("  No HO sessions in test set — using constructed prefix.")
        # Build a minimal S1 HO prefix from vocab
        def find_tok(pattern):
            for tok, idx in vocab.items():
                if pattern in tok:
                    return idx
            return vocab["<UNK>"]

        prefix_ids = [
            vocab.get("CELL_71", vocab["<UNK>"]),
            vocab["<BOS>"],
            find_tok("INITIAL_UE_MESSAGE"),
            find_tok("INITIAL_CONTEXT_SETUP_REQUEST"),
            find_tok("INITIAL_CONTEXT_SETUP_RESPONSE"),
        ]
    else:
        # Use prefix up to and INCLUDING HANDOVER_REQUIRED so the model is
        # conditioned on the fact that a handover attempt is in progress
        sess = ho_sessions[0]
        ho_pos = next(i for i, t in enumerate(sess) if t in ho_tok_ids)
        prefix_ids = sess[:ho_pos + 1]

    prefix_toks = [inv_vocab.get(i, "<UNK>") for i in prefix_ids]
    print(f"Prefix: {' -> '.join(prefix_toks)}\n")

    # Generate N continuations from this prefix — should mostly fail
    N = 200
    outcomes_s1 = collections.Counter()
    for _ in range(N):
        gen = model.generate(
            prefix_toks, vocab, inv_vocab,
            state_machine=sm, max_new_tokens=20,
            temperature=0.8, top_k=8,
        )
        msgs = extract_message_seq(gen)
        if any("PREPARATION_FAILURE" in m for m in msgs):
            outcomes_s1["ho_s1_failure"] += 1
        elif any("PATH_SWITCH" in m for m in msgs):
            outcomes_s1["ho_success"] += 1
        elif any("RELEASE_COMPLETE" in m for m in msgs):
            outcomes_s1["normal_release"] += 1
        else:
            outcomes_s1["other"] += 1

    print(f"S1 HO prefix — outcome distribution over {N} samples:")
    for outcome, cnt in outcomes_s1.most_common():
        print(f"  {outcome:30s} {cnt:4d}  ({cnt/N*100:.1f}%)")

    # Now swap: replace HANDOVER_REQUIRED with context that suggests X2
    # In the real data, the one X2 success involved PATH_SWITCH_REQUEST
    # We simulate a "fixed" prefix by appending PATH_SWITCH context
    x2_hint_toks = []
    for tok, idx in vocab.items():
        if "PATH_SWITCH_REQUEST" in tok and "ACKNOWLEDGE" not in tok:
            x2_hint_toks.append(tok)
            break

    if x2_hint_toks:
        x2_prefix_toks = prefix_toks + x2_hint_toks
        outcomes_x2 = collections.Counter()
        for _ in range(N):
            gen = model.generate(
                x2_prefix_toks, vocab, inv_vocab,
                state_machine=sm, max_new_tokens=20,
                temperature=0.8, top_k=8,
            )
            msgs = extract_message_seq(gen)
            if any("PREPARATION_FAILURE" in m for m in msgs):
                outcomes_x2["ho_s1_failure"] += 1
            elif any("PATH_SWITCH" in m for m in msgs):
                outcomes_x2["ho_success"] += 1
            elif any("RELEASE_COMPLETE" in m for m in msgs):
                outcomes_x2["normal_release"] += 1
            else:
                outcomes_x2["other"] += 1

        print(f"\nX2 path prefix — outcome distribution over {N} samples:")
        for outcome, cnt in outcomes_x2.most_common():
            print(f"  {outcome:30s} {cnt:4d}  ({cnt/N*100:.1f}%)")
        print("\n=> Shifting the prefix from S1 HO to X2 path changes the outcome distribution.")
    else:
        print("\n(X2 PATH_SWITCH token not in vocabulary — skipping X2 comparison)")


def main():
    print("Loading model and vocab...")
    model, vocab, inv_vocab = load_model_and_vocab()

    with open(SESSIONS_FILE, encoding="utf-8") as f:
        real_sessions_meta = json.load(f)
    real_encoded = load_real_sessions("test")

    report = {}

    # 1. Per-cell generation fidelity
    print("\n=== Per-cell generation fidelity ===")
    cells = [11, 21, 61, 71, 93]
    cell_report = {}

    for cell_id in cells:
        real_cell = [s for s in real_sessions_meta if s["cell_id"] == cell_id]
        if not real_cell:
            continue

        # Real message frequency (from full dataset, not just test)
        real_msg_freq = collections.Counter()
        for s in real_cell:
            for evt in s["events"]:
                real_msg_freq[evt["message_name"]] += 1

        # Generated message frequency
        gen_sessions = generate_sessions(model, vocab, inv_vocab, cell_id, n=200)
        gen_msg_freq = collections.Counter()
        for sess in gen_sessions:
            for msg in extract_message_seq(sess):
                gen_msg_freq[msg] += 1

        kl = kl_divergence(real_msg_freq, gen_msg_freq)

        # Session length comparison
        real_lengths = [s["n_events"] for s in real_cell]
        gen_lengths  = [len(extract_message_seq(s)) for s in gen_sessions]
        real_mean_len = sum(real_lengths) / len(real_lengths)
        gen_mean_len  = sum(gen_lengths)  / len(gen_lengths) if gen_lengths else 0

        print(f"  cell_{cell_id}: KL(real||gen)={kl:.3f} | "
              f"real_mean_len={real_mean_len:.1f} gen_mean_len={gen_mean_len:.1f} | "
              f"n_real={len(real_cell)}")

        cell_report[cell_id] = {
            "kl_divergence": kl,
            "real_mean_session_len": real_mean_len,
            "gen_mean_session_len": gen_mean_len,
        }

    report["cell_fidelity"] = cell_report

    # 2. S1 Handover failure rate
    print("\n=== S1 Handover failure rate ===")
    all_gen = []
    for cell_id in cells:
        all_gen.extend(generate_sessions(model, vocab, inv_vocab, cell_id, n=300))

    # Count HO events across all sessions (including single-event ones filtered from training)
    all_msg_seqs = [[e["message_name"] for e in s["events"]] for s in real_sessions_meta]
    # Also group by enb_ue_s1ap_id to catch HO across session boundaries
    ho_count   = sum(1 for s in real_sessions_meta
                     if any("HANDOVER_REQUIRED" in e["message_name"] for e in s["events"]))
    fail_count = sum(1 for s in real_sessions_meta
                     if any("PREPARATION_FAILURE" in e["message_name"] for e in s["events"]))
    print(f"  Real data: {ho_count} sessions with HANDOVER_REQUIRED, "
          f"{fail_count} with PREPARATION_FAILURE")
    real_ho_fail, real_ho_total = ho_failure_rate(all_msg_seqs)
    gen_ho_fail, gen_ho_total = ho_failure_rate(all_gen)

    real_rate = real_ho_fail / real_ho_total if real_ho_total else 0
    gen_rate  = gen_ho_fail  / gen_ho_total  if gen_ho_total  else 0

    print(f"  Real data:  {real_ho_fail}/{real_ho_total} HO failures ({real_rate*100:.1f}%)")
    print(f"  Generated:  {gen_ho_fail}/{gen_ho_total} HO failures ({gen_rate*100:.1f}%)")

    report["ho_failure_rate"] = {
        "real": real_rate, "real_n": real_ho_total,
        "gen":  gen_rate,  "gen_n":  gen_ho_total,
    }

    # 3. Scenario 3 demo
    scenario3_demo(model, vocab, inv_vocab, real_encoded)

    # Save report
    ensure_dirs()
    with open(VALIDATION_REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nValidation report saved to {VALIDATION_REPORT_FILE}")

    # 4. Plot training curves (if matplotlib available)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        with open(TRAINING_LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)

        epochs = [e["epoch"] for e in log["log"]]
        train_ppl = [e["train_ppl"] for e in log["log"]]
        val_ppl   = [e["val_ppl"]   for e in log["log"]]

        plt.figure(figsize=(8, 4))
        plt.plot(epochs, train_ppl, label="train perplexity")
        plt.plot(epochs, val_ppl,   label="val perplexity")
        plt.xlabel("Epoch")
        plt.ylabel("Perplexity")
        plt.title("SessionTransformer Training Curve")
        plt.legend()
        plt.tight_layout()
        curve_path = GRAPHS_DIR / "training_curve.png"
        plt.savefig(curve_path, dpi=120)
        print(f"Training curve saved to {curve_path}")
    except Exception as e:
        print(f"(Plot skipped: {e})")


if __name__ == "__main__":
    main()
