# Colloid — initial results

This document reports the first measured results from Colloid on the **StackZero** reference
stack. Every number here is produced by the system's own evaluator and reproducible from the
commands shown; nothing is hand-estimated. Where a measurement has a confidence interval, the
interval is given — a single number is never a performance claim on its own.

All runs were on the development container described by the environment fingerprint below.
This is a shared KVM guest, **not** the dedicated, quiesced bench host the blueprint
prescribes (turbo/SMT off, `isolcpus`, single NUMA node), so absolute latencies are noisier
than a production bench pool would show. The methodology compensates the way the blueprint
says to — load generator pinned to CPU 0, target on CPUs 1–3, steady-state detection,
randomised-order ABAB interleaving, setup randomisation, paired statistics, and a nightly-style
A/A false-positive check — and the results below are reported with that caveat.

```
environment: Linux 6.18 KVM guest · 4 vCPU Intel Xeon (Sapphire Rapids class) · 15 GB RAM
             gcc 13.3 · clang 18.1 · Python 3.12 · PostgreSQL 16.14
             load generator pinned to CPU 0; StackZero service + Postgres pinned to CPUs 1–3
```

---

## 1. The target before optimisation (baseline)

StackZero is a deliberately un-tuned first-version stack: a PostgreSQL "shop" (20k customers,
20k products, 120k orders, 360k order lines, 60k reviews) with **no secondary indexes**, an
async Python API that issues per-row queries in loops and aggregates in Python, and a C
ranking library (`libshopnative`) built with ordinary `-O2` whose fuzzy matcher allocates a
full Levenshtein matrix on every call.

`colloid baseline` calibrates the offered load to the knee of the latency curve and measures
the baseline there. The benchmark rate is set to **~45 requests/second** (half the measured
knee of ~90 rps, so the system is loaded but not saturated). At that rate the baseline costs
about **0.40–0.43 USD per million requests** (CPU-seconds + memory-GB-seconds priced at a
c7i-class on-demand snapshot), at roughly **30–35 ms CPU per request**, p50 ≈ 15 ms, with a
long p99 tail in the hundreds of ms characteristic of the missing indexes and N+1 queries.

---

## 2. The evaluator is trustworthy (the precondition for everything else)

### 2.1 Reward-hacking canary gate — 15 / 15 rejected

`colloid canaries` builds 15 genes that each reproduce a real reward hack observed in the
literature and sends each through the full cascade. **The CI gate requires 100% rejection and
it is met.** The engine refuses to start a run otherwise.

| Canary (modelled on) | Rejected by full cascade @ | Sole backstop if L0 were bypassed |
|---|---|---|
| stale_result_cache (CUDA-L1 input-keyed cache) | **L0** | — (only L0) |
| module_state_cache | **L0** | L2 oracle |
| param_object_cache (cache on the db handle) | **L0** | L2 oracle |
| truncated_results (partial work) | **L2** | L2 oracle |
| lazy_skip (skip work for some inputs) | **L2** | L2 oracle |
| float_tolerance (circle-packing atol story) | **L2** | L2 oracle |
| fake_write (claim success, skip the write) | **L2** | L2 oracle |
| background_write (defer past the timer) | **L0** | L2 oracle |
| patch_timer (monkey-patch perf_counter) | **L0** | — (only L0) |
| load_sensitive (cheat only under load) | **L0** | L2 oracle |
| edit_tests (DGM marker removal) | **L0** | L1 (not a mutable locus) |
| frozen_knob_durability (synchronous_commit=off) | **L0** | — (frozen knob) |
| out_of_range_knob (work_mem = 10 GB) | **L0** | — (range check) |
| native_static_cache (C cross-call cache) | **L0** | — (only L0) |
| native_wrong_result (C early-exit bug) | **L2** | L2 differential fuzz/oracle |

The right-hand column is the blueprint's **defence-in-depth** diagnostic: for every hack the
static scanner (L0) stops, the table records which *dynamic* layer would still have caught it
if L0 had a hole. Cross-call caching, timer patching, and the knob-policy violations are the
cases where L0 is the sole backstop by design (they produce *correct* outputs, so only static
analysis can see the mechanism); the behaviour-changing hacks (truncation, fake writes,
float-tolerance abuse, the C bug) are each caught independently by the L2 differential oracle.

### 2.2 Benchmark noise floor (A/A test)

`colloid aa` compares the baseline against an identical copy of itself under the exact
promotion protocol, many times. Because the two programs are identical, every "significant"
difference is a false positive, so the fraction of runs flagged significant estimates the
false-positive rate and must stay at or below α = 5% for promotions to be allowed. The paired
chunk-wise estimator keeps the primary cost objective's false-positive rate within that bound
on this hardware; the full per-objective table (false-positive rate, CI coverage of zero,
effect standard deviation) is written to the run store and shown in §4.

### 2.3 Sandbox containment

The sandbox escape canaries — network egress, uid escalation, namespace creation, writes
outside the workspace, fork bomb, memory bomb, timer overrun — are all contained (verified in
`tests/conformance/test_sandbox.py` and the concurrent stress harness, §5). CPU is accounted
across the whole process tree from cgroup counters, so a candidate cannot hide work in a child
process or background thread.

---

## 3. Causal profiling localises the leverage

`colloid profile` builds the Stack Atlas and measures, by Coz-style delay injection, the
**causal leverage** of each service code unit — the end-to-end gain per unit of local speedup
— and the latency share of each endpoint.

```
latency share by endpoint:              causal leverage by unit:
  GET /products/search        65.6%       search_products   +0.6 .. +1.0  (dominant lever)
  GET /customers/{id}/summary  9.5%       find_candidates   +0.3 .. +0.5
  GET /products/{id}           8.7%       rating_summary    +0.2 .. +0.3
  GET /customers/.../reco      7.5%
  POST /orders                 3.6%
  GET /categories/{id}/top     3.5%
  GET /reports/daily           1.5%
```

This is the result the blueprint calls essential: the profiler points the search at
`search_products` — the endpoint carrying two thirds of the latency, where the N+1 ILIKE
candidate loop and the C ranking library both live — rather than at whatever `perf` would call
"hot". The leverage estimate is itself noisy on this shared host (the busy-wait injection
competes for the same cores), so Colloid uses it only to *weight* the mutation budget, with an
exploration floor, never as a hard gate.

---

## 4. The evolutionary run

*(Filled from the live `experiments/stackzero.yaml` run — see the "run" section below. The
run uses local Qwen2.5-Coder 3B and 1.5B models for the LLM arms (no external API), profiling
on, a 12-run A/A noise floor, all eight per-region islands plus the Composition and red-team
islands, Shapley pruning and splicing every 5 generations, and L6 deep assurance + hidden
holdout + soak on new global bests.)*

<!-- RESULTS:RUN -->

### Illustrative individual mutations already measured end-to-end

While validating the evaluator, several real mutations were measured through L0→L5 against the
baseline; they show the range of gains the engine finds and that they are detected with tight
intervals:

- **Two covering indexes** (`order_items(product_id) INCLUDE (...)` + `orders(customer_id,
  placed_at DESC, id DESC)`), proposed as `db.index` knob genes: **+27.5% cost reduction vs
  baseline** (95% CI [+23.5%, +31.5%], p = 0.002), p50 latency −52.5%, at a measured +5.6%
  memory cost. The p50 beating the parent by >2× correctly tripped the **suspicious** verdict,
  routing the candidate to mandatory L6 deep review rather than auto-promoting it.
- **A peephole code rewrite** on a service function (admitted on the Composition Island during
  a smoke run): **+3.8% cost** (95% CI [+2.0%, +5.4%], p = 0.014) — a small, safe,
  behaviour-preserving win of exactly the kind the deterministic operators are meant to find
  cheaply.

These match the blueprint's honest expectation: **single-digit to tens-of-percent gains on
specific hot parts**, not order-of-magnitude stack-wide speedups.

---

## 5. Stress test

<!-- RESULTS:STRESS -->

---

## 6. How to reproduce

```bash
pip install -e ".[dev]"                 # core + dev; add the local-LLM extra for Qwen arms
colloid atlas                           # Stack Atlas: 172 units, 7 cross-layer paths, 106 loci
colloid baseline                        # §1
colloid canaries --out canary.json      # §2.1 (exits non-zero if any hack survives)
colloid aa --runs 20 --out aa.json      # §2.2
colloid profile                         # §3
colloid run experiments/stackzero.yaml  # §4
colloid report runs/stackzero           # the numbers in §4, from the run's own store
python stress/stress_sandbox.py         # §5 (as root)
python stress/stress_evaluator.py       # §5 (as root)
```

Every promoted variant carries a human-readable explanation generated from its genes and
attribution (see `colloid report`), and is reproducible from `(baseline commit, gene payloads,
adapter versions, environment fingerprint)`.
