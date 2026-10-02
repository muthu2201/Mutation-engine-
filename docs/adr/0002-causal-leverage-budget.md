# ADR 0002 — Causal leverage, not hotness, allocates the mutation budget

**Status:** accepted

## Context

A cross-layer engine could spend its (expensive) LLM and benchmark budget anywhere. The naive
choice is to follow a sampling profiler's "hotness". But hotness is misleading: Coz's causal
profiler improved SQLite by 25.6% by optimising functions that `perf` attributed just 0.15% of
runtime to. Optimising a hot function that is not on the critical path moves nothing
end-to-end.

## Decision

Colloid decorates the Stack Atlas with **causal leverage** per unit — "if this unit were x%
faster, how much faster would the end-to-end objective get?" — measured Coz-style by injecting
a controlled delay (its dual, a virtual speedup) and observing the end-to-end change. The
**Locus Opportunity Score** is `causal_leverage × dollar_share × mutability`, and the budget
scheduler splits each generation's proposal slots across islands in proportion to it (times a
recent-improvement momentum term, with an exploration floor).

Leverage is used only to *weight* the budget, never as a hard gate, because the injection
estimate is noisy on shared hardware. Hotness and latency share are retained as weaker
fallback signals when leverage has not been measured for a unit.

## Consequences

- On StackZero the scheduler concentrates effort on `search_products` (leverage ≈ +0.6–1.0, 66%
  of latency) — where the N+1 queries and the C ranking library are — rather than on
  incidentally hot utility code.
- Profiling is an optional, best-effort preamble: if delay injection fails (e.g. it serialises
  the async event loop and the load generator times out), the engine logs it and falls back to
  static opportunity rather than aborting the run.
- The exploration floor guarantees every layer still receives some budget, so a cheap win in a
  low-leverage layer is not permanently starved.
