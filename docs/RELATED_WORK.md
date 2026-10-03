# Related work, and what Colloid takes from it

These are papers read in full against Colloid's own evidence, as of 2026-10-03. The
last three sections started from research the owner supplied; every number quoted was
checked against the paper itself. Each entry
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

## Is SWE-bench Verified measuring memory? Three papers, checked against the sources

- **What they show.**
  - **"The SWE-Bench Illusion"** (Liang, Garg, Zilouchian Moghaddam, 2025) —
    [2506.12286](https://www.alphaxiv.org/abs/2506.12286). Given only the issue text, models name
    the buggy file on up to 76% of Verified instances, against under 53% on repositories outside
    SWE-bench. 5-gram overlap with the gold patch is 34.9% on Verified against 18.2% elsewhere,
    and 11.7–31.6% of instances are reproduced verbatim.
  - **"Does SWE-Bench-Verified Test Agent Ability or Model Memory?"** (Prathifkumar, Mathews,
    Nagappan, 2025) — [2512.10218](https://www.alphaxiv.org/abs/2512.10218).
    - Setting: Claude 3.5 and 3.7 Sonnet, issue text only.
    - Result: they name *all* gold files on 65% and 63% of Verified instances, against 12% and 8%
      on the September 2025 SWE-rebench split.
  - **SWE-bench-Live** (Zhang et al., Microsoft, 2025) —
    [2505.23419](https://www.alphaxiv.org/abs/2505.23419). The same agent and model (OpenHands,
    Claude 3.7 Sonnet, identical setup) resolve 43.20% of Verified against 19.25% of SWE-bench-Live,
    whose issues were all created after 2024.
  - OpenAI stopped reporting Verified in February 2026, citing gold-patch reproduction by frontier
    models. We could not fetch that page to check which instances it named.
- **Bearing on Colloid.**
  - The local arm's resolved instances are 2-line Django fixes from the 7B Qwen2.5-Coder.
  - Every gold fix is a public GitHub commit made years before that model's 2024 training data
    was collected, so the model could have seen it.
  - A runtime leak, such as an agent running `git log --all` to find the future fix, was not
    possible here. The search never had a shell, and every judge stage ran in a network-off
    container.
  - Training-data memory remains possible, and nothing so far measured it.
- **What changes, now.**
  - Post-hoc memorisation probes adapted from 2506.12286: issue-only file path, task-ID patch
    recall, and submission overlap with gold.
  - Their verdict rule was fixed before any probe ran (ADR 0011 addendum).
  - The probes cover both arms (ADR 0012 amendment). Each arm's resolve rate is also reported
    on the instances that are clean for the solving model.
- **What changes, next.** A contamination-resistant run on issues created after the API model's
  release (SWE-rebench or SWE-bench-Live). It is pending an image-access check: SWE-rebench's
  images are on Docker Hub, which rate-limits this host.

## Real-repository performance benchmarks: SWE-fficiency and a cross-machine audit

- **What they show.**
  - **SWE-fficiency** (2025, v3 June 2026) — [2511.06090](https://www.alphaxiv.org/abs/2511.06090).
    - Data: 498 performance tasks from 9 scientific-Python repositories (numpy, pandas, scipy,
      scikit-learn, matplotlib, xarray, sympy, dask, astropy).
    - Each task has a workload, the expert's speedup and "guarding" unit tests whose coverage
      meets the expert diff.
    - Score: model speedup ÷ expert speedup, aggregated as a harmonic mean with a 0.001 floor.
    - Harness: one 4-vCPU / 16 GB worker per task, with CPU pinning (including the Docker
      daemon's), and prebuilt images. It checks for stack-frame introspection and for caches that
      persist across timing runs.
    - Result: the best model reaches 0.225× the expert's speedup.
  - **Cross-machine audit** (Chen et al., July 2026) —
    [2607.01211](https://www.alphaxiv.org/abs/2607.01211).
    - Method: replays every reference patch on four cloud CPU families.
    - Result: the patches stay valid under each benchmark's *own* rule on only 39/102 GSO, 11/140
      SWE-Perf and 411/498 SWE-fficiency tasks.
    - It also shows that SWE-fficiency's 0.001 floor gives the worst ten tasks 58.5–82.8% of the
      score's weight.
- **Bearing on Colloid.**
  - Experiments 1 and 2 optimised StackZero, a system we wrote. The SWE-bench track tests
    *repair*, not cost.
  - SWE-fficiency is the closest external match to what Colloid does: mutate a real repository
    to make a workload cheaper, under correctness tests someone else wrote.
  - Its reward hacks are the ones Colloid's judge was built against: input-specific fast paths,
    timing-loop detection, caching across runs.
- **What changes.** ADR 0013 (proposed) sets out a SWE-fficiency track:
  - the 411 replay-valid tasks;
  - Colloid's ABAB timing and A/A noise floor instead of the benchmark's inclusion rule;
  - a fresh process for each timing repetition;
  - an AST denylist for stack introspection;
  - per-task speedup ratios reported next to the floored harmonic mean.

## LLMs tuning databases: high variance, distilled rules (Wang, Wu, Narasayya, Chaudhuri, Microsoft, 2026) — [2603.09181](https://www.alphaxiv.org/abs/2603.09181)

- **What it shows.** The setup: index tuning on TPC-H and four enterprise workloads, measured by
  real execution time.
  - An LLM often finds configurations competitive with the Database Tuning Advisor, sometimes
    much better.
  - Its worst cases are far worse, and its variance grows with workload size.
  - Validating its candidates costs more than generating them.
  - A deterministic rule-based tuner distilled from the LLM's reasoning beat DTA in many of the
    cases where DTA lost to the LLM. Its rules: cut costly scans, order key columns from plan
    cues, prefer covering indexes, ignore small tables.
- **Bearing on Colloid.** This is the CRL argument of ADR 0009, reached independently on a
  production database product. LLM search finds wins, but they are noisy. The durable,
  transferable artefact is the rule distilled from wins that were *verified*. Colloid's lake
  holds exactly those wins, with their evidence.
- **What changes.**
  - The M2 rule-mining candidates gain two seeds from the database literature: batching an N+1
    lookup, and indexing the columns hot queries filter or join on.
  - Each must earn its place through the same verified-gene evidence as the existing rules.
  - No rule is added on the paper's authority alone.

## Also relevant, not yet read in full

- CodeEvolve (Salesforce, 2026), multi-language, runtime-guided target selection:
  [2605.04677](https://www.alphaxiv.org/abs/2605.04677).
- Component-Aware Feedback for Self-Evolving Programs (2026), attributing fitness to component
  edits: [2609.38639](https://www.alphaxiv.org/abs/2609.38639). Colloid's exact Shapley and
  leave-one-out attribution is the measured version of this idea.
- Relay, Don't Route (2026), cost-efficient model handoff in LLM-driven evolution:
  [2608.05651](https://www.alphaxiv.org/abs/2608.05651).
- AdaEvolve (2026), adaptive exploration intensity: [2602.20133](https://www.alphaxiv.org/abs/2602.20133).
