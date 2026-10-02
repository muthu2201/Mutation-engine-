"""Genome algebra: content addressing, conflict detection, commutativity/associativity (T04)."""

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from colloid.core.genome import Genome, LocusConflict
from colloid.core.models import Gene, Locus, Mutability, PayloadKind, Provenance, RiskClass, Surface


class FakeResolver:
    """Loci l0..l9; l{i} and l{i+100} are ancestor/descendant (share a unit family)."""

    def __init__(self):
        self.loci = {}
        for i in range(20):
            self.loci[f"l{i}"] = Locus(id=f"l{i}", unit_id=f"u{i}", surface=Surface.KNOB, risk_class=RiskClass.A, mutability=Mutability.ALLOWED)

    def locus(self, locus_id):
        return self.loci[locus_id]

    def units_related(self, a, b):
        return a == b


def gene(locus_id, value):
    return Gene.make(locus_id, PayloadKind.VALUE, {"value": value}, Provenance(operator="test"))


def test_content_address_dedup():
    g1 = gene("l0", 5)
    g2 = gene("l0", 5)
    assert g1.id == g2.id  # same locus+payload -> same id regardless of provenance object
    g3 = Gene.make("l0", PayloadKind.VALUE, {"value": 5}, Provenance(operator="other"))
    assert g3.id == g1.id


def test_program_id_order_independent():
    r = FakeResolver()
    a = Genome.of([gene("l0", 1), gene("l1", 2)], r)
    b = Genome.of([gene("l1", 2), gene("l0", 1)], r)
    assert a.program_id("base") == b.program_id("base")


def test_same_locus_conflict():
    r = FakeResolver()
    with pytest.raises(LocusConflict):
        Genome.of([gene("l0", 1), gene("l0", 2)], r)


def test_with_gene_replaces_locus():
    r = FakeResolver()
    g = Genome.of([gene("l0", 1), gene("l1", 2)], r)
    g2 = g.with_gene(gene("l0", 9), r)
    assert len(g2) == 2
    assert g2.by_locus()["l0"].value == 9


@given(ids=st.lists(st.integers(0, 9), min_size=0, max_size=6, unique=True), vals=st.lists(st.integers(0, 3), min_size=10, max_size=10))
@settings(max_examples=200, deadline=None)
def test_union_commutative_and_associative(ids, vals):
    r = FakeResolver()
    genes = [gene(f"l{i}", vals[i]) for i in ids]
    rng = random.Random(sum(ids))
    perm = genes[:]
    rng.shuffle(perm)
    a = Genome.of(genes, r)
    b = Genome.of(perm, r)
    assert a.gene_ids == b.gene_ids  # canonical order -> identical representation
    # union with a disjoint genome, both orders
    extra = Genome.of([gene("l15", 1)], r)
    assert a.union(extra, r).gene_ids == extra.union(a, r).gene_ids


def test_without_and_subset():
    r = FakeResolver()
    g = Genome.of([gene("l0", 1), gene("l1", 2), gene("l2", 3)], r)
    ids = list(g.gene_ids)
    assert len(g.without([ids[0]])) == 2
    assert set(g.subset(ids[:2]).gene_ids) == set(ids[:2])


def test_conflict_detection_without_resolver_on_same_locus():
    with pytest.raises(LocusConflict):
        Genome.of([gene("l0", 1), gene("l0", 2)])
