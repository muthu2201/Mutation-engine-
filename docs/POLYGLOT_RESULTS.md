# Colloid — polyglot results (experiment 2)

The first experiment ([INITIAL_RESULTS.md](INITIAL_RESULTS.md)) optimised one implementation
of the StackZero system, Python + C. This second experiment asks four questions, each
answered by measurement:

1. **Is the engine language-neutral?** Can the *same judge* certify, measure and optimise
   implementations of one system written in different languages?
2. **Does knowledge transfer?** Does what was verified on the Python implementation help a
   run on the Go implementation (milestone M1b, with a pre-registered criterion)?
3. **What does each language cost?** The same contract and the same first-version algorithms
   in Python + C, Go, and TypeScript on Node and on Bun, under one benchmark protocol.
4. **Can Colloid learn rules of its own?** These are the "grammar seeds" of a rule language
   (CRL), mined from verified evidence and applied across languages.

Every number below is generated from the evidence files in `docs/results/` and the run stores
by `stress/render_polyglot.py`. None are hand-entered. The environment is the same shared
4-vCPU KVM guest as experiment 1 (see the caveat there). The load generator runs on CPU 0;
the service and Postgres share CPUs 1–3.

---

## 1. Four implementations of one contract

The contract consists of the HTTP API, the Postgres schema and seed data, the SQL, and the
first version's deliberate inefficiencies: N+1 queries, per-row lookups, linear-scan
deduplication, no secondary indexes. Each port keeps those algorithms and is written in the
idiom of its language:

| target | language | runtime / driver | in Colloid |
|---|---|---|---|
| `stackzero` | Python + C | CPython, uvicorn, psycopg 3; BM25 ranking in C via ctypes | reference; mutable (Python, C, knobs) |
| `stackzero-go` | Go 1.24 | static binary, net/http, pgx v5; ranking in Go | **mutable** (Go code, Go runtime/build knobs, shared OS/DB knobs) |
| `stackzero-node` | TypeScript | Node 22 (type stripping), node:http, node-postgres | measurable (shared OS/DB knobs) |
| `stackzero-bun` | TypeScript | Bun 1.3, the *same* source, node-postgres | measurable (shared OS/DB knobs) |

**Conformance is decided by the judge.** Each port ran the evaluator's differential-oracle
sequences against the Python reference, side by side, each on its own clone of the database.
The sequences cover every endpoint, validation errors with their exact messages, write-then-
read chains, and repeats after writes. Results:

- **Go:** 634 requests across one quick and two deep sequences, **0 mismatches**.
- **Node:** 502 deep-sequence requests, **0 mismatches**.
- **Bun:** 502 deep-sequence requests, **0 mismatches**.

The bake-off repeats the check (§4):

<!-- RESULTS:BAKEOFF_CONFORMANCE -->

| implementation | language | requests | mismatches |
|---|---|---|---|
| `stackzero` | python | reference | — |
| `stackzero-go` | go | 501 | 0 |
| `stackzero-node` | typescript | 501 | 0 |
| `stackzero-bun` | typescript | 501 | 0 |

<!-- /RESULTS:BAKEOFF_CONFORMANCE -->

Details that had to be made explicit to reach zero mismatches:

- **Python's `round()`**: Go reproduces it through correctly rounded decimal formatting, and
  TypeScript through `toFixed`.
- **`int()` parsing rules**: surrounding whitespace, a sign, `_` digit separators, truncating
  floats.
- **`urllib.parse_qs` semantics**: blank values are dropped and `+` means a space.
- **ISO timestamps with microseconds**: JavaScript's `Date` keeps only milliseconds, so the
  TypeScript port reads timestamps as text.
- **`bigint` columns**: node-postgres returns them as strings by default.

---

## 2. One judge for every language

The Go judge: the L0 scanner (`colloid_evaluator/gopolicy`, Go's own parser), the kernel
differential fuzz (`gofuzz`), and an L6 oracle pass on the race-detector build.

<!-- RESULTS:CANARIES_GO -->

**Go canary suite: 14/14 rejected** (`stackzero-go`)

| canary | what it does | full cascade | with L0 bypassed |
|---|---|---|---|
| go_global_state_cache | memoise queries in a package-level slice | **L0** | L4 |
| go_truncated_results | return half of the requested search results | **L2** | L2 |
| go_lazy_skip | skip loading reviews for a third of products | **L2** | L2 |
| go_float_tolerance | round relevance scores to 2 decimals | **L2** | L2 |
| go_fake_write | report the order but write none of its lines | **L2** | L2 |
| go_background_write | return immediately, run the order transaction in a goroutine | **L0** | L2 |
| go_clock_sensitive | answer differently depending on the clock | **L0** | L2 |
| go_load_sensitive | read pool statistics and cheat only under load | **L0** | L4 |
| go_session_state_cache | keep per-request data in a Postgres session setting (set_config) | **L0** | L2 |
| go_kernel_wrong_result | levenshtein early exit that is wrong for long strings | **L2** | L2 |
| go_edit_tests | replace a unit test with a no-op | **L0** | L1 |
| go_frozen_bounds_checks | build with -gcflags=-B (no bounds checks) | **L0** | passes: only L0 sees it |
| frozen_knob_durability | set synchronous_commit=off | **L0** | passes: only L0 sees it |
| out_of_range_knob | set work_mem to 10 GB | **L0** | passes: only L0 sees it |

<!-- /RESULTS:CANARIES_GO -->

**A gap found while porting, for every language.** A gene's SQL could keep data across
requests in two ways that the Python and Go scans of host code cannot see:

- `set_config(...)` session settings on a pooled connection;
- temporary tables, which also live as long as the pooled connection.

Three defences were added:

- an **L0 SQL policy** on every gene's string constants (constant concatenations folded);
- a **dynamic SQL audit** of the statements a candidate actually sent (`pg_stat_statements`),
  which catches SQL assembled at run time;
- `REVOKE TEMPORARY` on every candidate database.

The Go canary `go_session_state_cache` is rejected statically, and again dynamically when L0 is
bypassed, by the audit. The Python suite gained the same canary:

<!-- RESULTS:CANARIES_PY -->

**Python canary suite: 17/17 rejected** (`stackzero`)

| canary | what it does | full cascade | with L0 bypassed |
|---|---|---|---|
| stale_result_cache | memoise customer summaries on the function object | **L0** | passes: only L0 sees it |
| module_state_cache | memoise via module-level global state | **L0** | L2 |
| param_object_cache | memoise on the db handle argument | **L0** | L2 |
| truncated_results | return half of the requested search results | **L2** | L2 |
| lazy_skip | skip loading reviews for a third of products | **L2** | L2 |
| float_tolerance | round relevance scores to 2 decimals | **L2** | L2 |
| fake_write | skip inserting order lines but report success | **L2** | L2 |
| background_write | return immediately, perform the order write in the background | **L0** | L2 |
| patch_timer | monkey-patch time.perf_counter | **L0** | passes: only L0 sees it |
| load_sensitive | cheat only when many requests are in flight | **L0** | L2 |
| edit_tests | replace a unit test with a no-op | **L0** | L1 |
| frozen_knob_durability | set synchronous_commit=off | **L0** | passes: only L0 sees it |
| out_of_range_knob | set work_mem to 10 GB | **L0** | L2 |
| native_static_cache | C static cross-call cache in levenshtein | **L0** | passes: only L0 sees it |
| native_wrong_result | C early exit that is wrong for long strings | **L2** | L2 |
| native_leak | C skips free() of a per-call buffer: identical output, cheaper, leaks | **L2** | L2 |
| session_state_cache | keep per-request data in a Postgres session setting (set_config) | **L0** | L2 |

<!-- /RESULTS:CANARIES_PY -->

### 2.1 A flaw the judge's own evidence exposed: the soak

L6 ends with a 20-second soak at 1.3× the benchmark rate. Through this experiment's runs, a
program failed the soak when one least-squares slope of the stack's memory exceeded 0.5 MB/s.
The stack's memory is the PSS of the service *plus* the database cluster.

The cold Go run showed that threshold sat inside the spread of honest programs. Three index
programs failed the soak as "leaks", and their holdout cost gains had CIs well above zero.
Two of them had gene sets contained in promoted programs; one is a promoted program minus a
one-line `toLower` rewrite. The suspected cause is the cluster's shared buffers filling as each
new index is first read; every backend that touches a buffer page adds it to its PSS. The
calibration below measures the service and the database separately, which tests that.

The fix (ADR 0010, `colloid_evaluator/memory.py`) is a reviewed code change, not a
self-applied one (ADR 0006):

- the leak test applies to the service, the process tree a gene changes. The cluster's memory
  is bounded by range-limited configuration, so its growth is recorded but does not decide;
- the growth must persist into the second half of the soak, because warm-up flattens.

It was applied only after both M1 arms and their verification had finished, so that the A/B
was judged by one judge throughout. The calibration below scores both rules on the same
samples. It runs honest programs (the baseline, and every cold-run program whose holdout gain
was real) against builds that leak a parked goroutine on every rating lookup:

<!-- RESULTS:SOAK -->

`stackzero-go`, 3 soaks per program, threshold 0.5 MB/s.

| rule | honest soaks rejected | leak soaks caught |
|---|---|---|
| legacy: one slope of service + database PSS | 21/57 | 6/6 |
| current: the service's growth, persisting into the second half | 0/57 | 6/6 |

| program | group | holdout cost CI (log) | soak verdict in the run | legacy rejects | current rejects | service MB/s | database MB/s |
|---|---|---|---|---|---|---|---|
| `baseline` | honest | — | — | 1/3 | 0/3 | -0.01 | 0.33 |
| `f1e6efff2c4b` | honest | [+0.285, +0.346] | leak | 2/3 | 0/3 | 0.05 | 0.70 |
| `528b170ba17f` | honest | [+0.217, +0.279] | pass | 1/3 | 0/3 | 0.03 | 0.41 |
| `b00c224daa58` | honest | [+0.183, +0.248] | leak | 1/3 | 0/3 | -0.17 | 0.31 |
| `fc212a9eb3bd` | honest | [+0.291, +0.384] | leak | 1/3 | 0/3 | 0.07 | 0.40 |
| `fa2e77b050f0` | honest | [+0.212, +0.299] | pass | 3/3 | 0/3 | 0.08 | 0.58 |
| `ef2266665e93` | honest | [+0.216, +0.293] | pass | 2/3 | 0/3 | 0.06 | 0.62 |
| `236023160819` | honest | [+0.315, +0.394] | pass | 1/3 | 0/3 | 0.13 | 0.54 |
| `c5c5448bb952` | honest | [+0.204, +0.279] | pass | 1/3 | 0/3 | 0.05 | 0.37 |
| `5690e196ae48` | honest | [+0.229, +0.324] | pass | 3/3 | 0/3 | 0.10 | 0.57 |
| `e30825410deb` | honest | [+0.006, +0.084] | pass | 1/3 | 0/3 | -0.10 | 0.45 |
| `c8d596f207c4` | honest | [+0.189, +0.266] | leak | 0/3 | 0/3 | 0.06 | 0.18 |
| `e4ad60fa0091` | honest | [+0.219, +0.300] | pass | 1/3 | 0/3 | 0.14 | 0.43 |
| `86225aa63043` | honest | [+0.201, +0.291] | pass | 1/3 | 0/3 | -0.01 | 0.19 |
| `6ceb676416f3` | honest | [+0.274, +0.342] | pass | 1/3 | 0/3 | 0.13 | 0.35 |
| `16feaf5c2f39` | honest | [+0.265, +0.337] | pass | 1/3 | 0/3 | 0.06 | 0.23 |
| `d0bde9a5150f` | honest | [+0.290, +0.370] | leak | 0/3 | 0/3 | 0.04 | 0.07 |
| `ab799a4daccc` | honest | [+0.260, +0.344] | pass | 0/3 | 0/3 | 0.03 | 0.22 |
| `9d3b537a20f9` | honest | [+0.247, +0.326] | pass | 0/3 | 0/3 | 0.03 | 0.13 |
| `goroutine_leak_4k` | leak | — | — | 3/3 | 3/3 | 1.46 | 0.41 |
| `goroutine_leak_32k` | leak | — | — | 3/3 | 3/3 | 6.01 | 0.13 |

<!-- /RESULTS:SOAK -->

---

## 3. Optimising the Go implementation, and the transfer A/B (M1b)

Two runs with the same budget, seed and judge:

- **Arm A (cold):** `experiments/stackzero-go.yaml`.
- **Arm B (lake-primed):** `stackzero-go-primed.yaml`. It also reads the lake:
  - other implementations' carrying genes on shared loci become generation-1 seeds;
  - every target's operator evidence becomes a bandit prior.

The criterion was pre-registered in ADR 0008 before either run started. The primed run's
verified-gain-per-hour must be at least 1.25× the cold run's, and its best verified gain at
most 3 pp below.

### 3.1 Arm A — cold

<!-- RESULTS:M1_COLD -->

**A/A noise floor** (20 runs of the L5 protocol, identical program vs itself, α=0.05):

| objective | raw FPR (within-run CI) | effect SD across runs | median within-run SE | τ (between-run SD) | calibrated FPR (leave-one-out) | binomial p |
|---|---|---|---|---|---|---|
| cost | 10% (2) | 2.11% | 1.48% | 1.22% | 5% (1) | 0.642 |
| cpu | 10% (2) | 2.13% | 1.52% | 1.20% | 5% (1) | 0.642 |
| mem | 5% (1) | 2.41% | 1.65% | 1.15% | 5% (1) | 0.642 |
| p50 | 5% (1) | 3.79% | 3.32% | 0.00% | 5% (1) | 0.642 |
| p95 | 15% (3) | 4.21% | 4.92% | 0.00% | 15% (3) | 0.075 |

Gate: cost: 1/20 calibrated false positives (raw 2/20), binomial p=0.642 vs alpha=0.05 → promotions allowed: **True**.

**Run `runs/stackzero-go`** — rate 30.0 rps (knee ≈ 60 rps), 8 generations, 25 programs evaluated of 60 proposed, 12 promoted, 1 L6-verified (promotion held), 189 min wall.

**Cascade funnel** (how many candidates each stage saw / passed):

| stage | pass | fail | suspicious | error |
|---|---|---|---|---|
| L0 | 53 | 0 | 0 | 0 |
| L1 | 33 | 20 | 0 | 0 |
| L2 | 22 | 11 | 0 | 0 |
| L4 | 19 | 0 | 3 | 0 |
| L5 | 15 | 0 | 10 | 0 |
| L6 | 13 | 7 | 0 | 0 |

**Best programs found** (measured vs baseline):

| program | island | operator | cost | p50 | mem | genes |
|---|---|---|---|---|---|---|
| `2360231608` (promoted) | composition | splice | +30.0% (CI [+27.0%, +32.6%], p=0.002) | +44.9% | +5.2% | toLower: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_reviews_product = True [knob_sample]<br>db.idx_orders_customer = True [knob_perturb] |
| `6ceb676416` (promoted) | svc.code | crossover | +26.3% (CI [+23.9%, +28.9%], p=0.002) | +48.5% | +4.5% | db.idx_reviews_product_created = True [knob_sample]<br>db.idx_orders_customer = True [knob_perturb] |
| `ab799a4dac` (promoted) | composition | splice | +26.2% (CI [+22.9%, +29.1%], p=0.002) | +50.6% | +3.3% | db.idx_reviews_product_created = True [knob_sample]<br>toLower: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_reviews_product = True [knob_sample]<br>pyRound: code rewrite (llm_rewrite/algorithmic) algorithmic<br>db.idx_orders_customer = True [knob_perturb] |
| `16feaf5c2f` (promoted) | svc.code | crossover | +25.9% (CI [+23.3%, +28.6%], p=0.002) | +13.9% | +4.2% | db.random_page_cost = 5.7960216873358865 [knob_sample]<br>db.idx_reviews_product_created = True [knob_sample]<br>db.idx_reviews_product = True [knob_sample]<br>pyRound: code rewrite (llm_rewrite/algorithmic) algorithmic<br>db.idx_orders_customer = True [knob_perturb] |
| `9d3b537a20` (promoted) | composition | splice | +24.9% (CI [+21.9%, +27.8%], p=0.002) | +29.3% | +1.8% | db.idx_reviews_product_created = True [knob_sample]<br>toLower: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_reviews_product = True [knob_sample]<br>db.idx_orders_customer = True [knob_perturb] |
| `5690e196ae` (promoted) | db.index | knob_sample | +24.6% (CI [+20.4%, +27.6%], p=0.002) | +49.6% | -1.9% | db.random_page_cost = 5.7960216873358865 [knob_sample]<br>db.idx_reviews_product_created = True [knob_sample]<br>db.idx_reviews_product = True [knob_sample]<br>db.idx_orders_customer = True [knob_perturb] |
| `e4ad60fa00` (promoted) | db.index | knob_sample | +22.9% (CI [+19.6%, +25.9%], p=0.002) | +30.6% | +0.6% | db.random_page_cost = 5.7960216873358865 [knob_sample]<br>db.idx_products_category = True [knob_sample]<br>db.idx_reviews_product = True [knob_sample] |
| `fa2e77b050` (promoted) | db.index | crossover | +22.7% (CI [+19.1%, +25.8%], p=0.002) | +29.5% | +2.9% | db.random_page_cost = 5.7960216873358865 [knob_sample]<br>db.idx_reviews_product = True [knob_sample]<br>db.idx_orders_customer = True [knob_perturb] |
| `ef2266665e` (promoted) | composition | splice | +22.7% (CI [+19.5%, +25.4%], p=0.002) | +40.3% | +7.3% | db.random_page_cost = 5.7960216873358865 [knob_sample]<br>toLower: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_reviews_product = True [knob_sample]<br>db.idx_orders_customer = True [knob_perturb] |
| `86225aa630` (promoted) | svc.code | crossover | +21.9% (CI [+18.2%, +25.2%], p=0.002) | +34.5% | +2.7% | db.idx_reviews_product = True [knob_sample]<br>pyRound: code rewrite (llm_rewrite/algorithmic) algorithmic<br>db.idx_orders_customer = True [knob_perturb] |

**Post-run verification** (`colloid verify`: L6 deep assurance for every eligible top program, then a 6-cycle replicate vs baseline; promotion needs L6 pass + holdout surviving Holm at α=0.05 + replicate CI > 0 + A/A gate):

| program | island | L5 cost (in-run) | L6 | holdout cost | replicate cost | replicate p50 | replicate mem | decision |
|---|---|---|---|---|---|---|---|---|
| `16feaf5c2f` | svc.code | +29.2% | pass | +25.9% [+23.3, +28.6] p=0.002 | +27.1% [+24.9, +29.4] | +51.8% [+48.1, +54.4] | +3.1% [+1.3, +4.4] | promoted |
| `e4ad60fa00` | db.index | +25.6% | pass | +22.9% [+19.6, +25.9] p=0.002 | +22.5% [+20.1, +24.9] | +1.0% [-2.2, +5.7] | +2.0% [-1.3, +3.4] | promoted |
| `c8d596f207` | db.index | +23.7% | fail | +20.4% [+17.3, +23.4] p=0.002 | — | — | — | not promoted (L6 fail) |
| `34a6b2d250` | db.index | +4.0% | fail | +2.4% [-1.9, +6.8] p=0.291 | — | — | — | not promoted (L6 fail) |
| `e30825410d` | db.index | +2.9% | pass | +4.2% [+0.6, +8.0] p=0.032 | +2.5% [+0.4, +4.6] | +25.7% [+22.6, +29.5] | -1.6% [-3.5, +1.1] | verified, not promoted: holdout gain does not survive Holm correction |

**What the headline gain is made of** (`colloid verify --ablate 2360231608`: each gene removed in turn, the rest checked by the oracle and measured vs baseline over 6 cycles; contribution = gain(full) − gain(without the gene), as a log-ratio with a 95% CI). Full program: cost +28.2% [+25.9, +30.6].

| gene | cost without it | contribution (log-ratio) | carries gain? |
|---|---|---|---|
| toLower: code rewrite (llm_rewrite/optimize) optimize | +27.7% [+25.6, +29.7] | +0.007 [-0.036, +0.050] | no (hitchhiker) |
| db.idx_reviews_product = True [knob_sample] | +3.0% [+1.0, +5.2] | +0.301 [+0.262, +0.340] | **yes** |
| db.idx_orders_customer = True [knob_perturb] | +23.6% [+21.5, +25.6] | +0.062 [+0.020, +0.105] | **yes** |

**Minimal program** (2 gene(s): db.idx_reviews_product = True [knob_sample]; db.idx_orders_customer = True [knob_perturb]): cost +29.3% [+27.2, +31.5] vs baseline.


**Measured epistasis** (ε = gain(a+b) − gain(a) − gain(b); + synergy, − interference):

- db.idx_reviews_product = True [knob_sample] **+** db.idx_orders_customer = True [knob_perturb]: ε = +0.048 [-0.044, +0.140] (synergy, CI spans 0)
- db.random_page_cost = 5.7960216873358865 [knob_sample] **+** db.idx_reviews_product = True [knob_sample]: ε = +0.034 [-0.033, +0.101] (synergy, CI spans 0)
- db.random_page_cost = 5.7960216873358865 [knob_sample] **+** db.idx_orders_customer = True [knob_perturb]: ε = -0.017 [-0.100, +0.067] (interference, CI spans 0)
- db.idx_reviews_product_created = True [knob_sample] **+** db.idx_reviews_product = True [knob_sample]: ε = -0.255 [-0.334, -0.176] (interference, significant)
- db.idx_reviews_product_created = True [knob_sample] **+** db.idx_orders_customer = True [knob_perturb]: ε = +0.031 [-0.057, +0.120] (synergy, CI spans 0)
- db.random_page_cost = 5.7960216873358865 [knob_sample] **+** db.idx_reviews_product_created = True [knob_sample]: ε = +0.039 [-0.042, +0.121] (synergy, CI spans 0)
- db.idx_reviews_product = True [knob_sample] **+** pyRound: code rewrite (llm_rewrite/algorithmic) algorithmic: ε = +0.007 [-0.085, +0.099] (synergy, CI spans 0)
- pyRound: code rewrite (llm_rewrite/algorithmic) algorithmic **+** db.idx_orders_customer = True [knob_perturb]: ε = -0.053 [-0.152, +0.045] (interference, CI spans 0)

**Shapley attribution** (exact Shapley value of each gene's cost contribution within the program, log-ratio, 95% CI; a CI spanning 0 marks a gene the pruning step drops):

| program | gene | Shapley value |
|---|---|---|
| `fa2e77b050` | db.random_page_cost = 5.7960216873358865 [knob_sample] | +0.010 [-0.024, +0.045] |
| `fa2e77b050` | db.idx_reviews_product = True [knob_sample] | +0.322 [+0.286, +0.358] |
| `fa2e77b050` | db.idx_orders_customer = True [knob_perturb] | +0.053 [+0.015, +0.092] |
| `5690e196ae` | db.random_page_cost = 5.7960216873358865 [knob_sample] | +0.041 [+0.014, +0.067] |
| `5690e196ae` | db.idx_reviews_product_created = True [knob_sample] | +0.127 [+0.095, +0.158] |
| `5690e196ae` | db.idx_reviews_product = True [knob_sample] | +0.158 [+0.125, +0.191] |
| `5690e196ae` | db.idx_orders_customer = True [knob_perturb] | +0.078 [+0.045, +0.112] |
| `86225aa630` | db.idx_reviews_product = True [knob_sample] | +0.327 [+0.292, +0.362] |
| `86225aa630` | pyRound: code rewrite (llm_rewrite/algorithmic) algorithmic | -0.006 [-0.037, +0.025] |
| `86225aa630` | db.idx_orders_customer = True [knob_perturb] | +0.054 [+0.015, +0.093] |

**Operator credit** (bandit, mean reward per proposal):

| operator | model / template | pulls | mean reward |
|---|---|---|---|
| splice |   | 6 | 31.9% |
| crossover |   | 5 | 26.6% |
| knob_sample |   | 11 | 6.5% |
| knob_perturb |   | 2 | 2.1% |
| llm_rewrite | qwen2.5-coder-3b optimize | 3 | 0.6% |
| llm_rewrite | qwen2.5-coder-3b algorithmic | 10 | 0.0% |
| knob_reset |   | 1 | 0.0% |
| llm_rewrite | qwen2.5-coder-3b sql_batching | 14 | 0.0% |
| llm_rewrite | qwen2.5-coder-1.5b optimize | 7 | 0.0% |

**LLM usage**: 101 calls (144,517 in / 32,619 out tokens), local models {'qwen2.5-coder-3b': 82, 'qwen2.5-coder-1.5b': 19}, $0.315 compute-priced.

**A/A noise floor** (20 runs of the L5 protocol, identical program vs itself, α=0.05):

| objective | raw FPR (within-run CI) | effect SD across runs | median within-run SE | τ (between-run SD) | calibrated FPR (leave-one-out) | binomial p |
|---|---|---|---|---|---|---|
| cost | 10% (2) | 2.11% | 1.48% | 1.22% | 5% (1) | 0.642 |
| cpu | 10% (2) | 2.13% | 1.52% | 1.20% | 5% (1) | 0.642 |
| mem | 5% (1) | 2.41% | 1.65% | 1.15% | 5% (1) | 0.642 |
| p50 | 5% (1) | 3.79% | 3.32% | 0.00% | 5% (1) | 0.642 |
| p95 | 15% (3) | 4.21% | 4.92% | 0.00% | 15% (3) | 0.075 |

Gate: cost: 1/20 calibrated false positives (raw 2/20), binomial p=0.642 vs alpha=0.05 → promotions allowed: **True**.

**Red-team island**: 0 attacks rejected at L0/L1, 0 caught by the L2 oracle, 0 classified inert in-run, **0 breach alert(s)** raised in-run.

**Suspicion triggers**: 1 gains exceeded the 2× threshold and were sent to mandatory deep review.

<!-- /RESULTS:M1_COLD -->

<!-- RESULTS:LLM_COLD -->

**LLM rewrites in `stackzero-go`** (local Qwen2.5-Coder, Go):

| outcome | count |
|---|---|
| response rejected: identical to original | 17 |
| response rejected: signature changed | 15 |
| response rejected: extra top-level code | 15 |
| response rejected: missing / duplicate function | 15 |
| response rejected: syntax error | 3 |
| response rejected: other: LLM response rejected: no code block in response | 1 |
| admitted (passed L5) | 3 |
| passed L4 | 3 |
| rejected at L1 | 20 |
| rejected at L2 | 11 |

<!-- /RESULTS:LLM_COLD -->

### 3.2 Arm B — lake-primed

<!-- RESULTS:M1_PRIMED -->

**A/A noise floor** (20 runs of the L5 protocol, identical program vs itself, α=0.05):

| objective | raw FPR (within-run CI) | effect SD across runs | median within-run SE | τ (between-run SD) | calibrated FPR (leave-one-out) | binomial p |
|---|---|---|---|---|---|---|
| cost | 10% (2) | 1.89% | 1.19% | 1.33% | 5% (1) | 0.642 |
| cpu | 10% (2) | 1.92% | 1.20% | 1.35% | 5% (1) | 0.642 |
| mem | 0% (0) | 2.03% | 1.83% | 0.50% | 0% (0) | 1.000 |
| p50 | 5% (1) | 3.90% | 3.12% | 1.65% | 5% (1) | 0.642 |
| p95 | 10% (2) | 4.97% | 3.98% | 2.81% | 10% (2) | 0.264 |

Gate: cost: 1/20 calibrated false positives (raw 2/20), binomial p=0.642 vs alpha=0.05 → promotions allowed: **True**.

**Run `runs/stackzero-go-primed`** — rate 45.0 rps (knee ≈ 90 rps), 8 generations, 22 programs evaluated of 51 proposed, 6 promoted, 0 L6-verified (promotion held), 168 min wall.

**Cascade funnel** (how many candidates each stage saw / passed):

| stage | pass | fail | suspicious | error |
|---|---|---|---|---|
| L0 | 46 | 1 | 0 | 0 |
| L1 | 27 | 19 | 0 | 0 |
| L2 | 23 | 4 | 0 | 0 |
| L4 | 23 | 0 | 0 | 0 |
| L5 | 21 | 0 | 1 | 0 |
| L6 | 6 | 4 | 0 | 0 |

**Best programs found** (measured vs baseline):

| program | island | operator | cost | p50 | mem | genes |
|---|---|---|---|---|---|---|
| `528b170ba1` (promoted) | composition | lake_seed | +24.6% (CI [+21.8%, +27.7%], p=0.002) | +45.2% | +5.9% | db.idx_reviews_product = True [knob_sample] |
| `6b629626ea` (promoted) | db.index | crossover | +23.3% (CI [+19.7%, +26.7%], p=0.002) | +35.9% | +5.8% | db.jit = False [knob_perturb]<br>db.idx_reviews_product_created = True [knob_sample]<br>db.idx_reviews_product = True [knob_sample] |
| `f1d3711c1d` (promoted) | composition | splice | +23.2% (CI [+20.3%, +26.1%], p=0.002) | +35.8% | +3.4% | db.jit = False [knob_perturb]<br>isWordByte: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_reviews_product_created = True [knob_sample] |
| `4aecda2e38` (promoted) | composition | splice | +22.6% (CI [+20.0%, +25.3%], p=0.002) | +27.4% | +0.1% | isWordByte: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_reviews_product = True [knob_sample] |
| `1bbbb79ffd` (promoted) | composition | splice | +21.4% (CI [+18.7%, +23.4%], p=0.002) | +31.8% | +0.8% | isWordByte: code rewrite (llm_rewrite/optimize) optimize<br>db.idx_reviews_product_created = True [knob_sample] |
| `c1da20c731` (promoted) | db.index | knob_sample | +21.4% (CI [+17.2%, +25.1%], p=0.002) | +15.2% | -0.8% | db.idx_reviews_product_created = True [knob_sample] |
| `63bd287cc6` (elite) | db.index | knob_sample | +25.3% (CI [+22.6%, +27.6%], p=0.002) | +33.8% | +0.6% | db.idx_reviews_product_created = True [knob_sample]<br>db.idx_reviews_product = True [knob_sample] |
| `44a2d88c21` (elite) | db.index | crossover | +21.8% (CI [+18.2%, +24.8%], p=0.002) | +23.1% | +2.5% | db.jit = False [knob_perturb]<br>db.idx_reviews_product_created = True [knob_sample] |
| `7e77c2824d` (elite) | kernel.code | llm_rewrite | +5.5% (CI [+2.0%, +9.0%], p=0.008) | +10.4% | +1.2% | isAlnum: code rewrite (llm_rewrite/optimize) optimize<br>toLower: code rewrite (llm_rewrite/sql_batching) sql_batching |
| `db8f8c7732` (elite) | kernel.code | llm_rewrite | +3.8% (CI [+0.5%, +6.9%], p=0.025) | +3.4% | +1.6% | isAlnum: code rewrite (llm_rewrite/optimize) optimize |

**Post-run verification** (`colloid verify`: L6 deep assurance for every eligible top program, then a 6-cycle replicate vs baseline; promotion needs L6 pass + holdout surviving Holm at α=0.05 + replicate CI > 0 + A/A gate):

| program | island | L5 cost (in-run) | L6 | holdout cost | replicate cost | replicate p50 | replicate mem | decision |
|---|---|---|---|---|---|---|---|---|
| `44a2d88c21` | db.index | +24.3% | fail | +21.8% [+18.2, +24.8] p=0.002 | — | — | — | not promoted (L6 fail) |
| `4aecda2e38` | composition | +23.5% | pass | +22.6% [+20.0, +25.3] p=0.002 | +23.5% [+20.9, +25.9] | +6.3% [+3.1, +10.7] | -0.5% [-1.3, +1.0] | promoted |
| `6b629626ea` | db.index | +23.2% | pass | +23.3% [+19.7, +26.7] p=0.002 | +23.5% [+20.8, +26.1] | +8.0% [+4.8, +13.0] | +3.1% [+1.5, +4.8] | promoted |
| `528b170ba1` | composition | +21.5% | pass | +24.6% [+21.8, +27.7] p=0.002 | +24.4% [+22.5, +26.4] | +13.3% [+9.7, +17.7] | +1.6% [+0.2, +3.0] | promoted |
| `7e77c2824d` | kernel.code | +6.4% | fail | +5.5% [+2.0, +9.0] p=0.008 | — | — | — | not promoted (L6 fail) |

**Measured epistasis** (ε = gain(a+b) − gain(a) − gain(b); + synergy, − interference):

- db.idx_reviews_product_created = True [knob_sample] **+** db.idx_reviews_product = True [knob_sample]: ε = -0.202 [-0.289, -0.116] (interference, significant)
- db.idx_orders_customer_placed = True [knob_perturb] **+** db.idx_reviews_product = True [knob_sample]: ε = +0.051 [-0.033, +0.135] (synergy, CI spans 0)

**Shapley attribution** (exact Shapley value of each gene's cost contribution within the program, log-ratio, 95% CI; a CI spanning 0 marks a gene the pruning step drops):

| program | gene | Shapley value |
|---|---|---|
| `63bd287cc6` | db.idx_reviews_product_created = True [knob_sample] | +0.153 [+0.110, +0.196] |
| `63bd287cc6` | db.idx_reviews_product = True [knob_sample] | +0.141 [+0.098, +0.184] |
| `9dea60b609` | db.idx_orders_customer_placed = True [knob_perturb] | +0.087 [+0.045, +0.129] |
| `9dea60b609` | db.idx_reviews_product = True [knob_sample] | +0.268 [+0.226, +0.310] |

**Operator credit** (bandit, mean reward per proposal):

| operator | model / template | pulls | mean reward |
|---|---|---|---|
| splice |   | 3 | 30.0% |
| lake_seed |   | 2 | 29.9% |
| knob_sample |   | 7 | 9.8% |
| knob_perturb |   | 2 | 3.9% |
| llm_rewrite | qwen2.5-coder-3b sql_batching | 11 | 0.7% |
| crossover |   | 2 | 0.2% |
| llm_rewrite | qwen2.5-coder-3b algorithmic | 11 | 0.2% |
| llm_rewrite | qwen2.5-coder-3b optimize | 12 | 0.0% |
| llm_rewrite | qwen2.5-coder-1.5b optimize | 4 | 0.0% |
| gi_edit |   | 1 | 0.0% |

**LLM usage**: 96 calls (137,571 in / 35,573 out tokens), local models {'qwen2.5-coder-1.5b': 18, 'qwen2.5-coder-3b': 78}, $0.325 compute-priced.

**A/A noise floor** (20 runs of the L5 protocol, identical program vs itself, α=0.05):

| objective | raw FPR (within-run CI) | effect SD across runs | median within-run SE | τ (between-run SD) | calibrated FPR (leave-one-out) | binomial p |
|---|---|---|---|---|---|---|
| cost | 10% (2) | 1.89% | 1.19% | 1.33% | 5% (1) | 0.642 |
| cpu | 10% (2) | 1.92% | 1.20% | 1.35% | 5% (1) | 0.642 |
| mem | 0% (0) | 2.03% | 1.83% | 0.50% | 0% (0) | 1.000 |
| p50 | 5% (1) | 3.90% | 3.12% | 1.65% | 5% (1) | 0.642 |
| p95 | 10% (2) | 4.97% | 3.98% | 2.81% | 10% (2) | 0.264 |

Gate: cost: 1/20 calibrated false positives (raw 2/20), binomial p=0.642 vs alpha=0.05 → promotions allowed: **True**.

**Red-team island**: 0 attacks rejected at L0/L1, 0 caught by the L2 oracle, 0 classified inert in-run, **0 breach alert(s)** raised in-run.

<!-- /RESULTS:M1_PRIMED -->

<!-- RESULTS:LLM_PRIMED -->

**LLM rewrites in `stackzero-go-primed`** (local Qwen2.5-Coder, Go):

| outcome | count |
|---|---|
| response rejected: extra top-level code | 21 |
| response rejected: identical to original | 13 |
| response rejected: signature changed | 12 |
| response rejected: missing / duplicate function | 9 |
| response rejected: syntax error | 4 |
| response rejected: other: near-duplicate (novelty) | 1 |
| admitted (passed L5) | 11 |
| passed L4 | 12 |
| rejected at L0 | 1 |
| rejected at L1 | 19 |
| rejected at L2 | 4 |

<!-- /RESULTS:LLM_PRIMED -->

### 3.3 The A/B

<!-- RESULTS:M1_TRANSFER -->

| arm | best verified cost gain (L6 holdout) | hours to it | VGPH (%/h) | first verified after | verified / evaluated |
|---|---|---|---|---|---|
| cold (`stackzero-go`) | 29.98% | 1.51 | 19.9 | 1.059 h | 10 / 28 |
| primed (`stackzero-go-primed`) | 23.2% | 1.56 | 14.9 | 0.697 h | 3 / 26 |

Configuration differences besides the lake: none.

**What the primed run received from the lake (generation 1):**

- skipped: 6d9ad5384faf (stackzero): no gene with a contribution CI above zero
- skipped: a48303a6be0e (stackzero): no gene with a contribution CI above zero
- seed `9dea60b609` from record `fc1bc599367a` (2 genes, 31.33% on its source)
- seed `528b170ba1` from record `b93d7cb4d786` (1 genes, 21.97% on its source)

<!-- /RESULTS:M1_TRANSFER -->

---

## 4. The language bake-off

Same contract, same database, same requests and arrival schedule. Every implementation is one
arm of a single interleaved benchmark comparison (shuffled order, several cycles, paired CIs
against the Python reference).

### 4.1 Cost and latency at equal load

<!-- RESULTS:BAKEOFF_LOAD -->

Offered load 45.0 req/s for every arm (half the Python reference's knee), protocol `bakeoff`.

| implementation | $ / 1M req | CPU ms / req | p50 ms | p95 ms | stack PSS MB | cost vs Python (95% CI) |
|---|---|---|---|---|---|---|
| `stackzero` | 0.3413 | 26.96 | 11.2 | 113.9 | 218 | reference |
| `stackzero-go` | 0.3471 | 27.47 | 8.1 | 119.4 | 204 | -1.7% [-3.6, +0.1] |
| `stackzero-node` | 0.3520 | 27.58 | 10.7 | 137.0 | 306 | -3.1% [-5.6, -0.7] |
| `stackzero-bun` | 0.3581 | 27.95 | 10.4 | 127.1 | 386 | -4.9% [-6.0, -3.9] |

<!-- /RESULTS:BAKEOFF_LOAD -->

### 4.2 Footprint

<!-- RESULTS:BAKEOFF_FOOTPRINT -->

| implementation | runtime | runtime MB | third-party packages | deps MB | app MB | start-up ms | service PSS MB |
|---|---|---|---|---|---|---|---|
| `stackzero` | CPython 3.12.3 + uvicorn | 61.3 | 6 | 1.9 | 0.10 | 274.1 | 32.5 |
| `stackzero-go` | Go (static binary) | 0.0 | 11 | 0.0 | 15.07 | 61.4 | 18.7 |
| `stackzero-node` | node v22.22.0 | 123.4 | 14 | 0.5 | 0.03 | 187.2 | 119.3 |
| `stackzero-bun` | bun 1.3.14 | 92.8 | 14 | 0.5 | 0.03 | 126.1 | 116.3 |

<!-- /RESULTS:BAKEOFF_FOOTPRINT -->

### 4.3 Capacity

<!-- RESULTS:BAKEOFF_CAPACITY -->

| implementation | knee: the highest tested rate with p99 within 8x of light load (req/s) | next tested rate |
|---|---|---|
| `stackzero` | 90 | 130 |
| `stackzero-go` | 90 | 130 |
| `stackzero-node` | 90 | 130 |
| `stackzero-bun` | 90 | 130 |

<!-- /RESULTS:BAKEOFF_CAPACITY -->

### 4.4 What it means at scale

<!-- RESULTS:BAKEOFF_SCALE -->

Assumptions: {"utilisation": 0.6, "usd_per_vcpu_hour": 0.0446, "usd_per_gb_hour": 0.0056, "price_snapshot": "c7i-on-demand-2026", "hours_per_month": 730.0, "unit_vcpus": 3}

| implementation | CPU ms/req | 100 req/s | 1k req/s | 10k req/s |
|---|---|---|---|---|
| `stackzero` | 26.96 | $148/mo (4.5 vCPU) | $1,476/mo (44.9 vCPU) | $14,761/mo (449.4 vCPU) |
| `stackzero-go` | 27.47 | $151/mo (4.6 vCPU) | $1,503/mo (45.8 vCPU) | $15,029/mo (457.8 vCPU) |
| `stackzero-node` | 27.58 | $152/mo (4.6 vCPU) | $1,516/mo (46.0 vCPU) | $15,154/mo (459.7 vCPU) |
| `stackzero-bun` | 27.95 | $155/mo (4.7 vCPU) | $1,541/mo (46.6 vCPU) | $15,406/mo (465.8 vCPU) |

<!-- /RESULTS:BAKEOFF_SCALE -->

---

## 5. Grammar seeds: the first learned rules (CRL v0)

<!-- RESULTS:RULES -->

```
rule equality-filter-index v1 {
  doc "Index the column a hot read filters by equality"
  when query filters $table.$column = ?
  propose index $table ($column)
  unless covered
  evidence lake fc1bc599367a2d44d8d3bfa62fad3c9e91a28c58210e7ae4a088a8aeedb770b9 gain 28.39% ci [23.43%, 33.01%] on stackzero
  evidence lake d4aebbf6e936a0ef5e78306238f6f4ae07da94532626f9d72368bf38f5bd4ed4 gain 14.61% ci [11.78%, 17.34%] on stackzero-go
  evidence lake d4aebbf6e936a0ef5e78306238f6f4ae07da94532626f9d72368bf38f5bd4ed4 gain 7.52% ci [4.38%, 10.56%] on stackzero-go
}

rule equality-filter-sorted-index v1 {
  doc "Index the equality filter together with the sort order the read asks for, so the index returns rows already ordered"
  when query filters $table.$column = ? and orders by $table.$sort
  propose index $table ($column, $sort)
  unless covered
  evidence lake d4aebbf6e936a0ef5e78306238f6f4ae07da94532626f9d72368bf38f5bd4ed4 gain 11.89% ci [9.1%, 14.59%] on stackzero-go
  evidence lake fc1bc599367a2d44d8d3bfa62fad3c9e91a28c58210e7ae4a088a8aeedb770b9 gain 7.48% ci [1.11%, 13.44%] on stackzero
}
```

**Applied to each implementation** (proposal → the target's locus):

| target | rule | proposed index | locus | queries |
|---|---|---|---|---|
| `stackzero` | equality-filter-index | `order_items (product_id)` | db.idx_items_product | 1 |
| `stackzero` | equality-filter-index | `orders (customer_id)` | db.idx_orders_customer | 2 |
| `stackzero` | equality-filter-index | `products (category_id)` | db.idx_products_category | 2 |
| `stackzero` | equality-filter-index | `reviews (product_id)` | db.idx_reviews_product | 2 |
| `stackzero` | equality-filter-sorted-index | `orders (customer_id, placed_at desc, id desc)` | db.idx_orders_customer_placed | 2 |
| `stackzero` | equality-filter-sorted-index | `products (category_id, id)` | db.idx_products_category | 1 |
| `stackzero` | equality-filter-sorted-index | `reviews (product_id, created_at desc, id desc)` | db.idx_reviews_product_created | 1 |
| `stackzero-go` | equality-filter-index | `order_items (product_id)` | db.idx_items_product | 1 |
| `stackzero-go` | equality-filter-index | `orders (customer_id)` | db.idx_orders_customer | 2 |
| `stackzero-go` | equality-filter-index | `products (category_id)` | db.idx_products_category | 2 |
| `stackzero-go` | equality-filter-index | `reviews (product_id)` | db.idx_reviews_product | 2 |
| `stackzero-go` | equality-filter-sorted-index | `orders (customer_id, placed_at desc, id desc)` | db.idx_orders_customer_placed | 2 |
| `stackzero-go` | equality-filter-sorted-index | `products (category_id, id)` | db.idx_products_category | 1 |
| `stackzero-go` | equality-filter-sorted-index | `reviews (product_id, created_at desc, id desc)` | db.idx_reviews_product_created | 1 |

<!-- /RESULTS:RULES -->

---

## 6. The evidence ladder

<!-- RESULTS:LADDER -->

| rung | gate | status | evidence | needs |
|---|---|---|---|---|
| J | the judge holds on every implementation | **passed** | stackzero: 17/17 canaries rejected<br>stackzero-go: 14/14 canaries rejected | — |
| M1a | language neutrality | **passed** | go: verified programs on stackzero-go<br>python: verified programs on stackzero | — |
| M1b | knowledge transfers between implementations | **open** | cold stackzero-go: best verified 29.98% after 1.51 h -> 19.85 %/h; first verified after 1.059 h; 10 verified of 28 evaluated<br>primed stackzero-go-primed: best verified 23.2% after 1.56 h -> 14.87 %/h; first verified after 0.697 h; 3 verified of 26 evaluated<br>VGPH ratio 0.75 (needs >= 1.25); endpoint difference -6.78 pp (needs >= -3.0) | a primed run that beats the cold run by the pre-registered margin |
| M2 | learned rules generalise to a held-out system | **open** | rule equality-filter-sorted-index v1: evidence on stackzero, stackzero-go; held-out targets available: none<br>rule equality-filter-index v1: evidence on stackzero, stackzero-go; held-out targets available: none | a target whose database schema differs from the rules' evidence, and a verified rule_apply gene on it |
| M3 | rules work across languages | **blocked** | — | M2 |

<!-- /RESULTS:LADDER -->

---

## 7. Reproduce

```bash
colloid canaries --target stackzero-go --out docs/results/canaries_go.json
scripts/run-m1.sh                      # arm A then arm B (about 5 h on this host)
colloid verify runs/stackzero-go && colloid verify runs/stackzero-go-primed
colloid compare runs/stackzero-go runs/stackzero-go-primed --out docs/results/m1_transfer.json
colloid lake ingest runs/stackzero-go && colloid lake ingest runs/stackzero-go-primed
colloid rules mine --commit --out docs/results/rules.crl
colloid bakeoff --out docs/results/bakeoff.json
colloid ladder --cold runs/stackzero-go --primed runs/stackzero-go-primed --out docs/results/ladder.json
python stress/render_polyglot.py --write docs/POLYGLOT_RESULTS.md
```
