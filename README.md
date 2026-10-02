# Colloid

**An AI-driven, cross-layer mutation engine for software stacks.**

Colloid optimises a real software stack — from kernel knobs through the memory allocator, the
compiler, a native C library, the language runtime, the database, and the service code — by
evolving a population of *mutations*, judging every one with a paranoid, cascaded evaluator,
and spending its effort where measured *causal leverage* says the leverage is.

It is a faithful, production implementation of the **Colloid blueprint**. The central design
commitment is the blueprint's hardest lesson from 2023–2026 AI-for-systems work: **evaluation
sits at the centre and the language model is a swappable part.** Every public failure in this
field (Sakana's CUDA engineer, the Darwin Gödel Machine, inflated KernelBench numbers) was an
evaluator failure, so the evaluator here is an adversarial security boundary, kept in a
separate package that the search machinery cannot influence.

## What's here

- **A pure, hexagonal domain core** (`colloid/core`, I/O-free and import-linter-enforced):
  the tagged **Stack Atlas**, locus-addressed **genomes**, NSGA-II/III selection with
  confidence-aware dominance, **MAP-Elites islands** with simulated-annealing acceptance,
  migration and tabu-basin reseed, a contextual **Thompson bandit** for operator/model
  selection, **Shapley** attribution + epistasis, fractional-factorial **splicing**, a
  causal-leverage **budget scheduler**, a self-auditing **surrogate**, and MinHash novelty.
- **Ports & adapters**: a hardened Linux **sandbox** (cgroups v1/v2 + net and mount namespaces
  with a read-only filesystem jail + seccomp + uid-drop), a portable process sandbox for
  macOS/Windows hosts, SQLite/Postgres **program store**, Anthropic and local-Qwen **LLM
  providers**, a Go open-loop **load generator**, cost/telemetry, and the **StackZero**
  reference target.
- **The mutation data lake** (`colloid lake`): every verified mutation as a content-addressed
  record in an append-only, **hash-chained ledger** on its own branch (`colloid/datalake`).
  New runs warm-start from it (seeds + bandit priors). Verified programs materialise as
  deployable stacks (`colloid stack`, branch `stack/stackzero-verified`).
- **The evaluator** (`colloid_evaluator`): the L0–L6 **cascade**, the static **policy
  scanner**, the differential **oracle** + native fuzzing, the paired-statistics **benchmark
  protocol**, the causal **profiler**, the 16-case reward-hacking **canary suite**, and the
  **A/A** noise-floor calibration. The judge's own code is never a mutable locus, for any
  target.
- **StackZero** (`targets/stackzero`): a real Postgres + Python-service + C-library + OS/knobs
  stack with realistic optimisation headroom at every layer.
- **One contract, many languages** (ADR 0008):
  - The same StackZero system is implemented in **Go** (`targets/stackzero-go`, a full Colloid
    target) and in **TypeScript** (`targets/stackzero-ts`, run on Node and on Bun).
  - Each is certified by the evaluator's differential oracle against the Python reference.
  - The same judge measures and mutates all of them; per-language parts (L0 scanners, canary
    suites, deep checks) are written against each language's own parser.
  - Database knowledge transfers between implementations through shared loci (`lake_transfer`).
  - `colloid bakeoff` compares the languages under one benchmark protocol.
- **CRL, the Colloid Rule Language** (ADR 0009): declarative optimisation rules *learned* from
  the lake's verified evidence. A rule cannot exist without evidence, and its proposals are
  candidates the judge must verify. `colloid rules mine | apply`.
- **The evidence ladder** (`colloid ladder`): milestone gates with pre-registered criteria, so
  the roadmap grows by findings, never ahead of them.

## Quick start

```bash
pip install -e ".[dev]"                 # add .[llm-local] and .[anthropic] for LLM arms
colloid atlas                           # print the Stack Atlas
colloid canaries                        # reward-hacking gate — must reject 100%
colloid baseline                        # the target's SLO and $ / 1M requests
colloid profile                         # causal-leverage curves per unit
colloid run experiments/stackzero.yaml  # a full cross-layer optimisation run
colloid verify runs/stackzero           # post-run L6 + replicate; promotes what survives Holm + A/A gate
colloid redteam-recheck runs/stackzero  # re-adjudicate red-team breach alerts (live vs inert)
colloid lake ingest runs/stackzero      # verified mutations -> the hash-chained data lake branch
colloid stack materialize <record> --carrying-only --out DIR   # a deployable verified stack
colloid report runs/stackzero           # summarise the run
colloid dashboard runs/stackzero        # live dashboard (FastAPI)

# the same system in other languages (ADR 0008)
colloid canaries --target stackzero-go  # the Go judge's gate (14/14)
colloid run experiments/stackzero-go-primed.yaml   # optimise the Go implementation, warm-started from Python's evidence
colloid bakeoff --out docs/results/bakeoff.json    # Python vs Go vs Node vs Bun: one contract, one judge
colloid rules mine --commit             # CRL rules from the lake; `colloid rules apply --target T`
colloid ladder                          # which milestones the evidence has earned
```

**Platforms.** The engine runs natively on Linux, macOS and Windows (CI covers all three).
The full-fidelity judge needs Linux (cgroup v1 or v2, as root). On macOS and Windows, run it in
the Colloid container (`docker build -t colloid .`, see [docker/README.md](docker/README.md)).
`scripts/provision-linux.sh` provisions a bench host from scratch.

## Branches

| branch | job |
|---|---|
| **`main`** | the project: engine, evaluator, targets, tests, docs, CI (default branch, full history) |
| `colloid/datalake` | data only: the hash-chained mutation lake (`colloid lake`) |
| `stack/stackzero-verified` | data only: the deployable verified stack (`colloid stack`) |

Code and data never share a branch, and the lake and stack writers refuse to write to a code
branch (see `docs/ARCHITECTURE.md` §20).

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — the full architecture in plain English:
  every component, why it exists, and how the pieces fit. Each module also carries a long
  docstring with the same intent at code level.
- **[docs/INITIAL_RESULTS.md](docs/INITIAL_RESULTS.md)** — the first measured results: the
  canary gate, the A/A noise floor, causal-leverage localisation, the evolutionary run, and
  the stress test.
- **[docs/POLYGLOT_RESULTS.md](docs/POLYGLOT_RESULTS.md)** — the second experiment: Go and
  TypeScript implementations, the transfer A/B, the language bake-off, the learned rules and
  the ladder's verdicts.
- **[docs/adr/](docs/adr/)** — architecture decision records, including platforms and
  isolation (0004), the data lake (0005), why the judge is frozen (0006), and what it would
  take to go from verified mutations to a larger stack (0007), polyglot neutrality and the
  evidence ladder (0008), and the rule language (0009).

## What to expect

The blueprint is explicit that realistic gains are **single-digit to tens of percent on
specific hot parts**, not order-of-magnitude stack-wide speedups, and that reward hacking is
the default outcome without a strong evaluator. Colloid is built to deliver the former and
refuse the latter: measured, confidence-bounded, holdout-persistent gains, with every
promoted variant reproducible and explained from its genes.

## Tests & quality gates

```bash
pytest tests                 # unit / property / portable suite (runs on any OS)
sudo COLLOID_INTEGRATION=1 pytest tests          # + sandbox, StackZero end-to-end, evaluator (Linux, root)
mypy                         # strict on the pure core and ports
ruff check .
lint-imports                 # 4 architecture contracts
python stress/stress_sandbox.py && python stress/stress_evaluator.py   # stress (root)
```

License: Apache-2.0.
