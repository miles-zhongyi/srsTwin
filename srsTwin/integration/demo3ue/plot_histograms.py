#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Plot DU processing delay and call duration histograms from analyze.py's
results.json — 1 pair alone vs 3 pairs concurrent, side by side.

Usage (run from integration/, after analyze.py):
  python3 demo3ue/plot_histograms.py
"""
from __future__ import annotations

import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))


def load_results() -> dict:
    with open(os.path.join(HERE, "results.json"), encoding="utf-8") as f:
        return json.load(f)


def vals(rows: list[dict], field: str) -> list[float]:
    return [r[field] for r in rows if r[field] is not None]


def plot_field(ax, one: list[float], three: list[float], title: str, xlabel: str, bins: int):
    ax.hist(one, bins=bins, alpha=0.6, label=f"1 pair alone (n={len(one)})", color="#3fb950")
    ax.hist(three, bins=bins, alpha=0.6, label=f"3 pairs concurrent (n={len(three)})", color="#f85149")
    if one:
        ax.axvline(sum(one) / len(one), color="#3fb950", linestyle="--", linewidth=2)
    if three:
        ax.axvline(sum(three) / len(three), color="#f85149", linestyle="--", linewidth=2)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("count")
    ax.legend(fontsize=8)


def main() -> int:
    data = load_results()
    one, three = data["one_pair"], data["three_pair"]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

    plot_field(ax1, vals(one, "du_delay_ms"), vals(three, "du_delay_ms"),
               "DU processing delay (PRACH -> RAR, Msg1->Msg2)", "delay (ms)", bins=8)
    plot_field(ax2, [v / 1000 for v in vals(one, "session_ms")],
               [v / 1000 for v in vals(three, "session_ms")],
               "Call duration (Attach Complete -> Release)", "duration (s)", bins=8)

    fig.suptitle("srsTwin 4G LTE — 1 vs 3 concurrent UE+eNB pairs sharing one host/EPC",
                 fontsize=12, fontweight="bold")
    fig.tight_layout()

    out_path = os.path.join(HERE, "histograms.png")
    fig.savefig(out_path, dpi=130)
    print(f"Saved -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
