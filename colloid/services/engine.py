"""The Colloid engine: the generation loop that ties the hexagon together.

One generation:

1. **Budget** - the scheduler splits the generation's proposal slots across islands in
   proportion to each island's summed Locus Opportunity Score (causal leverage × dollar
   share) and recent improvement momentum, with an exploration floor (``budget.py``).
2. **Propose** - per island, for each slot: the contextual Thompson bandit picks an arm
   ``(operator, model, template)`` given the parent locus's tags; a parent is sampled from
   the island (weighted by fitness rank and down-weighted inside tabu basins); the mutation
   factory turns the arm + parent into a Proposal (an LLM call happens here when the arm is
   an LLM arm). Novelty rejection drops near-duplicates before any compute is spent.
3. **Cascade** - every proposal goes L0 (policy) → L1 (build) → L2 (oracle). Survivors are
   ranked by the surrogate (L3); the top fraction per island go to L4 (micro-benchmark vs
   parent). The best L4 survivors across all islands go to L5 (macro vs baseline+parent).
4. **Archive & credit** - survivors enter their island's MAP-Elites grid and parent pool;
   the measured child-vs-parent gain becomes the bandit reward and a lineage-credit record;
   the surrogate learns from the realised L4 metric.
5. **Promote** - a new global-best elite is sent to L6 (deep assurance + holdout + soak).
6. **Periodic** - ring migration and stagnation reseed every few generations; Shapley
   pruning of elite genomes; cross-region splicing on the Composition Island.

Everything is persisted (programs, genes, evaluations, lineage, attribution, epistasis,
LLM calls, alerts, archive cells, bandit state) and emitted as telemetry, so a run is fully
reconstructable and the dashboard can show it live.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from colloid.adapters.cost.static_prices import StaticPriceCostModel
from colloid.adapters.store.sql_store import open_store
from colloid.adapters.target.stackzero.adapter import StackZeroTarget
from colloid.adapters.telemetry.jsonl import JsonlTelemetry
from colloid.core import budget as budget_mod
from colloid.core.archive import Axis, Elite, Island, IslandModel
from colloid.core.attribution import LineageLedger
from colloid.core.bandit import ThompsonBandit
from colloid.core.genome import Genome
from colloid.core.models import (
    Alert,
    AttributionRecord,
    EpistasisRecord,
    Mutability,
    Program,
    ProgramStatus,
    Stage,
    UnitKind,
    Verdict,
)
from colloid.core.novelty import NoveltyFilter
from colloid.core.objectives import Fitness, gain_percent, scalar_score
from colloid.core.surrogate import GeneFeature, Surrogate, vectorise
from colloid.services.config import EngineConfig
from colloid.services.factory import Arm, MutationFactory, signature_of
from colloid.services.shapley_runner import ShapleyRunner
from colloid.services.splice_runner import SpliceRunner
from colloid_evaluator.cascade import OBJECTIVES, Evaluator, StageResult
from colloid_evaluator.profiler import Bench, CausalProfiler, ProfileConfig, decorate_atlas

PRIMARY = "cost"
CODE_OPS = {"py_rewrite", "gi_edit", "llm_rewrite"}


@dataclass
class ProgramState:
    program: Program
    genome: Genome
    fitness: Fitness | None = None
    score: float = -math.inf
    features: dict[str, Any] = field(default_factory=dict)


class Engine:
    def __init__(self, config: EngineConfig, target: StackZeroTarget | None = None, providers: dict[str, Any] | None = None) -> None:
        self.cfg = config
        self.rng = random.Random(config.seed)
        run_dir = config.run_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        self.target = target or StackZeroTarget()
        self.cost = StaticPriceCostModel(config.cost_usd_per_vcpu_hour, config.cost_usd_per_gb_hour)
        self.store = open_store(config.resolved("store_url"))
        self.tele = JsonlTelemetry(config.resolved("telemetry_path"), run_id=config.name, echo=True)
        self.providers = providers or {}
        self.evaluator = Evaluator(self.target, self.cost, rate=config.rate_rps, log=lambda m: self.tele.emit("log", msg=m))
        self.atlas = self.target.atlas_seed()
        self.knobs = {k.name: k for k in self.target.knobs()}
        self.knob_of_locus = self.target.knob_name_of_locus(self.atlas)
        self.baseline_id = self.target.baseline_id()
        self.factory = MutationFactory(self.atlas, self.knobs, self.knob_of_locus, self.target.unit_source, self.target.mutation_context, self.providers)
        self.bandit = ThompsonBandit(prior_mean=config.bandit_prior_mean)
        self.surrogate = Surrogate()
        self.ledger = LineageLedger()
        self.novelty = NoveltyFilter(threshold=0.97)
        self.islands: dict[str, Island] = {}
        self.island_model: IslandModel | None = None
        self.programs: dict[str, ProgramState] = {}
        self.evaluated: set[str] = set()
        self.epistasis: dict[tuple[str, str], EpistasisRecord] = {}
        self.best_score = 0.0
        self.best_program: str | None = None
        self.promoted: list[str] = []
        self.shapley = ShapleyRunner(self)
        self.splicer = SpliceRunner(self)
        self.started = 0.0
        self.counts: dict[str, int] = {}

    # ------------------------------------------------------------------ setup
    def setup(self) -> None:
        self.started = time.monotonic()
        self.tele.emit("run.start", config=self.cfg.model_dump(), baseline=self.baseline_id)
        info = self.evaluator.setup(self.baseline_id)
        self.cfg.rate_rps = info["rate_rps"]
        self.store.kv_set("config", self.cfg.model_dump())
        self.store.kv_set("setup", {"rate_rps": info["rate_rps"], "calibration": info["calibration"], "fingerprint": info["fingerprint"]})
        self.store.put_program(Program(id=self.baseline_id, baseline_id=self.baseline_id, gene_ids=(), island="baseline", generation=0, status=ProgramStatus.EVALUATED))
        self.programs[self.baseline_id] = ProgramState(self.store.get_program(self.baseline_id), Genome(), Fitness.zero(OBJECTIVES), 0.0, self._descriptor(Genome(), Fitness.zero(OBJECTIVES), 0))
        if self.cfg.profile:
            self._profile()
        if self.cfg.aa_runs:
            self._aa_test()
        self._build_islands()
        self.store.put_atlas(self.atlas)
        self.tele.emit("atlas.built", units=len(self.atlas.units), loci=len(self.atlas.loci), paths=len(self.atlas.paths))

    def _profile(self) -> None:
        with self.tele.span("profile"):
            bench = Bench(self.target, self.evaluator.universe, self.cost, rate=self.cfg.rate_rps)
            prof = CausalProfiler(self.target, bench, ProfileConfig(delays=tuple(self.cfg.profile_delays)))
            latency = prof.latency_share(None)
            # profile the highest-latency-share code units on request paths
            code_units = []
            for path in self.atlas.paths:
                for uid in path.unit_ids:
                    u = self.atlas.units[uid]
                    if u.kind == UnitKind.FUNCTION and u.layer.value == "svc" and u.tags.get("language") == "python" and uid not in code_units:
                        code_units.append(uid)
            lev = prof.causal_leverage(code_units[: prof.cfg.top_units], log=lambda m: self.tele.emit("log", msg=m))
            decorate_atlas(self.atlas, latency, lev)
            self.store.kv_set("profile", {"latency_share": {self.atlas.units[k].name: v for k, v in latency.latency_share.items()},
                                          "leverage": {self.atlas.units[k].name: c.slope for k, c in lev.leverage.items()}})
            self.tele.emit("profile.done", endpoints=len(latency.latency_share), leverage_units=len(lev.leverage))

    def _aa_test(self) -> None:
        with self.tele.span("aa_test", runs=self.cfg.aa_runs):
            report = self.evaluator.aa_test(self.cfg.aa_runs, on_run=lambda i, row: self.tele.emit("aa.run", i=i, **{k: v["log_ratio"] for k, v in row.items()}))
        self.store.kv_set("aa_test", report)
        fp = report["objectives"][PRIMARY]["false_positive_rate"]
        self.tele.emit("aa.done", false_positive_rate=fp, allowed=report["promotions_allowed"])
        if not report["promotions_allowed"]:
            self.store.put_alert(Alert(id="aa-" + str(time.time_ns())[:12], kind="aa_test", severity="critical",
                                       message=f"A/A false-positive rate {fp:.2%} exceeds alpha; promotions halted"))

    def _build_islands(self) -> None:
        regions = {r.name: r for r in self.target.regions()}
        wanted = self.cfg.regions or list(regions)
        size_edges = (1.5, 3.5, 6.5)
        axes_numeric = (Axis("genes", edges=size_edges), Axis("resource", edges=(-0.05, 0.0, 0.05)))
        for name in wanted:
            region = regions[name]
            loci = self.atlas.loci_in(region)
            if not loci:
                continue
            self.islands[name] = Island(name=name, axes=axes_numeric, objectives=(PRIMARY, "mem"),
                                        pool_capacity=self.cfg.pool_capacity, stagnation_limit=self.cfg.stagnation_limit)
        # Composition island (cross-region genomes) with a region-membership descriptor.
        self.islands["composition"] = Island(name="composition", axes=(Axis("genes", edges=size_edges), Axis("resource", edges=(-0.05, 0.0, 0.05))),
                                              objectives=(PRIMARY, "p95", "mem"), pool_capacity=self.cfg.pool_capacity, stagnation_limit=self.cfg.stagnation_limit)
        if self.cfg.redteam_island:
            self.islands["redteam"] = Island(name="redteam", axes=(Axis("genes", edges=size_edges),), objectives=(PRIMARY,), pool_capacity=6)
        ring = tuple(n for n in self.islands if n not in ("redteam",))
        self.island_model = IslandModel(self.islands, ring, migration_interval=self.cfg.migration_interval)
        self._seed_islands()
        for arm in self._all_arms():
            self.bandit.add_arm(arm)

    def _seed_islands(self) -> None:
        root = self.programs[self.baseline_id]
        root_elite = Elite(self.baseline_id, (), Fitness.zero(OBJECTIVES), 0.0, root.features, 0,
                           signature=signature_of(Genome(), self.target.unit_source, self.atlas))
        for isl in self.islands.values():
            isl.pool.append(root_elite)

    # ------------------------------------------------------------------ arms
    def _all_arms(self) -> list[Arm]:
        arms: list[Arm] = [(op, None, None) for op in (*self.cfg.knob_operators, *[o for o in self.cfg.code_operators if o != "llm_rewrite"])]
        arms.append(("crossover", None, None))
        for a in self.cfg.llm_arms:
            for tmpl in a.templates:
                arms.append(("llm_rewrite", a.model, tmpl))
        return arms

    def _island_arms(self, island: str) -> list[Arm]:
        if island == "redteam":
            return [("redteam", None, None)]
        loci = self.atlas.loci_in(self._region(island)) if island != "composition" else []
        has_code = island == "composition" or any(self.atlas.loci[lid.id].surface.value == "code_region" for lid in loci)
        has_knob = island == "composition" or any(lid.id in self.knob_of_locus for lid in loci)
        arms = []
        for arm in self._all_arms():
            op = arm[0]
            if op in CODE_OPS and not has_code:
                continue
            if op.startswith("knob") and not has_knob:
                continue
            arms.append(arm)
        return arms or self._all_arms()

    def _region(self, island: str):
        return next(r for r in self.target.regions() if r.name == island)

    # ------------------------------------------------------------------ main loop
    def run(self) -> dict[str, Any]:
        self.setup()
        for gen in range(1, self.cfg.generations + 1):
            if self.cfg.max_runtime_minutes and (time.monotonic() - self.started) / 60 > self.cfg.max_runtime_minutes:
                self.tele.emit("run.time_limit", generation=gen)
                break
            self._generation(gen)
        return self.finish()

    def _generation(self, gen: int) -> None:
        with self.tele.span("generation", generation=gen):
            alloc = self._allocate(gen)
            self.tele.emit("budget", generation=gen, alloc=alloc)
            proposals: list[tuple[str, Any]] = []  # (island, ProposalResult)
            for island, slots in alloc.items():
                proposals += [(island, p) for p in self._propose(island, slots, gen)]
            survivors = self._cascade_generation(gen, proposals)
            self._run_l5(gen, survivors)
            self._periodic(gen)
            self._snapshot(gen)

    def _allocate(self, gen: int) -> dict[str, int]:
        opp: dict[str, float] = {}
        momentum: dict[str, float] = {}
        for name, isl in self.islands.items():
            if name in ("composition", "redteam"):
                opp[name] = 0.0
                continue
            loci = self.atlas.loci_in(self._region(name))
            opp[name] = sum(self.atlas.opportunity(lc.id) for lc in loci)
            recent = isl.best_history[-3:]
            delta = (isl.best_score - recent[0]) if recent else 0.0
            momentum[name] = max(0.0, delta) * 5 if math.isfinite(delta) else 0.0
        # give composition and redteam fixed minimums
        minimum = {}
        if "composition" in self.islands and gen >= 3:
            minimum["composition"] = max(1, self.cfg.proposals_per_generation // 8)
        if "redteam" in self.islands:
            minimum["redteam"] = 1
        if sum(opp.values()) <= 0:
            opp = {k: 1.0 for k in opp}
        return budget_mod.allocate_slots(opp, self.cfg.proposals_per_generation, epsilon=self.cfg.budget_epsilon, momentum=momentum, minimum=minimum)

    def _propose(self, island: str, slots: int, gen: int) -> list[Any]:
        if slots <= 0:
            return []
        isl = self.islands[island]
        results = []
        region_loci = [lc.id for lc in self.atlas.loci_in(self._region(island))] if island not in ("composition", "redteam") else [lc.id for lc in self.atlas.loci.values() if lc.mutability != Mutability.FROZEN]
        opp = {lid: self.atlas.opportunity(lid) for lid in region_loci}
        arms = self._island_arms(island)
        attempts = 0
        while len(results) < slots and attempts < slots * 4:
            attempts += 1
            parent = isl.sample_parent(self.rng, self._root_elite())
            pstate = self.programs.get(parent.program_id)
            if pstate is None:
                continue
            ctx_tags = self._locus_context(island)
            arm = self.bandit.select(ctx_tags, self.rng, available=arms)
            ctx = self.factory.context(region_loci, opp, self.rng, pstate.genome)
            second = None
            if arm[0] == "crossover":
                other = isl.sample_parent(self.rng, self._root_elite())
                os = self.programs.get(other.program_id)
                if os is None or os.program.id == parent.program_id:
                    continue
                second = (os.genome, os.program.id)
            res = self.factory.build(arm, pstate.genome, pstate.program.id, ctx, second=second)
            if res.llm_call is not None:
                self.store.put_llm_call(res.llm_call)
            if res.proposal is None:
                self.tele.emit("propose.reject", island=island, arm=list(arm), reason=res.reject_reason[:120])
                continue
            prop = res.proposal
            pid = prop.genome.program_id(self.baseline_id)
            if pid in self.programs or pid == self.baseline_id or len(prop.genome) == 0:
                # an empty genome is the baseline; nothing new to evaluate
                continue
            sig = signature_of(prop.genome, self.target.unit_source, self.atlas)
            if island != "redteam" and not self.novelty.is_novel(sig):
                self.tele.emit("propose.reject", island=island, arm=list(arm), reason="near-duplicate (novelty)")
                continue
            self.novelty.add(sig)
            program = Program(id=pid, baseline_id=self.baseline_id, gene_ids=prop.genome.gene_ids, island=island, generation=gen,
                              parent_ids=(prop.parent_id,) + ((prop.second_parent_id,) if prop.second_parent_id else ()), operator=prop.operator)
            self.programs[pid] = ProgramState(program, prop.genome)
            for g in prop.genome:
                self.store.put_gene(g)
            self.store.put_program(program)
            self.store.put_lineage(pid, prop.parent_id, prop.operator, list(set(prop.genome.gene_ids) - set(pstate.genome.gene_ids)))
            self.store.put_signature(pid, sig.to_hex())
            results.append((prop, arm, sig, ctx_tags))
            self.counts[island] = self.counts.get(island, 0) + 1
        return results

    def _locus_context(self, island: str) -> tuple:
        if island in ("composition", "redteam"):
            return (island,)
        region = self._region(island)
        loci = self.atlas.loci_in(region)
        layers = {self.atlas.units[lc.unit_id].layer.value for lc in loci}
        surfaces = {lc.surface.value for lc in loci}
        return (island, tuple(sorted(layers)), tuple(sorted(surfaces)))

    def _root_elite(self) -> Elite:
        r = self.programs[self.baseline_id]
        return Elite(self.baseline_id, (), r.fitness or Fitness.zero(OBJECTIVES), 0.0, r.features, 0)

    # ------------------------------------------------------------------ cascade
    def _cascade_generation(self, gen: int, proposals: Sequence[tuple[str, Any]]) -> list[dict[str, Any]]:
        passed_l2: list[dict[str, Any]] = []
        for island, (prop, arm, sig, ctx_tags) in proposals:
            pid = prop.genome.program_id(self.baseline_id)
            rec = {"island": island, "prop": prop, "arm": arm, "sig": sig, "ctx": ctx_tags, "pid": pid}
            r0 = self.evaluator.l0(pid, prop.genome)
            self.store.put_evaluation(r0.evaluation)
            if not r0.passed:
                self._reject(rec, Stage.L0, r0)
                continue
            r1 = self.evaluator.l1(pid, prop.genome)
            self.store.put_evaluation(r1.evaluation)
            if not r1.passed:
                self._reject(rec, Stage.L1, r1)
                continue
            r2 = self.evaluator.l2(pid, prop.genome, r1.ws)
            self.store.put_evaluation(r2.evaluation)
            if island == "redteam":
                self._redteam_outcome(rec, r2)
                continue
            if not r2.passed:
                self._reject(rec, Stage.L2, r2)
                continue
            rec["ws"] = r1.ws
            passed_l2.append(rec)
        return self._l4(gen, passed_l2)

    def _reject(self, rec: dict[str, Any], stage: Stage, res: StageResult) -> None:
        self.store.set_status(rec["pid"], ProgramStatus.REJECTED)
        reason = (res.evaluation.reasons or ("",))[0]
        self.factory._memory(rec["prop"].changed_loci[0] if rec["prop"].changed_loci else "").add_failure(reason) if rec["arm"][0] == "llm_rewrite" else None
        self.bandit.update(rec["ctx"], rec["arm"], 0.0, cost=res.evaluation.duration_s)
        self.ledger.record(rec["pid"], rec["prop"].parent_id, rec["prop"].changed_loci, rec["arm"], None, None, passed=False)
        self.tele.emit("cascade.reject", island=rec["island"], stage=stage.value, operator=rec["arm"][0], reason=reason[:160])

    def _redteam_outcome(self, rec: dict[str, Any], r2: StageResult) -> None:
        if r2.passed:  # a red-team attack that the oracle did NOT catch is an evaluator breach
            self.store.put_alert(Alert(id="breach-" + rec["pid"][:12], kind="redteam_breach", severity="critical",
                                       message=f"red-team program {rec['pid']} passed L2 (oracle breach): {rec['prop'].notes}", program_id=rec["pid"]))
            self.tele.emit("redteam.breach", program=rec["pid"], notes=rec["prop"].notes)
            self.bandit.update(rec["ctx"], rec["arm"], 0.1)
        else:
            self.tele.emit("redteam.caught", program=rec["pid"], stage="L2")
            self.bandit.update(rec["ctx"], rec["arm"], 0.0)
        self.store.set_status(rec["pid"], ProgramStatus.REJECTED)

    def _l4(self, gen: int, recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # L3 surrogate rank: keep the top fraction per island
        for rec in recs:
            rec["surrogate"] = self._predict(rec)
        kept: list[dict[str, Any]] = []
        by_island: dict[str, list[dict[str, Any]]] = {}
        for rec in recs:
            by_island.setdefault(rec["island"], []).append(rec)
        for island, group in by_island.items():
            idx = self.surrogate.rank_keep([r["surrogate"] for r in group], max(1, int(len(group) * self.cfg.surrogate_keep_fraction)))
            for i, rec in enumerate(group):
                (kept.append(rec) if i in idx else self.tele.emit("cascade.surrogate_cut", island=island, program=rec["pid"], predicted=round(rec["surrogate"], 4)))
        survivors = []
        for rec in kept:
            prop = rec["prop"]
            parent = self.programs[prop.parent_id]
            r4 = self.evaluator.l4(rec["pid"], prop.genome, rec["ws"], prop.parent_id, parent.genome)
            self.store.put_evaluation(r4.evaluation)
            self._learn_surrogate(rec, r4)
            if not r4.passed:
                self._reject(rec, Stage.L4, r4)
                continue
            rec["l4"] = r4
            survivors.append(rec)
            self.tele.emit("cascade.l4_pass", island=rec["island"], program=rec["pid"], operator=rec["arm"][0])
        return survivors

    def _predict(self, rec: dict[str, Any]) -> float:
        feats = self._features(rec["prop"].genome)
        return self.surrogate.predict(feats)

    def _features(self, genome: Genome):
        gfs = []
        for g in genome:
            if g.locus_id in self.knob_of_locus:
                from colloid.core.knobs import knob_features

                spec = self.knobs[self.knob_of_locus[g.locus_id]]
                gfs.append(GeneFeature(g.locus_id, tuple(knob_features(spec, g.value))))
            else:
                gfs.append(GeneFeature(g.locus_id, (float(g.payload.get("diff_lines", 1)),)))
        return vectorise(gfs, "mix")

    def _learn_surrogate(self, rec: dict[str, Any], r4: StageResult) -> None:
        est = r4.evaluation.objective(PRIMARY, "parent")
        if est is not None and math.isfinite(est.log_ratio):
            self.surrogate.add_example(self._features(rec["prop"].genome), est.log_ratio, prediction=rec.get("surrogate"))
            if len(self.surrogate.ys) % 10 == 0:
                self.surrogate.fit()
                self.tele.emit("surrogate.fit", examples=len(self.surrogate.ys), model_kind=self.surrogate.kind, rho=self.surrogate.rho)

    # ------------------------------------------------------------------ L5 + archive
    def _run_l5(self, gen: int, survivors: list[dict[str, Any]]) -> None:
        survivors.sort(key=lambda r: -(r["l4"].evaluation.objective(PRIMARY, "parent").log_ratio if r["l4"].evaluation.objective(PRIMARY, "parent") else -9))
        for rec in survivors[: self.cfg.run_l5_top_k]:
            prop = rec["prop"]
            parent = self.programs[prop.parent_id]
            r5 = self.evaluator.l5(rec["pid"], prop.genome, rec["ws"], prop.parent_id, parent.genome)
            self.store.put_evaluation(r5.evaluation)
            if r5.evaluation.verdict in (Verdict.FAIL, Verdict.ERROR):
                self._reject(rec, Stage.L5, r5)
                continue
            self._admit(rec, r5)
        # survivors not sent to L5 still update the bandit with their L4 parent-gain (cheap signal)
        for rec in survivors[self.cfg.run_l5_top_k :]:
            est = rec["l4"].evaluation.objective(PRIMARY, "parent")
            reward = self.bandit.reward_from_gain(est.log_ratio if est else None, True)
            self.bandit.update(rec["ctx"], rec["arm"], reward, cost=rec["l4"].evaluation.duration_s)
            self.ledger.record(rec["pid"], rec["prop"].parent_id, rec["prop"].changed_loci, rec["arm"], est.log_ratio if est else None, None, passed=True)

    def _admit(self, rec: dict[str, Any], r5: StageResult) -> None:
        prop = rec["prop"]
        pid = rec["pid"]
        fitness = Fitness.from_evaluation(r5.evaluation, OBJECTIVES)
        if fitness is None:
            self._reject(rec, Stage.L5, r5)
            return
        est_parent = r5.evaluation.objective(PRIMARY, "parent")
        gene_count = len(prop.genome)
        score = scalar_score(fitness, cost_objective=PRIMARY, slo_objective="p95", gene_count=gene_count)
        pstate = self.programs[pid]
        pstate.fitness, pstate.score = fitness, score
        pstate.features = self._descriptor(prop.genome, fitness, gene_count)
        self.store.set_status(pid, ProgramStatus.ELITE if r5.evaluation.verdict != Verdict.SUSPICIOUS else ProgramStatus.EVALUATED)
        self.evaluated.add(pid)
        for obj in OBJECTIVES:
            e = r5.evaluation.objective(obj, "baseline")
            if e is not None:
                self.store.put_attribution(AttributionRecord(program_id=pid, gene_id="__program__", method="lineage", objective=obj, value=e.log_ratio, ci_lo=e.ci_lo, ci_hi=e.ci_hi))
        sig = rec["sig"]
        elite = Elite(pid, prop.genome.gene_ids, fitness, score, pstate.features, prop and self.islands[rec["island"]].generation, signature=sig, parent_id=prop.parent_id)
        placed = self.islands[rec["island"]].offer(elite, self._parent_elite(prop.parent_id, rec["island"]), self.rng)
        if rec["island"] != "composition" and len(prop.genome) > 1 and any(self.atlas.units[self.atlas.loci[g.locus_id].unit_id].layer for g in prop.genome):
            self.islands["composition"].offer(elite, None, self.rng)
        reward = self.bandit.reward_from_gain(est_parent.log_ratio if est_parent else None, True)
        self.bandit.update(rec["ctx"], rec["arm"], reward, cost=r5.evaluation.duration_s)
        self.ledger.record(pid, prop.parent_id, prop.changed_loci, rec["arm"], est_parent.log_ratio if est_parent else None,
                           (est_parent.ci_lo, est_parent.ci_hi) if est_parent else None, passed=True)
        # neighbour memory for LLM prompts
        for lid in prop.changed_loci:
            base = r5.evaluation.objective(PRIMARY, "baseline")
            self.factory._memory(lid).add_neighbour(prop.notes or prop.operator, gain_percent(base.log_ratio) if base else None, "elite" if placed["grid"] else "kept")
        base_cost = r5.evaluation.objective(PRIMARY, "baseline")
        self.tele.emit("admit", island=rec["island"], program=pid, operator=prop.operator, score=round(score, 4),
                       cost_gain_pct=round(gain_percent(base_cost.log_ratio), 2) if base_cost else None,
                       genes=gene_count, verdict=r5.evaluation.verdict.value, in_grid=placed["grid"])
        self.store.put_archive_cell(rec["island"], str(sorted(pstate.features.items())), pid, fitness.value, score)
        if score > self.best_score + 1e-9 and r5.evaluation.verdict != Verdict.SUSPICIOUS:
            self._new_best(pid, score, rec, r5)
        elif r5.evaluation.verdict == Verdict.SUSPICIOUS:
            self._promote(pid, rec, "suspicious")

    def _descriptor(self, genome: Genome, fitness: Fitness, gene_count: int) -> dict[str, Any]:
        return {"genes": float(gene_count), "resource": float(fitness.value.get(PRIMARY, 0.0) - fitness.value.get("mem", 0.0))}

    def _parent_elite(self, parent_id: str, island: str) -> Elite | None:
        ps = self.programs.get(parent_id)
        if ps is None or ps.fitness is None:
            return None
        return Elite(parent_id, ps.genome.gene_ids, ps.fitness, ps.score, ps.features, 0)

    def _new_best(self, pid: str, score: float, rec: dict[str, Any], r5: StageResult) -> None:
        self.best_score, self.best_program = score, pid
        base = r5.evaluation.objective(PRIMARY, "baseline")
        self.tele.emit("best", program=pid, score=round(score, 4), cost_gain_pct=round(gain_percent(base.log_ratio), 2) if base else None)
        if self.cfg.promote:
            self._promote(pid, rec, "new_best")

    def _promote(self, pid: str, rec: dict[str, Any], why: str) -> None:
        store = self.evaluator
        with self.tele.span("l6", program=pid, why=why):
            r6 = store.l6(pid, rec["prop"].genome, rec["ws"])
        self.store.put_evaluation(r6.evaluation)
        if r6.passed:
            self.store.set_status(pid, ProgramStatus.PROMOTED)
            self.promoted.append(pid)
            base = r6.evaluation.objective(PRIMARY, "baseline")
            self.tele.emit("promoted", program=pid, holdout_cost_gain_pct=round(gain_percent(base.log_ratio), 2) if base else None)
        else:
            self.tele.emit("l6.reject", program=pid, reasons=list(r6.evaluation.reasons)[:3])
            if any("suspicion" in r or "holdout" in r for r in r6.evaluation.reasons):
                self.store.put_alert(Alert(id="susp-" + pid[:12], kind="suspicion", severity="warning",
                                           message=f"{pid} failed deep review: {'; '.join(r6.evaluation.reasons)[:200]}", program_id=pid))

    # ------------------------------------------------------------------ periodic
    def _periodic(self, gen: int) -> None:
        assert self.island_model is not None
        moves = self.island_model.migrate(gen)
        if moves:
            self.tele.emit("migrate", generation=gen, moves=len(moves))
        for name, isl in self.islands.items():
            if isl.end_generation():
                reseeded = self.island_model.reseed(name)
                self.tele.emit("stagnation", island=name, reseeded=len(reseeded), temperature=round(isl.temperature, 4))
        if self.cfg.shapley_every and gen % self.cfg.shapley_every == 0:
            self.shapley.run(gen)
        if self.cfg.splice_every and gen % self.cfg.splice_every == 0 and gen >= self.cfg.splice_every:
            self.splicer.run(gen)
        self.store.kv_set("bandit", self.bandit.to_dict())

    def _snapshot(self, gen: int) -> None:
        from colloid.core.archive import summarize_islands

        self.tele.emit("islands", generation=gen, islands=summarize_islands(list(self.islands.values())))
        self.store.kv_set("progress", {"generation": gen, "best_score": self.best_score, "best_program": self.best_program,
                                       "evaluated": len(self.evaluated), "promoted": len(self.promoted), "elapsed_min": (time.monotonic() - self.started) / 60})

    # ------------------------------------------------------------------ finish
    def finish(self) -> dict[str, Any]:
        best = self.programs.get(self.best_program) if self.best_program else None
        result = {
            "name": self.cfg.name, "generations": self.cfg.generations, "rate_rps": self.cfg.rate_rps,
            "programs_evaluated": len(self.evaluated), "programs_total": len(self.programs),
            "best_program": self.best_program, "best_score": self.best_score, "promoted": self.promoted,
            "best_genes": list(best.genome.gene_ids) if best else [], "elapsed_min": (time.monotonic() - self.started) / 60,
            "arms": self.bandit.table(), "alerts": [a.model_dump() for a in self.store.alerts()],
        }
        if best is not None and best.fitness is not None:
            result["best_fitness_pct"] = {o: round(gain_percent(best.fitness.value[o]), 2) for o in OBJECTIVES}
        self.store.kv_set("result", result)
        self.tele.emit("run.done", **{k: v for k, v in result.items() if k not in ("arms", "alerts")})
        self.tele.close()
        self.evaluator.shutdown()
        self.store.close()
        return result
