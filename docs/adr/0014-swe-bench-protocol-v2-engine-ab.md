# ADR 0014 — SWE-bench protocol v2: change the engine, hold the model fixed

**Status:** accepted. Pre-registered on 2026-10-03, before any instance ran under v2. The pilot
instances run under v2 first, and that pilot may fix bugs only, never this protocol. The owner
approved the judge change (point 1c) under ADR 0006.

## Context

ADRs 0011 and 0012 held the engine fixed and changed the model:

- **The local arm** (Qwen2.5-Coder 1.5B/3B/7B) and **the API arm** (Kimi K3, about 400 times the
  7B's parameters) each resolved **2 / 30**, the same two instances, with an exact McNemar
  p of 1.0.
- **Both resolved issues state their own fix.**

The API arm's diagnosis points at the engine, not the model:

- **L3 rewards partial fixes.** 9 of the 11 submissions that resolved a validated reproduction
  script failed the official tests. A single script checks one facet of an issue, and the stop
  rule ("every validated reproduction resolved") ended those searches at the first patch that
  satisfied it. This is the failure SWE-Doctor (arXiv 2607.00990) describes.
- **L2 misses real regressions.** Submissions passed L2 while breaking 78 (pytest-7982), 18
  (django-15569) and 1 (sphinx-8621) official PASS_TO_PASS tests. L2 runs only the 3 test files
  that name the edited module.
- **The budget is wasted.**
  - 30% of Kimi K3's calls were empty endpoint responses.
  - 34% of its proposals returned the snippet unchanged, often a correct "the bug is not here".
  - Both were charged as spent calls.
- **Routing.** In the local arm the cost-aware bandit gave 56% of proposals to the 1.5B model,
  which resolved nothing.

So the next experiment changes the engine and holds the model fixed. It measures the engine's
own effect, which no experiment so far has isolated.

## Decision

1. **Protocol v2** is enabled with `--protocol v2`. Without the flag, v1 behaves exactly as
   pre-registered, and `tests/evaluator/test_protocol_v2.py` checks it.
   - **a. Reproductions.**
     - The engine writes three reproduction scripts, each with a different focus: the issue's
       own example; a different case the expected behaviour must cover; the behaviour stated in
       the title or summary (SWE-Doctor's one script per behaviour).
     - Duplicate scripts are dropped.
     - There is **no early stop**: the search spends its budget.
   - **b. Selection among L0–L2 passers.** Most reproduction votes first; then **consensus**, the
     number of independent candidates that made the same edit, compared on whitespace-insensitive
     changed lines (as in Agentless's majority voting); then the smaller diff.
   - **c. L2 (judge).** Test files are selected **one hop through the import graph**: tests of the
     modules that import an edited module score as well as the edited module's own tests. Up to 6
     files are selected and **each runs on its own** with the 300 s timeout. A file that times out
     at `base_commit` is left out of the comparison, so a truncated log cannot produce false
     regressions.
   - **d. Unchanged responses are declines.** The snippet's decline is recorded, and the search
     moves on. A snippet declined twice is dropped. Declines are not charged to the 16-call
     budget, up to half of it (8 free declines).
   - **e. Empty endpoint responses** are retried up to twice and not charged. Every retry is
     logged.
   - **f. Unfenced code.** A response with no code fence is taken whole if it parses as Python.
   - **g. Routing.** The first proposal on each snippet comes from the largest model (LEVI,
     arXiv 2605.09764). The bandit spends the rest.

   Unchanged: the sample and its order, localisation, the fix prompts, the budget (16 calls and
   20 minutes per instance, 300 s per test run), the official grader, and each model's own
   settings.
2. **The arms** all run on the 30 instances of ADR 0011, in the same order, one at a time:

   | arm | model | protocol | paired with |
   |---|---|---|---|
   | K-v2 | Kimi K3 (NVIDIA, effort `low`, as its pilot fixed in ADR 0012) | v2 | the existing K-v1 arm (ADR 0012) |
   | G-v1 | GLM 5 (`zai.glm-5`, Amazon Bedrock) | v1 | G-v2 |
   | G-v2 | GLM 5 | v2 | G-v1 |

   **GLM 5 settings:**
   - Bedrock's defaults (no `reasoning_effort`), with 8,000 extra `max_tokens`.
   - Before G-v1 and G-v2 run, the same pilot instances run under both protocols to check the
     plumbing; their empty and truncation shares are reported.
   - Nothing about GLM 5 is tuned on sampled instances.

   **Order:** K-v2 first, because its key is available now. G-v1 and G-v2 follow when the Bedrock
   key is visible to the session, with G-v2 first.
3. **Endpoints.**
   - **Primary, for each model separately:** the paired difference in resolved instances
     (v2 − v1), with an exact McNemar test on the discordant pairs. The two models' results are
     reported side by side; their p-values are not pooled.
   - **Secondary, the mechanisms this ADR targets:**
     - **L3 false-resolution rate:** submissions with at least one vote that fail the official
       tests.
     - **Missed regressions:** submissions with official PASS_TO_PASS failures.
     - **Wasted calls:** the share of charged calls that are empty or unchanged.
     - **Submission in a gold file.**
     - **Wall time, and cost in USD for Bedrock.**
   - **Contamination:** memorisation probes for GLM 5 (ADR 0011's addendum rule), and the
     comparison repeated on the instances clean for both protocols' model.
4. **Decision rule**, fixed now.
   - **Claim an engine effect** only if, for at least one model, v2 − v1 > 0 with an exact
     McNemar p < 0.05.
   - **Adopt v2 as the default** if the secondary mechanism metrics improve and v2 resolves no
     fewer instances than v1 for either model.
   - **Power.** With 30 instances only large effects are detectable: six discordant pairs, all
     favouring one protocol, give p ≈ 0.031. A null result means "no large engine effect on
     this sample", not "no effect".
5. **Cost.** At GLM 5's Bedrock prices ($1.00 / $3.20 per million tokens), each GLM arm costs about
   $1, and its probes about $0.20. The experiment uses under $5 of the $125 credit.

## Consequences

- For the first time the engine's contribution is measured with the model held fixed, on two
  models of very different scale.
- The sample is the same contaminated Verified stratum as before, but both protocols face it
  equally, so the paired comparison is fair. Whether a v2 effect holds on issues no model has seen
  needs the fresh-issue follow-up of ADR 0012's first amendment.
- If v2 helps, it becomes the default, and the next pre-registration starts from it. If it does
  not, the bottleneck is somewhere this ADR did not touch, most likely localisation or the fix
  prompts, and that is reported.

## Amendment (2026-10-03, before any GLM arm ran): GLM 5.3 on NVIDIA instead of GLM 5 on Bedrock

**Why.** The Bedrock key is not yet visible to the session. NVIDIA's free endpoint, whose key is
available, serves **GLM 5.3** (`z-ai/glm-5.3`). The owner proposed it. A smoke test of three
calls answered all three, in about 10 s each, with no empty response.

**What changes:** the G arms only. Both are now **GLM 5.3 on NVIDIA**:

- **Request settings:** no reasoning setting (`--reasoning-effort none`), with 8,000 extra
  `max_tokens`.
- **Sampling:** the engine's own temperatures, because GLM 5.3 accepts them.
- **Order:** G-v2 first, then G-v1, after K-v2.
- **Unchanged:** everything else in this ADR, including the pilot on the two pilot instances
  under both protocols, with empty and truncation shares reported and nothing tuned.
- **Cost:** none (free endpoint), so point 5 no longer applies. The Bedrock credit stays
  available for a later replication with GLM 5 (`zai.glm-5`), recorded as its own amendment
  before it runs.

## Amendment 2 (2026-10-03, after the pilot, before any sampled instance ran)

The pilot did what pilots are for: it found two harness bugs and one unusable setting. All three
were fixed before any sampled instance ran.

1. **GLM 5.3 runs with thinking off** (`--no-thinking`, through the chat template).
   - With its default thinking mode on, a real reproduction prompt (about 5,400 characters)
     ran past NVIDIA's 300 s gateway limit, and the server disconnected.
   - The same prompt with thinking off came back in 105 s, as a usable script of 1,245 tokens.
   - Both G arms use this setting, so the paired comparison is unaffected.
2. **Harness: an API error while writing reproduction scripts is recorded, not fatal.**
   - Before, it aborted the whole instance. The first pilot instance was lost after 38 minutes
     of disconnects.
   - This applies to both protocols. It changes only runs that would otherwise have crashed,
     and no earlier arm had a crashed instance.
3. **Harness: no call outlives the search budget.**
   - Each call's timeout is now the time left in the instance's 20-minute search, at least
     60 s and at most 600 s.
   - Before, one call could block for 600 s on each of several retries.
   - This applies to both protocols. In the K-v1 arm the median call took 20.9 s, so its
     pairing with K-v2 is essentially unaffected; the comparison notes the difference.

The GLM pilots are rerun on the fixed harness. Their evidence is committed with the arms.
