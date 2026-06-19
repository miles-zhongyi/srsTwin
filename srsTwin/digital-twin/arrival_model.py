"""
Step 6: Session arrival process modeling (Hawkes process per cell).

A Hawkes process captures the self-exciting burstiness in UE arrivals —
one UE connecting makes it slightly more likely others arrive shortly after
(e.g., people arriving at a venue together).

Fits one process per cell_id using MLE on inter-arrival times.
Saves arrival_model.json with fitted parameters.

Also provides a sampler for generating synthetic arrival timestamps.
"""

import json
import math
import random
import collections
from datetime import datetime

from config import ARRIVAL_MODEL_FILE, SESSIONS_FILE


# ---------------------------------------------------------------------------
# Hawkes process: intensity λ(t) = μ + α * Σ exp(-β*(t - t_i)) for t_i < t
# Parameters: μ (baseline), α (excitation), β (decay)
# ---------------------------------------------------------------------------

def hawkes_log_likelihood(times, mu, alpha, beta):
    """
    Log-likelihood for a univariate Hawkes process with exponential kernel.
    times: sorted list of event times (seconds from epoch or relative).
    """
    if len(times) < 2:
        return -1e9
    T = times[-1] - times[0]
    if T <= 0 or mu <= 0 or alpha < 0 or beta <= 0:
        return -1e9
    if alpha >= beta:  # stability condition
        return -1e9

    ll = 0.0
    # Compensator (integral of intensity)
    ll -= mu * T

    R = 0.0  # recursive sum
    for i, t in enumerate(times):
        if i > 0:
            R = math.exp(-beta * (t - times[i - 1])) * (1 + R)
        intensity = mu + alpha * R
        if intensity <= 0:
            return -1e9
        ll += math.log(intensity)
        ll -= (alpha / beta) * (1 - math.exp(-beta * (T - t + times[0])))

    return ll


def fit_hawkes(times, n_restarts=20, max_iter=500):
    """
    Fit Hawkes(μ, α, β) via random-restart coordinate ascent.
    Returns best (mu, alpha, beta) found.
    """
    if len(times) < 3:
        rate = len(times) / max(times[-1] - times[0], 1)
        return {"mu": rate, "alpha": 0.0, "beta": 1.0, "n_events": len(times)}

    T = times[-1] - times[0]
    baseline_rate = len(times) / T

    best_ll  = -1e18
    best_params = (baseline_rate, 0.01, 1.0)

    for _ in range(n_restarts):
        mu    = random.uniform(0.01, baseline_rate * 2)
        alpha = random.uniform(0.001, 0.5)
        beta  = random.uniform(alpha + 0.01, 5.0)

        for _ in range(max_iter):
            # Gradient step for mu
            for param_name in ["mu", "alpha", "beta"]:
                for delta in [1.1, 0.9]:
                    p = {"mu": mu, "alpha": alpha, "beta": beta}
                    p[param_name] *= delta
                    ll = hawkes_log_likelihood(times, p["mu"], p["alpha"], p["beta"])
                    if ll > best_ll:
                        best_ll = ll
                        mu, alpha, beta = p["mu"], p["alpha"], p["beta"]
                        best_params = (mu, alpha, beta)

    mu, alpha, beta = best_params
    return {
        "mu":      mu,
        "alpha":   alpha,
        "beta":    beta,
        "n_events": len(times),
        "duration_s": T,
        "mean_rate": len(times) / T,
        "log_likelihood": hawkes_log_likelihood(times, mu, alpha, beta),
    }


def sample_hawkes(params, duration_s, seed=None):
    """
    Ogata thinning algorithm for sampling from a Hawkes process.
    Returns list of event times (relative seconds from 0).
    """
    if seed is not None:
        random.seed(seed)
    mu    = params["mu"]
    alpha = params["alpha"]
    beta  = params["beta"]

    times = []
    t = 0.0
    history = []

    while t < duration_s:
        # Upper bound on intensity
        lam_bar = mu + alpha * sum(math.exp(-beta * (t - s)) for s in history)
        if lam_bar <= 0:
            break
        # Sample candidate next event
        u = random.random()
        dt = -math.log(u) / lam_bar
        t += dt
        if t >= duration_s:
            break
        # Actual intensity at t
        lam_t = mu + alpha * sum(math.exp(-beta * (t - s)) for s in history)
        # Accept/reject
        if random.random() <= lam_t / lam_bar:
            times.append(t)
            history.append(t)

    return times


def ts_to_seconds(ts_str, epoch_str):
    """Convert ISO timestamp to seconds since epoch_str."""
    t  = datetime.fromisoformat(ts_str)
    t0 = datetime.fromisoformat(epoch_str)
    return (t - t0).total_seconds()


def main():
    with open(SESSIONS_FILE, encoding="utf-8") as f:
        sessions = json.load(f)

    # Use only sessions with a valid start time
    sessions.sort(key=lambda s: s["start_time"])
    epoch = sessions[0]["start_time"]

    # Group session start times by cell_id
    by_cell = collections.defaultdict(list)
    for s in sessions:
        by_cell[s["cell_id"]].append(ts_to_seconds(s["start_time"], epoch))

    results = {}
    print("Fitting Hawkes process per cell...")
    for cell_id in sorted(by_cell.keys()):
        times = sorted(by_cell[cell_id])
        params = fit_hawkes(times)
        results[cell_id] = params
        print(f"  cell_{cell_id}: {params['n_events']:5d} sessions | "
              f"mean_rate={params['mean_rate']:.3f}/s | "
              f"mu={params['mu']:.4f} alpha={params['alpha']:.4f} beta={params['beta']:.4f}")

    # Sanity check: sample 60 seconds from each cell and compare to observed rate
    print("\nSampling sanity check (60s window):")
    for cell_id, params in results.items():
        sampled = sample_hawkes(params, duration_s=60, seed=42)
        obs_60s = sum(1 for t in by_cell[cell_id] if t < 60)
        print(f"  cell_{cell_id}: observed={obs_60s} sampled={len(sampled)}")

    # Save
    results["epoch"] = epoch
    with open(ARRIVAL_MODEL_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved arrival model to {ARRIVAL_MODEL_FILE}")


if __name__ == "__main__":
    main()
