"""Budget scheduling: spend mutation effort where the measured leverage is.

This is what replaces "pick a layer by hand" (blueprint A3 tier 1, Recommendation 3). Each
generation the engine has a fixed number of proposal slots. The scheduler splits them
across islands in proportion to

    weight(island) = (1 − ε) · opportunity_share(island) · momentum(island)  +  ε / n_islands

* ``opportunity_share`` - the island's summed Locus Opportunity Scores (causal leverage ×
  dollar share × mutability) as a fraction of the total;
* ``momentum`` - ``1 + recent improvement rate``, so islands that are producing gains get
  more budget while they are hot;
* ``ε`` - an exploration floor so no island starves (a low-leverage layer can still hide a
  cheap win, and leverage estimates themselves are noisy).

Slots are integers, so the shares are rounded with the largest-remainder method, which
preserves the total exactly.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence


def largest_remainder(weights: Mapping[str, float], total: int) -> dict[str, int]:
    names = list(weights)
    w = [max(0.0, weights[n]) if math.isfinite(weights[n]) else 0.0 for n in names]
    s = sum(w)
    if total <= 0 or not names:
        return dict.fromkeys(names, 0)
    if s <= 0:
        w = [1.0] * len(names)
        s = float(len(names))
    raw = [x / s * total for x in w]
    base = [math.floor(r) for r in raw]
    rem = total - sum(base)
    order = sorted(range(len(names)), key=lambda i: -(raw[i] - base[i]))
    for i in order[:rem]:
        base[i] += 1
    return dict(zip(names, base, strict=True))


def allocate_slots(
    opportunity: Mapping[str, float],
    total: int,
    *,
    epsilon: float = 0.2,
    momentum: Mapping[str, float] | None = None,
    minimum: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Split ``total`` proposal slots across islands."""
    names = list(opportunity)
    if not names:
        return {}
    opp_total = sum(max(0.0, v) for v in opportunity.values())
    weights = {}
    for n in names:
        share = (max(0.0, opportunity[n]) / opp_total) if opp_total > 0 else 1.0 / len(names)
        mom = 1.0 + max(0.0, (momentum or {}).get(n, 0.0))
        weights[n] = (1 - epsilon) * share * mom + epsilon / len(names)
    minimum = minimum or {}
    reserved = sum(minimum.values())
    alloc = largest_remainder(weights, max(0, total - reserved))
    for n, m in minimum.items():
        alloc[n] = alloc.get(n, 0) + m
    return alloc


def pick_locus(loci: Sequence[str], opportunity: Mapping[str, float], rng: random.Random, epsilon: float = 0.25) -> str:
    """Choose a locus inside an island: ε-uniform, otherwise ∝ opportunity."""
    if not loci:
        raise ValueError("no loci")
    if rng.random() < epsilon:
        return rng.choice(list(loci))
    weights = [max(opportunity.get(locus, 0.0), 1e-6) for locus in loci]
    return rng.choices(list(loci), weights=weights, k=1)[0]
