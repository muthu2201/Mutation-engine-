"""Mutation factory: turns a bandit arm + a parent into a concrete Proposal.

This is the glue between the pure core operators (which only propose) and the I/O ports
(LLM provider, target source). It owns the one unavoidable side effect of mutation - the
LLM call - and keeps it out of the core:

* **knob arms** (``knob_sample`` / ``knob_perturb`` / ``knob_reset``) call the pure knob
  operators directly.
* **code arms** on the deterministic operators (``py_rewrite`` picking one rewrite rule at a
  random applicable site; ``gi_edit`` a random statement edit) build a source gene with no
  model call.
* **LLM arms** build a :class:`MutationContext` from the locus (current source, Atlas tags,
  causal leverage, archive neighbours with measured gains, and summaries of earlier failed
  attempts at this locus), call the provider, parse the response back to a single confined
  function, and build a source gene. The prompt/response hashes, model, template and token
  counts are returned for the LLM-call log.
* **crossover** / **splice** recombine two parents' genomes.
* **redteam** arms build a deliberately-incorrect variant (used only by the red-team island,
  whose fitness is "fool the evaluator").
* **rule_apply** arms (one per CRL rule) set the index knob of one of the rule's proposals
  for this stack (``services.rules``): learned patterns as candidate generators.

Every returned proposal carries the arm that produced it, so lineage credit flows back to
the right bandit arm.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from colloid.core.atlas import StackAtlas
from colloid.core.genome import Genome, LocusConflict
from colloid.core.ids import sha256_hex
from colloid.core.knobs import KnobSpec
from colloid.core.models import Gene, LLMCallRecord, PayloadKind, Provenance, Unit
from colloid.core.novelty import MinHash
from colloid.core.operators.base import OperatorContext, Proposal, code_gene, diff_lines
from colloid.core.operators.crossover import gene_crossover
from colloid.core.operators.gi_edit import gi_edit
from colloid.core.operators.knob_ops import knob_perturb, knob_reset, knob_sample
from colloid.core.operators.llm_rewrite import (
    MutationContext,
    Neighbour,
    ParseResult,
    build_request,
    parse_c_response,
    parse_python_response,
    summarize_failures,
)
from colloid.core.operators.py_rewrite import find_rewrites
from colloid.core.operators.redteam import HACKS, attack_reachable, redteam_variant
from colloid.ports import LLMError, LLMProvider

Arm = tuple[str, str | None, str | None]


@dataclass
class ProposalResult:
    proposal: Proposal | None
    llm_call: LLMCallRecord | None = None
    reject_reason: str = ""


@dataclass
class LocusMemory:
    """Per-locus neighbours and failed attempts shown to the LLM."""

    neighbours: list[Neighbour] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def add_neighbour(self, summary: str, gain_percent: float | None, status: str) -> None:
        self.neighbours.insert(0, Neighbour(summary, gain_percent, status))
        self.neighbours = self.neighbours[:6]

    def add_failure(self, reason: str) -> None:
        self.failures.append(reason)
        self.failures = self.failures[-8:]


class MutationFactory:
    def __init__(
        self,
        atlas: StackAtlas,
        knobs: Mapping[str, KnobSpec],
        knob_of_locus: Mapping[str, str],
        unit_source: Callable[[str, Genome], str],
        mutation_context: Callable[[str], Mapping[str, Any]],
        providers: Mapping[str, LLMProvider],
        *,
        max_tokens: int = 1400,
        parse_code: Callable[[str, str, Unit], ParseResult] | None = None,
        rule_options: Mapping[str, Sequence[tuple[str, str]]] | None = None,
    ) -> None:
        self.atlas = atlas
        self.knobs = knobs
        self.knob_of_locus = knob_of_locus
        self.unit_source = unit_source
        self.mutation_context = mutation_context
        self.providers = providers
        self.max_tokens = max_tokens
        # Response parser for languages the pure core cannot parse itself (Go: the target's
        # own Go parser). Python and C responses are parsed in the core.
        self.parse_code = parse_code
        # CRL rule name -> [(locus id of the index knob implementing a proposal, description)]
        self.rule_options = dict(rule_options or {})
        self.memory: dict[str, LocusMemory] = {}

    def _memory(self, locus_id: str) -> LocusMemory:
        return self.memory.setdefault(locus_id, LocusMemory())

    def context(self, region_loci: Sequence[str], opportunity: Mapping[str, float], rng: random.Random, genome: Genome) -> OperatorContext:
        return OperatorContext(
            atlas=self.atlas, knobs=self.knobs, knob_of_locus=self.knob_of_locus, region_loci=list(region_loci),
            opportunity=opportunity, rng=rng, unit_source=self.unit_source,
        )

    # ------------------------------------------------------------------ dispatch
    def build(self, arm: Arm, parent: Genome, parent_id: str, ctx: OperatorContext, *, second: tuple[Genome, str] | None = None) -> ProposalResult:
        op = arm[0]
        try:
            if op == "knob_sample":
                return self._wrap(knob_sample(parent, parent_id, ctx))
            if op == "knob_perturb":
                return self._wrap(knob_perturb(parent, parent_id, ctx))
            if op == "knob_reset":
                return self._wrap(knob_reset(parent, parent_id, ctx))
            if op == "py_rewrite":
                return self._py_rewrite(parent, parent_id, ctx)
            if op == "gi_edit":
                return self._gi_edit(parent, parent_id, ctx)
            if op == "crossover":
                if second is None:
                    return ProposalResult(None, reject_reason="crossover needs a second parent")
                return self._wrap(gene_crossover(parent, parent_id, second[0], second[1], ctx))
            if op == "llm_rewrite":
                return self._llm(arm, parent, parent_id, ctx)
            if op == "redteam":
                return self._redteam(parent, parent_id, ctx)
            if op == "rule_apply":
                return self._rule_apply(arm, parent, parent_id, ctx)
        except LocusConflict as exc:
            return ProposalResult(None, reject_reason=f"locus conflict: {exc.why}")
        return ProposalResult(None, reject_reason=f"unknown operator {op}")

    def _wrap(self, proposal: Proposal | None) -> ProposalResult:
        if proposal is None:
            return ProposalResult(None, reject_reason="operator produced no change")
        return ProposalResult(proposal)

    # ------------------------------------------------------------------ code: deterministic
    def _pick_code_locus(self, ctx: OperatorContext) -> str | None:
        loci = ctx.code_loci()
        if not loci:
            return None
        weights = [max(ctx.opportunity.get(lid, 0.0), 1e-4) for lid in loci]
        return ctx.rng.choices(loci, weights=weights, k=1)[0]

    def _py_rewrite(self, parent: Genome, parent_id: str, ctx: OperatorContext) -> ProposalResult:
        loci = [lid for lid in ctx.code_loci() if self.atlas.units[self.atlas.loci[lid].unit_id].tags.get("language") == "python"]
        ctx.rng.shuffle(loci)
        for lid in loci:
            unit = self.atlas.units[self.atlas.loci[lid].unit_id]
            source = self.unit_source(unit.id, parent)
            sites = find_rewrites(source)
            if not sites:
                continue
            site = ctx.rng.choice(sites)
            new = site.apply()
            gene = code_gene(lid, unit, source, new, Provenance(operator="py_rewrite", template=site.rule, notes=site.description))
            try:
                child = parent.with_gene(gene, self.atlas)
            except LocusConflict:
                continue
            return ProposalResult(Proposal(child, parent_id, "py_rewrite", template=site.rule, changed_loci=(lid,), notes=site.description))
        return ProposalResult(None, reject_reason="no applicable peephole rewrite")

    def _gi_edit(self, parent: Genome, parent_id: str, ctx: OperatorContext) -> ProposalResult:
        loci = [lid for lid in ctx.code_loci() if self.atlas.units[self.atlas.loci[lid].unit_id].tags.get("language") == "python"]
        ctx.rng.shuffle(loci)
        for lid in loci:
            unit = self.atlas.units[self.atlas.loci[lid].unit_id]
            source = self.unit_source(unit.id, parent)
            res = gi_edit(source, ctx.rng)
            if res is None:
                continue
            new, desc = res
            gene = code_gene(lid, unit, source, new, Provenance(operator="gi_edit", notes=desc))
            try:
                child = parent.with_gene(gene, self.atlas)
            except LocusConflict:
                continue
            return ProposalResult(Proposal(child, parent_id, "gi_edit", changed_loci=(lid,), notes=desc))
        return ProposalResult(None, reject_reason="no GI edit produced")

    # ------------------------------------------------------------------ learned rules
    def _rule_apply(self, arm: Arm, parent: Genome, parent_id: str, ctx: OperatorContext) -> ProposalResult:
        rule = arm[2] or ""
        allowed = set(ctx.region_loci)
        cfg = ctx.effective_config(parent)
        options = [(lid, what) for lid, what in self.rule_options.get(rule, [])
                   if lid in allowed and cfg.get(self.knob_of_locus.get(lid, "")) is not True]
        if not options:
            return ProposalResult(None, reject_reason=f"rule {rule}: every proposal is already applied or outside this region")
        lid, what = ctx.rng.choice(options)
        gene = Gene.make(lid, PayloadKind.VALUE, {"value": True}, Provenance(operator="rule_apply", template=rule, notes=what))
        try:
            child = parent.with_gene(gene, self.atlas)
        except LocusConflict as exc:
            return ProposalResult(None, reject_reason=f"locus conflict: {exc.why}")
        return ProposalResult(Proposal(child, parent_id, "rule_apply", template=rule, changed_loci=(lid,), notes=f"CRL {rule}: {what}"))

    # ------------------------------------------------------------------ code: LLM
    def _llm(self, arm: Arm, parent: Genome, parent_id: str, ctx: OperatorContext) -> ProposalResult:
        _, model, template = arm
        provider = next((p for p in self.providers.values() if any(m.name == model for m in p.models())), None)
        if provider is None or model is None:
            return ProposalResult(None, reject_reason=f"no provider serves model {model}")
        lid = self._pick_code_locus(ctx)
        if lid is None:
            return ProposalResult(None, reject_reason="no code locus in region")
        unit = self.atlas.units[self.atlas.loci[lid].unit_id]
        language = str(unit.tags.get("language", "python"))
        source = self.unit_source(unit.id, parent)
        tctx = self.mutation_context(unit.id)
        mem = self._memory(lid)
        paths = [self.atlas.units[p.unit_ids[0]].name for p in self.atlas.paths_through(unit.id) if p.unit_ids]
        mctx = MutationContext(
            locus_id=lid, unit_name=unit.name, module=str(unit.tags.get("file", "")), layer=unit.layer.value, language=language,
            source=source, template=template or "optimize", leverage=self.atlas.tag(unit.id, "causal_leverage"),
            hotness=self.atlas.tag(unit.id, "hotness"), paths=tuple(dict.fromkeys(paths))[:4],
            target_context=str(tctx.get("target_context", "")), available_names=tuple(tctx.get("available_names", ())),
            neighbours=tuple(mem.neighbours), failures=summarize_failures(mem.failures),
        )
        req = build_request(mctx, max_tokens=self.max_tokens)
        try:
            completion = provider.complete(model, req.system, req.messages, max_tokens=req.max_tokens, temperature=req.temperature)
        except LLMError as exc:
            return ProposalResult(None, reject_reason=f"LLM error: {exc}")
        rec = LLMCallRecord(
            id=sha256_hex(req.prompt_hash + str(ctx.rng.random()))[:16], provider=provider.name, model=completion.model,
            template=req.template, params={"max_tokens": req.max_tokens, "temperature": req.temperature},
            prompt_hash=req.prompt_hash, response_hash=sha256_hex(completion.text)[:16], tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out, latency_s=completion.latency_s, cost_usd=completion.cost_usd, ok=True,
        )
        if language == "python":
            parsed = parse_python_response(completion.text, source)
        elif language == "c":
            parsed = parse_c_response(completion.text, source, unit.name)
        elif self.parse_code is not None:
            parsed = self.parse_code(completion.text, source, unit)
        else:
            parsed = ParseResult(False, reason=f"no response parser for {language}")
        if not parsed.ok:
            mem.add_failure(parsed.reason)
            return ProposalResult(None, rec, reject_reason=f"LLM response rejected: {parsed.reason}")
        gene = code_gene(lid, unit, source, parsed.source, Provenance(
            operator="llm_rewrite", model=completion.model, template=req.template, prompt_hash=req.prompt_hash, response_hash=rec.response_hash))
        try:
            child = parent.with_gene(gene, self.atlas)
        except LocusConflict as exc:
            return ProposalResult(None, rec, reject_reason=f"locus conflict: {exc.why}")
        notes = f"{unit.name}: {diff_lines(source, parsed.source)} lines via {req.template}"
        return ProposalResult(Proposal(child, parent_id, "llm_rewrite", model=completion.model, template=req.template, changed_loci=(lid,), notes=notes), rec)

    # ------------------------------------------------------------------ redteam
    def _redteam(self, parent: Genome, parent_id: str, ctx: OperatorContext) -> ProposalResult:
        # Only units on a request path whose function returns a value: elsewhere the attack is
        # inert by construction and an oracle "pass" would be a false breach.
        loci = []
        for lid in ctx.code_loci():
            unit = self.atlas.units[self.atlas.loci[lid].unit_id]
            if unit.tags.get("language") == "python" and self.atlas.paths_through(unit.id) and attack_reachable(self.unit_source(unit.id, parent)):
                loci.append(lid)
        if not loci:
            return ProposalResult(None, reject_reason="no reachable code locus for red team")
        lid = ctx.rng.choice(loci)
        hack = ctx.rng.choice(HACKS)
        return self._redteam_at(parent, parent_id, lid, hack, ctx.rng, maximal=False)

    def redteam_maximal(self, attack: Proposal, rng: random.Random) -> Proposal | None:
        """The same hack on the same locus at its extreme setting (liveness confirmation)."""
        hack = attack.notes.rsplit(":", 1)[-1].strip()
        if hack not in HACKS or not attack.changed_loci:
            return None
        parent = Genome.of([g for g in attack.genome if g.locus_id != attack.changed_loci[0]], self.atlas)
        res = self._redteam_at(parent, attack.parent_id, attack.changed_loci[0], hack, rng, maximal=True)
        return res.proposal

    def _redteam_at(self, parent: Genome, parent_id: str, lid: str, hack: str, rng: random.Random, *, maximal: bool) -> ProposalResult:
        unit = self.atlas.units[self.atlas.loci[lid].unit_id]
        source = self.unit_source(unit.id, parent)
        new = redteam_variant(source, hack, rng, maximal=maximal)
        if new is None:
            return ProposalResult(None, reject_reason="red-team variant not produced")
        kind = "maximal liveness probe" if maximal else "deliberate evaluator attack"
        gene = code_gene(lid, unit, source, new, Provenance(operator="redteam", notes=f"{kind}: {hack}"))
        try:
            child = parent.with_gene(gene, self.atlas)
        except LocusConflict:
            return ProposalResult(None, reject_reason="locus conflict")
        label = "red-team maximal" if maximal else "red-team attack"
        return ProposalResult(Proposal(child, parent_id, "redteam", changed_loci=(lid,), notes=f"{label} on {unit.name}: {hack}"))

    # ------------------------------------------------------------------ splice
    def splice(self, union_genes: Sequence[Gene], parents: Sequence[str]) -> ProposalResult:
        try:
            child = Genome.of(union_genes, self.atlas)
        except LocusConflict as exc:
            return ProposalResult(None, reject_reason=f"splice conflict: {exc.why}")
        return ProposalResult(Proposal(child, parents[0] if parents else "baseline", "splice", changed_loci=tuple(g.locus_id for g in child), notes=f"union of {len(parents)} elites"))


def signature_of(genome: Genome, unit_source: Callable[[str, Genome], str], atlas: StackAtlas) -> MinHash:
    """A code-embedding signature of the whole program: concatenated source of every mutated
    code unit, plus a line per knob gene (so knob-only genomes still get distinct signatures)."""
    parts = []
    for g in sorted(genome.genes, key=lambda x: x.id):
        unit = atlas.units[atlas.loci[g.locus_id].unit_id]
        if g.payload_kind == PayloadKind.SOURCE:
            parts.append(unit_source(unit.id, genome))
        else:
            parts.append(f"{unit.name}={g.value}")
    return MinHash.of_code("\n".join(parts) if parts else "baseline")
