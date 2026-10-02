# ADR 0005 — The mutation data lake: verified knowledge, content-addressed and hash-chained

**Status:** accepted

## Context

Without memory, a run forgets everything when it ends. The expensive output of a run is not
the winning program. It is the *verified knowledge* behind it:
- which change, at which locus, written against which source;
- the measured effect with confidence intervals, holdout persistence and attribution;
- the platform it was measured on.

That knowledge should accumulate across runs, days, machines and targets. It must be
impossible to forge or reorder silently, because future runs act on it.

## Decision

- **Records.** A *gene* record describes one change in language- and platform-neutral terms:
  - the locus (unit path, surface, layer, language);
  - the payload, plus the hash of the source it was written against;
  - an explanation and provenance.

  A *program* record describes a verified combination of genes and its evidence: effects
  with CIs and protocol, holdout, Shapley and leave-one-gene-out attribution, ablation
  summary, noise floor, platform, run and engine commit.
- **Addressing.** `id = sha256(schema ‖ kind ‖ canonical JSON)`, where canonical JSON means
  sorted keys, no NaN and UTF-8. The same knowledge has the same id on every machine, and
  re-ingesting it is a no-op. Idempotency is decided on the evidence alone, excluding the
  lineage links, which depend on what else the lake holds.
- **Order.** An append-only ledger. Entry *n* = `{seq, record, kind, prev, recorded_at,
  entry_hash}` with `prev` = entry *n−1*'s hash. The head hash commits to the whole history.
  `verify_chain` detects edits, reordering, deletion, clock rewinds, forward references and
  kind swaps. *Older and newer* is `seq`, and the chain makes it tamper-evident.
- **Lineage.** `derived_from` links a program record to earlier records it extends (strict
  subsets of its genes) or supersedes (the same genes with newer evidence).
- **Admission.** Only *promoted* or *L6-verified* programs enter the lake. A good L5 score is
  not knowledge.
- **Storage.** The same layout serves both stores. `DirectoryLake` is portable (atomic
  replace, `O_EXCL` lock). `GitBranchLake` is a parentless data-only branch
  (`colloid/datalake`) written with git plumbing and compare-and-swap ref updates, and never
  touches the checkout. Git adds a second hash chain, plus history and mirroring.
- **Use.**
  - *Seeds*: the best applicable programs re-enter generation 1 and go through the full
    cascade. The lake is a prior, never a verdict.
  - *Operator priors*: attribution evidence becomes weighted pseudo-observations for the
    bandit.
  - *Materialised stacks*: `colloid stack` turns a record into a deployable artifact on its
    own branch.

## Consequences

- Every discovery has a stable, verifiable identity, and its provenance survives the run
  directory, the machine and the engine version.
- A gene applies to a new stack only while its locus exists and its `base_hash` matches.
  Stale knowledge is skipped, never misapplied.
- The lake is append-only. Corrections are new records that supersede, never edits.
