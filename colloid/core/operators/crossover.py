"""Gene-set crossover.

Because genomes are sets of locus-addressed genes, crossover is simple and always yields a
well-formed child: for every locus present in either parent, the child takes the gene of
parent A, the gene of parent B, or (with small probability) neither. Loci where both
parents agree are inherited unchanged. If the resulting set contains conflicting genes
(ancestor/descendant code units) the later choice is dropped, so the child is valid by
construction. Knob requirements are re-checked against the child's own configuration and
inert genes are removed.
"""

from __future__ import annotations

from colloid.core.genome import Genome, LocusConflict, genes_conflict
from colloid.core.knobs import requirement_met
from colloid.core.models import Gene
from colloid.core.operators.base import OperatorContext, Proposal


def gene_crossover(a: Genome, a_id: str, b: Genome, b_id: str, ctx: OperatorContext, drop_p: float = 0.1) -> Proposal | None:
    by_a, by_b = a.by_locus(), b.by_locus()
    loci = sorted(set(by_a) | set(by_b))
    chosen: list[Gene] = []
    changed: list[str] = []
    for lid in loci:
        ga, gb = by_a.get(lid), by_b.get(lid)
        if ga is not None and gb is not None and ga.id == gb.id:
            pick: Gene | None = ga
        else:
            r = ctx.rng.random()
            options = [g for g in (ga, gb) if g is not None]
            pick = None if r < drop_p else options[int(ctx.rng.random() * len(options))]
            if pick is not ga:
                changed.append(lid)
        if pick is not None and all(genes_conflict(pick, c, ctx.atlas) is None for c in chosen):
            chosen.append(pick)
    try:
        child = Genome.of(chosen, ctx.atlas)
    except LocusConflict:
        return None
    # Remove knob genes that became inert in the child's configuration.
    cfg = ctx.effective_config(child)
    inert = [g.id for g in child if g.locus_id in ctx.knob_of_locus and not requirement_met(ctx.knobs[ctx.knob_of_locus[g.locus_id]], cfg)]
    child = child.without(inert)
    if child.gene_ids in (a.gene_ids, b.gene_ids):
        return None
    return Proposal(child, a_id, "crossover", changed_loci=tuple(changed), second_parent_id=b_id, notes=f"cross {a_id[:8]} x {b_id[:8]}")
