"""Attribution: who earned the gain? (blueprint A3)

Three tiers, from cheap to rigorous:

1. **Prior leverage** lives in :mod:`colloid.core.atlas` (Locus Opportunity Score).

2. **Lineage credit** (:class:`LineageLedger`). Every child is ``parent + Δgenes`` and is
   measured *paired* against its parent, so ``Δobjective`` with a CI is available for free.
   The ledger credits that Δ to the loci that changed and to the ``(operator, model,
   template)`` arm that produced them; the bandit consumes these rewards.

3. **Shapley values and interactions** for elites. Treat the elite genome's genes as players
   and ``v(S)`` = measured log-ratio gain of the sub-genome ``S`` versus baseline. The
   Shapley value of a gene is its average marginal contribution over all orderings; it is
   the unique attribution that is efficient (values sum to ``v(all)``), symmetric and
   additive. Genes with Shapley value ≈ 0 or negative are *bloat* and are pruned.

   The core never runs experiments. It *plans* which subsets must be measured
   (:func:`shapley_plan`) and *estimates* from measured values (:func:`shapley_exact`,
   :func:`shapley_permutation`); the engine performs the measurements in between.

   Measurement noise is propagated: each ``v(S)`` comes with a standard error, and the
   Shapley value is a linear combination of ``v(S)`` values, so its variance is the
   weighted sum of variances (independent measurements).

**Epistasis.** With effects as log-ratios, ``ε_ij = e_ij − e_i − e_j``. Positive ε means
synergy, negative means interference. The pairwise Shapley interaction index generalises
this to the context of the other genes.
"""

from __future__ import annotations

import itertools
import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from colloid.core.stats import normal_ci

Subset = frozenset[str]


@dataclass(frozen=True)
class Measured:
    value: float  # log-ratio gain vs baseline (positive = better)
    se: float


@dataclass(frozen=True)
class GeneCredit:
    gene_id: str
    value: float
    ci_lo: float
    ci_hi: float
    method: str


# ------------------------------------------------------------------------ lineage


@dataclass
class LineageLedger:
    """Accumulates paired child-vs-parent gains per locus and per arm."""

    by_locus: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    by_arm: dict[tuple[str, str | None, str | None], list[float]] = field(default_factory=lambda: defaultdict(list))
    records: list[dict[str, object]] = field(default_factory=list)

    def record(
        self,
        child_id: str,
        parent_id: str,
        changed_loci: Sequence[str],
        arm: tuple[str, str | None, str | None],
        log_ratio: float | None,
        ci: tuple[float, float] | None,
        passed: bool,
    ) -> None:
        gain = log_ratio if (passed and log_ratio is not None and math.isfinite(log_ratio)) else 0.0
        share = gain / max(len(changed_loci), 1)
        for loc in changed_loci:
            self.by_locus[loc].append(share)
        self.by_arm[arm].append(gain)
        self.records.append(
            {"child": child_id, "parent": parent_id, "loci": list(changed_loci), "arm": list(arm), "gain": gain, "ci": ci, "passed": passed}
        )

    def locus_credit(self) -> dict[str, float]:
        return {k: sum(v) / len(v) for k, v in self.by_locus.items() if v}

    def arm_credit(self) -> dict[tuple[str, str | None, str | None], tuple[int, float]]:
        return {k: (len(v), sum(v) / len(v)) for k, v in self.by_arm.items() if v}


# ------------------------------------------------------------------------ Shapley


def shapley_plan(genes: Sequence[str], budget_subsets: int, rng: random.Random) -> tuple[list[Subset], list[tuple[str, ...]]]:
    """Decide which subsets to measure.

    If all ``2^k`` subsets fit in the budget, return them all (exact Shapley). Otherwise
    sample permutations and return the union of their prefixes; with ``M`` permutations
    that is at most ``M·k + 1`` subsets (fewer, because prefixes are shared and memoised).
    Returns ``(subsets, permutations)``; ``permutations`` is empty in the exact case.
    """
    k = len(genes)
    if k == 0:
        return [frozenset()], []
    if 2**k <= budget_subsets:
        subsets = [frozenset(c) for r in range(k + 1) for c in itertools.combinations(genes, r)]
        return subsets, []
    perms: list[tuple[str, ...]] = []
    subsets_set: set[Subset] = {frozenset()}
    while True:
        perm = tuple(rng.sample(list(genes), k))
        new = {frozenset(perm[:i]) for i in range(1, k + 1)}
        if len(subsets_set | new) > budget_subsets and perms:
            break
        perms.append(perm)
        subsets_set |= new
        if len(perms) >= 64:
            break
    return sorted(subsets_set, key=lambda s: (len(s), sorted(s))), perms


def _value(values: Mapping[Subset, Measured], s: Subset) -> Measured:
    if not s:
        return values.get(s, Measured(0.0, 0.0))
    return values[s]


def shapley_exact(genes: Sequence[str], values: Mapping[Subset, Measured]) -> dict[str, GeneCredit]:
    """Exact Shapley values from all ``2^k`` measured subsets, with propagated CIs."""
    n = len(genes)
    out: dict[str, GeneCredit] = {}
    fact = [math.factorial(i) for i in range(n + 1)]
    for g in genes:
        others = [x for x in genes if x != g]
        coef: dict[Subset, float] = defaultdict(float)
        for r in range(len(others) + 1):
            w = fact[r] * fact[n - r - 1] / fact[n]
            for combo in itertools.combinations(others, r):
                s = frozenset(combo)
                coef[s | {g}] += w
                coef[s] -= w
        phi = sum(c * _value(values, s).value for s, c in coef.items())
        var = sum(c * c * _value(values, s).se ** 2 for s, c in coef.items())
        lo, hi = normal_ci(phi, math.sqrt(var))
        out[g] = GeneCredit(g, phi, lo, hi, "shapley_exact")
    return out


def shapley_permutation(
    genes: Sequence[str], values: Mapping[Subset, Measured], permutations: Sequence[Sequence[str]]
) -> dict[str, GeneCredit]:
    """Monte-Carlo Shapley from permutation prefixes. The CI combines the sampling
    variance across permutations with the measurement noise of the subset values."""
    contrib: dict[str, list[float]] = defaultdict(list)
    noise: dict[str, list[float]] = defaultdict(list)
    for perm in permutations:
        prefix: Subset = frozenset()
        for g in perm:
            nxt = prefix | {g}
            a, b = _value(values, nxt), _value(values, prefix)
            contrib[g].append(a.value - b.value)
            noise[g].append(a.se**2 + b.se**2)
            prefix = nxt
    out = {}
    for g in genes:
        xs = contrib.get(g, [])
        if not xs:
            out[g] = GeneCredit(g, 0.0, -math.inf, math.inf, "shapley_permutation")
            continue
        m = len(xs)
        mean = sum(xs) / m
        samp_var = sum((x - mean) ** 2 for x in xs) / (m - 1) / m if m > 1 else 0.0
        meas_var = sum(noise[g]) / m / m
        lo, hi = normal_ci(mean, math.sqrt(samp_var + meas_var))
        out[g] = GeneCredit(g, mean, lo, hi, "shapley_permutation")
    return out


def shapley_interaction(genes: Sequence[str], values: Mapping[Subset, Measured], i: str, j: str) -> tuple[float, float]:
    """Pairwise Shapley interaction index I_ij (exact; needs all subsets). Returns (value, se)."""
    n = len(genes)
    others = [x for x in genes if x not in (i, j)]
    total, var = 0.0, 0.0
    for r in range(len(others) + 1):
        w = math.factorial(r) * math.factorial(n - r - 2) / math.factorial(n - 1)
        for combo in itertools.combinations(others, r):
            s = frozenset(combo)
            terms = [(1, s | {i, j}), (-1, s | {i}), (-1, s | {j}), (1, s)]
            for sign, sub in terms:
                m = _value(values, sub)
                total += w * sign * m.value
                var += (w**2) * m.se**2
    return total, math.sqrt(var)


def prune(credits: Mapping[str, GeneCredit], *, min_effect: float = 0.0) -> tuple[list[str], list[str]]:
    """Blueprint T10 pruning rule: drop genes whose Shapley CI includes values ≤ 0
    (``ci_lo <= min_effect``). Returns ``(keep, drop)``. The caller must re-verify the
    pruned genome, because a noisy CI can hide a small real effect."""
    keep, drop = [], []
    for g, c in credits.items():
        (drop if c.ci_lo <= min_effect else keep).append(g)
    return sorted(keep), sorted(drop)


# ------------------------------------------------------------------------ epistasis


@dataclass(frozen=True)
class Epistasis:
    a: str
    b: str
    epsilon: float
    se: float

    @property
    def ci(self) -> tuple[float, float]:
        return normal_ci(self.epsilon, self.se)

    @property
    def significant(self) -> bool:
        lo, hi = self.ci
        return lo > 0 or hi < 0


def epistasis(single_a: Measured, single_b: Measured, pair: Measured, a: str, b: str) -> Epistasis:
    eps = pair.value - single_a.value - single_b.value
    se = math.sqrt(pair.se**2 + single_a.se**2 + single_b.se**2)
    return Epistasis(a, b, eps, se)


def epistasis_pairs_to_test(
    genes: Sequence[str], prior: Mapping[tuple[str, str], float], budget: int
) -> list[tuple[str, str]]:
    """Order candidate pairs by Atlas prior (shared path/resource first) and take the
    top ``budget``. Turns quadratic interaction testing into a sparse, graph-guided one."""
    pairs = [(a, b) for a, b in itertools.combinations(sorted(genes), 2)]
    pairs.sort(key=lambda p: -prior.get(p, prior.get((p[1], p[0]), 0.1)))
    return pairs[:budget]


def all_measured(values: Mapping[Subset, Measured], subsets: Iterable[Subset]) -> bool:
    return all((not s) or s in values for s in subsets)
