# ADR 0010 — The soak's leak test measures the service's persistent growth

**Status:** accepted (applied after experiment 2's M1 runs, which kept the old rule throughout)

## Context

L6 ends with a 20-second soak at 1.3× the benchmark rate. Through experiment 2, a program
failed it when one least-squares slope of the *stack's* memory exceeded 0.5 MB/s. The stack's
memory was the PSS of the service and of the database cluster together.

The M1 runs on the Go implementation showed this rule failing honest programs:

- **The cold arm.** Three index programs failed the soak. Their holdout cost gains had
  confidence intervals well above zero, and two of them had gene sets contained in programs
  that were promoted.
- **The lake-primed arm.** It rejected the lake's best seed this way, in both attempts.
  That seed is the two-index program verified on Python.
- **The slopes of programs that passed.** Across the runs they ranged from 0.2 to 0.9 MB/s,
  so the threshold sat inside the spread of honest programs.

The suspected mechanism is the cluster's shared buffers. They fill as a new index is first
read, and every backend that touches a buffer page adds it to its PSS. That is bounded
warm-up, not a leak.

## Decision

1. The PSS sampler keeps the service's and the database's series separately. Their sum, the
   memory objective, is unchanged.
2. The leak test applies to the **service**, the process tree a gene changes. The cluster's
   memory is bounded by range-limited configuration (`shared_buffers`, and `work_mem` per
   operation). Its growth is recorded and does not decide the verdict.
3. Growth must **persist**. The verdict is a leak only when the whole-soak slope exceeds
   0.5 MB/s *and* the second-half slope still exceeds half of that. Warm-up flattens; a leak
   does not.
4. The legacy verdict is still computed and recorded on every soak. Calibration
   (`stress/soak_calibration.py`) scores both rules on the same samples. It runs honest
   programs (the baseline, and every cold-run program whose holdout gain was real) against
   builds that leak a parked goroutine on every rating lookup. Its results are in
   POLYGLOT_RESULTS.md §2.1.

## Consequences

- This is a change to the judge, made the way ADR 0006 requires: a reviewed commit with
  calibration evidence, not a self-applied lesson. The M1 A/B, its verification, and the lake
  records ingested from it were all judged by the old rule. Records ingested later carry the
  new one, and every soak records both verdicts.
- Sensitivity to a leak *in the database* is given up. No gene can reach such a leak:
  - temporary tables are revoked;
  - session state is caught by the SQL audit;
  - the drivers cap their prepared-statement caches.

  If a gene type that can reach one appears, the database series is already recorded, and a
  persistent-growth test on it is one line.
