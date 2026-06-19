"""
Anomaly detection using the trained SessionTransformer.

For each session, the model computes a perplexity score — how "surprising"
that sequence of messages is given everything it learned from training data.

  Low perplexity  = normal, expected sequence (model has seen this before)
  High perplexity = unusual sequence (model finds it surprising = flag it)

The test set contains 76 known S1 HO failure sessions. We use these as
ground truth to evaluate whether the detector can identify anomalies.

Usage:
  .venv/bin/python anomaly_detection.py [--input path/to/sessions.json]
  .venv/bin/python anomaly_detection.py --input my_new_traces/sessions.json
"""

import json
import sys
import argparse
import collections
import math
from pathlib import Path

import torch
import torch.nn.functional as F

_PKG = Path(__file__).resolve().parents[1]
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from config import (
    ANOMALY_SCORES_FILE,
    CHECKPOINT_FILE,
    GRAPHS_DIR,
    SESSIONS_FILE,
    VOCAB_FILE,
    ensure_dirs,
)
from model import SessionTransformer
from tokenize_sessions import session_to_tokens, dt_bucket

THRESHOLD_PERCENTILE = 95   # flag sessions above this perplexity percentile


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_session(model, token_ids, pad_idx, device):
    """
    Compute perplexity for a single session token sequence.
    Perplexity = exp( average negative log-probability of each actual next token ).
    Returns float. Higher = more anomalous.
    """
    if len(token_ids) < 2:
        return float("nan")

    x = torch.tensor(token_ids[:-1], dtype=torch.long, device=device).unsqueeze(0)
    y = torch.tensor(token_ids[1:],  dtype=torch.long, device=device)

    with torch.no_grad():
        logits = model(x).squeeze(0)           # (T, vocab)
        losses = F.cross_entropy(
            logits, y,
            ignore_index=pad_idx,
            reduction="none",
        )
        valid  = (y != pad_idx)
        if valid.sum() == 0:
            return float("nan")
        avg_loss = losses[valid].mean().item()

    return math.exp(avg_loss)


def tokenize_and_encode(session, vocab):
    """Tokenize a session dict and encode to integer IDs."""
    toks = session_to_tokens(session)
    unk  = vocab.get("<UNK>", 0)
    return [vocab.get(t, unk) for t in toks], toks


# ---------------------------------------------------------------------------
# Known anomaly labels from session metadata
# ---------------------------------------------------------------------------

def ground_truth_label(session):
    msgs = [e["message_name"] for e in session["events"]]
    if any("HANDOVER_REQUIRED" in m for m in msgs):
        return "S1_HO_ATTEMPT"
    if any("PREPARATION_FAILURE" in m for m in msgs):
        return "S1_HO_FAILURE"
    if any("PATH_SWITCH" in m for m in msgs):
        return "X2_HO"
    if any("ERAB_RELEASE" in m for m in msgs):
        return "ERAB_RELEASE"
    return "NORMAL"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(SESSIONS_FILE),
                        help="Path to sessions.json to score")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Fixed perplexity threshold (overrides percentile)")
    parser.add_argument("--top-n", type=int, default=20,
                        help="Show this many highest-perplexity sessions")
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load model
    with open(VOCAB_FILE, encoding="utf-8") as f:
        vocab = json.load(f)
    pad_idx = vocab["<PAD>"]

    ckpt = torch.load(CHECKPOINT_FILE, map_location=device, weights_only=True)
    cfg  = ckpt["cfg"]
    model = SessionTransformer(
        vocab_size=ckpt["vocab_size"],
        d_model=cfg["d_model"], n_heads=cfg["n_heads"],
        n_layers=cfg["n_layers"], max_len=cfg["max_len"], dropout=0.0,
    ).to(device)
    model.load_state_dict(ckpt["model_state"])
    model.eval()

    # Load sessions
    with open(args.input, encoding="utf-8") as f:
        sessions = json.load(f)

    # Score only sessions with >=2 events (single-event sessions are unscoreable).
    # "unknown"/"partial_release" sessions are file-boundary artifacts that were
    # excluded from training (see tokenize_sessions.py) — the model never learned
    # these as valid endings, so they score as extreme outliers that drown out
    # real anomalies like ho_failure. Exclude them here too.
    scoreable = [s for s in sessions if s["n_events"] >= 2
                 and s["outcome"] not in ("unknown", "partial_release")]
    print(f"Scoring {len(scoreable)} sessions from {args.input} ...")

    results = []
    for sess in scoreable:
        ids, toks = tokenize_and_encode(sess, vocab)
        ppl       = score_session(model, ids, pad_idx, device)
        label     = ground_truth_label(sess)
        results.append({
            "session_id": sess["session_id"],
            "cell_id":    sess["cell_id"],
            "n_events":   sess["n_events"],
            "outcome":    sess["outcome"],
            "label":      label,
            "perplexity": ppl,
            "tokens":     toks,
        })

    # Sort by perplexity descending
    results.sort(key=lambda r: r["perplexity"] if not math.isnan(r["perplexity"]) else 0,
                 reverse=True)

    # Determine threshold
    ppls = [r["perplexity"] for r in results if not math.isnan(r["perplexity"])]
    ppls_sorted = sorted(ppls)
    if args.threshold:
        threshold = args.threshold
    else:
        idx = int(len(ppls_sorted) * THRESHOLD_PERCENTILE / 100)
        threshold = ppls_sorted[min(idx, len(ppls_sorted) - 1)]

    flagged = [r for r in results if r["perplexity"] >= threshold]

    # ---------------------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------------------
    print(f"\n{'='*68}")
    print(f"  ANOMALY DETECTION REPORT")
    print(f"{'='*68}")
    print(f"\n  Sessions scored:          {len(results)}")
    print(f"  Perplexity threshold:     {threshold:.3f}  "
          f"({THRESHOLD_PERCENTILE}th percentile)" if not args.threshold else
          f"  Perplexity threshold:     {threshold:.3f}  (user-specified)")
    print(f"  Sessions flagged:         {len(flagged)}")

    # Perplexity distribution
    print(f"\n  Perplexity distribution:")
    print(f"    min:    {min(ppls):.3f}")
    print(f"    p25:    {ppls_sorted[len(ppls_sorted)//4]:.3f}")
    print(f"    median: {ppls_sorted[len(ppls_sorted)//2]:.3f}")
    print(f"    p95:    {ppls_sorted[int(len(ppls_sorted)*0.95)]:.3f}")
    print(f"    max:    {max(ppls):.3f}")

    # How many known anomalies were caught
    known_anomalies = [r for r in results if r["label"] in
                       ("S1_HO_ATTEMPT", "S1_HO_FAILURE", "X2_HO", "ERAB_RELEASE")]
    caught = [r for r in flagged if r["label"] in
              ("S1_HO_ATTEMPT", "S1_HO_FAILURE", "X2_HO", "ERAB_RELEASE")]

    print(f"\n  Ground truth — known anomalous session types in dataset:")
    label_counts = collections.Counter(r["label"] for r in results)
    for label, cnt in label_counts.most_common():
        marker = " *" if label != "NORMAL" else ""
        print(f"    {label:25s} {cnt:5d}{marker}")

    if known_anomalies:
        recall = len(caught) / len(known_anomalies) * 100
        print(f"\n  Recall on known anomalies: "
              f"{len(caught)}/{len(known_anomalies)} = {recall:.1f}%")

    # Top-N most anomalous sessions
    print(f"\n{'='*68}")
    print(f"  TOP {args.top_n} MOST ANOMALOUS SESSIONS")
    print(f"{'='*68}")
    for r in results[:args.top_n]:
        flag = "*** FLAGGED" if r["perplexity"] >= threshold else ""
        print(f"\n  Session {r['session_id']:>6}  "
              f"cell={r['cell_id']}  ppl={r['perplexity']:6.3f}  "
              f"label={r['label']:20s}  {flag}")
        print(f"  Outcome: {r['outcome']}")
        # Show the token sequence (skip CELL_ and BOS/EOS)
        msg_toks = [t for t in r["tokens"]
                    if not t.startswith("CELL_") and t not in ("<BOS>","<EOS>","<PAD>")]
        print(f"  Sequence: {' -> '.join(msg_toks)}")

    # Save results
    ensure_dirs()
    with open(ANOMALY_SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "threshold": threshold,
            "n_flagged": len(flagged),
            "scores": [{"session_id": r["session_id"], "cell_id": r["cell_id"],
                        "perplexity": r["perplexity"], "label": r["label"],
                        "outcome": r["outcome"]}
                       for r in results]
        }, f, indent=2)
    print(f"\n  Full scores saved to {ANOMALY_SCORES_FILE}")

    # Plot
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        labels_list = ["NORMAL", "S1_HO_ATTEMPT", "S1_HO_FAILURE", "X2_HO", "ERAB_RELEASE"]
        colors_map  = {
            "NORMAL":       "#aec7e8",
            "S1_HO_ATTEMPT":"#d62728",
            "S1_HO_FAILURE":"#ff7f0e",
            "X2_HO":        "#2ca02c",
            "ERAB_RELEASE": "#9467bd",
        }

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
        fig.suptitle("Session Anomaly Detection — Perplexity Scores\n"
                     "SessionTransformer trained on Telus LTE traces (eNB 499557)")

        # Left: perplexity histogram coloured by label
        for lbl in labels_list:
            lbl_ppls = [r["perplexity"] for r in results
                        if r["label"] == lbl and not math.isnan(r["perplexity"])]
            if lbl_ppls:
                ax1.hist(lbl_ppls, bins=40, alpha=0.65,
                         color=colors_map.get(lbl, "gray"), label=lbl)
        ax1.axvline(threshold, color="black", linestyle="--", linewidth=1.5,
                    label=f"Threshold ({threshold:.2f})")
        ax1.set_xlabel("Perplexity")
        ax1.set_ylabel("Number of sessions")
        ax1.set_title("Perplexity distribution by session type")
        ax1.legend(fontsize=8)

        # Right: scatter plot — perplexity vs session length, coloured by label
        for lbl in labels_list:
            lbl_res = [r for r in results
                       if r["label"] == lbl and not math.isnan(r["perplexity"])]
            if lbl_res:
                ax2.scatter(
                    [r["n_events"] for r in lbl_res],
                    [r["perplexity"] for r in lbl_res],
                    alpha=0.5, s=15,
                    color=colors_map.get(lbl, "gray"), label=lbl,
                )
        ax2.axhline(threshold, color="black", linestyle="--", linewidth=1.5,
                    label=f"Threshold ({threshold:.2f})")
        ax2.set_xlabel("Session length (events)")
        ax2.set_ylabel("Perplexity")
        ax2.set_title("Perplexity vs session length")
        ax2.legend(fontsize=8)

        plt.tight_layout()
        out_png = GRAPHS_DIR / "anomaly_scores.png"
        plt.savefig(out_png, dpi=130, bbox_inches="tight")
        print(f"  Plot saved to {out_png}")
    except Exception as e:
        print(f"  (Plot skipped: {e})")


if __name__ == "__main__":
    main()
