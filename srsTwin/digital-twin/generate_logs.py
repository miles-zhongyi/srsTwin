"""
Synthetic S1AP log generator.

Produces a JSON file of synthetic session traces in the same format as the
original decoded trace files. Useful for:
  - Testing downstream tools (OSS, MME simulators) that consume S1AP logs
  - Load testing with realistic session arrival patterns
  - Generating training data for other models

The generator combines two learned components:
  1. Hawkes arrival model  — when sessions start, per cell
  2. SessionTransformer    — what message sequence each session produces

Usage:
  .venv/bin/python generate_logs.py --duration 300 --output synthetic_traces.json
  .venv/bin/python generate_logs.py --duration 60  --cell 71 --output cell71_test.json
  .venv/bin/python generate_logs.py --duration 3600 --output hour_simulation.json
"""

import json
import argparse
import random
import collections
from datetime import datetime, timedelta, timezone

import torch

from config import ARRIVAL_MODEL_FILE, CHECKPOINT_FILE, DEFAULT_SYNTHETIC_OUTPUT, VOCAB_FILE
from model import SessionTransformer
from arrival_model import sample_hawkes
import state_machine as sm

# Time bucket midpoints in ms — used to reconstruct realistic inter-event delays.
# Must match tokenize_sessions.TIME_BUCKETS = [0, 5, 20, 40, 100, 500, 2000, 10000],
# i.e. T0<=0, T1=(0,5], T2=(5,20], T3=(20,40], T4=(40,100], T5=(100,500],
# T6=(500,2000], T7=(2000,10000], T8=(10000,inf).
BUCKET_MIDPOINTS = [0, 3, 12, 30, 70, 300, 1000, 5000, 15000]

# Base timestamp for synthetic logs
BASE_TIME = datetime(2026, 1, 22, 4, 0, 0, tzinfo=timezone.utc)

# Static eNB metadata (matches the real dataset)
ENB_ID    = "499557"
PLMN      = "302 221"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_model_and_vocab():
    with open(VOCAB_FILE, encoding="utf-8") as f:
        vocab = json.load(f)
    inv_vocab = {v: k for k, v in vocab.items()}

    ckpt  = torch.load(CHECKPOINT_FILE, map_location="cpu", weights_only=True)
    cfg   = ckpt["cfg"]
    model = SessionTransformer(
        vocab_size=ckpt["vocab_size"],
        d_model=cfg["d_model"], n_heads=cfg["n_heads"],
        n_layers=cfg["n_layers"], max_len=cfg["max_len"], dropout=0.0,
    )
    model.load_state_dict(ckpt["model_state"])
    model.eval()
    return model, vocab, inv_vocab


def load_arrival_params():
    with open(ARRIVAL_MODEL_FILE, encoding="utf-8") as f:
        params = json.load(f)
    # Keys are strings in JSON; convert to int
    return {int(k): v for k, v in params.items() if k != "epoch"}


def cell_distribution(arrival_params):
    """Probability of a session being on each cell, weighted by mean arrival rate."""
    rates  = {cell: p["mean_rate"] for cell, p in arrival_params.items()}
    total  = sum(rates.values())
    return {cell: r / total for cell, r in rates.items()}


def token_to_message(tok):
    """Extract the S1AP message name from a token string."""
    return tok.split("|")[0]


def token_to_dt_ms(tok):
    """Extract the time delta midpoint in ms from a token string."""
    parts = tok.split("|")
    if len(parts) == 3 and parts[2].startswith("T"):
        try:
            bucket = int(parts[2][1:])
            if bucket < len(BUCKET_MIDPOINTS):
                return BUCKET_MIDPOINTS[bucket]
        except ValueError:
            pass
    return 0


def generate_session_tokens(model, vocab, inv_vocab, cell_id, temperature=0.9, top_k=8):
    """Generate one session token sequence for a given cell."""
    prefix = [f"CELL_{cell_id}", "<BOS>"]
    return model.generate(
        prefix, vocab, inv_vocab,
        state_machine=sm,
        max_new_tokens=30,
        temperature=temperature,
        top_k=top_k,
    )


def tokens_to_events(token_seq, session_start_ts, cell_id, enb_ue_id, mme_ue_id):
    """
    Convert a generated token sequence into a list of S1AP event dicts
    matching the format of the original decoded trace files.
    """
    events = []
    current_ts = session_start_ts

    for tok in token_seq:
        msg = token_to_message(tok)

        # Skip structural tokens
        if msg in ("<BOS>", "<EOS>", "<PAD>", "<UNK>") or msg.startswith("CELL_"):
            continue

        dt = token_to_dt_ms(tok)
        current_ts = current_ts + timedelta(milliseconds=dt)

        events.append({
            "interface":        "S1",
            "message_name":     msg,
            "timestamp":        current_ts.isoformat(),
            "serving_plmn":     PLMN,
            "enb_id":           ENB_ID,
            "cell_id":          cell_id,
            "direction":        2,
            "m_tmsi":           1048575,      # anonymised
            "enb_ue_s1ap_id":   enb_ue_id,
            "mme_ue_s1ap_id":   mme_ue_id,
            "protocol":         "S1AP",
            "synthetic":        True,         # flag so downstream tools can filter
        })

    return events, current_ts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration",    type=int,   default=300,
                        help="Simulation duration in seconds (default: 300)")
    parser.add_argument("--cell",        type=int,   default=None,
                        help="Restrict to a single cell_id (default: all cells)")
    parser.add_argument("--output",      default=str(DEFAULT_SYNTHETIC_OUTPUT),
                        help="Output JSON file path")
    parser.add_argument("--temperature", type=float, default=0.9,
                        help="Sampling temperature (higher = more diverse, default: 0.9)")
    parser.add_argument("--seed",        type=int,   default=42)
    args = parser.parse_args()

    random.seed(args.seed)
    torch.manual_seed(args.seed)

    print("Loading model and arrival parameters...")
    model, vocab, inv_vocab = load_model_and_vocab()
    arrival_params = load_arrival_params()
    cell_dist      = cell_distribution(arrival_params)

    cells = [args.cell] if args.cell else sorted(arrival_params.keys())

    # -----------------------------------------------------------------------
    # Generate arrival times per cell
    # -----------------------------------------------------------------------
    print(f"Simulating {args.duration}s window across cells: {cells}")

    # List of (start_time_s, cell_id) sorted chronologically
    arrivals = []
    for cell in cells:
        params = arrival_params[cell]
        times  = sample_hawkes(params, duration_s=args.duration, seed=args.seed + cell)
        for t in times:
            arrivals.append((t, cell))

    arrivals.sort(key=lambda x: x[0])
    print(f"Total session arrivals: {len(arrivals)}")

    per_cell = collections.Counter(c for _, c in arrivals)
    for cell in cells:
        print(f"  cell_{cell}: {per_cell.get(cell, 0)} sessions")

    # -----------------------------------------------------------------------
    # Generate session content for each arrival
    # -----------------------------------------------------------------------
    print("\nGenerating session content...")
    all_events = []
    enb_ue_counter = 1000   # synthetic session ID counter

    for arrival_s, cell_id in arrivals:
        session_start = BASE_TIME + timedelta(seconds=arrival_s)
        enb_ue_id     = enb_ue_counter
        mme_ue_id     = enb_ue_counter + 50000
        enb_ue_counter += 1

        toks = generate_session_tokens(
            model, vocab, inv_vocab, cell_id,
            temperature=args.temperature,
        )
        events, _ = tokens_to_events(toks, session_start, cell_id, enb_ue_id, mme_ue_id)
        all_events.extend(events)

    # Sort all events by timestamp (sessions may interleave)
    all_events.sort(key=lambda e: e["timestamp"])

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print(f"\nGenerated {len(all_events)} S1AP events across {len(arrivals)} sessions")
    msg_counts = collections.Counter(e["message_name"] for e in all_events)
    print("\nTop message types:")
    for msg, cnt in msg_counts.most_common(10):
        print(f"  {msg:50s} {cnt:5d}")

    # -----------------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------------
    output = {
        "metadata": {
            "generator":      "SessionTransformer digital twin",
            "enb_id":          ENB_ID,
            "plmn":            PLMN,
            "sim_duration_s":  args.duration,
            "cells":           cells,
            "n_sessions":      len(arrivals),
            "n_events":        len(all_events),
            "base_time":       BASE_TIME.isoformat(),
            "temperature":     args.temperature,
            "seed":            args.seed,
        },
        "events": all_events,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {args.output}")
    print(f"  Events span: {all_events[0]['timestamp']}  →  {all_events[-1]['timestamp']}")


if __name__ == "__main__":
    main()
