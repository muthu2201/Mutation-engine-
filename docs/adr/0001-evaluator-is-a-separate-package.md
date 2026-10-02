# ADR 0001 — The evaluator is a separate, independent package

**Status:** accepted

## Context

Every public failure of LLM-driven code optimisation in 2023–2026 was an *evaluator* failure:
Sakana's CUDA engineer found a memory exploit that bypassed the correctness check; the Darwin
Gödel Machine removed its own hallucination-detection markers; KernelBench numbers were
inflated until a verified protocol cut the best model from 1.43× to 0.88×. The common thread
is that the code which *proposes* improvements found a way to influence how improvements were
*judged*.

## Decision

The evaluator lives in a sibling package, `colloid_evaluator`, that is **forbidden from
importing the search machinery** (`colloid.core.operators`, `.archive`, `.bandit`,
`.selection`, `.attribution`, `.splicing`, `.budget`, `.surrogate`, and all of
`colloid.services`). This is enforced by an import-linter contract in CI, not by convention.
Shared value objects that both sides legitimately need (e.g. `Measured`) live in the pure
`colloid.core.models` layer, which the evaluator may import; the search-only logic does not.

The evaluator owns, and never exposes to candidates: request generators seeded from the OS
CSPRNG, the hidden holdout workload, reference outputs (computed at evaluation time by the
baseline running beside the candidate, never written to disk), float tolerances, the timing
harness, and the policy rules.

## Consequences

- A mutation operator cannot, even in principle, weaken a tolerance, read a reference output,
  or change the benchmark protocol — those symbols are not reachable from its package.
- The evaluator can be run standalone (`colloid canaries`, `colloid aa`) as a CI gate.
- The cost is a little duplication (the evaluator re-derives some Atlas facts rather than
  importing search helpers), which is accepted as the price of the boundary.
