# ADR 0008 — Polyglot neutrality, cross-implementation transfer, and the evidence ladder

**Status:** accepted (criteria pre-registered before the measurements they judge; this file
was committed before the M1 runs started)

## Context

ADR 0007 set falsifiable milestones on the way from "a mutation engine that finds verified
wins" to anything larger (learned rules, rule backends). It named M1 first: a second target
in another language, judged by the same evaluator, and a test of whether knowledge
transfers. The first experiment used Python + C. Every claim of neutrality so far rested on
one implementation.

## Decision

1. **Same contract, several languages.** The StackZero shop contract (HTTP API, Postgres
   schema, SQL, seed data, the deliberate first-version inefficiencies) gets more
   implementations, each a faithful port in idiomatic code of its language. Go
   (`targets/stackzero-go`, pgx) is a full Colloid target. TypeScript
   (`targets/stackzero-ts`, node-postgres) is one codebase run on Node and on Bun. Porting
   faithfully, same algorithms and different language, isolates what the language and
   runtime contribute.
2. **Conformance is decided by the judge, not by inspection.** A port counts as an
   implementation of the contract when the evaluator's differential oracle finds no mismatch
   against the Python reference on its request sequences (edge cases, write-then-read chains,
   repeats after writes). Every result in this ADR is conditioned on that.
3. **One judge for every language.** Workloads, oracles, the benchmark protocol, the A/A
   gate and the statistics are shared. What is language-specific is the static half of the
   judge, each written against the language's own parser:
   - the L0 policy scanner (`colloid_evaluator/gopolicy` for Go);
   - a canary suite per language that must be 100% rejected;
   - the language-specific deep checks: the C differential fuzzer and sanitizers, the Go
     kernel differential fuzzer and race detector.

   Two checks are language-neutral and apply to all implementations:
   - an SQL policy on every gene's string constants;
   - a dynamic SQL audit of what a candidate actually sent.

   Session settings and temporary tables on a pooled connection are a cross-request cache no
   host-language scan can see. That gap was found while porting, and it existed for Python
   too.
4. **Transfer through shared loci only.** A database index proven on one implementation is
   the same locus on another. Knobs transfer only when their specification (mechanism, key,
   type, unit, DDL) is identical (`knob_fingerprint`). Code genes never cross languages.
   Transferred genes are candidates that must pass the receiving run's own cascade.
5. **The evidence ladder** (`colloid ladder`, `colloid.core.ladder`) computes which rungs
   the evidence has earned. Nothing above an open rung is claimed. The gates:

   | rung | gate |
   |---|---|
   | J | every implementation with verified programs has a canary report with every canary rejected |
   | M1a | verified programs on implementations in at least two languages |
   | M1b | primed vs cold run, same implementation, budget and seed: primed VGPH ≥ **1.25×** cold VGPH **and** primed best verified gain ≥ cold best − **3 pp**. VGPH = best verified cost gain (L6 holdout point estimate) ÷ hours from run start until that program passed L6. One run per arm; reported as a single pair |
   | M2 | a CRL rule (ADR 0009) mined from the lake proposes a gene that carries a verified gain on a target whose **database schema differs** from every target the rule's evidence came from |
   | M3 | one rule verified on implementations in two languages, with M2 passed |

## Consequences

- A port is held to exactly what the Python stack is held to, by the same code.
- The Go canary suite cannot test two Python hacks, and does not pretend to:
  - monkey-patching a library function: Go cannot rebind functions;
  - memoising on an argument object: an interface value carries no fields.

  Its clock- and load-sensitive canaries are the Go forms of the same intent.
- M2 cannot pass on two implementations of one schema, however many languages they use. Its
  gate needs a held-out system. That is deliberate: a rule that only ever re-proposes what it
  was mined from has not generalised.
