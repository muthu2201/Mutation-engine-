# ADR 0006 — Self-improvement: discoveries change the searcher, never the judge

**Status:** accepted

## Context

The goal is a loop in which what Colloid discovers also improves Colloid. The obvious
version, letting the optimiser mutate any part of itself, is the textbook reward hack. An
optimiser that can edit its own tests, oracles, statistics or stopwatch will "improve" by
making them lenient. That is the same failure the canary suite exists to catch, one level up.

## Decision

1. **The judge is never a mutable locus**, for any target, including a future target that is
   Colloid's own search code. `colloid_evaluator.policy.JUDGE_PATHS` lists it, on the
   evaluator side and beyond the search side's reach: the evaluator package, `tests/`,
   `stress/`, `core/stats.py` (every CI and the noise floor), `core/lake.py` (the evidence
   ledger), the sandbox, the load generator and the platform layer. L0 rejects any gene there
   (`is_judge_path`). The engine refuses to start on an Atlas that exposes one
   (`judge_violations`).
2. **Discoveries flow into the search side through data, not through code edits:**
   - **seeds**: verified programs from the lake re-enter the next run's cascade;
   - **operator priors**: every attribution in the lake scores the arm that produced the gene.
     A CI above zero counts as a win worth its contribution; a hitchhiker counts as zero.
     These enter the bandit as weighted pseudo-observations (`ThompsonBandit.seed`), which
     shifts where budget goes. Measured credit in the new run still dominates after a few
     pulls.
3. **Lessons about the method become reviewed code changes, never self-applied ones.** This
   run showed that promoted programs carry hitchhiker genes. The response was a measured
   leave-one-gene-out ablation (`colloid verify --ablate`) and a carrying-only
   materialisation, both written, tested and reviewed like any other change.

## Consequences

- The loop is real but bounded. Knowledge compounds through the lake. The rules that decide
  what counts as knowledge change only by human-reviewed commits, and CI plus the canaries
  must pass before they do.
- A future self-target can optimise Colloid's archives, operators, surrogate and
  orchestration. It can never touch how they are judged.
