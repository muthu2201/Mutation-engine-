# ADR 0012 — SWE-bench API arm: the same engine with a stronger hosted model

**Status:** accepted (pre-registered before any API-arm instance was run; the local arm of ADR 0011
was already running and is unaffected)

## Context

ADR 0011's local arm runs the repair search with Qwen2.5-Coder 1.5B/3B/7B on three CPU cores.
That measures what the engine achieves with small models. It does not separate the engine's
contribution from the model's.

A hosted model became available through the owner's OpenRouter account: **Qwen3.8 27B**
(`qwen/qwen3.8-27b:free`, released August 2026). Its card lists a 262k context and an
Artificial Analysis coding index of 68.1. Running it through the *same* engine on the *same*
instances isolates the model's effect.

The OpenRouter connector is available to the operator (Claude), not to the engine's process. If
the connector relayed the experiment's model responses, every response would pass through the
operator, a stronger model, before being judged, and a reviewer could not rule out that the
operator altered one. So the engine calls OpenRouter directly, through the same provider code
as the local arm. The connector is used only for:

- model selection;
- credit and limit checks;
- one smoke test on a pilot instance, never on a sampled one.

## Decision

1. **Same everything but the model:**
   - the 30 instances of ADR 0011, in the same order;
   - the same localisation, snippets, prompts and response parsing;
   - the same Thompson bandit over the model's (fix, fix_think) arms;
   - the same judge (L0 policy, L1 compile, L2 regression, L3 validated reproductions) and
     the same selection rule;
   - the same official grader;
   - the same budget: 16 calls and 20 minutes of search per instance, 300 s per test run.
2. **Model settings:**
   - `qwen/qwen3.8-27b:free`, router-chosen provider;
   - reasoning effort `medium`;
   - `max_tokens` raised by 6,000 on every request, because a reasoning model spends tokens
     before it answers;
   - the same temperatures as the local arm.
3. **The key:**
   - read only from `OPENROUTER_API_KEY`, set in the environment's settings;
   - never stored in the repository, a file or a log.
4. **Rate limits:**
   - free variants are capped per day;
   - before each instance, the run checks the remaining free requests;
   - if fewer than 16 remain, the run waits for the daily reset, so no instance's search is cut
     short;
   - the run resumes per instance after any interruption.
5. **Order:** the API arm starts only after the local arm has finished. They never share the
   CPU.
6. **Endpoints:**
   - **Primary:** the paired difference in resolved instances (API − local), with an exact
     McNemar test on the discordant pairs.
   - **Reported for each arm:** resolved / 30 with Wilson 95% CIs, and the per-stage funnel.
7. **Interpretation:**
   - The arms differ in the model and in what follows from it: reasoning tokens, and latency
     inside the same 20-minute budget.
   - A difference is "what a stronger model adds to this engine", not a property of the engine
     alone.
   - Neither arm is comparable to full-set leaderboard numbers (ADR 0011, point 5).

## Consequences

- The local arm's numbers stand on their own, whatever the API arm shows.
- With fewer than $10 of all-time credits, OpenRouter has historically allowed about 50
  free-model requests a day. Its documentation pages render the exact caps only as
  placeholders. At up to 480 calls, the arm could then take up to about ten days of resets; with
  credits bought it fits in a day. Resolved instances enter the lake exactly as in ADR 0011, with
  the model in each gene's provenance.
