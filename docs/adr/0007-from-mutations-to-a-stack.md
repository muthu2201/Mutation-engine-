# ADR 0007 — From verified mutations to a stack: what is real, what is not, how to get further

**Status:** accepted

## Context

The vision behind this ADR:
- treat the efficient mechanisms Colloid discovers as bare-metal-agnostic building blocks;
- build from them a new language and a full stack, from language to UI;
- feed them back into the engine, until it becomes a timeless, universal compatibility layer
  in which every existing and future language blends seamlessly.

This ADR records what the evidence supports today, what was built in response, and the path
by which the ambitious version could be pursued and *falsified*.

## What the evidence supports

- **What the engine actually found.** The first full run produced four verified programs.
  Leave-one-gene-out ablation of the best one (`INITIAL_RESULTS.md` §4) shows its entire
  +31% cost reduction comes from two database indexes. The three code genes in it (two
  LLM-rewritten C functions and a line-level GI edit) contribute nothing distinguishable from
  zero. Mutation search finds *local improvements to an existing stack*. It does not invent
  new semantics, and nothing in these results is a primitive from which a language could
  honestly be derived.
- **"Compatible with every future language" cannot be guaranteed by any system**, because
  the languages do not exist yet. What *does* work in practice, as in LLVM IR, WebAssembly,
  the Language Server Protocol and tree-sitter, is a **stable, versioned interchange format
  plus a small adapter contract that each language implements**.

## Decision

1. **Build the real version of the compatibility layer.** Colloid's interchange format is the
   mutation record (`colloid.mutation/1`, ADR 0005). It is language- and platform-neutral:
   loci are named by symbol path, surface, layer and language; payloads are source text or
   knob values; evidence is attached. A language joins by implementing the
   `CodeRepresentation` port (enumerate units, splice a replacement), as Python and C do
   today. That is the honest form of "any language can plug in".
2. **Ship verified artifacts, not claims.** `colloid stack materialize` turns a lake record
   into a deployable stack: code with source genes applied, idempotent migrations, settings,
   and a manifest that ties every change to its evidence. `colloid stack publish` writes it as
   a data-only branch. The first is `stack/stackzero-verified`: two indexes, +31.3% cost
   (CI [+28.2%, +34.1%]), with hitchhikers left out and the exact provenance of its evidence
   stated.
3. **Branch layout.** One branch per kind of thing:
   - the engine code;
   - `colloid/datalake`: knowledge, append-only and hash-chained;
   - `stack/<target>-verified`: deployable artifacts, one commit per materialisation.

   A new branch is created only when there is a real artifact to put on it. Empty
   placeholder branches are not created.
4. **The road to anything larger runs through falsifiable milestones**, each with a gate that
   can fail:
   - **M1: a second target in another language** (a Go or Rust service) via a new
     `CodeRepresentation` adapter. *Gate:* runs on both targets produce verified lake
     records, and a lake-primed run on target B beats a cold run on B in an A/B of
     verified-gain-per-hour. Otherwise knowledge does not transfer and M2 is moot.
   - **M2: mined transformation rules.** Recurring verified changes across at least three
     targets (for example "index the foreign key read inside an N+1 loop", or "batch an N+1
     query") are distilled into declarative, language-neutral rewrite rules. This small rule
     language is the only defensible "language from mutations": every rule cites the lake
     records that justify it. *Gate:* a mined rule reproduces a verified gain on a held-out
     target that it was not mined from.
   - **M3: rule backends.** Only if M2's rules generalise: compile them to several languages'
     adapters, and let the engine propose them as a new operator, judged like any other. The
     judge stays frozen (ADR 0006).

## Consequences

- Nothing is presented as more than it is. The results show where the leverage actually is:
  data access patterns. The engine is pointed there instead of at an imagined language.
- Each milestone produces evidence in the lake whether it succeeds or fails, so the decision
  to go further is made on measurements.
