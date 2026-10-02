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
- **Ports & adapters**: a hardened Linux **sandbox** (cgroups + netns + seccomp + uid-drop),
  SQLite/Postgres **program store**, Anthropic and local-Qwen **LLM providers**, a Go
  open-loop **load generator**, cost/telemetry, and the **StackZero** reference target.
- **The evaluator** (`colloid_evaluator`): the L0–L6 **cascade**, the static **policy
  scanner**, the differential **oracle** + native fuzzing, the paired-statistics **benchmark
  protocol**, the causal **profiler**, the 15-case reward-hacking **canary suite**, and the
  **A/A** noise-floor test.
- **StackZero** (`targets/stackzero`): a real Postgres + Python-service + C-library + OS/knobs
  stack with realistic optimisation headroom at every layer.

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
colloid report runs/stackzero           # summarise the run
colloid dashboard runs/stackzero        # live dashboard (FastAPI)
```

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — the full architecture in plain English:
  every component, why it exists, and how the pieces fit. Each module also carries a long
  docstring with the same intent at code level.
- **[docs/INITIAL_RESULTS.md](docs/INITIAL_RESULTS.md)** — the first measured results: the
  canary gate, the A/A noise floor, causal-leverage localisation, the evolutionary run, and
  the stress test.
- **[docs/adr/](docs/adr/)** — architecture decision records.

## What to expect

The blueprint is explicit that realistic gains are **single-digit to tens of percent on
specific hot parts**, not order-of-magnitude stack-wide speedups, and that reward hacking is
the default outcome without a strong evaluator. Colloid is built to deliver the former and
refuse the latter: measured, confidence-bounded, holdout-persistent gains, with every
promoted variant reproducible and explained from its genes.

## Tests & quality gates

```bash
pytest tests                 # 90 unit/property/conformance tests (fast suite)
COLLOID_INTEGRATION=1 pytest tests/conformance   # sandbox + StackZero end-to-end (as root)
mypy colloid/core colloid/ports                  # strict
ruff check colloid colloid_evaluator
lint-imports                 # 4 architecture contracts
```

License: Apache-2.0.
