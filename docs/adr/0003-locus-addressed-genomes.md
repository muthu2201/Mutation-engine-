# ADR 0003 — Genomes are sparse, locus-addressed sets of genes

**Status:** accepted

## Context

The engine must mutate many layers at once (kernel knobs, allocator settings, compiler flags,
SQL indexes, service code, C code) and must be able to *combine* improvements found
independently in different layers. A representation that stores whole modified programs, or an
ordered edit script, makes cross-layer composition and per-change attribution hard.

## Decision

A **gene** is `(locus, payload)` — one change at one mutable site, where a locus is
`(unit, surface)` in the Stack Atlas. A **genome** is a *sparse set* of genes applied on top of
the fixed baseline; the baseline is the genome with zero genes. This is the Genetic-Improvement
patch representation generalised across every layer.

Everything is content-addressed: a gene's id is `hash(locus, payload)`, a program's id is
`hash(baseline, sorted gene ids)`.

## Consequences

- **Composition is set union.** Genomes from different layers combine with no merge logic, as
  long as their loci do not overlap (no shared unit, no ancestor/descendant conflict in the
  Atlas). This is what makes the Composition Island and cross-layer splicing possible.
- **Attribution is set-theoretic.** "Which genes earn their keep" is answered directly by
  Shapley values over gene subsets, and bloat (zero/negative Shapley) is pruned.
- **Deduplication and reproducibility are free.** Two operators proposing the same change
  produce the same id (evaluated once); any program rebuilds exactly from its gene payloads.
- **Conflicts are explicit.** `Genome.union`/`with_gene` raise `LocusConflict` on overlapping
  loci, and the policy scanner re-checks this at L0, so an invalid genome never reaches a build.
- Tests, oracles, and frozen knobs are deliberately *not* loci, so "edit the tests" or "disable
  fsync" cannot be expressed as genes at all.
