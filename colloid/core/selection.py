"""Selection: confidence-aware Pareto dominance, NSGA-II and NSGA-III.

**Confidence-aware dominance** (blueprint A6). Noise is the enemy: with ordinary dominance a
program that is 0.4% faster on a 3%-noise benchmark "dominates" its parent and pushes out
real winners. We compare *intervals* instead:

* A is *clearly better* than B on objective k when ``lo_A[k] > hi_B[k]``;
* A is *clearly worse* when ``hi_A[k] < lo_B[k]``;
* A dominates B iff A is clearly better on at least one objective and clearly worse on
  none.

Overlapping intervals are ties. This promotes only differences the measurement can resolve.

**NSGA-II** (Deb et al. 2002): fast non-dominated sorting into fronts, then crowding distance
within a front to prefer spread-out solutions. Used inside islands (≤ 3 objectives).

**NSGA-III** (Deb & Jain 2014): replaces crowding with association to a structured set of
reference directions (Das–Dennis points on the unit simplex) and niche-preserving
selection, which keeps diversity when there are many objectives. Used by the Composition
Island where every layer's objectives meet.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from typing import TypeVar

import numpy as np

from colloid.core.objectives import Fitness

T = TypeVar("T")


def confident_dominates(a: Fitness, b: Fitness, names: Sequence[str]) -> bool:
    better = False
    for n in names:
        if a.hi[n] < b.lo[n]:
            return False
        if a.lo[n] > b.hi[n]:
            better = True
    return better


def point_dominates(a: Sequence[float], b: Sequence[float]) -> bool:
    """Classical dominance for maximisation."""
    better = False
    for x, y in zip(a, b, strict=True):
        if x < y:
            return False
        if x > y:
            better = True
    return better


def fast_non_dominated_sort(n: int, dominates: Callable[[int, int], bool]) -> list[list[int]]:
    """Deb's O(MN²) algorithm. Returns fronts of indices, best first."""
    dominated_by: list[list[int]] = [[] for _ in range(n)]
    counts = [0] * n
    fronts: list[list[int]] = [[]]
    for p in range(n):
        for q in range(n):
            if p == q:
                continue
            if dominates(p, q):
                dominated_by[p].append(q)
            elif dominates(q, p):
                counts[p] += 1
        if counts[p] == 0:
            fronts[0].append(p)
    i = 0
    while fronts[i]:
        nxt: list[int] = []
        for p in fronts[i]:
            for q in dominated_by[p]:
                counts[q] -= 1
                if counts[q] == 0:
                    nxt.append(q)
        i += 1
        fronts.append(nxt)
    return [f for f in fronts if f]


def crowding_distance(front: Sequence[int], vectors: Sequence[Sequence[float]]) -> dict[int, float]:
    dist = dict.fromkeys(front, 0.0)
    if len(front) <= 2:
        return {i: math.inf for i in front}
    m = len(vectors[front[0]])
    for k in range(m):
        ordered = sorted(front, key=lambda i: vectors[i][k])
        lo, hi = vectors[ordered[0]][k], vectors[ordered[-1]][k]
        dist[ordered[0]] = dist[ordered[-1]] = math.inf
        span = hi - lo
        if span <= 0:
            continue
        for j in range(1, len(ordered) - 1):
            dist[ordered[j]] += (vectors[ordered[j + 1]][k] - vectors[ordered[j - 1]][k]) / span
    return dist


def nsga2_select(items: Sequence[T], fitness: Sequence[Fitness], names: Sequence[str], k: int) -> list[T]:
    """Select ``k`` items by (confidence-aware front rank, crowding distance)."""
    if k >= len(items):
        return list(items)
    fronts = fast_non_dominated_sort(len(items), lambda p, q: confident_dominates(fitness[p], fitness[q], names))
    vectors = [f.vector(names) for f in fitness]
    chosen: list[int] = []
    for front in fronts:
        if len(chosen) + len(front) <= k:
            chosen.extend(front)
            continue
        cd = crowding_distance(front, vectors)
        rest = sorted(front, key=lambda i: (-cd[i], -sum(vectors[i])))
        chosen.extend(rest[: k - len(chosen)])
        break
    return [items[i] for i in chosen]


def pareto_front(fitness: Sequence[Fitness], names: Sequence[str]) -> list[int]:
    if not fitness:
        return []
    return fast_non_dominated_sort(len(fitness), lambda p, q: confident_dominates(fitness[p], fitness[q], names))[0]


# ---------------------------------------------------------------------------- NSGA-III


def das_dennis(m: int, divisions: int) -> np.ndarray:
    """Uniformly spaced reference points on the (m-1)-simplex."""
    if m == 1:
        return np.ones((1, 1))
    pts = []
    for combo in itertools.combinations(range(divisions + m - 1), m - 1):
        prev = -1
        coords = []
        for c in combo:
            coords.append(c - prev - 1)
            prev = c
        coords.append(divisions + m - 2 - prev)
        pts.append([x / divisions for x in coords])
    return np.asarray(pts, dtype=np.float64)


def nsga3_select(
    items: Sequence[T], fitness: Sequence[Fitness], names: Sequence[str], k: int, divisions: int | None = None
) -> list[T]:
    """NSGA-III environmental selection of ``k`` items (maximisation objectives)."""
    n = len(items)
    if k >= n:
        return list(items)
    m = len(names)
    fronts = fast_non_dominated_sort(n, lambda p, q: confident_dominates(fitness[p], fitness[q], names))
    chosen: list[int] = []
    last: list[int] = []
    for front in fronts:
        if len(chosen) + len(front) <= k:
            chosen.extend(front)
            if len(chosen) == k:
                return [items[i] for i in chosen]
            continue
        last = front
        break
    pool = chosen + last
    # Minimisation form, translated by the ideal point.
    f = -np.asarray([fitness[i].vector(names) for i in pool], dtype=np.float64)
    ideal = f.min(axis=0)
    ft = f - ideal
    nadir = ft.max(axis=0)
    nadir[nadir <= 1e-12] = 1.0
    fn = ft / nadir
    if divisions is None:
        divisions = max(2, min(12, int(round((2 * k) ** (1.0 / max(m - 1, 1))))))
    refs = das_dennis(m, divisions)
    refs_unit = refs / np.linalg.norm(refs, axis=1, keepdims=True)
    # perpendicular distance from each normalised point to each reference line
    proj = fn @ refs_unit.T  # (n_pool, n_refs)
    dist = np.sqrt(np.maximum((fn**2).sum(axis=1, keepdims=True) - proj**2, 0.0))
    assoc = dist.argmin(axis=1)
    assoc_d = dist[np.arange(len(pool)), assoc]
    niche = np.zeros(len(refs), dtype=int)
    for idx in range(len(chosen)):
        niche[assoc[idx]] += 1
    candidates = list(range(len(chosen), len(pool)))
    excluded_refs: set[int] = set()
    while len(chosen) < k and candidates:
        open_refs = [r for r in range(len(refs)) if r not in excluded_refs]
        min_count = min(niche[r] for r in open_refs)
        j = min((r for r in open_refs if niche[r] == min_count), key=lambda r: r)
        members = [c for c in candidates if assoc[c] == j]
        if not members:
            excluded_refs.add(j)
            continue
        pick = min(members, key=lambda c: assoc_d[c]) if niche[j] == 0 else members[0]
        chosen.append(pool[pick])
        candidates.remove(pick)
        niche[j] += 1
    return [items[i] for i in chosen]
