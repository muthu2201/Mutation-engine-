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
<!-- /RESULTS:M1_COLD -->

<!-- RESULTS:LLM_COLD -->
<!-- /RESULTS:LLM_COLD -->

### 3.2 Arm B — lake-primed

<!-- RESULTS:M1_PRIMED -->
<!-- /RESULTS:M1_PRIMED -->

<!-- RESULTS:LLM_PRIMED -->
<!-- /RESULTS:LLM_PRIMED -->

### 3.3 The A/B

<!-- RESULTS:M1_TRANSFER -->
<!-- /RESULTS:M1_TRANSFER -->

---

## 4. The language bake-off

Same contract, same database, same requests and arrival schedule. Every implementation is one
arm of a single interleaved benchmark comparison (shuffled order, several cycles, paired CIs
against the Python reference).

### 4.1 Cost and latency at equal load

<!-- RESULTS:BAKEOFF_LOAD -->
<!-- /RESULTS:BAKEOFF_LOAD -->

### 4.2 Footprint

<!-- RESULTS:BAKEOFF_FOOTPRINT -->
<!-- /RESULTS:BAKEOFF_FOOTPRINT -->

### 4.3 Capacity

<!-- RESULTS:BAKEOFF_CAPACITY -->
<!-- /RESULTS:BAKEOFF_CAPACITY -->

### 4.4 What it means at scale

<!-- RESULTS:BAKEOFF_SCALE -->
<!-- /RESULTS:BAKEOFF_SCALE -->

---

## 5. Grammar seeds: the first learned rules (CRL v0)

<!-- RESULTS:RULES -->
<!-- /RESULTS:RULES -->

---

## 6. The evidence ladder

<!-- RESULTS:LADDER -->
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
