#!/usr/bin/env python3
"""Forecast GTA VI trophy earn rates on Exophase at a case's target.

Reads every ``*.in`` case in $MAKEFUN_INPUT_DIR (else /input/) and writes
one ``<id>.out`` forecast per case to $MAKEFUN_OUTPUT_DIR (else /output/).

Method, per trophy:
  * Fit a least-squares line of logit(rate) against ln(1 + hours since
    2026-11-19T00:00:00Z) over the case's last 24 hours of reads and read
    the line at the target.  Fewer than 3 usable reads fall back to the
    last observed rate (persistence).
  * Shrink the extrapolated move toward a per-tier prior: platinum
    trophies move slowest, bronze fastest.  The shrink caps how far the
    median may travel from the last rate in logit space.
  * Refresh-drop prior for a flat, short-horizon case: when every read in
    the last 24 hours shows the same rates (Exophase had not refreshed
    yet) and the horizon is short, the next refresh dilutes the rates as
    newly tracked players join holding fewer trophies, so the median is
    pulled down (except a trophy already at 100%) and the spread widened.
  * Quantiles are logit-normal around the median: spread combines the
    random-walk term (0.5 * sqrt(horizon in days)), the fit residual and,
    for flat histories, an extra refresh-uncertainty term.

Standard library only, deterministic, no network, no wall-clock use.
"""

import glob
import json
import math
import os

Z = (-1.6448536269514722, -0.6744897501960817, 0.0,
     0.6744897501960817, 1.6448536269514722)
EPOCH = "2026-11-19T00:00:00Z"

# Per-tier cap on the median's logit move away from the last rate, and
# the tier's prior pull (logit points toward the tier's typical level).
TIER_CAP = {"platinum": 0.6, "gold": 1.0, "silver": 1.5, "bronze": 2.2}
# Typical earn rate (percent) a trophy of the tier settles toward early.
TIER_PRIOR_RATE = {"platinum": 0.5, "gold": 2.0, "silver": 5.0,
                   "bronze": 12.0}
TIER_PRIOR_WEIGHT = 0.05


def parse_time(text):
    """Parse an ISO-8601 UTC timestamp to epoch seconds (float)."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    from datetime import datetime
    return datetime.fromisoformat(text).timestamp()


def clamp_rate(pct):
    return min(99.99, max(0.01, pct))


def logit(pct):
    p = clamp_rate(pct) / 100.0
    return math.log(p / (1.0 - p))


def inv_logit(x):
    if x >= 0:
        return 100.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return 100.0 * e / (1.0 + e)


def fit_line(xs, ys):
    """Least-squares y = a + b*x; returns (a, b, residual_std)."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return my, 0.0, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    var = sum(r * r for r in resid) / n
    return a, b, math.sqrt(var)


def forecast_trophy(trophy, history, horizon_minutes, at_ts):
    """Five quantiles (percent) for one trophy at the case target."""
    tid = trophy["id"]
    tier = trophy.get("tier", "bronze")
    horizon_days = horizon_minutes / 1440.0
    epoch_ts = parse_time(EPOCH)

    # Series of (x, logit rate) over the last 24 h before the checkpoint.
    xs, ys = [], []
    last_rate = None
    for read in history:
        rates = read.get("rates") or []
        if tid - 1 >= len(rates) or rates[tid - 1] is None:
            continue
        rate = rates[tid - 1]
        last_rate = rate
        read_ts = parse_time(read["at"])
        if at_ts - read_ts > 24 * 3600:
            continue
        hours = (read_ts - epoch_ts) / 3600.0
        xs.append(math.log1p(max(0.0, hours)))
        ys.append(logit(rate))
    if last_rate is None:
        last_rate = 1.0

    flat = len(set(round(y, 9) for y in ys)) <= 1 and len(ys) >= 2
    last_logit = logit(last_rate)

    if len(xs) >= 3:
        a, b, resid = fit_line(xs, ys)
        target_hours = (at_ts + horizon_minutes * 60.0 - epoch_ts) / 3600.0
        x_target = math.log1p(max(0.0, target_hours))
        median_logit = a + b * x_target
    else:
        median_logit = last_logit
        resid = 0.0

    # Shrink the move toward the tier prior and cap it.
    cap = TIER_CAP.get(tier, 2.2)
    move = median_logit - last_logit
    if move > cap:
        move = cap
    elif move < -cap:
        move = -cap
    prior_logit = logit(TIER_PRIOR_RATE.get(tier, 12.0))
    median_logit = last_logit + move
    median_logit = ((1.0 - TIER_PRIOR_WEIGHT) * median_logit
                    + TIER_PRIOR_WEIGHT * prior_logit)

    # Refresh-drop prior: flat history + short horizon means the next
    # refresh dilutes the rates (new players hold fewer trophies).
    spread_extra = 0.0
    if flat and horizon_minutes <= 180 and last_rate < 99.0:
        drop = 0.45  # logit points down at the median
        median_logit -= drop
        spread_extra = 0.85
    elif flat and horizon_minutes <= 1440 and last_rate < 99.0:
        median_logit -= 0.15
        spread_extra = 0.5

    # Spread: random walk + fit residual + refresh uncertainty.
    rw = 0.5 * math.sqrt(max(0.0, horizon_days))
    spread = math.sqrt(rw * rw + resid * resid
                       + spread_extra * spread_extra)
    spread = max(0.35, spread)

    qs = []
    prev = None
    for z in Z:
        q = inv_logit(median_logit + z * spread)
        q = min(100.0, max(0.0, q))
        if prev is not None and q < prev:
            q = prev
        qs.append(round(q, 4))
        prev = q
    return qs


def run_case(case):
    at_ts = parse_time(case["at"])
    horizon = case["horizon_minutes"]
    history = case.get("history", [])
    rows = []
    for trophy in case["trophies"]:
        rows.append({"id": trophy["id"],
                     "q": forecast_trophy(trophy, history, horizon,
                                          at_ts)})
    return {"trophies": rows}


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input/")
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output/")
    os.makedirs(out_dir, exist_ok=True)
    paths = sorted(glob.glob(os.path.join(in_dir, "*.in")))
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            case = json.load(fh)
        result = run_case(case)
        out_path = os.path.join(out_dir, case["id"] + ".out")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, separators=(",", ":"))
            fh.write("\n")


if __name__ == "__main__":
    main()
