# ADR 0009 — CRL: a rule language seeded by evidence ("grammar seeds")

**Status:** accepted (v0)

## Context

The goal is to turn what Colloid discovers into a language of its own: patterns stated once,
applicable to any stack in any implementation language, eventually compiled to several
backends.

ADR 0007 named the only defensible form of a "language from mutations": declarative rewrite
rules mined from recurring verified changes, each citing the lake records that justify it,
whose worth is decided by a held-out test. Two failure modes have to be avoided:

1. a grammar designed ahead of the evidence, bloated with constructs nothing has earned;
2. rules that are opinions, with no measurement behind them.

## Decision

1. **CRL v0** (`colloid.core.rules`) is a small, versioned, declarative language with a
   hand-written lexer and recursive-descent parser (located errors), a validator, a canonical
   printer (print ∘ parse is the identity), and identity by content. A rule's `rule_id`
   hashes what it *says* (name, version, `when`, `propose`, `unless`). Its lake record hashes
   everything, evidence included.
2. **The grammar contains only what evidence has needed.** At v0 the lake's verified,
   carrying genes are two database indexes, so the language has exactly:
   - one pattern form, an equality filter optionally with the sort order of the same read,
     over language-neutral *query facts* (`sqlfacts`);
   - one proposal form, an index;
   - one guard, `unless covered`, where primary keys and indexes already in the genome cover
     the proposal.

   A new construct enters the grammar by an ADR citing the verified evidence that needs it.
3. **Rules are learned, and the parser enforces it.** A rule without an evidence line, or with
   an evidence CI that includes zero, does not parse. `colloid rules mine` derives rules from
   the lake:
   - each carrying index gene is explained against the Atlas of the target that verified it,
     by the query it serves;
   - each explanation becomes an evidence line carrying the gene's *own* leave-one-out
     contribution and CI, not the whole program's gain.

   A verified change that v0 cannot express is reported as unexplained, and nothing is
   invented for it.
4. **Rules propose, the judge decides.** In the engine each rule is a `rule_apply` arm. It
   proposes the index knob gene of one of the rule's proposals for the current stack, and the
   proposal goes through L0–L6 like any other. The bandit learns which rules pay; the lake
   records the outcome, and that outcome becomes new evidence for or against the rule.
5. **Rules are knowledge, so they live in the lake.** Rule records are hash-chained in the
   same ledger as genes and programs (record kind `rule`), on the `colloid/datalake` branch.
   The language implementation is code and lives on `main`.

## What v0 shows and what it does not

- **Shown:**
  - Mined from the Python implementation's evidence, the two v0 rules produce *identical*
    proposals on the Go implementation's Atlas. Query facts are language-neutral, so a rule
    learned in one language applies in another without translation.
  - The rules re-derive the two verified indexes. They also propose five more candidates,
    which the evaluator, not the rule, will judge.
- **Not shown:** generalisation. Both implementations share one schema; a rule re-proposing
  its own source is not a generalisation test. The M2 gate (ADR 0008) needs a held-out system
  with a different schema, and stays open until one exists and a rule-applied gene is
  verified on it.

## Consequences

- The proprietary asset is the corpus of evidence-carrying rules and the lake they cite,
  not a syntax. The syntax is cheap; verified rules are not.
- Growth path, each step gated by evidence:
  1. more pattern forms (range filters, joins, N+1 loops into set-based queries) as verified
     instances of them appear;
  2. proposals beyond index knobs (dynamic index loci, query rewrites) once a target can
     express them;
  3. code-level backends per language (M3) only after M2.
