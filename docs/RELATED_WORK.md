# Related work, and what Colloid takes from it

These are papers read in full against Colloid's own evidence, as of 2026-10-03. Each entry
says what the paper shows, how it bears on a measured Colloid result, and what changes, if
anything. Nothing here alters a pre-registered protocol that is already running: the M1 A/B
(ADR 0008) and the SWE-bench sample (ADR 0011). Changes go into the *next* pre-registration.

## Cross-run memory: EvoMem (Volkov et al., 2026) — [2608.10795](https://www.alphaxiv.org/abs/2608.10795)

- **What it shows.** Distilling successful mutations into provenance-carrying "memory cards"
  works. Those cards are injected as bounded advice (at most 3 per mutation) into later runs on
  *other* tasks, and this gave an average 5.9× search speed-up (candidates needed to reach the
  baseline's best) and a +6.4% better final metric. Variance was large: the minimum speed-up
  on several benchmarks is below 1. The evaluator and selection stay unchanged; memory is advice
  only.
- **Bearing on M1b.** Colloid's lake transferred knowledge as **seeded programs** and
  **bandit priors**. M1b did not pass, partly because the best seed was blocked by the soak
  flaw (ADR 0010). EvoMem's channel is different: abstract tactics retrieved into the
  mutation prompt.
- **What changes.**
  - The lake already stores the provenance EvoMem needs (gene records, attribution, CIs). A
    third transfer channel, "advice cards" mined from attributed genes (CI > 0), is cheap to
    add.
  - EvoMem's transfer metric, *candidates evaluated to reach the cold run's best*, becomes a
    secondary endpoint in the M1 re-test pre-registration, next to VGPH.

## Small models, strong harness: LEVI (Tanveer, 2026) — [2605.09764](https://www.alphaxiv.org/abs/2605.09764)

- **What it shows.** Three investments in the search architecture let small open-weight models
  match or beat frontier-model runs at 3.3–6.7× lower cost:
  - a CVT-MAP-Elites archive with AST descriptors, seeded with deliberately diverse
    (even weak) solutions;
  - role-aware routing: about 90% of mutations go to a small model, with periodic
    "paradigm-shift" calls to a large one;
  - rank-preserving proxy benchmarks.

  In the ablations, removing the diverse seeding hurt most.
- **Bearing on Colloid.**
  - Colloid's search uses local Qwen2.5-Coder 1.5B/3B/7B only.
  - Its Thompson bandit is cost-aware, so it drifts towards the cheapest model. In the first SWE-bench
    pilot instance, the 1.5B arm took most of the calls and produced every L2 regression.
  - LEVI's routing is the principled alternative: the large model for structural attempts, the
    small one for local variants.
- **What changes, in the next protocol, not the running one.**
  - **Repair:** the first proposal on each localised snippet comes from the largest local
    model; the small models produce variants of candidates that already passed L2.
  - **Optimisation:** bootstrap diverse seeds per island.

## Reproduction tests: SWE-Doctor (Guo et al., 2026) — [2607.00990](https://www.alphaxiv.org/abs/2607.00990)

- **What it shows.** Using generated bug-reproduction tests (BRTs) directly as pass/fail
  targets does not help patch generation:
  - fail-to-pass tests cover one facet of the issue, which leads to partial patches;
  - fail-to-fail tests mislead.

  What helps is BRTs for each behavioural requirement in the issue, *executed under a debugger*
  to produce runtime diagnoses (fault location, symptom, values) that feed the patch prompt. It
  gives +8–9 pp on SWE-bench Pro.
- **Bearing on Colloid.**
  - ADR 0011's L3 keeps only reproduction scripts that print `ISSUE REPRODUCED` at
    `base_commit`, and uses them only to *rank* candidates that already passed L2. That is
    close to SWE-Doctor's patch validation and avoids their fail-to-fail failure mode.
  - It does not use runtime diagnosis. In the first pilot instance, neither reproduction script
    (one from the 7B model, one from the 3B) reproduced the issue.
- **What changes, in the next protocol.**
  - one reproduction script per stated behaviour;
  - when a script fails, its traceback and the locals at the failing frame go into the fix
    prompt as a diagnosis.

## Check the model before trusting it: "Broken on Arrival" (Patodiya, 2026) — [2609.05881](https://www.alphaxiv.org/abs/2609.05881)

- **What it shows.** 1.6% of official quantised GGUF code models in a major registry are
  silently dead. They produce fluent output that solves 0 of 164 tasks. Among them was a batch
  of **Qwen2.5-Coder-3B** low-bit conversions (q2_K–q3_K_L). Output statistics cannot catch
  the worst class; only executing code can. The same file can also work on one inference
  backend and fail on another.
- **Bearing on Colloid.** Colloid runs quantised GGUFs: Qwen2.5-Coder 1.5B, 3B and 7B, all
  Q4_K_M, not the defective q2/q3 builds. Until now nothing checked them functionally; the only
  evidence was that they produce code that passes the judge.
- **What changes, now.** A model acceptance gate, the same idea as canaries for the judge:
  - Before a run uses a local model, the model must pass a small executed code suite on this
    host's backend.
  - Its pass count is recorded in the run's setup.
  - A model that fails is refused.

  This joins the judge's other self-checks: the canaries and the A/A gate.

## Also relevant, not yet read in full

- CodeEvolve (Salesforce, 2026), multi-language, runtime-guided target selection:
  [2605.04677](https://www.alphaxiv.org/abs/2605.04677).
- Component-Aware Feedback for Self-Evolving Programs (2026), attributing fitness to component
  edits: [2609.38639](https://www.alphaxiv.org/abs/2609.38639). Colloid's exact Shapley and
  leave-one-out attribution is the measured version of this idea.
- Relay, Don't Route (2026), cost-efficient model handoff in LLM-driven evolution:
  [2608.05651](https://www.alphaxiv.org/abs/2608.05651).
- AdaEvolve (2026), adaptive exploration intensity: [2602.20133](https://www.alphaxiv.org/abs/2602.20133).
