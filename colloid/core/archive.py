"""Islands and MAP-Elites archives (blueprint A4) with the tunneling mechanisms of A7.

Each **Region** of the Atlas (``db.config``, ``alloc``, ``svc.code``...) gets its own
**Island**: a parent pool plus a MAP-Elites grid. MAP-Elites keeps the best program *per
behaviour cell* rather than the best program overall, so the archive retains different
*kinds* of solutions (a 1-gene tweak and a 6-gene rewrite; a CPU-saving variant and a
memory-saving one) instead of collapsing onto one lineage.

Escaping local optima (blueprint A7) is built in:

* **Simulated-annealing acceptance into the parent pool** (not the elite grid): a worse
  child can still become a parent with probability ``exp(Δ/T)``. The temperature decays each
  generation and *reheats* when the island stagnates.
* **Neutral drift**: a child that ties its parent (overlapping CIs) and has no more genes is
  always accepted - GI landscapes have large neutral plateaus that connect optima.
* **Stepping-stone preservation**: the parent pool is selected by NSGA-II over several
  objectives, not just the scalar score, so low-scoring but different lineages survive.
* **Stagnation reseed + tabu basins**: after ``stagnation_limit`` generations without a new
  best, the island records its elite's code signature as a tabu basin centre, reheats, and
  reseeds part of its pool from the global archive.
* **Ring migration** is handled by :class:`IslandModel`.
"""

from __future__ import annotations

import bisect
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from colloid.core.novelty import MinHash, TabuBasins
from colloid.core.objectives import Fitness
from colloid.core.selection import confident_dominates, nsga2_select


@dataclass(frozen=True)
class Axis:
    """A behaviour-descriptor axis: numeric (``edges``) or categorical (``categories``)."""

    name: str
    edges: tuple[float, ...] = ()
    categories: tuple[str, ...] = ()

    def bin(self, value: Any) -> int:
        if self.categories:
            return self.categories.index(value) if value in self.categories else len(self.categories)
        return bisect.bisect_right(self.edges, float(value))

    @property
    def size(self) -> int:
        return len(self.categories) + 1 if self.categories else len(self.edges) + 1


@dataclass
class Elite:
    program_id: str
    gene_ids: tuple[str, ...]
    fitness: Fitness
    score: float
    features: dict[str, Any]
    generation: int
    signature: MinHash | None = None
    parent_id: str | None = None

    @property
    def size(self) -> int:
        return len(self.gene_ids)


@dataclass
class MapElitesGrid:
    axes: tuple[Axis, ...]
    cells: dict[tuple[int, ...], Elite] = field(default_factory=dict)

    def cell_of(self, features: dict[str, Any]) -> tuple[int, ...]:
        return tuple(ax.bin(features.get(ax.name, 0)) for ax in self.axes)

    def try_insert(self, elite: Elite) -> bool:
        """Insert if the cell is empty or ``elite`` beats the occupant. Ties on score go to
        the smaller genome (anti-bloat)."""
        cell = self.cell_of(elite.features)
        cur = self.cells.get(cell)
        if cur is None or elite.score > cur.score + 1e-12 or (abs(elite.score - cur.score) <= 1e-12 and elite.size < cur.size):
            self.cells[cell] = elite
            return True
        return False

    def elites(self) -> list[Elite]:
        return sorted(self.cells.values(), key=lambda e: -e.score)

    @property
    def coverage(self) -> float:
        total = math.prod(ax.size for ax in self.axes)
        return len(self.cells) / total if total else 0.0

    def qd_score(self, offset: float = 1.0) -> float:
        """Quality-diversity score: sum of (score + offset) over filled cells."""
        return sum(max(0.0, e.score + offset) for e in self.cells.values())


@dataclass
class Island:
    name: str
    axes: tuple[Axis, ...]
    objectives: tuple[str, ...]
    pool_capacity: int = 12
    temperature: float = 0.05
    t_min: float = 0.002
    cooling: float = 0.85
    reheat_to: float = 0.08
    stagnation_limit: int = 4
    grid: MapElitesGrid = field(init=False)
    pool: list[Elite] = field(default_factory=list)
    generation: int = 0
    best_score: float = -math.inf
    best_history: list[float] = field(default_factory=list)
    since_improvement: int = 0
    basins: TabuBasins = field(default_factory=TabuBasins)
    stagnation_events: int = 0

    def __post_init__(self) -> None:
        self.grid = MapElitesGrid(self.axes)

    # ------------------------------------------------------------------ parents
    def sample_parent(self, rng: random.Random, root: Elite) -> Elite:
        """Weighted parent sampling over pool ∪ grid elites ∪ {root}.

        Weight = rank-based fitness term (power law over score rank) × a novelty term that
        down-weights candidates sitting in tabu basins. Rank-based weights are insensitive
        to the scale of scores, which drift as the island improves.
        """
        candidates: dict[str, Elite] = {root.program_id: root}
        for e in (*self.pool, *self.grid.elites()):
            candidates.setdefault(e.program_id, e)
        ordered = sorted(candidates.values(), key=lambda e: -e.score)
        weights = []
        for rank, e in enumerate(ordered):
            w = 1.0 / (1.0 + rank) ** 1.2
            if e.signature is not None:
                w *= 1.0 - 0.8 * self.basins.penalty(e.signature)
            weights.append(max(w, 1e-6))
        return rng.choices(ordered, weights=weights, k=1)[0]

    # ------------------------------------------------------------------ acceptance
    def offer(self, child: Elite, parent: Elite | None, rng: random.Random) -> dict[str, bool]:
        """Offer an evaluated child. Returns which structures accepted it."""
        in_grid = self.grid.try_insert(child)
        in_pool = self._accept_into_pool(child, parent, rng)
        if child.score > self.best_score + 1e-9:
            self.best_score = child.score
            self.since_improvement = -1  # end_generation() will bump it to 0
        return {"grid": in_grid, "pool": in_pool}

    def _accept_into_pool(self, child: Elite, parent: Elite | None, rng: random.Random) -> bool:
        accept = False
        if parent is None or child.score >= parent.score:
            accept = True
        else:
            names = list(self.objectives)
            tie = not confident_dominates(parent.fitness, child.fitness, names)
            if tie and child.size <= parent.size:
                accept = True  # neutral drift
            else:
                delta = child.score - parent.score
                accept = rng.random() < math.exp(delta / max(self.temperature, 1e-9))
        if not accept:
            return False
        self.pool = [e for e in self.pool if e.program_id != child.program_id] + [child]
        if len(self.pool) > self.pool_capacity:
            self.pool = nsga2_select(self.pool, [e.fitness for e in self.pool], list(self.objectives), self.pool_capacity)
        return any(e.program_id == child.program_id for e in self.pool)

    # ------------------------------------------------------------------ lifecycle
    def end_generation(self) -> bool:
        """Advance the annealing schedule. Returns True if the island just stagnated (the
        caller should then reseed it from the global archive)."""
        self.generation += 1
        self.since_improvement += 1
        self.best_history.append(self.best_score)
        self.temperature = max(self.t_min, self.temperature * self.cooling)
        if self.since_improvement >= self.stagnation_limit and self.grid.cells:
            best = self.grid.elites()[0]
            if best.signature is not None:
                self.basins.add(best.signature)
            self.temperature = self.reheat_to
            self.since_improvement = 0
            self.stagnation_events += 1
            return True
        return False

    def receive(self, migrants: Iterable[Elite]) -> None:
        for m in migrants:
            if all(e.program_id != m.program_id for e in self.pool):
                self.pool.append(m)
        if len(self.pool) > self.pool_capacity:
            self.pool = nsga2_select(self.pool, [e.fitness for e in self.pool], list(self.objectives), self.pool_capacity)

    def best(self) -> Elite | None:
        elites = self.grid.elites()
        return elites[0] if elites else None


@dataclass
class IslandModel:
    islands: dict[str, Island]
    ring: tuple[str, ...]
    migration_interval: int = 3
    migrants_per_step: int = 1

    def migrate(self, generation: int) -> list[tuple[str, str, str]]:
        """Ring migration: each island sends its best elite(s) to the next island in the
        ring every ``migration_interval`` generations. Returns (from, to, program) moves."""
        if generation == 0 or generation % self.migration_interval != 0 or len(self.ring) < 2:
            return []
        moves: list[tuple[str, str, str]] = []
        outgoing = {name: self.islands[name].grid.elites()[: self.migrants_per_step] for name in self.ring}
        for i, name in enumerate(self.ring):
            dst = self.ring[(i + 1) % len(self.ring)]
            if outgoing[name]:
                self.islands[dst].receive(outgoing[name])
                moves.extend((name, dst, e.program_id) for e in outgoing[name])
        return moves

    def global_elites(self, k: int) -> list[Elite]:
        seen: dict[str, Elite] = {}
        for isl in self.islands.values():
            for e in isl.grid.elites():
                if e.program_id not in seen or e.score > seen[e.program_id].score:
                    seen[e.program_id] = e
        return sorted(seen.values(), key=lambda e: -e.score)[:k]

    def reseed(self, island: str, k: int = 3) -> list[str]:
        """Replace the weakest part of an island's pool with global elites."""
        isl = self.islands[island]
        donors = [e for e in self.global_elites(k + 3) if all(p.program_id != e.program_id for p in isl.pool)][:k]
        if donors:
            isl.pool = sorted(isl.pool, key=lambda e: -e.score)[: max(0, isl.pool_capacity - len(donors))] + donors
        return [d.program_id for d in donors]


def descriptor_from_sizes(gene_count: int, diff_lines: int) -> dict[str, float]:
    return {"genes": float(gene_count), "diff_lines": float(diff_lines)}


def resource_tradeoff(fitness: Fitness, cpu: str, mem: str) -> float:
    """Behaviour descriptor: positive when the variant saves more CPU than memory."""
    return float(fitness.value.get(cpu, 0.0) - fitness.value.get(mem, 0.0))


def summarize_islands(islands: Sequence[Island]) -> list[dict[str, Any]]:
    return [
        {
            "name": i.name,
            "generation": i.generation,
            "best_score": i.best_score,
            "pool": len(i.pool),
            "cells": len(i.grid.cells),
            "coverage": round(i.grid.coverage, 4),
            "temperature": round(i.temperature, 5),
            "stagnation_events": i.stagnation_events,
            "basins": len(i.basins.centres),
        }
        for i in islands
    ]
