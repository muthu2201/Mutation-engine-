"""Knob operators: global sampling, local perturbation and reset.

* ``knob_sample``  - pick a knob locus (weighted by opportunity, with exploration) whose
  requirements are satisfied in the parent's effective configuration, and draw a fresh
  uniformly random value. This is the "basin hop" move for knobs.
* ``knob_perturb`` - take a knob the parent already sets (or any region knob) and move it
  a Gaussian step in normalised space. This is the "local polish" move.
* ``knob_reset``   - delete one knob gene (return it to the baseline default). This is an
  explicit anti-bloat move: if the reset child is not worse, neutral drift accepts the
  smaller genome.

Inert genes (knobs whose ``requires`` is unmet, e.g. a jemalloc setting while glibc malloc
is selected) are never produced.
"""

from __future__ import annotations

from colloid.core.budget import pick_locus
from colloid.core.genome import Genome
from colloid.core.knobs import perturb_value, requirement_met, sample_value
from colloid.core.models import Gene, PayloadKind, Provenance
from colloid.core.operators.base import OperatorContext, Proposal


def _applicable(ctx: OperatorContext, genome: Genome) -> list[str]:
    cfg = ctx.effective_config(genome)
    out = []
    for lid in ctx.knob_loci():
        spec = ctx.knobs[ctx.knob_of_locus[lid]]
        if spec.mutability == "frozen":
            continue
        if requirement_met(spec, cfg):
            out.append(lid)
    return out


def _with_value(ctx: OperatorContext, genome: Genome, locus_id: str, value: object, operator: str) -> Genome:
    spec = ctx.knobs[ctx.knob_of_locus[locus_id]]
    if value == spec.default:
        # Setting a knob to its default is the same program as not setting it.
        return genome.without([g.id for g in genome if g.locus_id == locus_id])
    gene = Gene.make(locus_id, PayloadKind.VALUE, {"value": value}, Provenance(operator=operator))
    return genome.with_gene(gene, ctx.atlas)


def knob_sample(parent: Genome, parent_id: str, ctx: OperatorContext) -> Proposal | None:
    loci = _applicable(ctx, parent)
    if not loci:
        return None
    for _ in range(8):
        lid = pick_locus(loci, ctx.opportunity, ctx.rng)
        spec = ctx.knobs[ctx.knob_of_locus[lid]]
        current = ctx.effective_config(parent)[spec.name]
        value = sample_value(spec, ctx.rng)
        if value != current:
            child = _with_value(ctx, parent, lid, value, "knob_sample")
            return Proposal(child, parent_id, "knob_sample", changed_loci=(lid,), notes=f"{spec.name}: {current!r} -> {value!r}")
    return None


def knob_perturb(parent: Genome, parent_id: str, ctx: OperatorContext, sigma: float = 0.15) -> Proposal | None:
    applicable = set(_applicable(ctx, parent))
    present = [g.locus_id for g in parent if g.locus_id in applicable]
    pool = present if present and ctx.rng.random() < 0.7 else sorted(applicable)
    if not pool:
        return None
    lid = ctx.rng.choice(pool)
    spec = ctx.knobs[ctx.knob_of_locus[lid]]
    current = ctx.effective_config(parent)[spec.name]
    value = perturb_value(spec, current, ctx.rng, sigma)
    if value == current:
        return None
    child = _with_value(ctx, parent, lid, value, "knob_perturb")
    return Proposal(child, parent_id, "knob_perturb", changed_loci=(lid,), notes=f"{spec.name}: {current!r} -> {value!r}")


def knob_reset(parent: Genome, parent_id: str, ctx: OperatorContext) -> Proposal | None:
    region = set(ctx.knob_loci())
    present = [g for g in parent if g.locus_id in region]
    if not present:
        return None
    victim = ctx.rng.choice(present)
    child = parent.without([victim.id])
    name = ctx.knob_of_locus[victim.locus_id]
    return Proposal(child, parent_id, "knob_reset", changed_loci=(victim.locus_id,), notes=f"{name}: reset to default")
