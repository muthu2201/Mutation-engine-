"""Genomes: sparse, locus-addressed sets of genes.

This is the Genetic-Improvement "patch representation" generalised across layers. A genome
never stores a whole program; it stores the *difference* from the baseline as a set of
genes, each pinned to exactly one locus of the Stack Atlas.

Why a set and not a list of edits?

* Genomes from different layers compose by plain set union, provided their loci do not
  overlap. That is what lets the Composition Island splice a kernel-knob winner with an
  allocator winner and a service-code winner without any merge logic.
* Attribution becomes the question "which elements of this set earn their keep", which is
  exactly what Shapley values answer.

Two genes *conflict* when they target the same locus with different payloads, or when
their units are in an ancestor/descendant relationship in the Atlas (rewriting a whole
function and, separately, a loop inside it cannot both be applied). Conflict checks need the
Atlas, so they go through the small :class:`LocusResolver` protocol rather than importing it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

from colloid.core.models import Gene, Locus, Program


class LocusConflict(ValueError):
    """Raised when two genes cannot coexist in one genome."""

    def __init__(self, a: Gene, b: Gene, why: str) -> None:
        super().__init__(f"gene {a.id} conflicts with gene {b.id}: {why}")
        self.a = a
        self.b = b
        self.why = why


class LocusResolver(Protocol):
    def locus(self, locus_id: str) -> Locus: ...

    def units_related(self, unit_a: str, unit_b: str) -> bool:
        """True if the units are identical or one is an ancestor of the other."""
        ...


def genes_conflict(a: Gene, b: Gene, resolver: LocusResolver) -> str | None:
    """Return a human-readable reason if ``a`` and ``b`` conflict, else ``None``."""
    if a.id == b.id:
        return None
    if a.locus_id == b.locus_id:
        return "same locus, different payloads"
    la, lb = resolver.locus(a.locus_id), resolver.locus(b.locus_id)
    if la.unit_id != lb.unit_id and resolver.units_related(la.unit_id, lb.unit_id):
        return "ancestor/descendant units in the Atlas"
    if la.unit_id == lb.unit_id and la.surface == lb.surface:
        return "same unit and surface"
    return None


@dataclass(frozen=True, slots=True)
class Genome:
    """An immutable, canonically ordered set of genes (sorted by gene id)."""

    genes: tuple[Gene, ...] = ()

    # ------------------------------------------------------------------ construction
    @staticmethod
    def of(genes: Iterable[Gene], resolver: LocusResolver | None = None) -> Genome:
        """Build a genome, de-duplicating identical genes and (when a resolver is given)
        rejecting conflicting ones."""
        unique: dict[str, Gene] = {}
        for g in genes:
            unique[g.id] = g
        ordered = tuple(sorted(unique.values(), key=lambda g: g.id))
        genome = Genome(ordered)
        if resolver is not None:
            genome.check(resolver)
        else:
            seen: dict[str, Gene] = {}
            for g in ordered:
                if g.locus_id in seen:
                    raise LocusConflict(seen[g.locus_id], g, "same locus, different payloads")
                seen[g.locus_id] = g
        return genome

    def check(self, resolver: LocusResolver) -> None:
        """Raise :class:`LocusConflict` for the first conflicting pair."""
        genes = self.genes
        for i in range(len(genes)):
            for j in range(i + 1, len(genes)):
                why = genes_conflict(genes[i], genes[j], resolver)
                if why:
                    raise LocusConflict(genes[i], genes[j], why)

    # ------------------------------------------------------------------ algebra
    def union(self, other: Genome, resolver: LocusResolver) -> Genome:
        """Set union. Commutative and associative for non-conflicting operands; raises
        :class:`LocusConflict` otherwise."""
        return Genome.of((*self.genes, *other.genes), resolver)

    def with_gene(self, gene: Gene, resolver: LocusResolver) -> Genome:
        """Add ``gene``, *replacing* any gene at the same locus (mutation semantics)."""
        kept = [g for g in self.genes if g.locus_id != gene.locus_id]
        return Genome.of((*kept, gene), resolver)

    def without(self, gene_ids: Iterable[str]) -> Genome:
        drop = set(gene_ids)
        return Genome(tuple(g for g in self.genes if g.id not in drop))

    def subset(self, gene_ids: Iterable[str]) -> Genome:
        keep = set(gene_ids)
        return Genome(tuple(g for g in self.genes if g.id in keep))

    # ------------------------------------------------------------------ views
    @property
    def gene_ids(self) -> tuple[str, ...]:
        return tuple(g.id for g in self.genes)

    def program_id(self, baseline_id: str) -> str:
        return Program.make_id(baseline_id, self.gene_ids)

    def by_locus(self) -> dict[str, Gene]:
        return {g.locus_id: g for g in self.genes}

    def get(self, gene_id: str) -> Gene | None:
        for g in self.genes:
            if g.id == gene_id:
                return g
        return None

    def __len__(self) -> int:
        return len(self.genes)

    def __iter__(self) -> Iterator[Gene]:
        return iter(self.genes)

    def __contains__(self, gene_id: object) -> bool:
        return any(g.id == gene_id for g in self.genes)
