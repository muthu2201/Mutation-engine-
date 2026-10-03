# ADR 0011 — SWE-bench: the gold tests are the hidden holdout

**Status:** accepted (pre-registered before any sampled instance was run)

## Context

Experiments 1 and 2 used StackZero, a system we wrote. A real test of the engine needs real,
complex code that other people wrote. Real issues need fixing in it, and the success criterion
must be owned by someone else.

SWE-bench Verified provides exactly that. It has 500 GitHub issues from 12 Python projects
(Django, SymPy, Sphinx, Matplotlib, scikit-learn, and others), each human-checked as solvable.
For each issue, the gold *tests* decide success:

- **FAIL_TO_PASS**: tests that fail before the real fix and pass after it;
- **PASS_TO_PASS**: tests that must keep passing.

The objective changes from cost to correctness. Colloid's structure carries over unchanged:

- a locus-addressed mutation is a function rewritten at a symbol path;
- a cascade of judges filters candidates cheaply first;
- a holdout the search never sees decides at the end.

## Decision

1. **What the search may see.** Only the issue text (`problem_statement`, without
   `hints_text`) and the repository at `base_commit`. The search never sees `patch`,
   `test_patch`, `FAIL_TO_PASS`, `PASS_TO_PASS` or `eval_script`; the last two name the
   relevant test module. The SWE-bench track keeps them in a file the search code does not
   read. A test (`test_search_never_reads_gold_fields`) checks this.
2. **The judge during search** (all inside the instance's container, network off):
   - **L0 patch policy**: Python source files only; no test files, test directories or
     `conftest.py`; no deleted files; a diff of at most 200 lines.
   - **L1**: the patch applies, and every edited file byte-compiles.
   - **L2 regression oracle**: the repository's *existing* tests that import the edited
     modules. They are found by the engine's own search of the test tree. Every test that
     passed at `base_commit` must still pass.
   - **L3 reproduction oracle**: reproduction scripts written by the LLM from the issue text
     (Agentless-style). A script counts only if, at `base_commit`, it prints
     `ISSUE REPRODUCED`. A candidate scores one vote for each validated script that then
     prints `ISSUE RESOLVED`.
3. **Submission.** Among candidates passing L0–L2, the one with the most L3 votes, then the
   smallest diff. If none passes L2, nothing is submitted, and the instance counts as
   unresolved.
4. **The holdout.** The official grader, `swebench` 5.0.2 `run_instance`, scores the one
   submitted patch: it applies the gold test patch, runs the instance's eval script and
   grades the log. **Resolved** means every FAIL_TO_PASS and PASS_TO_PASS test passes; this is
   the leaderboard definition.
5. **Pre-registered sample.**
   - **Dataset:** SWE-bench Verified test split (HF `SWE-bench/SWE-bench_Verified`, parquet
     sha256 `030cfd7f…5be3fa25`).
   - **Stratum:** the 194 instances labelled `<15 min fix`, sorted by `instance_id`.
   - **Draw:** 30 instances, `numpy.random.default_rng(20261002).choice(194, 30,
     replace=False)`. That is 12 Django, 4 Matplotlib, 4 Sphinx, 4 SymPy, 2 xarray,
     2 pytest, 1 astropy and 1 scikit-learn instance.

   This stratum gives the engine its best chance to produce real fixes, which is the point of
   the first test. The resolve rate is therefore **not comparable to full-set leaderboard
   numbers**; results are reported as resolved / 30 with a Wilson 95% CI.
6. **Fixed budget per instance.**
   - 20 minutes of search wall clock (image pull and final grading excluded);
   - at most 16 LLM calls;
   - at most 6 localised functions, each at most 150 lines;
   - test runs time out after 300 s each.
7. **Models.** Local only: Qwen2.5-Coder 1.5B and 3B (and 7B if it is on disk before the
   first sampled instance runs), Q4_K_M, CPU (3 cores). A Thompson bandit over
   (model × prompt) arms spends the budget. No API models.
8. **Pilot.** Two instances outside the sample are used to find bugs in the harness. The
   pilot may fix bugs. It may not change anything in points 1–7.
9. **The lake.** A gene record holds a rewritten function at its locus, in the
   language-neutral format of ADR 0003. A program record holds one submission, with the
   official grading as its evidence (`status: "verified"` only if resolved). Unresolved
   submissions stay out of the lake. Their operator evidence stays in the run store, as
   negative priors.
10. **Images.** The official SWE-bench instance images are on Docker Hub, which rate-limits
    this host's shared egress IP. The track uses Epoch AI's rebuilds of the same images
    (`ghcr.io/epoch-research/swe-bench.eval.x86_64.<instance_id>`), pulled one at a time and
    removed after grading. The results name the image digests.

## Consequences

- Each result line is checkable by anyone with the official harness: the patch, the image
  digest and the grader version are recorded.
- The run measures the engine with small CPU-only models, not the best achievable. Every
  stage's funnel is reported:
  - localisation (whether a candidate function lies in a gold-patch file; computed after
    grading, for diagnosis only);
  - patches that applied;
  - patches that did not regress;
  - validated reproductions;
  - resolved.

  So the bottleneck is visible.

## Addendum (2026-10-03, written before any probe ran): post-hoc memorisation probes

**Why.** Research checked against the primary sources on 2026-10-03 shows that SWE-bench
Verified is contaminated for frontier models:

- In "The SWE-Bench Illusion" (arXiv 2506.12286), models name the buggy file from the issue
  text alone on up to 76% of Verified instances, against under 53% on other repositories. They
  reproduce 12–32% of instances verbatim.
- OpenAI stopped reporting Verified in February 2026. It reportedly named `django__django-11451`
  as a task whose gold patch a frontier model reproduced. That is one of this run's two resolved
  instances. (The OpenAI page itself could not be fetched to confirm this.)

**What is added.** These probes are added *after* grading, as diagnosis. The pre-registered
resolve rate (point 5) is unchanged and stays the headline. For all 30 instances and each local
model (temperature 0, at most 600 tokens), `colloid_evaluator/swebench/contamination.py` runs:

1. **File-path probe:** the issue text and repository only. Does the model name a gold-patch
   file, and does the issue mention it?
2. **Task-ID probe:** the instance ID only. How much of the gold patch's added code does the
   model reproduce? This is measured as frequency-aware 5-gram overlap, and as non-trivial gold
   lines (4 tokens or more) repeated verbatim.
3. **Submission overlap:** no model call. How much of our winning patch's added code is the
   gold patch's?

**Verdict rule**, fixed now:

| verdict | condition |
|---|---|
| `suspect` | 5-gram overlap ≥ 0.5 (only when gold adds ≥ 12 tokens), or any non-trivial gold line reproduced verbatim |
| `path-only` | not suspect, but names a gold file the issue does not mention (familiarity with the repository, or memory) |
| `clean` | neither |

**Reported:**

- each resolved instance's verdict for the model that solved it;
- a resolve rate on the instances `clean` for that model, alongside the pre-registered rate.

**Limits.**

- A model can recall a fix when shown the code but not from the ID alone, so `clean` is not
  proof of absence.
- `path-only` cannot separate familiarity with the repository from instance memory.
- The search itself never had shell or network access (L0–L3 run in a network-off container,
  and the model sees only the issue and snippets). So retrieving the fix at run time, through
  `git log` or the web as reported for agentic scaffolds, was not possible here. Training-data
  memorisation is the only channel these probes test.
