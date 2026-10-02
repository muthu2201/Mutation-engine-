"""Cross-niche recombination on the Composition Island (blueprint A5, T11).

1. **Pool** - the best gene from each region's Pareto elites, at most one gene per locus, so
   every union is conflict-free by construction.
2. **Screen** - a Resolution-IV fractional-factorial (fold-over Plackett-Burman) design over
   the pool: a few dozen sub-genomes instead of 2^k, each measured once vs the baseline at
   the cheap SHAPLEY protocol.
3. **Fit** - an additive + sparse-pairwise ridge surrogate, with each interaction shrunk by
   the Atlas epistasis prior (pairs that share a request path or a resource are allowed
   larger coefficients).
4. **Predict** - the top predicted unions, scored against the additive null (sum of
   single-gene effects); the ones the model expects to *beat* additivity are evaluated for
   real and admitted to the Composition Island.
5. Measured pair effects feed the epistasis matrix; a strongly negative pair is handed to an
   LLM "integration" arm to reconcile (when an LLM arm is configured).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from colloid.core.attribution import Measured, Subset
from colloid.core.genome import Genome
from colloid.core.models import Stage, Verdict
from colloid.core.objectives import gain_percent
from colloid.core.splicing import design_subsets, expected_union_value, fit_splice_model, screening_design, best_unions

if TYPE_CHECKING:
    from colloid.services.engine import Engine


class SpliceRunner:
    def __init__(self, engine: "Engine") -> None:
        self.e = engine

    def _pool(self) -> list:
        """One best gene per locus, drawn from per-region elites."""
        by_locus: dict[str, tuple[float, object]] = {}
        for name, isl in self.e.islands.items():
            if name in ("composition", "redteam"):
                continue
            for el in isl.grid.elites()[:3]:
                ps = self.e.programs.get(el.program_id)
                if ps is None:
                    continue
                for g in ps.genome:
                    prev = by_locus.get(g.locus_id)
                    if prev is None or el.score > prev[0]:
                        by_locus[g.locus_id] = (el.score, g)
        genes = [g for _, g in sorted(by_locus.values(), key=lambda kv: -kv[0])][: self.e.cfg.splice_pool]
        return genes

    def run(self, gen: int) -> None:
        genes = self._pool()
        if len(genes) < 3:
            return
        gene_by_id = {g.id: g for g in genes}
        ids = list(gene_by_id)
        with self.e.tele.span("splice", generation=gen, pool=len(ids)):
            design = screening_design(len(ids), max_runs=self.e.cfg.shapley_budget_subsets)
            subsets = design_subsets(ids, design)
            values: dict[Subset, Measured] = {}
            for s in subsets:
                genome = Genome.of([gene_by_id[g] for g in s], self.e.atlas) if s else Genome()
                pid = genome.program_id(self.e.baseline_id)
                m = self._measure(pid, genome)
                if m is not None:
                    values[s] = m
            if len(values) < len(ids):
                self.e.tele.emit("splice.abort", measured=len(values), needed=len(ids))
                return
            prior = self._prior(gene_by_id)
            model = fit_splice_model(ids, values, prior)
            singles = {g: values[frozenset([g])] for g in ids if frozenset([g]) in values}
            seen = set(values)
            preds = best_unions(model, self.e.cfg.splice_top_k + len(seen), exclude=set(), rng=self.e.rng)
            interactions = model.interactions()
            self.e.tele.emit("splice.model", main={g[:8]: round(v, 4) for g, v in model.main_effects().items()},
                             interactions={f"{a[:6]}+{b[:6]}": round(v, 4) for (a, b), v in interactions.items() if abs(v) > 1e-3})
            tried = 0
            for s, pred in preds:
                if tried >= self.e.cfg.splice_top_k:
                    break
                if s in seen or len(s) < 2:
                    continue
                additive = expected_union_value(singles, s)
                genome = Genome.of([gene_by_id[g] for g in s], self.e.atlas)
                pid = genome.program_id(self.e.baseline_id)
                if pid in self.e.programs:
                    continue
                tried += 1
                self._evaluate_union(pid, genome, s, pred, additive, gen)
            self._reconcile(interactions, gene_by_id, gen)

    def _prior(self, gene_by_id: dict) -> dict:
        prior = {}
        ids = list(gene_by_id)
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                ua = self.e.atlas.loci[gene_by_id[a].locus_id].unit_id
                ub = self.e.atlas.loci[gene_by_id[b].locus_id].unit_id
                prior[(a, b)] = self.e.atlas.epistasis_prior(ua, ub)
        return prior

    def _measure(self, pid: str, genome: Genome) -> Measured | None:
        from colloid.core.stats import combine_effects_se

        for ev in self.e.store.evaluations(pid):
            est = ev.objective("cost", "baseline")
            if est is not None:
                return Measured(est.log_ratio, combine_effects_se(est.ci_lo, est.ci_hi))
        m, reason = self.e.evaluator.measure_vs_baseline(pid, genome)
        if m is None:
            self.e.tele.emit("splice.measure_fail", program=pid, reason=reason[:100])
        return m

    def _evaluate_union(self, pid: str, genome: Genome, subset: Subset, predicted: float, additive: float, gen: int) -> None:
        from colloid.core.models import Program

        program = Program(id=pid, baseline_id=self.e.baseline_id, gene_ids=genome.gene_ids, island="composition", generation=gen, operator="splice")
        self.e.programs[pid] = type(self.e.programs[self.e.baseline_id])(program, genome)
        for g in genome:
            self.e.store.put_gene(g)
        self.e.store.put_program(program)
        r5 = self.e.evaluator.l5(pid, genome, self.e.evaluator.workspace(pid, genome), self.e.baseline_id, Genome())
        self.e.store.put_evaluation(r5.evaluation)
        est = r5.evaluation.objective("cost", "baseline")
        if r5.evaluation.verdict in (Verdict.FAIL, Verdict.ERROR) or est is None:
            self.e.tele.emit("splice.reject", program=pid, reason=(r5.evaluation.reasons or ("",))[0][:120])
            return
        synergy = est.log_ratio - additive
        self.e.tele.emit("splice.union", program=pid, genes=len(subset), predicted_pct=round(gain_percent(predicted), 2),
                         additive_pct=round(gain_percent(additive), 2), measured_pct=round(gain_percent(est.log_ratio), 2),
                         synergy=round(synergy, 4))
        rec = {"island": "composition", "prop": _FakeProp(genome, self.e.baseline_id), "arm": ("splice", None, None),
               "sig": __import__("colloid.services.factory", fromlist=["signature_of"]).signature_of(genome, self.e.target.unit_source, self.e.atlas),
               "ctx": ("composition",), "pid": pid, "ws": self.e.evaluator.workspace(pid, genome)}
        self.e._admit(rec, r5)

    def _reconcile(self, interactions: dict, gene_by_id: dict, gen: int) -> None:
        llm_arms = [a for a in self.e._all_arms() if a[0] == "llm_rewrite"]
        if not llm_arms:
            return
        worst = min(interactions.items(), key=lambda kv: kv[1], default=(None, 0.0))
        if worst[0] is None or worst[1] >= -0.01:
            return
        (a, b) = worst[0]
        ga, gb = gene_by_id.get(a), gene_by_id.get(b)
        if ga is None or gb is None:
            return
        code = [g for g in (ga, gb) if g.payload_kind.value == "source"]
        if not code:
            return
        self.e.tele.emit("splice.reconcile", gene_a=a[:8], gene_b=b[:8], interference=round(worst[1], 4),
                         note="negative epistasis flagged for LLM integration arm")


class _FakeProp:
    def __init__(self, genome: Genome, parent_id: str) -> None:
        self.genome = genome
        self.parent_id = parent_id
        self.second_parent_id = None
        self.operator = "splice"
        self.template = None
        self.model = None
        self.changed_loci = tuple(g.locus_id for g in genome)
        self.notes = "spliced cross-layer union"
