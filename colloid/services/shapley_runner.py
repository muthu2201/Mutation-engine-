"""Periodic Shapley pruning of elite genomes (blueprint A3 tier 3, T10).

For each elite genome with k ≤ ``shapley_max_genes`` genes, estimate every gene's Shapley
value - its average marginal contribution to the cost gain over all orderings - by measuring
the sub-genomes the Shapley plan asks for (exact when 2^k fits the budget, else permutation
sampling), all at the cheap SHAPLEY protocol versus the baseline. Genes whose Shapley CI
includes ≤ 0 are bloat and are pruned; the pruned genome is then re-measured, and if it is
no worse it replaces the elite (anti-bloat, keeping evolved genomes minimal and reviewable).
Measured sub-genome values also yield pairwise epistasis ε_ij for the strongest genes,
graph-ordered so likely-interacting pairs (shared Atlas path/resource) are tested first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from colloid.core.attribution import (
    Measured,
    Subset,
    epistasis,
    epistasis_pairs_to_test,
    prune,
    shapley_exact,
    shapley_permutation,
    shapley_plan,
)
from colloid.core.models import AttributionRecord, EpistasisRecord
from colloid.core.objectives import gain_percent

if TYPE_CHECKING:
    from colloid.services.engine import Engine


class ShapleyRunner:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def run(self, gen: int) -> None:
        candidates = self._elites()
        if not candidates:
            return
        with self.e.tele.span("shapley", generation=gen, elites=len(candidates)):
            for pid in candidates[:3]:
                self._prune_one(pid, gen)

    def _elites(self) -> list[str]:
        out = []
        for isl in self.e.islands.values():
            for el in isl.grid.elites()[:2]:
                ps = self.e.programs.get(el.program_id)
                if ps and 2 <= len(ps.genome) <= self.e.cfg.shapley_max_genes and el.program_id not in out:
                    out.append(el.program_id)
        return out

    def _prune_one(self, pid: str, gen: int) -> None:
        genome = self.e.programs[pid].genome
        gene_ids = list(genome.gene_ids)
        subsets, perms = shapley_plan(gene_ids, self.e.cfg.shapley_budget_subsets, self.e.rng)
        values: dict[Subset, Measured] = {frozenset(): Measured(0.0, 0.0)}
        for s in subsets:
            if not s:
                continue
            sub = genome.subset(s)
            spid = sub.program_id(self.e.baseline_id)
            cached = self._cached(spid)
            if cached is not None:
                values[s] = cached
                continue
            m, reason = self.e.evaluator.measure_vs_baseline(spid, sub)
            if m is None:
                self.e.tele.emit("shapley.skip", program=pid, subset=len(s), reason=reason[:100])
                return
            values[s] = m
        credits = shapley_exact(gene_ids, values) if not perms else shapley_permutation(gene_ids, values, perms)
        for gid, c in credits.items():
            self.e.store.put_attribution(AttributionRecord(program_id=pid, gene_id=gid, method=c.method, objective="cost", value=c.value, ci_lo=c.ci_lo, ci_hi=c.ci_hi))
        keep, drop = prune(credits)
        self.e.tele.emit("shapley.result", program=pid, keep=len(keep), drop=len(drop),
                         values={gid[:8]: round(c.value, 4) for gid, c in credits.items()})
        self._epistasis(gene_ids, values, pid)
        if drop and keep:
            pruned = genome.subset(keep)
            ppid = pruned.program_id(self.e.baseline_id)
            if ppid not in self.e.programs:
                m, reason = self.e.evaluator.measure_vs_baseline(ppid, pruned)
                full = values.get(frozenset(gene_ids))
                if m is not None and full is not None and m.value >= full.value - full.se:
                    self.e.tele.emit("shapley.prune", original=pid, pruned=ppid, dropped=len(drop),
                                     before_pct=round(gain_percent(full.value), 2), after_pct=round(gain_percent(m.value), 2))

    def _cached(self, program_id: str) -> Measured | None:
        from colloid.core.stats import combine_effects_se

        for ev in self.e.store.evaluations(program_id):
            est = ev.objective("cost", "baseline")
            if est is not None:
                return Measured(est.log_ratio, combine_effects_se(est.ci_lo, est.ci_hi))
        return None

    def _epistasis(self, gene_ids: list[str], values: dict[Subset, Measured], pid: str) -> None:
        singles = {g: values.get(frozenset([g])) for g in gene_ids}
        prior = {}
        for i, a in enumerate(gene_ids):
            for b in gene_ids[i + 1 :]:
                ua = self.e.atlas.loci[self.e.programs[pid].genome.get(a).locus_id].unit_id
                ub = self.e.atlas.loci[self.e.programs[pid].genome.get(b).locus_id].unit_id
                prior[(a, b)] = self.e.atlas.epistasis_prior(ua, ub)
        for a, b in epistasis_pairs_to_test(gene_ids, prior, budget=6):
            pair = values.get(frozenset([a, b]))
            if singles[a] is None or singles[b] is None or pair is None:
                continue
            eps = epistasis(singles[a], singles[b], pair, a, b)
            lo, hi = eps.ci
            rec = EpistasisRecord(gene_a=a, gene_b=b, objective="cost", epsilon=eps.epsilon, ci_lo=lo, ci_hi=hi, n=1)
            self.e.store.put_epistasis(rec)
            self.e.epistasis[tuple(sorted((a, b)))] = rec
            if eps.significant:
                self.e.tele.emit("epistasis", gene_a=a[:8], gene_b=b[:8], epsilon=round(eps.epsilon, 4),
                                 kind="synergy" if eps.epsilon > 0 else "interference")
