# ADR 0013 — SWE-fficiency: optimisation on real repositories, judged by someone else's tests

**Status:** proposed. Not yet pre-registered: the three checks under *Before acceptance* come
first, and no task has been run.

## Context

Colloid's two optimisation experiments (M1, M1b) made StackZero cheaper. We wrote StackZero.
The SWE-bench track (ADRs 0011, 0012) moved to real code that other people wrote, but its
objective is *correctness*: it measures repair, not optimisation. The engine's actual claim is
"make real software cheaper without breaking it", and that has not yet been tested on a
repository we did not write.

SWE-fficiency ([2511.06090](https://www.alphaxiv.org/abs/2511.06090)) is the closest external
match (see `docs/RELATED_WORK.md`):

- **Tasks:** 498, from 9 scientific-Python repositories.
- **Per task:**
  - the repository at the commit before an expert's performance PR;
  - a `workload()` script that the PR made faster;
  - the guarding tests, meaning the repository tests whose coverage meets the expert diff.
- **Score:** the speedup ratio (SR), model speedup ÷ expert speedup. A patch that fails a
  guarding test scores as no edit.

The July 2026 cross-machine audit ([2607.01211](https://www.alphaxiv.org/abs/2607.01211)) found
that only 411 of the 498 reference patches stay valid under the benchmark's own rule on all four
CPU families it tried. It also found that the 0.001 floor in the harmonic mean lets the worst ten
tasks carry 58.5–82.8% of the score.

This host has 4 cores and 15 GB of RAM. The official worker has 4 vCPUs and 16 GB, with the
Docker daemon pinned to *other* cores. One faithful worker therefore does not fit.

## Proposed decision

1. **What the search may see.** It gets exactly what the official agent prompt gives an agent:
   - the repository at the base commit;
   - the `workload()` script;
   - the test command and the rebuild command.

   It never sees the expert patch, the expert speedup or the list of guarding tests. They stay in
   a file only the judge and grader read, as in ADR 0011, and a test checks this.
2. **Sample.**
   - Draw from the 411 replay-valid tasks, using the audit's public data, stratified by
     repository.
   - The size and seed are fixed at acceptance. The first draw is 30 tasks, as in ADR 0011.
   - A task stays in only if its expert patch clears *our* minimum detectable effect on this host
     (point 4), measured before any search.
   - Dropped tasks are counted and listed; they are never silently replaced.
3. **The judge during search.** It runs inside the task image, with the network off and
   `workload()` untouched:
   - **L0 policy:**
     - source files only;
     - no test, workload or benchmark files;
     - a diff of at most 200 lines;
     - an AST denylist for stack and timing introspection: `sys._getframe`, `inspect.stack` and
       `inspect.currentframe`, `traceback.extract_stack`, frame attributes such as `f_back`, and
       reads of the timer the workload uses.
   - **L1:** the patch applies, the edited files byte-compile, and the rebuild command succeeds if
     a non-Python file changed.
   - **L2 regression:** the repository's existing tests that import the edited modules, found by
     the engine's own search exactly as in ADR 0011. Everything that passed at base must still
     pass.
   - **L3 timing:** an ABAB protocol on `workload()`, with each repetition in a **fresh process**,
     so no cache survives between runs. Each candidate's speedup is a ratio of medians with a
     bootstrap 95% CI, and a candidate passes only if the CI's lower bound clears the A/A noise
     floor.
   - **L4 held-out workload:** the same `workload()` with its input sizes scaled by a factor that
     the search never sees. A speedup that disappears there is an input-specific fast path, and
     the candidate fails.
4. **Noise floor.**
   - Before search, run A/A on each task: base against base, fresh processes, the same schedule
     as L3.
   - That gives a per-task minimum detectable effect.
   - The benchmark's own inclusion rule (base − expert > 2 × post-edit standard deviation) is
     recorded but not used.
5. **Hardware.**
   - The task container gets cores 1–3, pinned, with 12 GB of memory; dockerd and the engine
     run on core 0.
   - Image pulls never overlap a timed phase.
   - Absolute speedups are therefore this host's, not the leaderboard's, and the report says so.
6. **The holdout.** The official `swefficiency eval` scores the one submitted patch, with the
   expert patch run on the same host as the reference. Reported per task:
   - speedup, expert speedup, SR, and pass/fail on the guarding tests.

   Reported in aggregate:
   - the harmonic mean of SR with the official 0.001 floor;
   - the same without the floor (failures as SR = 1, the no-edit convention);
   - the share of tasks with a correct patch and SR > 1 against no edit.
7. **Models and budget.**
   - The local models of ADR 0011, with the same Thompson bandit.
   - A per-task budget fixed at acceptance; the proposed figure is 45 minutes of search, because
     workloads and rebuilds are slow.
   - An API arm can follow, using ADR 0012's protocol.
8. **The lake.**
   - A submission whose official SR > 1 with all guarding tests passing becomes a program record
     with `status: "verified"`, and its rewritten functions become gene records.
   - Records carry the target `swefficiency:<repo>` and the cost objective "workload runtime".
   - Their evidence is the official measurement plus our L3 CI.
   - Everything else stays in the run store.

## Before acceptance

1. **Images.** The official images are `swefficiency/swefficiency_images:<instance_id>` on
   Docker Hub, which rate-limits this host's shared egress (ADR 0011, point 10). We need either a
   mirror this host can pull, or a build from the published Dockerfiles that is bit-identical in
   its installed dependencies. Without one, this track cannot run here.
2. **Disk.** Image sizes are not published. Measure a few with `docker manifest inspect`. The
   track pulls one image at a time and removes it after grading; it must fit in the free disk
   with the build cache.
3. **A/A on this host.** Run A/A on a stratified handful of replay-valid tasks to see how many
   expert speedups our noise floor can detect on 3 pinned cores. If too few survive, the sample
   is too small to answer anything, and the track waits for a bigger host.

## Consequences

- If accepted, this is the first test of Colloid's core claim, making real software cheaper,
  on code and tests we did not write.
- An L3 or L4 rejection whose patch the official harness would have accepted is reported as
  such. The judge is stricter than the benchmark on purpose: fresh processes, the
  introspection denylist and the held-out workload address the hacks SWE-fficiency and GSO
  documented.
- The SR will not be comparable to the leaderboard's. The hardware, the replay-valid sample and
  the reporting both with and without the floor all differ, and each difference is stated
  next to the number.
