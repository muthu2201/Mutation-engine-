# Colloid architecture

Colloid is an AI-driven, cross-layer mutation engine. It optimises a real software stack by
evolving a population of **mutations**, judging each with a paranoid **evaluation cascade**,
and spending its effort where measured **causal leverage** says the leverage is. This
document explains the whole system — what each piece is, why it exists, and how the pieces
fit — in plain English. Every module also carries a long docstring with the same intent at
the code level; this file is the map.

The design follows one hard lesson from 2023–2026 (AlphaEvolve, FunSearch, the Darwin Gödel
Machine, and the public failures of Sakana's CUDA engineer and inflated KernelBench numbers):
**LLM-guided evolution produces real gains only when a fast, trustworthy, automatic evaluator
exists, and fake gains or none when the evaluator is weak.** So the evaluator is the centre of
the system, not an add-on, and the language model is a swappable part.

---

## 1. The shape of the system (hexagonal)

```
                      ┌───────────────────────────────────────────────┐
   LLM providers ─────▶                                               │
   code adapters ─────▶          colloid.core  (pure domain)          ◀──── program store
   build / sandbox ───▶   Atlas · Genome · Operators · Selection       │     (SQLite/Postgres)
   cost / telemetry ──▶   Islands/MAP-Elites · Bandit · Attribution    ◀──── telemetry (JSONL)
   target adapter ────▶   Splicing · Budget · Surrogate · Novelty      │
                      └───────────────────────────────────────────────┘
                                         ▲
                                         │ drives
                      ┌──────────────────┴──────────────────┐
                      │   colloid.services  (engine loop,    │
                      │   CLI, dashboard, report)            │
                      └──────────────────┬──────────────────┘
                                         │ asks "is this correct and faster?"
                      ┌──────────────────▼──────────────────┐
                      │  colloid_evaluator  (the judge)      │
                      │  cascade L0–L6 · oracles · policy ·  │
                      │  benchmark protocol · canaries       │
                      └─────────────────────────────────────┘
```

* **`colloid/core`** is the pure domain. It performs no I/O and imports no SDK, compiler, or
  database driver. Everything in it is a function from data to data, which is what makes the
  search, attribution, and selection logic unit-testable and reproducible from a seed. This
  purity is **enforced by import-linter** (`pyproject.toml`), not just by convention.
* **`colloid/ports`** are `typing.Protocol` contracts (with semantic versions) that the core
  talks through. The core knows these protocols and the value objects, nothing more.
* **`colloid/adapters`** are the concrete implementations: LLM providers, the Linux sandbox,
  the stores, the cost model, telemetry, the Go load generator, and the StackZero target.
* **`colloid/services`** wires it together: the generation loop (`engine.py`), the CLI, the
  live dashboard, and the run-report builder.
* **`colloid_evaluator`** is a **separate package**, deliberately independent of the search
  machinery (also import-linter-enforced). It owns everything a candidate could exploit —
  request generators with hidden seeds, reference outputs, float tolerances, the timing
  harness, the policy rules — because the code that *proposes* mutations must not be able to
  influence how they are *judged*.

Why hexagonal? Because the core never imports an LLM SDK, a compiler, or Docker, a new
framework (a different database, a new runtime, a new model) is a new adapter plus a
conformance run — not a rewrite. The engine can even evaluate an adapter choice as a target:
"glibc vs jemalloc" is just a knob gene at the `alloc` layer.

---

## 2. The Stack Atlas — how Colloid "dissects" a stack

The Atlas (`core/atlas.py`) is a tagged, multi-resolution property graph of the target. It is
how Colloid answers three questions the search needs.

**Where can we mutate?** Nodes ("Units") exist at every resolution — layer → component →
module → function → region, with **knobs** hanging off the components they configure. Each
unit has a stable id that is a hash of its *symbolic path* (not its content), so tags survive
when the unit's content is mutated. A unit that is mutable exposes one or more **loci**: a
locus is `(unit, surface)`, where the surface is `code_region`, `knob`, `index_set`,
`compiler_flags`, and so on. Tests, oracles, and frozen knobs (e.g. `fsync`) are **not** loci,
so "edit the tests" or "turn off durability" are impossible by construction.

**Where is the leverage?** Dynamic analysis decorates units with four signals, in increasing
order of trustworthiness: `hotness` (sampling-profiler share), `latency_share` (from request
timing), `dollar_share` (cost model), and `causal_leverage` — "if this unit were x% faster,
how much faster would the whole workload get?" The **Locus Opportunity Score**
(`atlas.opportunity`) combines causal leverage × dollar share × mutability, and prefers
leverage over hotness because hotness lies: Coz's causal profiler found functions worth 25.6%
end-to-end that `perf` attributed just 0.15% of runtime to.

**What interacts with what?** Edges (`calls`, `queries`, `configures`, `executes_on`) and
**paths** (ordered walks, e.g. "HTTP endpoint → handler → SQL query → table") tell the
attribution engine which gene pairs are *likely* to be epistatic — those that share a request
path or a hardware resource (`atlas.epistasis_prior`) — so that quadratic interaction testing
becomes a sparse, graph-guided problem.

For StackZero the Atlas is built (`adapters/target/stackzero/atlas_builder.py`) from: Python
`ast` (functions, calls, SQL literals), clang's JSON AST (C functions and their call graph),
the ctypes binding (the cross-language `native.score → shop_score_batch` edge that makes a
single path span Python and C), the route table (endpoints), and the knob catalogue. The
result on StackZero: 172 units, 7 request paths crossing up to 3 layers, 106 loci.

---

## 3. Genomes — sparse, locus-addressed sets of genes

A **Gene** (`core/models.py`, `core/genome.py`) is `(locus, payload)`: one change at one site.
A payload is either a value (a knob setting) or a source replacement (a rewritten function). A
**Genome** is a *sparse set* of genes applied on top of the fixed baseline; the baseline is the
genome with zero genes. This is the Genetic-Improvement "patch representation" generalised
across every layer, and it is the single decision that makes "all layers at once" tractable:

* Genomes from different layers **compose by set union**, as long as their loci don't overlap
  (no shared unit, no ancestor/descendant conflict in the Atlas). That is exactly what lets the
  Composition Island splice a kernel-knob winner, an allocator winner, and a service-code
  winner with no merge logic.
* Everything is **content-addressed**: a gene's id is a hash of `(locus, payload)` and a
  program's id is `hash(baseline, sorted gene ids)`. Two operators that independently propose
  the same change produce the same id, so the evaluator never pays for it twice, and any
  program is reproducible from its id plus the stored payloads.

---

## 4. Niches — islands, MAP-Elites, and escaping local optima

Each **Region** of the Atlas (`os`, `alloc`, `compiler`, `runtime`, `db.config`, `db.index`,
`svc.code`, `native.code`) gets its own **Island** (`core/archive.py`): a parent pool plus a
**MAP-Elites grid**. MAP-Elites keeps the best program *per behaviour cell* (binned by genome
size and by CPU-vs-memory trade-off) rather than one global best, so the archive keeps
*different kinds* of solutions instead of collapsing onto a single lineage.

Escaping local optima (the "tunneling" the blueprint asks for) is built in:

* **Simulated-annealing acceptance** into the parent pool (not the elite grid): a worse child
  can still become a parent with probability `exp(Δ/T)`; the temperature cools each generation
  and **reheats on stagnation**.
* **Neutral drift**: a child that ties its parent and is no larger is always accepted — GI
  landscapes have large neutral plateaus that connect optima.
* **Tabu basins**: when an island converges, its elite's code signature (a MinHash over token
  shingles, `core/novelty.py`) becomes a basin centre, and later candidates near it are
  down-weighted, pushing search across the barrier.
* **Stepping-stone preservation**: the parent pool is kept by NSGA-II over several objectives,
  so low-scoring but *different* lineages survive (the DGM archive beat a greedy baseline 50%
  vs 40% because of exactly this).
* **Ring migration** moves elites between islands every few generations.

A separate **Composition Island** holds cross-region genomes built by splicing; its behaviour
descriptor is *which regions contributed genes*, so cross-layer combinations stay diverse. A
**red-team island** has the inverted fitness "fool the evaluator"; its successes would be
evaluator breaches and become new canaries.

---

## 5. Operators — how mutations are proposed

Operators (`core/operators/`) are pure functions from `(parent, context, rng)` to proposals;
they never evaluate anything. The factory (`services/factory.py`) is the glue that performs the
one unavoidable side effect — the LLM call — outside the core.

* **Knob operators**: `knob_sample` (fresh random value, the "basin hop"), `knob_perturb` (a
  Gaussian step in normalised space, the "local polish"), `knob_reset` (delete a gene — an
  explicit anti-bloat move). Inert genes (a jemalloc knob while glibc is selected) are never
  produced.
* **Peephole rewrites** (`py_rewrite.py`): a catalogue of AST-shape rewrites — list→set
  membership, `sorted(...)[0]`→`min`, append-loop→comprehension, N+1 list dedupe→shadow set,
  accumulation loop→`sum`, if/else→`dict.get`. These are deterministic, free, and the Python
  analogue of a compiler's peephole pass. "Semantics-preserving" is the *intent*, not a proof —
  the oracle is still the judge.
* **GI edits** (`gi_edit.py`): the classic delete/copy/swap statement edits. Most break the
  program and die in milliseconds at L0; occasionally one removes redundant work.
* **LLM rewrite** (`llm_rewrite.py`): split into a pure request builder and a pure response
  parser. The request is a **localised** prompt — the unit's source, its Atlas tags and causal
  leverage, the request paths it sits on, the target's schema/helper context, the top archive
  neighbours *with their measured gains*, and summaries of earlier failed attempts so the model
  doesn't repeat them. The parser enforces that the response is a drop-in replacement (same
  name, signature, decorators, async-ness, nothing else at top level); anything else is rejected
  before any compute is spent. The template and model are part of the bandit arm.
* **Crossover** and **splice** recombine two parents' gene sets.

---

## 6. Which operator, where? — bandit and budget

**Budget** (`core/budget.py`): each generation's proposal slots are split across islands in
proportion to each island's summed Locus Opportunity Score × recent improvement momentum, with
an exploration floor so no layer starves. This is what replaces "pick a layer by hand": search
effort follows measured leverage.

**Bandit** (`core/bandit.py`): within an island, a **contextual Thompson sampling** bandit picks
the arm `(operator, model, template)` given the locus's tags. The reward is the measured
child-vs-parent gain, clipped; a new arm (a freshly added model) starts optimistic and earns
budget by credit — which is exactly how a model swap happens at runtime. The bandit is
**cost-aware**: it maximises sampled reward *per unit wall-clock cost*, so a cheap knob operator
with small gains can out-compete an expensive LLM call with marginally larger ones.

---

## 7. The evaluation cascade — the judge (`colloid_evaluator`)

Every candidate passes through stages, cheapest first; each stage passes only survivors on.

| Stage | What | Cost |
|---|---|---|
| **L0** | static policy & validity (`policy.py`): locus validity, knob ranges/requirements, locus confinement, diff cap, and an AST/semantic scan that blocks forbidden imports/calls, introspection, cross-call state, caching, background tasks, timer patching, evaluator-path strings | ms |
| **L1** | hermetic, content-addressed, sandboxed build (apply genes → compile libshopnative with the genome's flags → byte-compile Python) | <1 s cached |
| **L2** | unit tests (the baseline copy) + the **differential oracle** (baseline and candidate run side-by-side on throwaway database clones, lock-step; every response must match in status and JSON value within an evaluator-owned float tolerance) + **native differential fuzzing** when C code or flags change, with a **ptrace-free leak check** (below) | ~10 s |
| **L3** | **surrogate** rank (`core/surrogate.py`): a learned model predicts each candidate's gain; the top fraction per island proceed. The surrogate is **prequentially audited** (every prediction is scored against the later truth) and is *distrusted* — the cascade stops filtering with it — when its rank correlation falls below a threshold | ms |
| **L4** | micro-benchmark on the touched paths only, candidate vs parent, paired | ~25 s |
| **L5** | macro-benchmark, full workload, candidate vs baseline *and* parent | ~60 s |
| **L6** | deep assurance for elites: the deep oracle with ASan/UBSan fuzzing, the **hidden holdout workload** (gains must persist on traffic search never saw), and a **soak test** (memory-leak detection under sustained load) | ~2 min |

**Leak checking without LeakSanitizer.** LSan's end-of-process scan stops the world through
a ptrace-attaching tracer thread. The sandbox's seccomp policy rightly forbids ptrace, so
inside the sandbox LSan can only abort with "LeakSanitizer has encountered a fatal error". The
first full run lost every native-code L6 to exactly that. The fuzz driver (`native_fuzz.c`)
checks leaks itself instead: every *candidate* call is bracketed by the heap's in-use byte
count (ASan's allocator statistics, or glibc `mallinfo2()` with the tcache disabled, because
`mallinfo2` does not see tcache-parked frees). After a warm-up, net growth means a leak (exit 4).
This is exact: 0 bytes of growth for leak-free code, and about 2 KB per call caught for the
`native_leak` canary, which drops a `free()`, keeps its output bit-identical and gets
cheaper. A sanitizer *runtime* that crashes is classified **ERROR** (an evaluator fault),
never **FAIL**.

**Post-run verification (`colloid verify`).** During a run L6 only fires on a new global best,
measured by a two-cycle L5. After the run, the top programs of the archive get L6 and a
many-cycle replicate against baseline; the between-run noise floor shrinks as 1/√cycles.
Only programs whose L6 never ran, or failed through an evaluator fault, are eligible:
re-testing a candidate that already failed L6 on its merits until it passes would be
p-hacking. Promotion requires an L6 pass, a holdout p-value that survives a **Holm–Bonferroni**
correction across the batch, a replicate cost CI that excludes zero, and the A/A gate.

Verdicts: **PASS**; **FAIL** (the candidate's fault, never retried); **ERROR** (an
infrastructure problem — e.g. the *baseline* arm failed — which may be retried); and
**SUSPICIOUS** — a gain above a threshold (>2× on a mature path) triggers mandatory L6 and an
alert, because such a gain is far more often a measurement or evaluator bug than a real win.

### Anti-reward-hacking (treat the genome as an adversary)

This is section D2 of the blueprint, made concrete:

1. **Physical separation**: the evaluator package never imports the search machinery; the
   candidate runs in a sandbox with no network, as an unprivileged user, over a Unix socket;
   reference outputs are computed at evaluation time by the baseline running *beside* the
   candidate, never left on disk where a candidate could read them.
2. **Fresh, hidden, randomised inputs** per evaluation, drawn from generators seeded by the OS
   CSPRNG, so a candidate cannot memorise answers to a fixed corpus.
3. **Whole-process-tree, window-aligned timing**: CPU is measured from the cgroup accounting of
   the service *and* the database process trees, so work cannot hide in a child process or a
   background thread; measurement chunks are *drained* (the load generator returns only when
   every request has completed) so no in-flight work leaks across a timing boundary.
4. **The AST/semantic policy scanner** at L0 (above).
5. **A canary suite** of 16 known reward hacks (`canaries/`) that the evaluator must reject 100%
   before any run starts — stale caches, truncation, lazy skipping, float-tolerance abuse, fake
   writes, background writes, timer patching, load-sensitive cheating, out-of-range knobs,
   durability trading, C static caches, C early-exit bugs, C memory leaks, test editing.
6. **Holdout persistence** required for promotion (the benchmark-overfitting check).
7. **Suspicion triggers** → automatic deep review (above).
8. **A red-team island** whose fitness is "fool the evaluator"; a survivor is a breach, raises a
   critical alert, and becomes a new canary. An attack that passes the oracle counts as a
   breach only if it could have changed an output. The first full run raised two alerts, and
   both were inert: a float-rounding wrapper on the ASGI startup hook, which returns `None`,
   and a list-truncating wrapper on a config parser, which returns a `bool`. Attacks now
   target only value-returning functions on an Atlas request path. When one passes L2, the
   engine sends the **maximal** version of the same hack on the same unit through the oracle
   (floats shifted instead of rounded, lists cut to one element, a key-less cache,
   replay-always). If that is caught, the channel reaches responses and the subtle attack
   slipping through is a genuine threshold breach. If it also passes, the attack was inert
   (`redteam.inert`). `colloid redteam-recheck RUN` re-adjudicates a finished run's alerts the
   same way.

### Benchmark methodology (`protocol.py`)

Noise is the enemy (Mytkowicz et al.: link order alone can swing a result 8%). The protocol:
the load generator is pinned to CPU 0, the service and database run on CPUs 1–3; warm-up runs
until **steady state is detected** (a fixed probe chunk is replayed until CPU/latency agree, not
a fixed time); arms are **interleaved in randomised order** (ABAB) so drift cancels; each arm
sees **setup randomisation** (random link order, 0–4096 bytes of environment padding, a random
hash seed) so a gain must survive layout noise; effects are **paired** — chunk *k* of every arm
replays the same requests, so CPU/cost compare chunk-by-chunk (ratio of totals, pair-bootstrap
CI, exact sign-flip permutation p-value) and latency quantiles use a paired bootstrap over
request indices, which removes the dominant noise source (the random mix of cheap and expensive
requests).

**The A/A noise floor and the promotion gate.** Pairing makes a comparison's CI honest about
noise *within* one run. It cannot see noise that every chunk of a run shares, such as a noisy
neighbour for those two minutes, frequency drift, or that run's random link order and padding.
On the shared development host that component was larger than the within-run noise: the first
A/A test had a 2.5% run-to-run SD against a ~1.5% within-run SE, and a 30% false-positive rate.
So before any search, the engine runs an **A/A test** (the identical program against itself,
N times, under the L5 protocol) and uses it in three ways (`core/stats.py::calibrate_aa`,
`cascade.py::aa_test`):

- It **estimates the between-run variance** τ² = Var(A/A effects) − mean(within-run SE²), a
  method-of-moments random-effects component, and stores it per benchmark cycle.
- It **widens every later CI** to √(SE² + τ²/cycles) (`with_noise_floor`). The p-value can
  only grow, so a candidate must beat the run-to-run noise, not just the chunk-to-chunk noise.
- It **checks the calibration out of sample** (leave-one-out over the A/A runs) and gates
  promotion with an exact binomial test of "calibrated FPR ≤ α". A naive "observed FPR ≤ α" on
  10 runs rejects a perfectly calibrated harness 40% of the time.

The gate is enforced in the engine. A new best that passes L6 while the gate is closed is
marked `verified` (and emits `promotion.held`), never `promoted`. A run with no A/A test
never promotes.

---

## 8. Attribution — who earned the gain?

Three tiers (`core/attribution.py`):

1. **Prior leverage** — the Locus Opportunity Score, before mutating (§2).
2. **Lineage credit** — every child is `parent + Δgenes`, measured paired against its parent, so
   `Δobjective` with a CI is free; it credits the locus and the `(operator, model, template)`
   arm, feeding the bandit.
3. **Shapley values and interactions** — for elite genomes (`services/shapley_runner.py`): treat
   the genes as players and `v(S)` = measured gain of the sub-genome `S`; a gene's Shapley value
   is its average marginal contribution over all orderings (exact when `2^k` fits the budget,
   else Monte-Carlo over permutations). Measurement noise is propagated into each value's CI.
   Genes whose Shapley CI includes ≤ 0 are **bloat and are pruned** (the pruned genome is then
   re-measured). With effects as log-ratios, the pairwise epistasis `ε_ij = e_ij − e_i − e_j`
   falls out of the same measurements; positive ε is synergy, negative is interference.

---

## 9. Splicing — cross-layer recombination (`services/splice_runner.py`)

The Composition Island assembles cross-layer genomes: pool the best gene per locus from each
region's elites; **screen** combinations with a Resolution-IV fractional-factorial (fold-over
Plackett–Burman) design — a few dozen runs instead of `2^k`; fit an **additive + sparse-pairwise
ridge surrogate**, with each interaction shrunk by the Atlas epistasis prior so pairs that share
a path or resource may take larger coefficients; **predict** the unions expected to beat the
additive null and evaluate them for real. A strongly interfering pair is handed to an LLM
"integration" arm to reconcile. This is the project's research bet: no published system has
shown epistasis-driven cross-layer co-design at full-stack scale.

---

## 10. Multi-objective fitness & selection

Fitness (`core/objectives.py`) is a vector of **log-ratios versus the baseline** — cost (USD per
1M requests, the headline), CPU, p50 and p95 latency, and peak memory — each with a confidence
interval. Hard gates (build, oracle, policy, licence) are never traded off. Selection
(`core/selection.py`) uses **confidence-aware dominance**: A dominates B only when every
objective's CI clears, so noise is never promoted. Islands use NSGA-II (≤ 3 objectives); the
Composition Island uses NSGA-III with Das–Dennis reference directions for the many-objective
case. The scalar used for the global best is "$ per unit work at a fixed SLO": the cost
log-ratio minus a steep penalty when the p95 CI shows an SLO regression, minus a tiny parsimony
term so smaller genomes win ties.

---

## 11. StackZero — the reference target

A deliberately ordinary first-version stack, so every layer has realistic headroom:

* **OS**: THP, scheduler, socket and CPU-placement knobs.
* **Allocator**: glibc / jemalloc / tcmalloc / mimalloc via `LD_PRELOAD`, with per-impl tunables.
* **Compiler**: the flags libshopnative is built with (`-O`, `-march`, LTO, unroll, …).
* **Native**: a C library (`libshopnative`) doing fuzzy BM25 product ranking with a textbook
  full-matrix Levenshtein allocated per call — real optimisation headroom.
* **Runtime**: uvicorn/event-loop/GC/pool knobs for the Python service.
* **DB**: a PostgreSQL "shop" (20k customers, 20k products, 120k orders, 360k lines, 60k
  reviews) with **no secondary indexes** — a realistic N+1-and-missing-index starting point —
  plus ~20 GUCs and a catalogue of candidate indexes.
* **Service**: an async API (`search`, product detail, customer summary, recommendations,
  category top-N, daily report, create order) written the unoptimised way: per-row queries in
  loops, aggregation in Python, linear scans.

The database uses **throwaway clones** of a template per evaluation (never shared mutable
state), and reads are **write-independent** (writes touch only reserved customers/categories),
which is what lets the evaluator spot-check responses produced *under load* against references
exactly. 58 knobs span every layer; 2 frozen knobs (`fsync`, `synchronous_commit`) model
"never trade durability for speed".

---

## 12. Sandbox & provenance

The Linux sandbox (`adapters/sandbox/`) runs each untrusted candidate under a small C launcher,
`sbx_exec.c`. The launcher:
- joins per-candidate cgroups: CPU accounting, memory cap, pid limit and freeze/kill, on
  cgroup v1 or unified v2;
- enters an empty network namespace;
- puts the candidate in a **filesystem jail**: a private mount namespace in which only the
  declared writable paths are writable, every other mount is read-only (nosuid/nodev/noexec
  kept, failing closed), and `/tmp`, `/var/tmp` and `/dev/shm` are fresh private tmpfs, so
  nothing outlives a candidate or leaks to the next;
- applies rlimits and drops to an unprivileged user;
- sets `PR_SET_NO_NEW_PRIVS` and installs a seccomp-BPF filter denying ptrace, mount,
  namespace creation, bpf, module loading and IP/packet sockets.

The whole process tree is frozen (or `cgroup.kill`ed) and killed atomically, which defeats
fork races. Risk classes A–C map to isolation strength. Class D (kernel) is refused by this
adapter, because it needs full VMs (ADR 0004). Trusted infrastructure (Postgres) runs under
its own account, outside the jail.

Every durable object is content-addressed; every evaluation records an **environment
fingerprint** (kernel, CPU, microcode, governor, compilers, package hashes, binary hashes,
commit); every LLM call logs model, params, prompt hash, and response hash. A result is
reproducible from `(baseline commit, gene payloads, adapter versions, fingerprint)`.

---

## 13. Platforms — a portable searcher, a Linux judge (ADR 0004)

The search side (core, operators, archives, bandit, attribution, lake, reports, LLM calls)
runs natively on Linux, macOS and Windows. CI runs lint, strict types, the architecture
contracts and the portable test suite on all three. The judge needs two kernel features to be
trustworthy, a security boundary around untrusted code and exact whole-tree CPU accounting,
and it adapts to what the host offers (`adapters/platform.py` probes it):

| host | sandbox | CPU accounting | memory | CPU pinning |
|---|---|---|---|---|
| Linux, root, cgroup v1 or v2 | `LinuxSandbox`: seccomp, net + mount namespaces, uid drop, **filesystem jail** (read-only except declared paths, private `/tmp`), cgroups | cgroup (`cpuacct.usage` / `cpu.stat`) | smaps PSS | `sched_setaffinity` |
| macOS / Windows (full fidelity) | the Colloid **Linux container** (`Dockerfile`), with `LinuxSandbox` inside it | cgroup | PSS | yes |
| anywhere (fallback) | `ProcessSandbox`, isolation C: watchdog + rlimits + tree kill, **refuses untrusted code** | `CpuCounterFile` (psutil-sampled, within 5% of cgroup on Linux) | psutil PSS/USS | psutil or recorded unpinned |

The fingerprint records the capability set. Runs on different backends or memory metrics are
never compared, and the A/A noise floor is measured where the judge runs.
`scripts/provision-linux.sh` builds a bench host (and the container) from scratch.

## 14. The mutation data lake (ADR 0005)

Verified knowledge outlives runs. Every promoted or L6-verified program becomes records in an
append-only, **hash-chained ledger** (`core/lake.py`, pure).

**Records.** A *gene* record holds one change: its language-neutral locus, its payload, the
hash of the source it was written against, an explanation and provenance. A *program* record
holds a verified combination of genes and its evidence: effects with CIs, holdout,
Shapley / leave-one-out attribution, ablation, noise floor, platform, run and engine commit.

**Identity and order.**
- A record's id is the sha256 of its canonical JSON, so it is identical on every machine and
  re-ingesting is a no-op.
- Ledger entry *n* chains to *n−1* by hash. Which mutation is older or newer is verifiable,
  and any edit, reordering or deletion breaks `colloid lake verify`.
- `derived_from` links newer knowledge to what it extends or supersedes.

**Where it lives.** The lake is stored in a directory or on the parentless data-only branch
`colloid/datalake`. That branch is written with git plumbing and compare-and-swap ref
updates, and never touches the checkout.

**How it is used:**
- **seeds**: applicable programs re-enter generation 1 and pass the full cascade again;
- **operator priors**: see §15;
- **materialised stacks**: `colloid stack materialize`, then `publish` to
  `stack/stackzero-verified`, a deployable artifact whose manifest ties every change to its
  record and evidence. `--carrying-only` drops hitchhiker genes.

## 15. Self-improvement — discoveries change the searcher, never the judge (ADR 0006)

Discoveries feed back as *data*:
- seeds for the next run;
- **bandit priors**: every attribution in the lake scores the arm that produced the gene,
  as a win worth its contribution, or zero for a hitchhiker. These enter
  `ThompsonBandit.seed` as weighted pseudo-observations, so measured credit in the new run
  still dominates.

Lessons about the *method* become reviewed code. The hitchhikers this run found led to
`verify --ablate` and carrying-only materialisation.

The judge is out of reach by construction. `policy.JUDGE_PATHS` lists the evaluator,
`tests/`, `stress/`, `core/stats.py`, `core/lake.py`, the sandbox, the load generator and the
platform layer. L0 rejects any gene there, and the engine refuses to start on an Atlas that
exposes one. An optimiser that can edit its grader will.

## 16. Branches

| branch | holds | written by |
|---|---|---|
| the engine branch | code, tests, docs | people + reviewed commits |
| `colloid/datalake` | the mutation lake (records, ledger, README) and nothing else | `colloid lake ingest` (plumbing, CAS) |
| `stack/stackzero-verified` | a deployable verified stack + MANIFEST | `colloid stack publish` |

A branch is created when there is a real artifact for it. ADR 0007 records the milestones
(a second-language target, mined cross-target rules) that would justify more.

## 17. Repository map

```
colloid/
  core/            pure domain (no I/O): atlas, genome, knobs, operators, selection,
                   archive, bandit, attribution, splicing, budget, surrogate, novelty, stats,
                   lake (records + hash chain)
  ports/           typing.Protocol contracts + versions (incl. LakeStore)
  adapters/        llm/ code/ sandbox/ (linux + portable) store/ cost/ telemetry/ bench/
                   target/stackzero/ lake/ (directory + git branch) gitref.py platform.py
  services/        engine (the generation loop), factory, shapley_runner, splice_runner,
                   config, cli, dashboard, report, verify (L6 + replicate + ablation),
                   lake (ingest, seeds, priors), stack (materialise + publish)
colloid_evaluator/ the judge: policy (L0 + JUDGE_PATHS), oracles (L2/L6), protocol (L4/L5),
                   profiler (causal leverage), cascade (L0–L6 + A/A noise floor), canaries,
                   workloads, fingerprint, native_fuzz.c (ptrace-free leak check)
targets/stackzero/ service/ (the shop API) · native/ (libshopnative C) · db/ (schema + seed)
tests/             core/ adapters/ evaluator/ conformance/ portable/
stress/            sandbox and evaluator stress harnesses, results renderer
scripts/           provision-linux.sh (bench host / container)
docker/            container docs (+ extra CA certs for proxied builds); Dockerfile at the root
.github/workflows/ CI: portable matrix (ubuntu/macos/windows) + cgroup v2 root sandbox job
experiments/       run configs (YAML, data only)
docs/              this file, INITIAL_RESULTS.md, adr/ (0001–0007)
```

## 18. Running it

```
pip install -e ".[dev]"            # plus the local-LLM extra to use Qwen arms
colloid atlas                      # print the Stack Atlas summary
colloid canaries                   # the reward-hacking gate (non-zero exit if any hack survives)
colloid aa --runs 20               # the benchmark noise floor (A/A false-positive rate)
colloid baseline                   # the baseline's SLO and $ / 1M requests
colloid profile                    # causal-leverage curves per unit
colloid run experiments/stackzero.yaml        # add `lake: git:colloid/datalake` to warm-start
colloid verify runs/stackzero      # post-run L6 + many-cycle replicate of the top programs
colloid verify runs/stackzero --ablate <program>   # what the gain is made of
colloid redteam-recheck runs/stackzero  # re-adjudicate red-team breach alerts (live vs inert)
colloid lake ingest runs/stackzero # add verified mutations to the data lake; lake verify | list | push
colloid stack materialize <record> --carrying-only --out DIR && colloid stack publish DIR --push
colloid report runs/stackzero      # summarise a run
colloid dashboard runs/stackzero   # live FastAPI dashboard
```

See `INITIAL_RESULTS.md` for measured results from the first full run.
