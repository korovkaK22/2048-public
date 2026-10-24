#!/usr/bin/env python
"""arena.py — inferential statistics + experiment planning for the farm bot.

The project measures a lot and infers nothing: no confidence interval, no
p-value, no power calculation appears anywhere in `research/experiments/`.
Every KEEP/DROP verdict in `docs/experiment-log.md` was taken inside the noise
(docs/archive/strategy-2026-07-11.md §6). This is the missing half.

It answers four questions, in the units the project actually cares about:

  METRIC = MOVES, not score (score = 18.2 x moves, r^2 = .998 — same signal,
  less noise), and the OBJECTIVE = P(moves >= 2570), a TAIL, not a mean.

  stats    per-arm  mu, sigma, fitted P(moves >= T) with a bootstrap 95% CI.
           Arms are RANKED BY z = (mu - T)/sigma, because P = Phi(z) — sigma
           enters the objective exactly as hard as mu. An arm can win the mean
           and LOSE the tail (measured: the shipped default does exactly that).
           mu + 1.645*sigma >= T is the separate "P >= 5%" goal line.
  compare  paired A/B vs a base arm: mean difference (t + Wilcoxon + CI),
           AND an F-test on the variances, since a sigma win is a real win.
  corr     proxy validation: Spearman/Kendall between a CHEAP run and an
           EXPENSIVE run over the same arms. Run this BEFORE trusting any
           cheap screen: a proxy that reverses ranks is worse than no screen.
  plan     power: seeds needed for a given MDE (on mu and on sigma), and a
           Monte-Carlo of how often a screening funnel drops the best arm.

  run      plays the arms (optional --crn, see below) and writes arm-*.jsonl.

CRN (--crn): spawns are indexed BY PLY — spawn k always consumes the k-th
pre-drawn pair (u1, u2) of the seed's stream, cell = empties[floor(u1*|E|)].
Unbiased (u1 uniform => the pick is uniform on the empties), but MEASURED to
buy ~nothing once two arms' boards diverge; kept as an option, not a default.
See the report accompanying this script.

Usage (from project/):
    .venv/bin/python scripts/sim/arena.py run ARMS.json --seeds 1000:100 --out DIR
    .venv/bin/python scripts/sim/arena.py stats DIR [DIR ...]
    .venv/bin/python scripts/sim/arena.py compare DIR --base baseline
    .venv/bin/python scripts/sim/arena.py corr CHEAP_DIR EXPENSIVE_DIR
    .venv/bin/python scripts/sim/arena.py plan --sd 300

DIR may be an arena output dir (arm-*.jsonl) OR one or more run folders
written by scripts/sim/experiment_runner.py (config.json + results.jsonl) — so it
works on everything already in research/experiments/.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# 48 000 score / 18.2 score-per-move (docs/archive/audit-2026-07-11.md §3.3).
TARGET_MOVES = 2570
RANK_1024 = 10
Z95, Z80 = 1.96, 0.84          # two-sided alpha=.05, power=.8
Z_P5 = 1.645                   # P(X >= T) = 5%  <=>  mu + 1.645 sigma = T

FARM_ENV_VARS = (
    "BOT2048_FARM_EMPTY_W", "BOT2048_FARM_MERGE_W", "BOT2048_FARM_CORNER_W",
    "BOT2048_FARM_EDGE_W", "BOT2048_FARM_STAIR_W", "BOT2048_FARM_SNAKE_W",
    "BOT2048_FARM_SNAKE_RATIO", "BOT2048_FARM_CORNER_RANK_W",
    "BOT2048_FARM_CONVEX_W", "BOT2048_FARM_LATE_EMPTY_W",
    "BOT2048_FARM_LATE_GRAD_MULT", "BOT2048_FARM_LATE_CORNER_RANK_W",
    "BOT2048_FARM_HOARD_TRIGGER", "BOT2048_CPROB_THRESH",
)

# ==========================================================================
# Distributions / tests (no scipy in this venv)
# ==========================================================================


def phi(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def norm_tail(mu: float, sd: float, t: float) -> float:
    if sd <= 0:
        return 1.0 if mu >= t else 0.0
    return 1.0 - phi((t - mu) / sd)


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a,b) via the Lentz continued fraction."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _betainc(b, a, 1.0 - x)
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1 - x) - lbeta) / a
    f, c, d = 1.0, 1.0, 0.0
    for i in range(300):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        d = math.copysign(max(abs(d), 1e-30), d)
        d = 1.0 / d
        c = 1.0 + num / c
        c = math.copysign(max(abs(c), 1e-30), c)
        f *= c * d
        if abs(1.0 - c * d) < 1e-12:
            break
    return front * (f - 1.0)


def t_pvalue(t: float, df: float) -> float:
    """Two-sided p-value of Student-t."""
    if df <= 0:
        return 1.0
    return _betainc(df / 2.0, 0.5, df / (df + t * t))


def f_pvalue(f: float, d1: int, d2: int) -> float:
    """Two-sided p-value of an F(d1,d2) variance-ratio test."""
    if f <= 0:
        return 1.0
    if f < 1.0:
        f, d1, d2 = 1.0 / f, d2, d1
    upper = _betainc(d2 / 2.0, d1 / 2.0, d2 / (d2 + d1 * f))
    return min(1.0, 2.0 * upper)


def paired_t(diffs: list[float]) -> dict[str, Any]:
    n = len(diffs)
    if n < 2:
        return {"n": n, "mean": 0.0, "sd": 0.0, "t": 0.0, "p": 1.0, "ci": (0.0, 0.0)}
    m = statistics.mean(diffs)
    sd = statistics.stdev(diffs)
    se = sd / math.sqrt(n)
    t = m / se if se else 0.0
    return {"n": n, "mean": m, "sd": sd, "se": se, "t": t,
            "p": t_pvalue(abs(t), n - 1),
            "ci": (m - Z95 * se, m + Z95 * se)}


def wilcoxon_p(diffs: list[float]) -> float:
    """Two-sided Wilcoxon signed-rank, normal approximation."""
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n < 6:
        return float("nan")
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w = sum(ranks[i] for i in range(n) if d[i] > 0)
    mu = n * (n + 1) / 4.0
    sd = math.sqrt(n * (n + 1) * (2 * n + 1) / 24.0)
    return 2.0 * (1.0 - phi(abs((w - mu) / sd))) if sd else 1.0


def bootstrap_ci(xs: list[float], fn, reps: int = 4000, alpha: float = 0.05,
                 seed: int = 7) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(xs)
    vals = sorted(fn([xs[rng.randrange(n)] for _ in range(n)]) for _ in range(reps))
    return vals[int(alpha / 2 * reps)], vals[min(reps - 1, int((1 - alpha / 2) * reps))]


def _ranks(xs: list[float]) -> list[float]:
    o = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[o[j + 1]] == xs[o[i]]:
            j += 1
        for k in range(i, j + 1):
            r[o[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return r


def pearson(a: list[float], b: list[float]) -> float:
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return num / (da * db) if da and db else 0.0


def spearman(a: list[float], b: list[float]) -> float:
    return pearson(_ranks(a), _ranks(b))


def kendall(a: list[float], b: list[float]) -> float:
    c = d = 0
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            s = (a[i] - a[j]) * (b[i] - b[j])
            c += s > 0
            d += s < 0
    return (c - d) / (c + d) if c + d else 0.0


def n_for_mean(delta: float, sd: float, rho: float = 0.0) -> int:
    """Seeds per arm to detect a `delta` shift of the MEAN (alpha .05, power .8).
    rho = correlation between the paired arms (0 for independent seeds)."""
    sd_d = sd * math.sqrt(2.0 * (1.0 - rho))
    return math.ceil(((Z95 + Z80) * sd_d / delta) ** 2)


def n_for_sigma(ratio: float) -> int:
    """Seeds per arm to detect a `ratio` change of SIGMA (log-s normal approx)."""
    if ratio <= 1.0:
        return 10 ** 9
    return math.ceil(1 + ((Z95 + Z80) / math.log(ratio)) ** 2)


# ==========================================================================
# Loading
# ==========================================================================


def load(paths: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Accepts arena dirs (arm-*.jsonl) and experiment_runner run folders."""
    arms: dict[str, list[dict[str, Any]]] = {}
    for p in paths:
        d = Path(p)
        for f in sorted(d.glob("arm-*.jsonl")):
            recs = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
            if recs:
                arms.setdefault(recs[0].get("arm", f.stem[4:]), []).extend(recs)
        res, cfg = d / "results.jsonl", d / "config.json"
        if res.is_file():
            label = json.loads(cfg.read_text())["label"] if cfg.is_file() else d.name
            recs = [json.loads(x) for x in res.read_text().splitlines() if x.strip()]
            arms.setdefault(label, []).extend(recs)
    for k in arms:
        arms[k].sort(key=lambda r: r["seed"])
    return arms


def arm_stats(recs: list[dict[str, Any]], metric: str, target: float) -> dict[str, Any]:
    xs = [float(r[metric]) for r in recs]
    n = len(xs)
    mu = statistics.mean(xs)
    sd = statistics.stdev(xs) if n > 1 else 0.0
    fit = norm_tail(mu, sd, target)
    ci = bootstrap_ci(xs, lambda s: norm_tail(
        statistics.mean(s), statistics.stdev(s) if len(s) > 1 else 0.0, target))
    se = sd / math.sqrt(n) if n else 0.0
    return {"n": n, "mean": mu, "sd": sd, "se": se,
            "mean_ci": (mu - Z95 * se, mu + Z95 * se),
            "median": statistics.median(xs), "max": max(xs),
            # RANK ON z. P(X>=T) = Phi((mu-T)/sigma) is monotone in z, so z (or,
            # equivalently, the fitted P) is THE ordering of the objective.
            # mu + 1.645*sigma is only the 5%-GOAL LINE (it hits T exactly when
            # P = 5%); ranking by it is wrong away from that line -- it would put
            # convex0 (mu 1762/sd 249, index 2172, P .06%) ABOVE the shipped
            # default (1572/362, index 2167, P .29%), which has the 5x fatter tail.
            "z": (mu - target) / sd if sd else -math.inf,
            "index": mu + Z_P5 * sd, "p_fit": fit, "p_ci": ci,
            "p_emp": sum(1 for x in xs if x >= target) / n}


# ==========================================================================
# Commands
# ==========================================================================


def cmd_stats(a: argparse.Namespace) -> None:
    arms = load(a.dirs)
    t = a.target
    print(f"\nmetric = {a.metric}   objective = P({a.metric} >= {t:g}) = Phi(z),  "
          f"z = (mu - {t:g}) / sigma\n")
    print(f"{'arm':<24}{'n':>4}{'mean':>7}{'95% CI':>17}{'sigma':>7}{'median':>8}"
          f"{'max':>7}{'z':>7}{'fitted P':>10}{'95% CI of P':>24}")
    rows = [(k, arm_stats(v, a.metric, t)) for k, v in arms.items()]
    for k, s in sorted(rows, key=lambda kv: -kv[1]["z"]):
        lo, hi = s["p_ci"]
        print(f"{k:<24}{s['n']:>4}{s['mean']:>7.0f}"
              f"  [{s['mean_ci'][0]:>5.0f},{s['mean_ci'][1]:>5.0f}]"
              f"{s['sd']:>7.0f}{s['median']:>8.0f}{s['max']:>7.0f}{s['z']:>7.2f}"
              f"{s['p_fit'] * 100:>9.3f}%   [{lo * 100:>6.3f}%,{hi * 100:>7.3f}%]")
    print("\nRANKED BY z (= by the objective), NOT by the mean. An arm can win the")
    print("mean and lose the tail: sigma enters z as hard as mu does, and the gap")
    print(f"to the target is ~3 sigma. mu + 1.645*sigma >= {t:g} is the 5% goal line.")
    print("If two arms' CIs of P overlap, the run CANNOT rank them — add seeds.")


def cmd_compare(a: argparse.Namespace) -> None:
    arms = load(a.dirs)
    if a.base not in arms:
        raise SystemExit(f"base arm {a.base!r} not found; have {list(arms)}")
    bmap = {r["seed"]: float(r[a.metric]) for r in arms[a.base]}
    bxs = list(bmap.values())
    nb = len(bxs)
    sdb = statistics.stdev(bxs)
    print(f"\npaired vs '{a.base}'  (n={nb}, sigma={sdb:.0f})   metric={a.metric}\n")
    print(f"{'arm':<24}{'d(mean)':>9}{'95% CI':>17}{'t':>6}{'p':>8}{'wilcox':>8}"
          f"{'sigma':>7}{'sd ratio':>9}{'F p':>7}{'d(P) x':>8}")
    for k, recs in arms.items():
        if k == a.base:
            continue
        pairs = [(r["seed"], float(r[a.metric])) for r in recs if r["seed"] in bmap]
        if len(pairs) < 2:
            continue
        xs = [v for _, v in pairs]
        d = [v - bmap[s] for s, v in pairs]
        st = paired_t(d)
        w = wilcoxon_p(d)
        sd = statistics.stdev(xs)
        n = len(xs)
        fr = (sd * sd) / (sdb * sdb) if sdb else float("inf")
        fp = f_pvalue(fr, n - 1, nb - 1)
        pb = norm_tail(statistics.mean(bxs), sdb, TARGET_MOVES)
        pa = norm_tail(statistics.mean(xs), sd, TARGET_MOVES)
        ratio = pa / pb if pb > 0 else float("inf")
        print(f"{k:<24}{st['mean']:>9.0f}  [{st['ci'][0]:>6.0f},{st['ci'][1]:>6.0f}]"
              f"{st['t']:>6.2f}{st['p']:>8.3f}{w:>8.3f}{sd:>7.0f}{math.sqrt(fr):>9.2f}"
              f"{fp:>7.3f}{ratio:>8.2f}")
    mde = (Z95 + Z80) * sdb * math.sqrt(2) / math.sqrt(nb)
    print(f"\nMDE at n={nb} (independent seeds, power .8): {mde:.0f} moves. "
          f"Any |d(mean)| below that is NOT measured — it is noise.")
    print("d(P) x = the arm's fitted tail probability DIVIDED BY the base's — the")
    print("only number that ranks arms on the objective. It can be < 1 while")
    print("d(mean) > 0: that is an arm that wins the mean and LOSES the game.")


def cmd_corr(a: argparse.Namespace) -> None:
    cheap, exp = load([a.cheap]), load([a.expensive])
    names = [n for n in exp if n in cheap]
    if len(names) < 3:
        raise SystemExit(f"need >=3 shared arms, got {names}")
    truth = {m: [statistics.mean([r[a.metric] for r in exp[n]]) for n in names]
             for m in (a.metric,)}
    tv = truth[a.metric]
    ti = [statistics.mean([r[a.metric] for r in exp[n]]) +
          Z_P5 * statistics.stdev([r[a.metric] for r in exp[n]]) for n in names]
    print(f"\nPROXY VALIDATION over {len(names)} arms: {', '.join(names)}")
    print(f"expensive truth = {a.expensive}\n")
    print(f"{'cheap metric':<26}{'spearman':>9}{'kendall':>9}{'pearson':>9}"
          f"{'  vs INDEX (spearman)':>22}")
    for m in a.cheap_metrics.split(","):
        try:
            cv = [statistics.mean([r[m] for r in cheap[n] if r.get(m) is not None])
                  for n in names]
        except (KeyError, statistics.StatisticsError):
            continue
        print(f"{m:<26}{spearman(cv, tv):>9.2f}{kendall(cv, tv):>9.2f}"
              f"{pearson(cv, tv):>9.2f}{spearman(cv, ti):>22.2f}")
    order = [names[i] for i in sorted(range(len(names)), key=lambda i: -tv[i])]
    iorder = [names[i] for i in sorted(range(len(names)), key=lambda i: -ti[i])]
    print(f"\ntruth order by MEAN : {' > '.join(order)}")
    print(f"truth order by INDEX: {' > '.join(iorder)}")
    print("\nspearman < ~0.7 => the cheap screen does NOT preserve ranks. A screen")
    print("with a systematic bias gets WORSE with more seeds, not better: it")
    print("estimates the WRONG quantity ever more precisely (see `plan`).")


def cmd_plan(a: argparse.Namespace) -> None:
    sd, K, T = a.sd, a.arms, a.target
    print(f"\n=== SEEDS PER ARM (sigma={sd:.0f} moves, alpha=.05, power=.8)\n")
    print(f"{'to detect':<28}{'n/arm':>7}{'games (2 arms)':>16}{'hours @' + str(a.rate):>12}")
    for d in (400, 200, 150, 100, 50):
        n = n_for_mean(d, sd)
        print(f"{'mean shift of ' + str(d) + ' moves':<28}{n:>7}{2 * n:>16}"
              f"{2 * n / a.rate:>12.1f}")
    for r in (2.0, 1.5, 1.3, 1.2):
        n = n_for_sigma(r)
        print(f"{'sigma ratio x' + str(r):<28}{n:>7}{2 * n:>16}{2 * n / a.rate:>12.1f}")

    print(f"\n=== RANKING ON THE TAIL IS ~4x DEARER THAN RANKING ON THE MEAN")
    print("Var(z_hat) = 1/n + z^2 / (2(n-1))  (delta method).  At n=200 the sigma")
    print("term is 84% of it: sigma, not mu, is what you are paying to estimate.\n")
    z0 = -abs(a.z0)
    print(f"{'n/arm':>7}{'sd(z_hat)':>11}{'MDE on delta z':>16}"
          f"{'detectable tail ratio':>24}{'MDE on the MEAN':>18}")
    for n in (25, 50, 100, 200, 400, 800):
        s = math.sqrt(1 / n + z0 ** 2 / (2 * (n - 1)))
        m = (Z95 + Z80) * s * math.sqrt(2)
        ratio = phi(z0 + m) / phi(z0)
        print(f"{n:>7}{s:>11.3f}{m:>16.2f}{ratio:>23.1f}x"
              f"{(Z95 + Z80) * sd * math.sqrt(2 / n):>17.0f}m")
    print("\n  n=200 resolves only a ~9x difference in the tail probability.")
    print("  The measured convex0-vs-default gap (5x) needs n ~ 400 PER ARM.")

    print(f"\n=== FUNNEL: P(the truly-best arm is DROPPED at stage 1)")
    print(f"K={K} arms, the best is +{a.spread:.0f} moves; keep the top {a.keep}.")
    print("`bias` = how far the cheap proxy systematically mis-places that arm.\n")
    print(f"{'n1 (seeds in stage 1)':<24}" + "".join(f"{'bias ' + str(b):>12}"
                                                     for b in (0, 100, 215, 400)))
    for n1 in (12, 25, 50, 100, 200):
        row = f"{n1:<24}"
        for b in (0, 100, 215, 400):
            row += f"{_p_drop(K, a.keep, n1, sd, a.spread, b) * 100:>11.0f}%"
        print(row)
    print("\nread the columns, not the rows: with an UNBIASED screen more seeds")
    print("help; with a BIASED one they HURT — you converge on the wrong arm.")

    print(f"\n=== SUCCESSIVE HALVING ON SEEDS ONLY (full fidelity, no bias)\n")
    n, arms_left, spent, stages = a.n1, K, 0, []
    while arms_left > 1:
        spent += arms_left * n
        stages.append(f"{arms_left}x{n}")
        arms_left = max(1, arms_left // 2)
        n *= 2
    flat = K * (n // 2)
    print(f"  halving : {'  '.join(stages)} = {spent} games = {spent / a.rate:.1f} h"
          f"   final MDE = {(Z95 + Z80) * sd * math.sqrt(2) / math.sqrt(n // 2):.0f} moves")
    print(f"  flat    : {K}x{n // 2} = {flat} games = {flat / a.rate:.1f} h"
          f"   same final MDE")
    print(f"  saving  : x{flat / spent:.2f}")

    print(f"\n=== HOW FAR IS P(moves >= {T:g}) = 5% ?")
    print(f"  need mu + 1.645*sigma >= {T:g}")
    for mu0, sd0, tag in ((1762, 249, "convex0"), (1572, 362, "shipped default")):
        idx = mu0 + Z_P5 * sd0
        print(f"  {tag:<16} mu={mu0} sigma={sd0} -> index {idx:.0f}  "
              f"(gap {T - idx:.0f})   P={norm_tail(mu0, sd0, T) * 100:.3f}%")
        print(f"{'':<18}reach 5% by mu alone: mu={T - Z_P5 * sd0:.0f} "
              f"(+{T - Z_P5 * sd0 - mu0:.0f} moves, +{(T - Z_P5 * sd0 - mu0) / mu0 * 100:.0f}%)"
              f"  |  by sigma alone: sigma={(T - mu0) / Z_P5:.0f} "
              f"(x{(T - mu0) / Z_P5 / sd0:.1f})")


def _p_drop(K: int, keep: int, n1: int, sd: float, spread: float, bias: float,
            reps: int = 20000, seed: int = 5) -> float:
    rng = random.Random(seed)
    se = sd / math.sqrt(n1)
    bad = 0
    for _ in range(reps):
        est = [spread - bias + rng.gauss(0, se)]
        est += [rng.gauss(0, se) for _ in range(K - 1)]
        if est[0] < sorted(est, reverse=True)[keep - 1]:
            bad += 1
    return bad / reps


# ==========================================================================
# run: play the arms
# ==========================================================================


class CRNStream:
    """Per-ply common random numbers: spawn k always uses the k-th (u1,u2).

    Unbiased: floor(u1 * |E|) is uniform on the empties for u1 ~ U(0,1), and
    u2 drives the 2-vs-4 roll exactly as random.random() does. It only COUPLES
    two arms; it does not change the law of either.
    """

    __slots__ = ("_p", "_r", "k")

    def __init__(self, seed: int, prefill: int = 3000) -> None:
        self._r = random.Random(seed)
        self._p = [(self._r.random(), self._r.random()) for _ in range(prefill)]
        self.k = 0

    def _cur(self) -> tuple[float, float]:
        while self.k >= len(self._p):
            self._p.append((self._r.random(), self._r.random()))
        return self._p[self.k]

    def randrange(self, n: int) -> int:
        return min(int(self._cur()[0] * n), n - 1)

    def random(self) -> float:
        u2 = self._cur()[1]
        self.k += 1
        return u2


WINDOWS = ((0, 500), (500, 1000), (1000, 1500), (1500, 1900), (1900, 10 ** 9))
PREFIX = 1200


def play_instrumented(strategy, model, seed: int, max_moves: int, crn: bool,
                      stop_exponent: int = 11) -> dict[str, Any]:
    """One game + the per-ply statistics a cheap PROXY would be built from."""
    from bot2048.core import engine

    rng: Any = CRNStream(seed) if crn else random.Random(seed)
    board, _ = engine.new_game(model, rng)
    score = ply = 0
    end = "game_over"
    wsum = [0.0] * len(WINDOWS)
    wn = [0] * len(WINDOWS)
    pre_e = pre_lo = pre_score = 0.0
    birth: dict[int, int] = {}
    peak = 0

    while True:
        if engine.is_game_over(board):
            break
        if ply >= max_moves:
            end = "aborted"
            break
        res = engine.move(board, strategy.choose(board).direction)
        if not res.moved:
            raise ValueError("engine chose an illegal move")
        score += res.score_gained
        if engine.max_exponent(res.board) >= stop_exponent:
            board, end = res.board, "tile_cap"
            ply += 1
            break
        board, _ = engine.spawn_random(res.board, model, rng)
        e = board.count(0)
        c = board.count(RANK_1024)
        if c > peak:
            peak = c
            for k in range(1, c + 1):
                birth.setdefault(k, ply)
        for i, (lo, hi) in enumerate(WINDOWS):
            if lo <= ply < hi:
                wsum[i] += e
                wn[i] += 1
                break
        if ply < PREFIX:
            pre_e += e
            pre_lo += e <= 2
            pre_score = score
        ply += 1

    h = max(1, min(ply, PREFIX))
    return {
        "seed": seed, "moves": ply, "score": score, "end_reason": end,
        "peak_1024": peak, "final_1024": board.count(RANK_1024),
        "prefix_score": pre_score,
        "prefix_mean_empty": round(pre_e / h, 4),
        "prefix_low_empty_frac": round(pre_lo / h, 4),
        "ply_k1": birth.get(1), "ply_k2": birth.get(2),
        "ply_k3": birth.get(3), "ply_k4": birth.get(4),
        "mean_empty_w": [round(wsum[i] / wn[i], 3) if wn[i] else None
                         for i in range(len(WINDOWS))],
    }


def _run_chunk(payload: dict[str, Any]) -> dict[str, Any]:
    arm, seeds = payload["arm"], payload["seeds"]
    for key in FARM_ENV_VARS:
        v = arm["weights"].get(key)
        os.environ.pop(key, None) if v is None else os.environ.update({key: str(v)})
    os.environ["GOMAXPROCS"] = "1"

    from bot2048.ai.go_engine import GoExpectimaxStrategy
    from bot2048.core.types import SpawnModel

    strat = GoExpectimaxStrategy(depth=int(arm["depth"]), p4=float(arm["p4"]),
                                 adaptive=bool(arm.get("adaptive", False)), farm=True)
    model = SpawnModel()
    t0 = time.perf_counter()
    out = []
    try:
        for s in seeds:
            r = play_instrumented(strat, model, s, int(arm["max_moves"]), payload["crn"])
            r["arm"] = arm["name"]
            out.append(r)
    finally:
        strat.close()
    return {"arm": arm["name"], "records": out, "wall_s": time.perf_counter() - t0}


def cmd_run(a: argparse.Namespace) -> None:
    spec = json.loads(Path(a.arms).read_text())
    dflt = {"depth": 6, "p4": 0.1, "adaptive": False, "max_moves": 6000, "weights": {}}
    dflt.update(spec.get("defaults", {}))
    base, cnt = (int(x) for x in a.seeds.split(":"))
    seeds = list(range(base, base + cnt))

    arms = []
    for x in spec["arms"]:
        arm = {**dflt, **x, "weights": {**dflt.get("weights", {}), **x.get("weights", {})}}
        if a.depth:
            arm["depth"] = a.depth
        arms.append(arm)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "spec.json").write_text(json.dumps(
        {"arms": arms, "seeds": seeds, "crn": a.crn}, indent=2))

    per = a.chunk or max(1, math.ceil(len(seeds) * len(arms) / (a.workers * 2)))
    payloads = [{"arm": arm, "seeds": seeds[i:i + per], "crn": a.crn}
                for arm in arms for i in range(0, len(seeds), per)]

    t0 = time.perf_counter()
    got: dict[str, list[dict[str, Any]]] = {arm["name"]: [] for arm in arms}
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        futs = [pool.submit(_run_chunk, p) for p in payloads]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            got[r["arm"]].extend(r["records"])
            print(f"  [{i}/{len(futs)}] {r['arm']} +{len(r['records'])} "
                  f"({r['wall_s']:.0f}s)", file=sys.stderr, flush=True)
    for name, recs in got.items():
        recs.sort(key=lambda r: r["seed"])
        with (out / f"arm-{name}.jsonl").open("w") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
    print(f"\n{sum(len(v) for v in got.values())} games in "
          f"{time.perf_counter() - t0:.0f}s -> {out}")
    cmd_stats(argparse.Namespace(dirs=[str(out)], metric="moves", target=TARGET_MOVES))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="play the arms")
    r.add_argument("arms", type=Path)
    r.add_argument("--seeds", default="1000:100", help="BASE:COUNT")
    r.add_argument("--out", required=True)
    r.add_argument("--crn", action="store_true", help="per-ply common random numbers")
    r.add_argument("--depth", type=int, default=None)
    r.add_argument("--workers", type=int, default=13)
    r.add_argument("--chunk", type=int, default=None)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("stats", help="mu, sigma, fitted tail + CIs")
    s.add_argument("dirs", nargs="+")
    s.add_argument("--metric", default="moves")
    s.add_argument("--target", type=float, default=TARGET_MOVES)
    s.set_defaults(func=cmd_stats)

    c = sub.add_parser("compare", help="paired A/B: t, Wilcoxon, F on sigma")
    c.add_argument("dirs", nargs="+")
    c.add_argument("--base", required=True)
    c.add_argument("--metric", default="moves")
    c.set_defaults(func=cmd_compare)

    k = sub.add_parser("corr", help="validate a cheap proxy against expensive truth")
    k.add_argument("cheap")
    k.add_argument("expensive")
    k.add_argument("--metric", default="moves")
    k.add_argument("--cheap-metrics",
                   default="moves,score,prefix_score,prefix_mean_empty,"
                           "prefix_low_empty_frac,ply_k3,peak_1024")
    k.set_defaults(func=cmd_corr)

    p = sub.add_parser("plan", help="power + funnel design")
    p.add_argument("--sd", type=float, default=300.0)
    p.add_argument("--arms", type=int, default=12)
    p.add_argument("--keep", type=int, default=4)
    p.add_argument("--n1", type=int, default=25)
    p.add_argument("--spread", type=float, default=150.0)
    p.add_argument("--rate", type=float, default=850.0, help="games/hour")
    p.add_argument("--z0", type=float, default=3.2, help="|z| of a typical arm")
    p.add_argument("--target", type=float, default=TARGET_MOVES)
    p.set_defaults(func=cmd_plan)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
