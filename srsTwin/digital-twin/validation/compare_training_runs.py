"""
Compare two training_log.json files — before vs after the data-formatting
changes (dropped boundary-artifact sessions, finer 20-100ms time bucket,
per-outcome test perplexity).

`training_log_before.json` is a frozen copy of training_log.json as it was
prior to the changes. After retraining in Colab, copy the new
training_log.json back into this repo and run:

  .venv/bin/python compare_training_runs.py training_log_before.json training_log.json
"""

import json
import sys


def load(path):
    with open(path) as f:
        return json.load(f)


def main():
    if len(sys.argv) != 3:
        print("Usage: python compare_training_runs.py <before.json> <after.json>")
        sys.exit(1)

    before = load(sys.argv[1])
    after = load(sys.argv[2])

    print("=== Config ===")
    keys = sorted(set(before["config"]) | set(after["config"]))
    for key in keys:
        b = before["config"].get(key)
        a = after["config"].get(key)
        flag = "" if b == a else "  <-- changed"
        print(f"  {key:12s} before={b!s:<8} after={a!s:<8}{flag}")

    print("\n=== Final epoch (train/val) ===")
    b_last = before["log"][-1]
    a_last = after["log"][-1]
    for key in ("train_loss", "train_ppl", "val_loss", "val_ppl"):
        delta = a_last[key] - b_last[key]
        print(f"  {key:12s} before={b_last[key]:.4f}  after={a_last[key]:.4f}  delta={delta:+.4f}")

    print("\n=== Aggregate test ===")
    for key in ("test_loss", "test_ppl"):
        delta = after[key] - before[key]
        print(f"  {key:10s} before={before[key]:.4f}  after={after[key]:.4f}  delta={delta:+.4f}")
    print("  Note: aggregate test_ppl mixes normal traffic with rare ho_failure")
    print("  sessions (74% of all ho_failure sessions land in the test split),")
    print("  so this number alone is hard to interpret. See breakdown below.")

    print("\n=== Test perplexity by outcome (after only) ===")
    by_outcome = after.get("test_by_outcome")
    if not by_outcome:
        print("  Not present in the 'after' file — make sure you ran the")
        print("  updated Train cell (writes test_by_outcome).")
        return

    for outcome, stats in sorted(by_outcome.items(), key=lambda kv: -kv[1]["ppl"]):
        print(f"  {outcome:30s} n={stats['n']:4d}  loss={stats['loss']:.4f}  ppl={stats['ppl']:.2f}")

    if "ho_failure" in by_outcome:
        normal_keys = [k for k in by_outcome if k != "ho_failure"]
        normal_n = sum(by_outcome[k]["n"] for k in normal_keys)
        if normal_n:
            normal_ppl = sum(by_outcome[k]["ppl"] * by_outcome[k]["n"] for k in normal_keys) / normal_n
            fail_ppl = by_outcome["ho_failure"]["ppl"]
            print(f"\n  Separation: ho_failure ppl ({fail_ppl:.2f}) vs "
                  f"normal-weighted ppl ({normal_ppl:.2f})  -> {fail_ppl / normal_ppl:.2f}x")
            print("  Higher ratio = model finds ho_failure sequences more 'surprising'")
            print("  relative to normal traffic, which is the signal anomaly_detection.py")
            print("  relies on.")


if __name__ == "__main__":
    main()
