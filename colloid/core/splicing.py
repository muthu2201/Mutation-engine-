"""Splicing: cross-niche recombination driven by measured epistasis (blueprint A5, T11).

The Composition Island assembles cross-layer genomes from per-region winners:

1. **Pool** - the pruned genes of per-region elites (at most one gene per locus so every
   union is conflict-free).
2. **Screen** - a two-level *fractional factorial* design: rows are sub-genomes, columns are
   genes (+1 include / −1 exclude). We use Plackett–Burman designs (Sylvester-Hadamard for
   powers of two, Paley construction for 12/20/24 runs). A PB design is Resolution III
   (main effects aliased with two-factor interactions); its **fold-over** (append the
   negated rows) is Resolution IV: main effects are then clear of two-factor interactions.
   With 11 candidate genes that is 24 runs instead of 2^11 = 2048.
3. **Fit** - an additive-plus-sparse-pairwise surrogate
   ``y = Σ β_i x_i + Σ_{(i,j)∈P} γ_ij x_i x_j`` by weighted ridge regression. Main effects
   are lightly shrunk; each interaction is shrunk by ``λ / prior_ij`` so pairs the Atlas
   says are likely to interact (shared path / shared resource) are allowed larger
   coefficients. Observations are weighted by measurement precision ``1/se²``.
4. **Predict** - enumerate (or greedily search) unions to maximise predicted gain minus a
   parsimony penalty, and return the top candidates for real evaluation.
5. Measured pair effects feed back into the epistasis matrix; negative ones are handed to
   the LLM "integration" template to reconcile.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from colloid.core.attribution import Subset
from colloid.core.models import Measured

# ------------------------------------------------------------------------ designs


def _sylvester(n: int) -> np.ndarray:
    h = np.array([[1]], dtype=int)
    while h.shape[0] < n:
        h = np.block([[h, h], [h, -h]])
    return h


def _paley(n: int) -> np.ndarray:
    """Paley-I Hadamard matrix of order n where q = n-1 is a prime ≡ 3 (mod 4)."""
    q = n - 1
    residues = {(i * i) % q for i in range(1, q)}
    chi = [0] + [1 if i in residues else -1 for i in range(1, q)]
    jac = np.array([[chi[(j - i) % q] for j in range(q)] for i in range(q)], dtype=int)
    s = np.zeros((n, n), dtype=int)
    s[0, 1:] = 1
    s[1:, 0] = -1
    s[1:, 1:] = jac
    return s + np.eye(n, dtype=int)


_ORDERS = (4, 8, 12, 16, 20, 24, 32, 64)


def hadamard(n: int) -> np.ndarray:
    if n & (n - 1) == 0:
        return _sylvester(n)
    if n in (12, 20, 24):
        return _paley(n)
    raise ValueError(f"no Hadamard construction for order {n}")


def plackett_burman(k: int) -> np.ndarray:
    """A PB design with at least k factor columns, values in {-1, +1}. Rows = runs."""
    for n in _ORDERS:
        if n - 1 >= k:
            h = hadamard(n)
            # normalise so the first column is all +1, then drop it
            h = h * h[:, [0]]
            pb: np.ndarray = h[:, 1 : k + 1]
            return pb
    raise ValueError(f"too many factors for screening: {k}")


def screening_design(k: int, *, foldover: bool = True, max_runs: int | None = None) -> np.ndarray:
    """Resolution-IV (fold-over PB) design when it fits ``max_runs``; else Resolution III."""
    base = plackett_burman(k)
    design = np.vstack([base, -base]) if foldover else base
    if max_runs is not None and design.shape[0] > max_runs:
        design = base
    # unique rows only
    _, idx = np.unique(design, axis=0, return_index=True)
    return design[np.sort(idx)]


def design_subsets(genes: Sequence[str], design: np.ndarray) -> list[Subset]:
    out: list[Subset] = []
    seen: set[Subset] = set()
    for row in design:
        s = frozenset(g for g, x in zip(genes, row, strict=True) if x > 0)
        if s not in seen:
            seen.add(s)
            out.append(s)
    full = frozenset(genes)
    if full not in seen:
        out.append(full)
    return out


# ------------------------------------------------------------------------ surrogate


@dataclass
class SpliceModel:
    genes: tuple[str, ...]
    pairs: tuple[tuple[str, str], ...]
    beta: np.ndarray  # main effects then interactions

    def features(self, subset: Subset) -> np.ndarray:
        x = [1.0 if g in subset else 0.0 for g in self.genes]
        x += [1.0 if (a in subset and b in subset) else 0.0 for a, b in self.pairs]
        return np.asarray(x)

    def predict(self, subset: Subset) -> float:
        return float(self.features(subset) @ self.beta)

    def main_effects(self) -> dict[str, float]:
        return {g: float(self.beta[i]) for i, g in enumerate(self.genes)}

    def interactions(self) -> dict[tuple[str, str], float]:
        k = len(self.genes)
        return {p: float(self.beta[k + i]) for i, p in enumerate(self.pairs)}


def fit_splice_model(
    genes: Sequence[str],
    observations: Mapping[Subset, Measured],
    prior: Mapping[tuple[str, str], float],
    *,
    lam_main: float = 1e-3,
    lam_int: float = 0.05,
    max_pairs: int | None = None,
) -> SpliceModel:
    """Weighted ridge fit of the additive + sparse pairwise model (no intercept: the empty
    genome is the baseline, whose gain is 0 by definition)."""
    genes = tuple(genes)
    candidate_pairs = sorted(itertools.combinations(genes, 2), key=lambda p: -prior.get(p, prior.get((p[1], p[0]), 0.1)))
    n_obs = len(observations) + 1
    if max_pairs is None:
        max_pairs = max(0, n_obs - len(genes) - 1)
    pairs = tuple(candidate_pairs[:max_pairs])
    model = SpliceModel(genes, pairs, np.zeros(len(genes) + len(pairs)))
    rows, ys, ws = [model.features(frozenset())], [0.0], [1e4]
    for s, m in observations.items():
        rows.append(model.features(s))
        ys.append(m.value)
        ws.append(1.0 / max(m.se, 1e-3) ** 2)
    x = np.vstack(rows)
    y = np.asarray(ys)
    w = np.asarray(ws)
    w = w / w.mean()
    pen = [lam_main] * len(genes) + [lam_int / max(prior.get(p, prior.get((p[1], p[0]), 0.1)), 1e-3) for p in pairs]
    xtw = x.T * w
    a = xtw @ x + np.diag(pen)
    b = xtw @ y
    model.beta = np.linalg.solve(a, b)
    return model


def best_unions(
    model: SpliceModel, k: int, *, parsimony: float = 0.002, exclude: set[Subset] | None = None, rng: random.Random | None = None
) -> list[tuple[Subset, float]]:
    """Top-``k`` unions by predicted gain − parsimony·|S|. Exhaustive for ≤ 16 genes,
    otherwise greedy forward selection plus random-restart local search."""
    exclude = exclude or set()
    genes = model.genes
    scored: dict[Subset, float] = {}
    if len(genes) <= 16:
        n = len(genes)
        masks = np.arange(1, 2**n, dtype=np.int64)
        bits = ((masks[:, None] >> np.arange(n)) & 1).astype(np.float64)
        inter = []
        idx = {g: i for i, g in enumerate(genes)}
        for a, b in model.pairs:
            inter.append(bits[:, idx[a]] * bits[:, idx[b]])
        feats = np.hstack([bits, np.column_stack(inter)]) if inter else bits
        preds = feats @ model.beta - parsimony * bits.sum(axis=1)
        order = np.argsort(-preds)
        for o in order:
            s = frozenset(g for i, g in enumerate(genes) if bits[o, i] > 0)
            if s in exclude:
                continue
            scored[s] = float(preds[o])
            if len(scored) >= k:
                break
        return sorted(scored.items(), key=lambda kv: -kv[1])
    rng = rng or random.Random(0)

    def score(s: Subset) -> float:
        return model.predict(s) - parsimony * len(s)

    def local_search(start: Subset) -> Subset:
        cur, cur_v = start, score(start)
        improved = True
        while improved:
            improved = False
            for g in genes:
                nxt = cur ^ {g}
                if nxt and score(nxt) > cur_v + 1e-12:
                    cur, cur_v, improved = frozenset(nxt), score(nxt), True
        return cur

    starts = [frozenset([g]) for g in genes] + [frozenset(rng.sample(list(genes), rng.randint(1, len(genes)))) for _ in range(32)]
    for st in starts:
        s = local_search(st)
        if s not in exclude:
            scored[s] = score(s)
    return sorted(scored.items(), key=lambda kv: -kv[1])[:k]


def hypervolume_2d(points: Sequence[tuple[float, float]], ref: tuple[float, float] = (0.0, 0.0)) -> float:
    """Dominated hypervolume for 2 maximised objectives relative to ``ref``."""
    pts = sorted((p for p in points if p[0] > ref[0] and p[1] > ref[1]), key=lambda p: -p[0])
    hv, best_y = 0.0, ref[1]
    for x, y in pts:
        if y > best_y:
            hv += (x - ref[0]) * (y - best_y)
            best_y = y
    return hv


def expected_union_value(singles: Mapping[str, Measured], subset: Subset) -> float:
    """Additive prediction from single-gene effects (the null model splicing must beat)."""
    return sum(singles[g].value for g in subset if g in singles)


def is_finite_measure(m: Measured) -> bool:
    return math.isfinite(m.value) and math.isfinite(m.se)
