"""Statistics for noisy performance measurement.

Every performance claim Colloid makes is a *log-ratio with a confidence interval*, never a
single number. This module provides the pieces the evaluator protocol uses:

* :func:`stratified_bootstrap_log_ratio` - effect size and CI of "candidate vs reference"
  for a statistic (median, p99, ...) where samples come in *strata* (benchmark phases). We
  resample within each stratum so that between-phase drift (thermal state, page cache...)
  is preserved in the resampled data rather than averaged away; this keeps CIs honest
  under ABAB interleaving.
* :func:`mann_whitney_p` - rank test on window-level values (distribution-free).
* :func:`benjamini_hochberg` - false-discovery-rate control. An evolutionary run makes
  thousands of comparisons; without FDR control a 5% test would "find" improvements in pure
  noise all day long.
* :func:`mser_truncation` - warm-up detection with MSER-5 (White 1997), the standard
  simulation-output-analysis rule: choose the truncation point that minimises the marginal
  standard error of what remains. We detect steady state instead of assuming it.
* :func:`steady` - a cheap online check used while a warm-up is still running.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats as sps

LN = math.log


@dataclass(frozen=True)
class Effect:
    log_ratio: float
    ci_lo: float
    ci_hi: float
    p_value: float
    n_candidate: int
    n_reference: int


def _as_groups(groups: Sequence[Sequence[float]]) -> list[np.ndarray]:
    out = [np.asarray(g, dtype=np.float64) for g in groups]
    return [g[np.isfinite(g)] for g in out if len(g)]


def _stat(values: np.ndarray, statistic: str, axis: int | None = None) -> np.ndarray:
    if statistic == "median":
        return np.asarray(np.median(values, axis=axis))
    if statistic == "mean":
        return np.asarray(np.mean(values, axis=axis))
    if statistic.startswith("p"):
        q = float(statistic[1:]) / 100.0
        return np.asarray(np.quantile(values, q, axis=axis))
    raise ValueError(f"unknown statistic {statistic}")


def pooled_statistic(groups: Sequence[Sequence[float]], statistic: str) -> float:
    gs = _as_groups(groups)
    if not gs:
        return float("nan")
    return float(_stat(np.concatenate(gs), statistic))


def _bootstrap_stat(groups: list[np.ndarray], statistic: str, n_resamples: int, rng: np.random.Generator) -> np.ndarray:
    """Bootstrap distribution of the pooled statistic, resampling within each group."""
    total = sum(len(g) for g in groups)
    # Chunk to bound memory: (chunk × total) float64 matrix.
    chunk = max(1, min(n_resamples, int(4_000_000 // max(total, 1))))
    out = np.empty(n_resamples, dtype=np.float64)
    done = 0
    while done < n_resamples:
        m = min(chunk, n_resamples - done)
        parts = []
        for g in groups:
            idx = rng.integers(0, len(g), size=(m, len(g)))
            parts.append(g[idx])
        mat = np.concatenate(parts, axis=1)
        out[done : done + m] = _stat(mat, statistic, axis=1)
        done += m
    return out


def stratified_bootstrap_log_ratio(
    reference: Sequence[Sequence[float]],
    candidate: Sequence[Sequence[float]],
    statistic: str = "median",
    *,
    n_resamples: int = 4000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Effect:
    """Effect of ``candidate`` versus ``reference`` on a minimised metric.

    ``log_ratio = log(stat(reference) / stat(candidate))`` so positive means the candidate
    is better (smaller). Each argument is a list of strata (one per benchmark phase).
    The p-value is the two-sided bootstrap p for "log ratio = 0".
    """
    ref, cand = _as_groups(reference), _as_groups(candidate)
    n_ref, n_cand = sum(map(len, ref)), sum(map(len, cand))
    if not ref or not cand:
        return Effect(float("nan"), float("nan"), float("nan"), 1.0, n_cand, n_ref)
    point_ref = float(_stat(np.concatenate(ref), statistic))
    point_cand = float(_stat(np.concatenate(cand), statistic))
    if point_ref <= 0 or point_cand <= 0:
        return Effect(float("nan"), float("nan"), float("nan"), 1.0, n_cand, n_ref)
    rng = np.random.default_rng(seed)
    b_ref = _bootstrap_stat(ref, statistic, n_resamples, rng)
    b_cand = _bootstrap_stat(cand, statistic, n_resamples, rng)
    valid = (b_ref > 0) & (b_cand > 0)
    lr = np.log(b_ref[valid] / b_cand[valid])
    point = LN(point_ref / point_cand)
    if len(lr) < 10:
        return Effect(point, float("-inf"), float("inf"), 1.0, n_cand, n_ref)
    lo, hi = np.quantile(lr, [alpha / 2, 1 - alpha / 2])
    # Two-sided bootstrap p-value for H0: log ratio = 0, using the shifted (null) distribution.
    centred = lr - lr.mean()
    p = float(np.mean(np.abs(centred) >= abs(point)))
    p = max(p, 1.0 / (len(lr) + 1))
    return Effect(point, float(lo), float(hi), min(1.0, p), n_cand, n_ref)


def bootstrap_ci(
    values: Sequence[float], fn: Callable[[np.ndarray], float], *, n_resamples: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float, float]:
    """Percentile bootstrap CI for an arbitrary statistic of one sample."""
    arr = np.asarray(values, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    point = float(fn(arr))
    if len(arr) == 1:
        return point, point, point
    idx = rng.integers(0, len(arr), size=(n_resamples, len(arr)))
    boots = np.array([fn(arr[i]) for i in idx])
    lo, hi = np.quantile(boots, [alpha / 2, 1 - alpha / 2])
    return point, float(lo), float(hi)


def mann_whitney_p(a: Sequence[float], b: Sequence[float]) -> float:
    a_arr, b_arr = np.asarray(a, float), np.asarray(b, float)
    if len(a_arr) < 2 or len(b_arr) < 2:
        return 1.0
    if np.all(a_arr == a_arr[0]) and np.all(b_arr == b_arr[0]) and a_arr[0] == b_arr[0]:
        return 1.0
    return float(sps.mannwhitneyu(a_arr, b_arr, alternative="two-sided").pvalue)


def benjamini_hochberg(p_values: Sequence[float], q: float = 0.05) -> tuple[list[bool], list[float]]:
    """Benjamini–Hochberg step-up procedure.

    Returns ``(rejected, adjusted_p)`` in the input order. ``rejected[i]`` means hypothesis
    *i* is declared a discovery at FDR level ``q``.
    """
    m = len(p_values)
    if m == 0:
        return [], []
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        val = min(1.0, p_values[i] * m / rank)
        running_min = min(running_min, val)
        adjusted[i] = running_min
    rejected = [adjusted[i] <= q for i in range(m)]
    return rejected, adjusted


def mser_truncation(series: Sequence[float], batch: int = 5) -> int:
    """MSER-k warm-up truncation point (index into ``series``).

    Batches the series into means of ``batch`` observations, then picks the truncation d
    (in batches) minimising ``var(tail) / len(tail)``, restricted to the first half so the
    estimate keeps enough data. Returns the index of the first steady-state observation.
    """
    x = np.asarray(series, dtype=np.float64)
    nb = len(x) // batch
    if nb < 4:
        return 0
    means = x[: nb * batch].reshape(nb, batch).mean(axis=1)
    best_d, best = 0, math.inf
    for d in range(0, nb // 2 + 1):
        tail = means[d:]
        if len(tail) < 2:
            break
        score = float(np.var(tail, ddof=1) / len(tail))
        if score < best:
            best, best_d = score, d
    return best_d * batch


def steady(window_values: Sequence[float], k: int = 3, tol: float = 0.08) -> bool:
    """Online steady-state check: the last ``k`` window values lie within ``tol`` relative
    spread of their median *and* show no monotone trend."""
    if len(window_values) < k:
        return False
    tail = np.asarray(window_values[-k:], dtype=np.float64)
    if not np.all(np.isfinite(tail)):
        return False
    med = float(np.median(tail))
    if med <= 0:
        return False
    spread = float((tail.max() - tail.min()) / med)
    diffs = np.diff(tail)
    monotone = bool(np.all(diffs > 0) or np.all(diffs < 0)) and spread > tol / 2
    return spread <= tol and not monotone


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) < 3:
        return float("nan")
    av, bv = np.asarray(a, float), np.asarray(b, float)
    # Spearman is undefined when either input is constant (no ranks to correlate).
    if np.ptp(av) == 0 or np.ptp(bv) == 0:
        return float("nan")
    res = sps.spearmanr(av, bv)
    return float(res.statistic) if np.isfinite(res.statistic) else float("nan")


def coefficient_of_variation(values: Sequence[float]) -> float:
    arr = np.asarray(values, dtype=np.float64)
    if len(arr) < 2 or arr.mean() == 0:
        return float("nan")
    return float(arr.std(ddof=1) / arr.mean())


def combine_effects_se(ci_lo: float, ci_hi: float) -> float:
    """Approximate standard error from a 95% CI (normal approximation)."""
    if not (math.isfinite(ci_lo) and math.isfinite(ci_hi)):
        return math.inf
    return (ci_hi - ci_lo) / (2 * 1.959964)


def normal_ci(value: float, se: float, alpha: float = 0.05) -> tuple[float, float]:
    z = float(sps.norm.ppf(1 - alpha / 2))
    return value - z * se, value + z * se


def normal_p(value: float, se: float) -> float:
    if se <= 0 or not math.isfinite(se):
        return 1.0
    return float(2 * sps.norm.sf(abs(value) / se))


def seeded(seed: int) -> random.Random:
    return random.Random(seed)


def stratified_bootstrap_ci(
    groups: Sequence[Sequence[float]], statistic: str = "median", *, n_resamples: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float, float, int]:
    """Point estimate and percentile CI of a pooled statistic, resampling within strata.
    Returns ``(point, lo, hi, n)``."""
    gs = _as_groups(groups)
    n = sum(len(g) for g in gs)
    if not gs:
        return float("nan"), float("nan"), float("nan"), 0
    point = float(_stat(np.concatenate(gs), statistic))
    boots = _bootstrap_stat(gs, statistic, n_resamples, np.random.default_rng(seed))
    lo, hi = np.quantile(boots, [alpha / 2, 1 - alpha / 2])
    return point, float(lo), float(hi), n


def paired_ratio_effect(
    reference: Sequence[float], candidate: Sequence[float], *, n_resamples: int = 4000, alpha: float = 0.05, seed: int = 0
) -> Effect:
    """Effect from *paired* totals (e.g. CPU per benchmark chunk, where chunk k of the
    reference and chunk k of the candidate replayed exactly the same requests).

    ``log_ratio = log(Σ reference / Σ candidate)`` (positive = candidate better). The CI
    resamples *pairs*, so the large chunk-to-chunk variation of the request mix cancels out
    and only execution differences remain. The p-value is an exact (or Monte-Carlo)
    sign-flip permutation test on the per-pair log ratios - valid with few pairs and no
    distributional assumptions.
    """
    ref = np.asarray(reference, dtype=np.float64)
    cand = np.asarray(candidate, dtype=np.float64)
    ok = np.isfinite(ref) & np.isfinite(cand) & (ref > 0) & (cand > 0)
    ref, cand = ref[ok], cand[ok]
    n = len(ref)
    if n == 0:
        return Effect(float("nan"), float("nan"), float("nan"), 1.0, 0, 0)
    point = float(np.log(ref.sum() / cand.sum()))
    if n == 1:
        return Effect(point, float("-inf"), float("inf"), 1.0, 1, 1)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_resamples, n))
    boots = np.log(ref[idx].sum(axis=1) / cand[idx].sum(axis=1))
    lo, hi = np.quantile(boots, [alpha / 2, 1 - alpha / 2])
    d = np.log(ref / cand)
    observed = abs(d.mean())
    if n <= 16:
        signs = _sign_matrix(n)
    else:
        signs = rng.choice([-1.0, 1.0], size=(20000, n))
    perm = np.abs((signs * d).mean(axis=1))
    p = float(np.mean(perm >= observed - 1e-15))
    return Effect(point, float(lo), float(hi), max(p, 1.0 / len(signs)), n, n)


def _sign_matrix(n: int) -> np.ndarray:
    """All 2^n sign vectors (exact permutation distribution for the sign-flip test)."""
    import itertools

    return np.array(list(itertools.product((-1.0, 1.0), repeat=n)), dtype=np.float64)


def paired_quantile_effect(
    reference: Sequence[Sequence[float]],
    candidate: Sequence[Sequence[float]],
    statistic: str = "median",
    *,
    n_resamples: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Effect:
    """Effect on a latency quantile with *request-level pairing*.

    ``reference[s][i]`` and ``candidate[s][i]`` are the latencies of the same request ``i``
    of stratum (cycle) ``s``. The bootstrap draws one set of request indices per stratum
    and applies it to both arms, so both resamples contain exactly the same requests - the
    variance from *which* requests were drawn cancels.
    """
    refs = [np.asarray(r, dtype=np.float64) for r in reference]
    cands = [np.asarray(c, dtype=np.float64) for c in candidate]
    pairs = [(r, c) for r, c in zip(refs, cands, strict=True) if len(r) == len(c) and len(r) > 0]
    if not pairs:
        return Effect(float("nan"), float("nan"), float("nan"), 1.0, 0, 0)
    ref_all = np.concatenate([r for r, _ in pairs])
    cand_all = np.concatenate([c for _, c in pairs])
    pr, pc = float(_stat(ref_all, statistic)), float(_stat(cand_all, statistic))
    if pr <= 0 or pc <= 0:
        return Effect(float("nan"), float("nan"), float("nan"), 1.0, len(cand_all), len(ref_all))
    point = float(np.log(pr / pc))
    rng = np.random.default_rng(seed)
    total = len(ref_all)
    chunk = max(1, min(n_resamples, int(2_000_000 // max(total, 1))))
    boots = np.empty(n_resamples)
    done = 0
    while done < n_resamples:
        m = min(chunk, n_resamples - done)
        rs, cs = [], []
        for r, c in pairs:
            idx = rng.integers(0, len(r), size=(m, len(r)))
            rs.append(r[idx])
            cs.append(c[idx])
        rq = _stat(np.concatenate(rs, axis=1), statistic, axis=1)
        cq = _stat(np.concatenate(cs, axis=1), statistic, axis=1)
        boots[done : done + m] = np.log(rq / cq)
        done += m
    lo, hi = np.quantile(boots, [alpha / 2, 1 - alpha / 2])
    centred = boots - boots.mean()
    p = float(np.mean(np.abs(centred) >= abs(point)))
    return Effect(point, float(lo), float(hi), max(p, 1.0 / (n_resamples + 1)), len(cand_all), len(ref_all))
