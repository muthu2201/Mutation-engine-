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
A/A false-positive check that sets a measured noise floor (§2.2) — and the results below are
reported with that caveat.

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
the baseline at half the knee, so the system is loaded but not saturated. The knee depends on
how busy the shared host is. During evaluator development it measured ~90 rps (benchmark rate
45 rps). At the start of the reported run (§4) it measured ~60 rps, so that run benchmarks at
**30 rps**. At those rates the baseline costs about **0.40–0.48 USD per million requests**
(CPU-seconds + memory-GB-seconds priced at a c7i-class on-demand snapshot), at roughly
**30–35 ms CPU per request**, p50 ≈ 15 ms, with a long p99 tail in the hundreds of ms that is
characteristic of the missing indexes and N+1 queries. Every comparison runs the baseline in
the same session as the candidate, interleaved, so the shifting knee changes the operating
point but never the comparison.

---

## 2. The evaluator is trustworthy (the precondition for everything else)

### 2.1 Reward-hacking canary gate — 16 / 16 rejected

`colloid canaries` builds 16 genes, each reproducing a real reward hack observed in the
literature, and sends each through the full cascade. **The CI gate requires 100% rejection, and
it is met.** The engine refuses to start a run otherwise. The table below comes from the final
run of the suite, with candidates under the filesystem jail
(`docs/results/canaries.json`).

| Canary (modelled on) | Rejected by full cascade @ | If L0 were bypassed, caught by |
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
| out_of_range_knob (work_mem = 10 GB) | **L0** | L4 (fails under load) |
| native_static_cache (C cross-call cache) | **L0** | L1 |
| native_wrong_result (C early-exit bug) | **L2** | L2 differential fuzz/oracle |
| **native_leak** (C drops a `free()`: identical output, cheaper) | **L2** | L2 leak check (new) |

The right-hand column is the blueprint's **defence-in-depth** diagnostic. For every hack the
static scanner (L0) stops, it records which *dynamic* layer would still have caught it if L0
had a hole.
- **Only L0 can catch it:** cross-call caching, timer patching and durability trading. These
  produce *correct* outputs, so only static analysis can see the mechanism.
- **The L2 oracle catches it independently:** every behaviour-changing hack.

Two rows were added or changed by this round of work:
- **`native_leak` is new.** A dropped `free()` makes a function cheaper while its output stays
  bit-identical. It is caught by the fuzz driver's ptrace-free heap accounting (§4.3).
- **`lazy_skip` was once missed at L2.** It is visible on 32% of product requests, and in one
  stress run it slipped past L2 at 8 samples per endpoint (expected miss rate 4.8%), to be
  caught at L4. The quick oracle now takes 12 samples per endpoint (1.0% miss), and it was
  caught at L2 in 20 of 20 independent trials.

### 2.2 Benchmark noise floor (A/A test) — failed first, then fixed

`colloid aa` compares the baseline against an identical copy of itself under the exact
promotion protocol (L5), many times. The two programs are identical, so every "significant"
difference is a false positive.

**The first measurement failed the gate, and that finding drove a real fix.** In the first
full run (10 A/A runs) the primary cost objective showed a **raw false-positive rate of 30%
(3/10)** at α = 5%. The A/A cost effects had a run-to-run standard deviation of **2.5%**, while
each run's own paired-chunk CI implied a standard error of only about 1.5%. The CI was
honest about noise *inside* a run (chunk to chunk) but blind to a **between-run** component
that every chunk of one run shares: noisy neighbours on the shared KVM host, frequency
drift, and the deliberately different link order / environment padding / hash seed each
run draws. A dedicated, quiesced bench host would shrink that component; it does not
remove it.

Colloid now measures that component instead of assuming it away (`core/stats.py`,
`colloid_evaluator/cascade.py::aa_test`):

1. **Noise floor.** From the A/A runs, τ² = Var(A/A effects) − mean(within-run SE²), a
   method-of-moments random-effects variance component (the DerSimonian–Laird idea). It is
   stored per benchmark cycle, and from then on **every comparison's CI is widened to
   √(SE² + τ²/cycles)**. Asymmetry is kept, and a p-value can only get larger, never smaller.
2. **Out-of-sample check.** The calibrated false-positive rate is measured leave-one-out. Run
   *i* is judged with τ estimated from the other runs only, so the calibration cannot grade
   itself on the data it was fitted to.
3. **A gate with the right statistics.** "Observed FPR ≤ α" on 10 runs would reject a
   *perfectly calibrated* harness 40% of the time (P[Binom(10, 0.05) ≥ 1] = 0.40).
   Promotions are allowed only if an exact one-sided binomial test cannot reject "calibrated
   FPR ≤ α".
4. **The gate is enforced.** The first run also exposed a real bug: when the A/A test
   failed, the engine logged "promotions halted" but still promoted a new best after L6. Now
   an elite that passes L6 while the gate is closed is marked **`verified`** (with a
   `promotion.held` event) and never `promoted`.

The measured noise floor, the raw and calibrated false-positive rates, and the gate decision
for the reported run are in §4. A separate 20-run stress A/A is in §5.

### 2.3 Sandbox containment

The sandbox escape canaries are all contained:
- network egress, uid escalation and namespace creation;
- writes outside the workspace;
- fork bomb, memory bomb and timer overrun.

This holds on this host (cgroup v1) and on a real **cgroup v2** kernel. The v2 check runs as
root in a CI job on GitHub's Ubuntu runners: `tests/conformance/test_sandbox.py` plus the
concurrent stress harness, §5.

Since that CI job found that write confinement relied on directory permissions, candidates run
in a **filesystem jail**. Only their declared paths are writable, every other mount is
read-only, and `/tmp`, `/var/tmp` and `/dev/shm` are private per candidate, so no file outlives
a candidate or reaches the next one.

CPU is accounted across the whole process tree from cgroup counters, so a candidate cannot hide
work in a child process or background thread.

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

`colloid run experiments/stackzero.yaml`: local Qwen2.5-Coder 3B and 1.5B models serve the LLM
arms (no external API). The run uses causal profiling, a **20-run A/A noise floor** with the
promotion gate from §2.2, all per-region islands plus the Composition and red-team islands,
Shapley pruning with epistasis and splicing every 5 generations, and L6 deep assurance
(deep oracle, sanitizer fuzzing, hidden holdout workload, soak) on every new global best.

### 4.1 First attempt: crashed at generation 5 (kept as evidence)

The first full run (`runs/stackzero-attempt1-crashed-gen5`, evidence in
`docs/results/stackzero-attempt1-crashed-gen5.report.json`) ran 4 complete generations and
then crashed in generation 5. The Shapley epistasis step emitted a telemetry field named
`kind`, which collided with the event-kind argument (`TypeError`). The fix renames the
field, makes the event kind positional-only, and namespaces any reserved key, so telemetry
can no longer crash the engine; a regression test covers it. What that attempt measured
before the crash:

| | |
|---|---|
| cascade funnel | L0 31 pass / 4 fail → L1 26 / 5 → **L2 9 pass / 17 fail** → L4 9 → L5 9 → L6 1 pass / 2 fail |
| admitted to archives | 9 programs |
| LLM usage | 42 local calls (40.3k in / 12.9k out tokens), all syntactically screened before L0 |
| red-team island | 1 attack generated, caught by the L2 oracle, 0 breaches |
| real win | `db.idx_orders_customer_placed` index gene: **+5.3% cost** on the hidden holdout (CI [+3.2%, +7.4%], p = 0.002), p50 −19% |
| noise "wins" caught | a `gi_edit` line duplication (+2.1% at L5) and an index crossover (+6.8% at L5) both became the global best, and **both were rejected by the L6 hidden-holdout check** (cost CI lower bound ≤ 0 on unseen traffic) |

Two of the three new bests were measurement noise, which is what the 30% raw A/A
false-positive rate predicted. The L6 holdout, an independent second measurement on a
workload the search never sees, rejected both. Only the real index win survived L6.
However, it was then **promoted while the A/A gate was closed**, which is the gate bug
described in §2.2.

### 4.2 The reported run

**Headline.** After 10 generations (164 min, 80 programs proposed, 30 evaluated through L5)
and post-run verification, **four programs were promoted**. The best, `58c5d609` (5 genes,
spliced on the Composition Island from elites of three regions), cuts **cost per request by
30.9% (95% CI [+27.3%, +34.5%], 6-cycle replicate, p = 5·10⁻⁵)**, from $0.457 to $0.316 per
million requests. On the same replicate p50 latency falls 46.9% and p95 40.4%, and memory is
unchanged. Its gain persists on the hidden holdout workload (+26.6%, CI [+21.3%, +31.0%]). It
passed the deep oracle, sanitized differential fuzzing and the soak test. Every one of these
numbers carries the run's measured A/A noise floor.

**What the gain is made of.** Shapley attribution and the leave-one-gene-out ablation below
agree. **`db.idx_reviews_product`**, an index on `reviews(product_id)`, carries most of the
gain: a contribution of +0.33 in log-ratio, CI [+0.27, +0.40]. The baseline loads every
product's rating with a sequential scan of the 60k-row reviews table, inside an N+1 loop over
search results. **`db.idx_orders_customer_placed`** carries the rest: +0.08, CI [+0.01, +0.14].
The three code genes are hitchhikers. The two indexes on their own measure **+31.3%
(CI [+28.2%, +34.1%])**, the same as the whole program (§4.4, §7).

<!-- RESULTS:RUN -->

**Run `runs/stackzero`** — rate 30.0 rps (knee ≈ 60 rps), 10 generations, 30 programs evaluated of 80 proposed, 4 promoted, 0 L6-verified (promotion held), 164 min wall.

**Cascade funnel** (how many candidates each stage saw / passed):

| stage | pass | fail | suspicious | error |
|---|---|---|---|---|
| L0 | 65 | 8 | 0 | 0 |
| L1 | 53 | 12 | 0 | 0 |
| L2 | 30 | 23 | 0 | 0 |
| L4 | 28 | 0 | 0 | 0 |
| L5 | 30 | 0 | 0 | 0 |
| L6 | 4 | 10 | 0 | 0 |

**Best programs found** (measured vs baseline):

| program | island | operator | cost | p50 | mem | genes |
|---|---|---|---|---|---|---|
| `cc73042ecc` (promoted) | composition | splice | +29.6% (CI [+23.4%, +34.9%], p=0.002) | +46.3% | +1.9% | db.idx_orders_customer_placed = True [knob_perturb]<br>shop_score_batch: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_reviews_product = True [knob_sample]<br>Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 |
| `8de55aa0a3` (promoted) | composition | splice | +27.6% (CI [+22.7%, +32.6%], p=0.002) | +45.0% | +0.5% | db.idx_orders_customer_placed = True [knob_perturb]<br>shop_score_batch: code rewrite (llm_rewrite/optimize) optimize<br>score: code rewrite (llm_rewrite/optimize) optimize<br>db.jit = False [knob_perturb]<br>db.idx_reviews_product = True [knob_sample]<br>Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 |
| `58c5d609e8` (promoted) | composition | splice | +26.6% (CI [+21.3%, +31.0%], p=0.002) | +33.2% | -0.1% | db.idx_orders_customer_placed = True [knob_perturb]<br>shop_score_batch: code rewrite (llm_rewrite/optimize) optimize<br>score: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_reviews_product = True [knob_sample]<br>Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 |
| `803f6d1d23` (promoted) | db.index | knob_sample | +15.4% (CI [+9.5%, +21.0%], p=0.002) | -1.0% | -0.2% | levenshtein: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_reviews_product = True [knob_sample]<br>Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 |
| `f281a4a575` (elite) | db.index | knob_sample | +22.3% (CI [+17.0%, +27.4%], p=0.002) | +18.7% | +1.0% | levenshtein: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_products_category = True [knob_sample]<br>db.idx_reviews_product = True [knob_sample]<br>Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 |
| `1abc381f85` (elite) | db.index | knob_sample | +15.7% (CI [-4.8%, +26.4%], p=0.059) | -15.3% | +0.3% | db.jit = False [knob_perturb]<br>db.idx_products_desc_trgm = True [knob_sample]<br>db.idx_reviews_product = True [knob_sample] |
| `b7c9d5bfcd` (elite) | composition | splice | +10.1% (CI [+3.3%, +16.3%], p=0.006) | +33.4% | +0.7% | db.idx_orders_customer_placed = True [knob_perturb]<br>shop_score_batch: code rewrite (llm_rewrite/optimize) optimize<br>db.jit = False [knob_perturb]<br>find_candidates: code rewrite (py_rewrite/dedupe_seen_set) list-membership dedupe of 'candidates' at line 12 uses a shadow set |
| `7e8e31486d` (elite) | db.index | knob_sample | +9.5% (CI [+1.2%, +17.1%], p=0.025) | +9.2% | +0.7% | levenshtein: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_items_product_cover = True [knob_sample]<br>Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 |
| `249259ede5` (elite) | composition | llm_rewrite | +9.3% (CI [+3.2%, +15.7%], p=0.006) | +28.1% | +0.2% | db.idx_orders_customer_placed = True [knob_perturb]<br>shop_free_tokens: code rewrite (llm_rewrite/algorithmic) algorithmic<br>find_candidates: code rewrite (py_rewrite/dedupe_seen_set) list-membership dedupe of 'candidates' at line 12 uses a shadow set |
| `1cf8b36361` (elite) | composition | splice | +6.4% (CI [-0.4%, +12.8%], p=0.065) | +24.3% | +0.8% | db.idx_orders_customer_placed = True [knob_perturb]<br>shop_score_batch: code rewrite (llm_rewrite/optimize) optimize<br>score: code rewrite (llm_rewrite/optimize) optimize |

**Post-run verification** (`colloid verify`: L6 deep assurance for every eligible top program, then a 6-cycle replicate vs baseline; promotion needs L6 pass + holdout surviving Holm at α=0.05 + replicate CI > 0 + A/A gate):

| program | island | L5 cost (in-run) | L6 | holdout cost | replicate cost | replicate p50 | replicate mem | decision |
|---|---|---|---|---|---|---|---|---|
| `58c5d609e8` | composition | +29.5% | pass | +26.6% [+21.3, +31.0] p=0.002 | +30.9% [+27.3, +34.5] | +46.9% [+39.7, +51.4] | -0.0% [-1.1, +0.7] | promoted |
| `cc73042ecc` | composition | +28.6% | pass | +29.6% [+23.4, +34.9] p=0.002 | +30.2% [+27.3, +33.2] | +52.3% [+45.3, +56.4] | +1.4% [+0.3, +2.2] | promoted |
| `8de55aa0a3` | composition | +27.1% | pass | +27.6% [+22.7, +32.6] p=0.002 | +27.9% [+24.4, +31.1] | +48.7% [+38.9, +52.1] | +2.6% [+1.2, +3.1] | promoted |
| `f281a4a575` | db.index | +24.7% | fail | +22.3% [+17.0, +27.4] p=0.002 | — | — | — | not promoted (L6 fail) |
| `803f6d1d23` | db.index | +24.3% | pass | +15.4% [+9.5, +21.0] p=0.002 | +22.0% [+18.4, +25.4] | +10.5% [+1.8, +17.6] | +1.8% [+1.0, +2.7] | promoted |
| `1abc381f85` | db.index | +22.8% | fail | +15.7% [-4.8, +26.4] p=0.059 | — | — | — | not promoted (L6 fail) |

**What the headline gain is made of** (`colloid verify --ablate 58c5d609e8`: each gene removed in turn, the rest checked by the oracle and measured vs baseline over 6 cycles; contribution = gain(full) − gain(without the gene), as a log-ratio with a 95% CI). Full program: cost +30.9% [+27.3, +34.5].

| gene | cost without it | contribution (log-ratio) | carries gain? |
|---|---|---|---|
| db.idx_orders_customer_placed = True [knob_perturb] | +25.4% [+22.3, +28.5] | +0.078 [+0.011, +0.144] | **yes** |
| shop_score_batch: code rewrite (llm_rewrite/optimize) optimize | +28.4% [+25.2, +31.5] | +0.036 [-0.032, +0.105] | no (hitchhiker) |
| score: code rewrite (llm_rewrite/optimize) optimize | +31.2% [+28.0, +34.3] | -0.003 [-0.073, +0.066] | no (hitchhiker) |
| db.idx_reviews_product = True [knob_sample] | +3.6% [-0.5, +7.6] | +0.334 [+0.267, +0.401] | **yes** |
| Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 | +30.9% [+27.7, +34.1] | -0.000 [-0.070, +0.070] | no (hitchhiker) |

**Minimal program** (2 gene(s): db.idx_orders_customer_placed = True [knob_perturb]; db.idx_reviews_product = True [knob_sample]): cost +31.3% [+28.1, +34.1] vs baseline.


**Measured epistasis** (ε = gain(a+b) − gain(a) − gain(b); + synergy, − interference):

- db.jit = False [knob_perturb] **+** db.idx_products_desc_trgm = True [knob_sample]: ε = +0.068 [-0.055, +0.192] (synergy, CI spans 0)
- db.idx_orders_customer_placed = True [knob_perturb] **+** find_candidates: code rewrite (py_rewrite/dedupe_seen_set) list-membership dedupe of 'candidates' at line 12 uses a shadow set: ε = -0.027 [-0.151, +0.096] (interference, CI spans 0)
- levenshtein: code rewrite (llm_rewrite/optimize) optimize **+** Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3: ε = +0.000 [-0.139, +0.139] (synergy, CI spans 0)
- levenshtein: code rewrite (llm_rewrite/optimize) optimize **+** db.random_page_cost = 4.51336845129871 [knob_perturb]: ε = +0.029 [-0.127, +0.185] (synergy, CI spans 0)
- db.random_page_cost = 4.51336845129871 [knob_perturb] **+** Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3: ε = -0.042 [-0.184, +0.100] (interference, CI spans 0)
- levenshtein: code rewrite (llm_rewrite/optimize) optimize **+** db.idx_products_category = True [knob_sample]: ε = +0.038 [-0.112, +0.188] (synergy, CI spans 0)
- levenshtein: code rewrite (llm_rewrite/optimize) optimize **+** db.idx_reviews_product = True [knob_sample]: ε = -0.047 [-0.239, +0.145] (interference, CI spans 0)
- db.idx_products_category = True [knob_sample] **+** db.idx_reviews_product = True [knob_sample]: ε = -0.051 [-0.232, +0.130] (interference, CI spans 0)
- db.idx_products_category = True [knob_sample] **+** Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3: ε = -0.065 [-0.202, +0.072] (interference, CI spans 0)
- db.idx_reviews_product = True [knob_sample] **+** Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3: ε = +0.083 [-0.133, +0.299] (synergy, CI spans 0)

**Shapley attribution** (exact Shapley value of each gene's cost contribution within the program, log-ratio, 95% CI; a CI spanning 0 marks a gene the pruning step drops):

| program | gene | Shapley value |
|---|---|---|
| `be3a9d49b7` | db.jit = False [knob_perturb] | -0.010 [-0.072, +0.052] |
| `be3a9d49b7` | db.idx_products_desc_trgm = True [knob_sample] | +0.002 [-0.060, +0.064] |
| `b7d4942627` | db.idx_orders_customer_placed = True [knob_perturb] | +0.027 [-0.034, +0.089] |
| `b7d4942627` | find_candidates: code rewrite (py_rewrite/dedupe_seen_set) list-membership dedupe of 'candidates' at line 12 uses a shadow set | +0.032 [-0.030, +0.094] |
| `49e143d754` | levenshtein: code rewrite (llm_rewrite/optimize) optimize | +0.000 [-0.068, +0.069] |
| `49e143d754` | Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 | +0.055 [-0.014, +0.123] |
| `d243510167` | levenshtein: code rewrite (llm_rewrite/optimize) optimize | -0.007 [-0.062, +0.049] |
| `d243510167` | db.random_page_cost = 4.51336845129871 [knob_perturb] | -0.030 [-0.084, +0.024] |
| `d243510167` | Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 | +0.014 [-0.038, +0.066] |
| `f281a4a575` | levenshtein: code rewrite (llm_rewrite/optimize) optimize | +0.002 [-0.045, +0.049] |
| `f281a4a575` | db.idx_products_category = True [knob_sample] | -0.026 [-0.071, +0.019] |
| `f281a4a575` | db.idx_reviews_product = True [knob_sample] | +0.323 [+0.276, +0.371] |
| `f281a4a575` | Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 | -0.016 [-0.061, +0.029] |
| `803f6d1d23` | levenshtein: code rewrite (llm_rewrite/optimize) optimize | -0.032 [-0.102, +0.039] |
| `803f6d1d23` | db.idx_reviews_product = True [knob_sample] | +0.272 [+0.202, +0.343] |
| `803f6d1d23` | Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3 | +0.037 [-0.022, +0.097] |

**Operator credit** (bandit, mean reward per proposal):

| operator | model / template | pulls | mean reward |
|---|---|---|---|
| splice |   | 6 | 20.6% |
| knob_sample |   | 7 | 9.6% |
| knob_perturb |   | 4 | 2.8% |
| redteam |   | 10 | 2.0% |
| gi_edit |   | 5 | 0.8% |
| py_rewrite |   | 3 | 0.8% |
| llm_rewrite | qwen2.5-coder-1.5b optimize | 3 | 0.7% |
| llm_rewrite | qwen2.5-coder-3b optimize | 7 | 0.4% |
| llm_rewrite | qwen2.5-coder-3b algorithmic | 21 | 0.1% |
| knob_reset |   | 1 | 0.0% |
| crossover |   | 2 | 0.0% |
| llm_rewrite | qwen2.5-coder-3b sql_batching | 10 | 0.0% |

**LLM usage**: 85 calls (81,164 in / 29,624 out tokens), local models {'qwen2.5-coder-3b': 74, 'qwen2.5-coder-1.5b': 11}, $0.248 compute-priced.

**A/A noise floor** (20 runs of the L5 protocol, identical program vs itself, α=0.05):

| objective | raw FPR (within-run CI) | effect SD across runs | median within-run SE | τ (between-run SD) | calibrated FPR (leave-one-out) | binomial p |
|---|---|---|---|---|---|---|
| cost | 35% (7) | 3.36% | 1.43% | 2.96% | 5% (1) | 0.642 |
| cpu | 35% (7) | 3.42% | 1.46% | 3.01% | 5% (1) | 0.642 |
| mem | 30% (6) | 0.81% | 0.31% | 0.71% | 5% (1) | 0.642 |
| p50 | 25% (5) | 8.18% | 4.37% | 6.84% | 10% (2) | 0.264 |
| p95 | 20% (4) | 5.86% | 3.75% | 4.02% | 5% (1) | 0.642 |

Gate: cost: 1/20 calibrated false positives (raw 7/20), binomial p=0.642 vs alpha=0.05 → promotions allowed: **True**.

**Red-team island**: 8 attacks rejected at L0/L1, 0 caught by the L2 oracle, 0 classified inert in-run, **2 breach alert(s)** raised in-run.

Re-adjudication of the breach alerts (`colloid redteam-recheck`: the maximal version of the same hack on the same unit is sent through the oracle; caught = live channel = genuine breach, passes = inert attack):

| flagged program | unit | on a request path | hack | verdict |
|---|---|---|---|---|
| `a1fc8c05b3` | _bool | False | truncate | **inert** |
| `6975330ff3` | lifespan | False | round_floats | **inert** |

**Genuine evaluator breaches after re-adjudication: 0.**

**Suspicion triggers**: 2 gains exceeded the 2× threshold and were sent to mandatory deep review.

<!-- /RESULTS:RUN -->

### 4.3 What running it at full scale exposed — and the fixes

Running the system end to end on real hardware found six defects that no unit test had
caught. Each fix has a regression test.

1. **Telemetry could crash the engine** (first attempt, §4.1). A field named `kind` collided
   with the event-kind argument. Reserved envelope keys are now namespaced.
2. **The A/A gate was advisory and the CIs miscalibrated** (§2.2). The engine promoted with
   the gate closed. Within-run CIs ignored between-run variance, giving a 35% raw false-positive
   rate. Fixed with the random-effects noise floor, leave-one-out calibration, a binomial gate
   and enforcement (calibrated false-positive rate 5%).
3. **Every native-code L6 failed because of the evaluator, not the candidate.**
   LeakSanitizer's stop-the-world needs ptrace, and the sandbox's seccomp policy denies it, so
   every sanitized fuzz run aborted. Six of the run's eight in-run L6 reviews were lost to it,
   including those of the eventual best program `58c5d609` and of `803f6d1d`. The other two
   failed on their merits: their holdout gain did not persist. The fuzz driver now measures leaks itself without ptrace, exactly:
   0 bytes of growth for leak-free code, and a new `native_leak` canary is caught at ~2 KB per
   call. A crashed sanitizer runtime is now an infrastructure ERROR. `colloid verify` then gave
   the affected programs a fair L6, without ever re-testing a program that had failed on its
   merits.
4. **Both red-team "breaches" were inert attacks.** A float-rounding wrapper on the ASGI
   startup hook (returns `None`) and a list-truncating wrapper on a config parser (returns a
   `bool`) could never change a response. Attacks now target only value-returning functions
   on a request path. The engine confirms liveness with the *maximal* version of the same
   attack, and the float attack now recurses into tuples. Re-adjudication confirmed both
   alerts inert. The probe is tested both ways: caught on `rating_summary`, inert on
   `lifespan`.
5. **A sandbox race.** On a wall-clock kill, the timer thread tears down the cgroups while
   the caller reads `memory.oom_control`. Mid-teardown that read returns ENODEV, which
   escaped (seen once in the integration suite). The OOM flag is now snapshotted under the
   process lock before removal, and a deterministic fault-injection test covers it.
6. **A stress metric that could not fail.** The sandbox stress test's "peak live cgroups" was
   sampled after each round had already cleaned up, so it always read 0. A 10 ms monitor
   thread now samples during the rounds.

### 4.4 Limitations this run makes visible

- **Promoted programs are not minimal.** Shapley pruning runs on a schedule over the islands'
  top programs. The three top splices were assembled late and never pruned before
  verification, so they carry hitchhiker genes, among them a `gi_edit` that duplicates a line
  in `Executor.fetchrow` and LLM rewrites of C code that add no measurable gain. Promotion
  should require a pruning pass. The ablation above shows what that pass would keep.
- **The L3 surrogate learned nothing at this scale.** Its prequential Spearman ρ was ≈ 0
  after 10–20 examples, so it was never trusted and cut no candidates (0 `surrogate_cut`
  events), which is the audit working as designed. A 10-generation run is too short for a
  learned pre-filter to pay off.
- **Small local LLMs had low yield.** Of 85 calls to Qwen2.5-Coder 3B/1.5B:
  - 44 were rejected by the response parser: 23 added imports outside the locus, 11 changed a
    function's sync/async nature, 8 returned the code unchanged, and 2 added extra top-level
    code;
  - 32 were rejected by the cascade: 12 failed to build and 20 failed the oracle;
  - **7 were admitted**.

  None of the admitted LLM edits that attribution measured has a contribution distinguishable
  from zero, and the bandit's credit for the LLM arms is near zero. The structural wins came
  from the knob, index and splice operators. This is a statement about 3B-class models under
  strict locus confinement, not about LLM operators in general.
- **The host sets the detection limit.** With τ ≈ 3% between runs, a two-cycle L5 can confirm
  gains above ≈ 6.5%. Smaller real improvements are found by the search but cannot be
  certified here without the many-cycle replicate or a quiet bench host.
- **One L6 rejection may be a soak false alarm.** `f281a4a5` failed the 20-second soak with
  0.89 MB/s of memory growth. That is above threshold but could be warm-up growth rather than a
  leak. It was not re-tested, because re-testing L6 failures until they pass is exactly what
  the verify rules forbid.

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

The blocks below come from this host (cgroup v1, root). The same concurrent sandbox stress
also runs on every push in CI on a **cgroup v2** kernel. Its result on the commit that added
the filesystem jail: 48/48 adversarial payloads contained, a peak of 8 live sandbox cgroups
(one unified cgroup per tree × 8 workers) → 0 after, no fd leak, every payload killed within
4.04 s.

<!-- RESULTS:STRESS -->

**Concurrent sandbox stress** (48 adversarial payloads, 8 concurrent workers, 21.4s): 48/48 contained (incl. killed within wall-clock + 3 s; slowest 4.06s), peak 36 live sandbox cgroups → 0 after (leak 0), fd leak 0, filesystem escape False. **Overall: PASS.**

**Evaluator robustness stress** (945.2s): all pathological genomes rejected = True (infinite loop killed fast = True); canary suite 16/16 rejected under stress; A/A promotions allowed = True. **Overall: PASS.**

| pathological genome | rejected at | verdict | wall time |
|---|---|---|---|
| infinite_loop | L2 | FAIL | 17.0 s |
| raises | L2 | FAIL | 5.2 s |
| wrong_type | L2 | FAIL | 4.6 s |
| syntax_error | L0 | FAIL | 0.0 s |
| huge_diff | L0 | FAIL | 0.0 s |
| wrong_result | L2 | FAIL | 4.1 s |

**Stress A/A (independent of the run's own A/A)** (20 runs of the L5 protocol, identical program vs itself, α=0.05):

| objective | raw FPR (within-run CI) | effect SD across runs | median within-run SE | τ (between-run SD) | calibrated FPR (leave-one-out) | binomial p |
|---|---|---|---|---|---|---|
| cost | 25% (5) | 2.91% | 1.53% | 2.12% | 0% (0) | 1.000 |
| cpu | 25% (5) | 2.96% | 1.56% | 2.15% | 0% (0) | 1.000 |
| p50 | 10% (2) | 6.21% | 5.95% | 1.76% | 10% (2) | 0.264 |
| p95 | 25% (5) | 6.19% | 4.14% | 4.09% | 0% (0) | 1.000 |
| mem | 35% (7) | 0.88% | 0.25% | 0.82% | 10% (2) | 0.264 |

Gate: cost: 0/20 calibrated false positives (raw 5/20), binomial p=1.000 vs alpha=0.05 → promotions allowed: **True**.

<!-- /RESULTS:STRESS -->

---

## 6. What the stress tests and cross-platform CI found

The run (§4.3) was not the only thing that found defects. The stress harnesses and the first
CI runs on other platforms found eight more, each fixed with a regression test:

1. **An infinite-loop candidate took 156 s to reject.** Each oracle request waited the full
   client timeout in turn. The oracle now stops at the first request that takes more than
   max(10 s, 50× the reference's latency): **17 s**.
2. **`lazy_skip` slipped past L2 once** at 8 samples per endpoint (§2.1). The quick oracle now
   takes 12 (deep: 20).
3. **The sandbox stress's "peak cgroups" metric could never be non-zero.** It was sampled after
   cleanup. A 10 ms monitor thread now samples during the rounds.
4. **cgroup v2 hosts:** the sandbox pre-created v1 controller directories. It now supports the
   unified hierarchy (`memory.max`, `memory.events`, `cpu.stat`, `pids.max`, `cgroup.kill`),
   and the load generator reads `cpu.stat`.
5. **Write confinement relied on directory permissions** (found on the v2 runner), so
   candidates now run in the filesystem jail (§2.3).
6. **Windows:** the portable CPU counter's atomic replace collides with a concurrent reader.
   Bounded retries now cover the writer, the reader and the Go load generator.
7. **The portable sandbox reported exit code 0 for a killed process,** because psutil reaped our
   own child before `Popen` could.
8. **macOS and Windows:** the target looked up the `postgres` OS account eagerly. It now does so
   on first use.

Final state: **CI green on Ubuntu, macOS and Windows** (lint, strict types, architecture
contracts, portable tests) **and on the cgroup v2 root sandbox job**. Locally, **158 tests
pass**, including every root integration test.

## 7. The data lake and the verified stack

The run's verified knowledge lives on the branch `colloid/datalake`:
- **12 ledger entries**: 7 gene records and 5 program records (the 4 promoted programs, plus
  a superseding record for `58c5d609` that carries its ablation evidence);
- hash chain head `5df73c4e…`, verified with `colloid lake verify`;
- the lineage links show smaller verified programs being extended by larger ones.

The deployable result is on the branch **`stack/stackzero-verified`**, materialised from that
record with hitchhikers left out:
- **What it changes:** two idempotent index migrations,
  `orders(customer_id, placed_at DESC, id DESC)` and `reviews(product_id)`, and no code
  changes.
- **Measured on its own:** cost per request **31.3% lower (95% CI [28.2%, 34.1%])**, 6-cycle
  replicate, after an L2 oracle check.
- **Provenance:** the MANIFEST ties each change to its lake record and evidence.

A new run with `lake: git:colloid/datalake` re-evaluates these genes from scratch and starts
its operator bandit from the attribution evidence (ADR 0005, 0006).

## 8. How to reproduce

```bash
pip install -e ".[dev]"                 # core + dev; add the local-LLM extra for Qwen arms
colloid atlas                           # Stack Atlas: 172 units, 7 cross-layer paths, 106 loci
colloid baseline                        # §1
colloid canaries --out canary.json      # §2.1 (exits non-zero if any hack survives)
colloid aa --runs 20 --out aa.json      # §2.2
colloid profile                         # §3
colloid run experiments/stackzero.yaml  # §4
colloid verify runs/stackzero --top 6   # §4.2 post-run L6 + 6-cycle replicate
colloid verify runs/stackzero --ablate 58c5d609e82eef78   # §4.2 what the gain is made of
colloid redteam-recheck runs/stackzero  # §4.3 re-adjudicate red-team alerts
colloid report runs/stackzero           # the numbers in §4, from the run's own store
python stress/render_results.py runs/stackzero --write docs/INITIAL_RESULTS.md --evidence docs/results
python stress/stress_sandbox.py         # §5 (as root)
python stress/stress_evaluator.py       # §5 (as root)
colloid lake ingest runs/stackzero --lake git:colloid/datalake    # §7
colloid stack materialize fc1bc599 --carrying-only --out stack-out && colloid stack publish stack-out   # §7
```

Every promoted variant carries a human-readable explanation generated from its genes and
attribution (see `colloid report`), and is reproducible from `(baseline commit, gene payloads,
adapter versions, environment fingerprint)`.
