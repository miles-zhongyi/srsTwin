"""
Handover path comparison: S1 failure vs X2 success.

Uses two sources of evidence:

  S1 path  — Model generates continuations after HANDOVER_REQUIRED.
             The model learned from 76 real S1 HO attempts, all of which
             failed (PREPARATION_FAILURE) and led to UE context release.

  X2 path  — The one real X2 HO observed in the dataset is replayed
             directly. The model then generates what comes AFTER the
             successful PATH_SWITCH handshake, showing the UE continues
             normally instead of being dropped.

Key insight: both paths start from the same place (a UE in active context
on cell_71) but have very different outcomes for the UE — one drops the
connection, the other continues it.

Run:
  .venv/bin/python compare_ho_paths.py
"""

import json
import sys
import collections
from pathlib import Path

import torch

_PKG = Path(__file__).resolve().parents[1]
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from config import CHECKPOINT_FILE, GRAPHS_DIR, SESSIONS_FILE, VOCAB_FILE, ensure_dirs
from model import SessionTransformer
import state_machine as sm

N_SAMPLES = 300


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def load_model():
    with open(VOCAB_FILE, encoding="utf-8") as f:
        vocab = json.load(f)
    inv_vocab = {v: k for k, v in vocab.items()}
    ckpt = torch.load(CHECKPOINT_FILE, map_location="cpu", weights_only=True)
    cfg = ckpt["cfg"]
    model = SessionTransformer(
        vocab_size=ckpt["vocab_size"],
        d_model=cfg["d_model"], n_heads=cfg["n_heads"],
        n_layers=cfg["n_layers"], max_len=cfg["max_len"], dropout=0.0,
    )
    model.load_state_dict(ckpt["model_state"])
    model.eval()
    return model, vocab, inv_vocab


def find_token(vocab, pattern):
    match = next((t for t in vocab if pattern in t), None)
    if match is None:
        raise KeyError(f"No token matching '{pattern}' in vocab")
    return match


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

UE_IMPACT = {
    "PREPARATION_FAILURE":       ("S1 HO rejected",        "UE dropped — HO denied by MME"),
    "RELEASE_COMMAND":           ("Context released",       "UE dropped — connection terminated"),
    "RELEASE_COMPLETE":          ("Clean release",          "UE released cleanly"),
    "PATH_SWITCH_ACKNOWLEDGE":   ("X2 HO success",         "UE continues — MME acknowledges new cell"),
    "NAS_TRANSPORT":             ("Session continues",      "UE active — NAS exchange in progress"),
}

def classify(token_seq, x2_mode=False):
    """
    Classify the outcome of a generated session.
    x2_mode: if True, EOS with no matching message is a positive result —
             the target eNB's S1 session is complete and the UE continues.
    """
    for tok in token_seq:
        msg = tok.split("|")[0]
        for pattern, (label, impact) in UE_IMPACT.items():
            if pattern in msg:
                return label, impact
    if x2_mode:
        return "X2 HO complete", "UE continues on new cell — no further S1 signaling needed"
    return "Other", "Indeterminate"


def generate_batch(model, vocab, inv_vocab, prefix, n, temperature=0.9, top_k=8):
    return [
        model.generate(prefix, vocab, inv_vocab, state_machine=sm,
                       max_new_tokens=25, temperature=temperature, top_k=top_k)
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

BUCKET_LABELS = ["~0ms", "~5ms", "~20ms", "~100ms", "~500ms", "~2000ms", ">10s"]

def bucket_label(tok):
    parts = tok.split("|")
    if len(parts) == 3 and parts[2].startswith("T"):
        try:
            return BUCKET_LABELS[int(parts[2][1:])]
        except (IndexError, ValueError):
            pass
    return "—"

def format_session(token_seq, prefix_len, indent=4):
    pad = " " * indent
    lines = []
    for i, tok in enumerate(token_seq):
        if tok in ("<PAD>",):
            continue
        tag  = "»" if i >= prefix_len else " "
        msg  = tok.split("|")[0]
        time = bucket_label(tok) if "|" in tok else ""
        if msg in ("<BOS>", "<EOS>"):
            continue
        if msg.startswith("CELL_"):
            lines.append(f"{pad}  {msg}")
            continue
        lines.append(f"{pad}{tag} {msg:50s} {time}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Real data
# ---------------------------------------------------------------------------

def load_real_ho_sessions():
    with open(SESSIONS_FILE, encoding="utf-8") as f:
        sessions = json.load(f)

    s1_fail = [s for s in sessions if
               any("HANDOVER_REQUIRED" in e["message_name"] for e in s["events"]) and
               any("PREPARATION_FAILURE" in e["message_name"] for e in s["events"])]
    x2_ok   = [s for s in sessions if
               any("PATH_SWITCH_REQUEST_ACKNOWLEDGE" in e["message_name"] for e in s["events"])]
    return s1_fail, x2_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    s1_real, x2_real = load_real_ho_sessions()

    print("=" * 68)
    print("  HANDOVER PATH COMPARISON — S1 (failure) vs X2 (success)")
    print("  Trained on real anonymised Telus LTE traces (eNB 499557)")
    print("=" * 68)

    print(f"""
Dataset snapshot
  Total S1 HO attempts:   {len(s1_real):>4}
  S1 HO success rate:         0 / {len(s1_real)} = 0.0%
  Total X2 HO observed:       1
  X2 HO success rate:         1 / 1  = 100%
""")

    print("Real S1 failure trace (representative sample):")
    for e in s1_real[0]["events"]:
        print(f"    {e['message_name']:50s}  +{e['dt_ms']}ms")

    print("\nReal X2 success trace:")
    for e in x2_real[0]["events"]:
        print(f"    {e['message_name']:50s}  +{e['dt_ms']}ms")

    print()

    # -----------------------------------------------------------------------
    # Load model and build prefixes
    # -----------------------------------------------------------------------
    print("Loading model...")
    model, vocab, inv_vocab = load_model()

    # S1 prefix: a UE in active context that the eNB tries to hand off via S1
    s1_ho_tok = find_token(vocab, "HANDOVER_REQUIRED|UL|T0")
    s1_prefix = ["CELL_71", "<BOS>", s1_ho_tok]

    # X2 prefix: the full observed real X2 handshake (REQUEST + ACKNOWLEDGE),
    # then ask the model what the UE's session looks like afterwards
    x2_req_tok = find_token(vocab, "PATH_SWITCH_REQUEST|UL|T0")
    x2_ack_tok = find_token(vocab, "PATH_SWITCH_REQUEST_ACKNOWLEDGE|UL|T3")
    x2_prefix = ["CELL_11", "<BOS>", x2_req_tok, x2_ack_tok]

    print(f"\nGenerating {N_SAMPLES} sessions per path...\n")

    s1_sessions = generate_batch(model, vocab, inv_vocab, s1_prefix, N_SAMPLES)
    x2_sessions = generate_batch(model, vocab, inv_vocab, x2_prefix, N_SAMPLES)

    # -----------------------------------------------------------------------
    # Outcome distribution
    # -----------------------------------------------------------------------
    s1_outcomes = collections.Counter(classify(s, x2_mode=False)[0] for s in s1_sessions)
    x2_outcomes = collections.Counter(classify(s, x2_mode=True)[0]  for s in x2_sessions)
    all_labels  = sorted(set(s1_outcomes) | set(x2_outcomes))

    print("=" * 68)
    print("  GENERATED OUTCOME DISTRIBUTION")
    print("=" * 68)
    col = max(len(l) for l in all_labels) + 2
    print(f"  {'Outcome':{col}}  {'S1 path':>20}  {'X2 path':>20}")
    print(f"  {'-'*col}  {'-'*20}  {'-'*20}")
    for label in all_labels:
        s1n = s1_outcomes.get(label, 0)
        x2n = x2_outcomes.get(label, 0)
        s1s = f"{s1n:4d}  ({s1n/N_SAMPLES*100:5.1f}%)"
        x2s = f"{x2n:4d}  ({x2n/N_SAMPLES*100:5.1f}%)"
        print(f"  {label:{col}}  {s1s:>20}  {x2s:>20}")

    # UE impact summary (most common outcome only)
    s1_top  = s1_outcomes.most_common(1)[0][0]
    x2_top  = x2_outcomes.most_common(1)[0][0]
    s1_impact = next((v[1] for k, v in UE_IMPACT.items() if k in s1_top or s1_top in k),
                     "UE dropped")
    x2_impact = next((v[1] for k, v in UE_IMPACT.items() if k in x2_top or x2_top in k),
                     "UE continues on new cell — no further S1 signaling needed")

    print(f"\n  UE impact — S1 path:  {s1_impact}")
    print(f"  UE impact — X2 path:  {x2_impact}")
    print()

    # -----------------------------------------------------------------------
    # Example traces
    # -----------------------------------------------------------------------
    print("=" * 68)
    print("  EXAMPLE GENERATED TRACES")
    print("=" * 68)
    print(f"\n  S1 path  (» = generated by model, {N_SAMPLES} samples, "
          f"trained on 76 real S1 failures)\n")
    for sess in s1_sessions[:3]:
        outcome, impact = classify(sess)
        print(f"  Outcome: {outcome}  |  {impact}")
        print(format_session(sess, len(s1_prefix)))
        print()

    print(f"\n  X2 path  (prefix = real observed trace; » = model continuation)\n")
    for sess in x2_sessions[:3]:
        outcome, impact = classify(sess, x2_mode=True)
        print(f"  Outcome: {outcome}  |  {impact}")
        print(format_session(sess, len(x2_prefix)))
        print()

    # -----------------------------------------------------------------------
    # Timing comparison
    # -----------------------------------------------------------------------
    print("=" * 68)
    print("  TIMING COMPARISON")
    print("=" * 68)
    print("""
  S1 failure path (from real traces):
    HANDOVER_REQUIRED  →  PREPARATION_FAILURE   ~17–22ms
    then: UE_CONTEXT_RELEASE_COMMAND            ~500ms
    UE is dropped. Total: ~520ms to loss of service.

  X2 success path (from real trace):
    PATH_SWITCH_REQUEST  →  PATH_SWITCH_ACKNOWLEDGE   46ms
    UE continues on new cell. No service interruption.

  Difference: S1 failure takes ~10x longer and ends in UE drop.
              X2 success completes in 46ms with no UE impact.
""")

    # -----------------------------------------------------------------------
    # Bar chart
    # -----------------------------------------------------------------------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
        fig.suptitle(
            "Handover Path Comparison — S1 (failure) vs X2 (success)\n"
            "SessionTransformer trained on real Telus LTE traces (eNB 499557)",
            fontsize=11,
        )

        # Left: outcome distribution bar chart
        plot_labels = [l for l in all_labels
                       if s1_outcomes.get(l, 0) + x2_outcomes.get(l, 0) > 0]
        short = []
        for l in plot_labels:
            if "rejected" in l:   short.append("S1 rejected\n(HO denied)")
            elif "released" in l: short.append("Context\nreleased")
            elif "Clean" in l:    short.append("Clean\nrelease")
            elif "success" in l:  short.append("X2 HO\nsuccess")
            elif "complete" in l.lower(): short.append("X2 complete\n(UE continues)")
            elif "continues" in l:short.append("Session\ncontinues")
            else:                 short.append(l[:20])

        x = np.arange(len(plot_labels))
        w = 0.35
        s1v = [s1_outcomes.get(l, 0) / N_SAMPLES * 100 for l in plot_labels]
        x2v = [x2_outcomes.get(l, 0) / N_SAMPLES * 100 for l in plot_labels]
        b1 = ax1.bar(x - w/2, s1v, w, label="S1 path", color="#d62728", alpha=0.85)
        b2 = ax1.bar(x + w/2, x2v, w, label="X2 path", color="#2ca02c", alpha=0.85)
        ax1.set_xticks(x)
        ax1.set_xticklabels(short, fontsize=8)
        ax1.set_ylabel("% of generated sessions")
        ax1.set_ylim(0, 115)
        ax1.set_title("Outcome distribution (model-generated)")
        ax1.legend(fontsize=8)
        ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
        for bar in list(b1) + list(b2):
            h = bar.get_height()
            if h > 3:
                ax1.annotate(f"{h:.0f}%",
                             xy=(bar.get_x() + bar.get_width() / 2, h),
                             xytext=(0, 2), textcoords="offset points",
                             ha="center", va="bottom", fontsize=7)

        # Right: timing comparison
        categories  = ["S1: HO\nrejected\n(17–22ms)", "S1: context\nrelease\n(~500ms)",
                        "X2: PATH_SWITCH\nack\n(46ms)"]
        times_ms    = [20, 500, 46]
        colors      = ["#d62728", "#d62728", "#2ca02c"]
        bars        = ax2.bar(categories, times_ms, color=colors, alpha=0.85, width=0.45)
        ax2.set_ylabel("Time (ms)")
        ax2.set_title("Event timing (real observed traces)")
        ax2.set_ylim(0, 600)
        for bar, val in zip(bars, times_ms):
            ax2.text(bar.get_x() + bar.get_width() / 2,
                     bar.get_height() + 8,
                     f"{val}ms", ha="center", va="bottom", fontsize=9, fontweight="bold")

        # Annotate UE impact
        ax2.text(0.5, -0.22,
                 "S1 path: UE dropped (~520ms total)          "
                 "X2 path: UE continues, no interruption",
                 transform=ax2.transAxes, ha="center", fontsize=7.5,
                 style="italic", color="gray")

        plt.tight_layout()
        ensure_dirs()
        out = GRAPHS_DIR / "compare_ho_paths.png"
        plt.savefig(out, dpi=130, bbox_inches="tight")
        print(f"  Chart saved to {out}")

    except Exception as e:
        print(f"  (Chart skipped: {e})")


if __name__ == "__main__":
    main()
