# Handoff: where Colloid stands, and how the next session runs the API arm

Written 2026-10-03 at the end of the local SWE-bench arm, for a new session that will have
`OPENROUTER_API_KEY` in its environment. Read this first, then `docs/SWEBENCH_RESULTS.md` and
ADRs 0011–0013.

## 1. State in one screen

| what | where | state |
|---|---|---|
| Engine, polyglot experiments (Go/TS ports, bake-off, M1 transfer, evidence ladder, CRL rules, soak fix) | branch `ccr-00675f8e-w4745o`, **PR #2** | ready for review, CI green; waiting on a human reviewer |
| SWE-bench track (ADRs 0011–0013, harness, local arm, probes, API-arm code) | branch `bench/swebench`, **PR #3** (draft, stacked on PR #2) | local arm finished; API arm not started |
| The mutation lake (hash-chained) | branch `colloid/datalake` | pushed; includes the SWE-bench local arm's resolved fixes |
| The verified StackZero stack | branch `stack/stackzero-verified` | unchanged since PR #2 |

Code reaches `main` only through PRs. The two data branches are written only by the engine's own
commands (`colloid lake push`, `colloid stack ...`), never by hand.

## 2. What the local SWE-bench arm showed

<!-- HANDOFF:LOCAL -->
(filled in when the run finished; see section 1 of docs/SWEBENCH_RESULTS.md for the generated tables)
<!-- /HANDOFF:LOCAL -->

## 3. Before the next session starts (the owner)

1. **Rotate the OpenRouter key that was pasted into chat earlier**, if you have not already. Then
   add the new key as `OPENROUTER_API_KEY` in this environment's settings (Claude Code on the
   web → the environment → environment variables). A running session does not see a new
   variable, so start a new session afterwards. Never paste a key into the chat: it is then in
   the transcript.
2. **Decide how the API arm is paid for:**
   - **Free tier.** On an account with under $10 of all-time credits, OpenRouter has historically
     allowed about 50 free-model requests a day. The arm needs up to 16 requests per instance (30
     instances), plus 60 for the probes. That is at most 540 requests, about **11 days** of daily
     resets. The run waits for each reset on its own and resumes after any interruption, but the
     container must survive that long, or the session must be resumed each day.
   - **$10 of credits.** That historically raised the free-model cap to about 1,000 a day, and the
     arm fits in **one day**. The model stays `:free`, so the credits are not spent on it.
   - Check the current caps first (the OpenRouter connector's credit and key checks are fine for
     that). OpenRouter's documentation renders the exact numbers only as placeholders.

## 4. The next session, step by step

All commands run from the repository root on branch `bench/swebench`.

```bash
git fetch origin bench/swebench && git checkout bench/swebench
scripts/setup-swebench.sh
```

`setup-swebench.sh` is idempotent. It:

- provisions the Colloid venv if it is missing;
- builds the grader's venv, pinned to the versions the local arm used (`swebench` 5.0.2);
- downloads the dataset and checks its sha256;
- re-draws the sample and asserts it equals the committed `docs/results/swebench/sample.json`;
- starts `dockerd`;
- says whether `OPENROUTER_API_KEY` is set, without printing it.

Then start the arm in the background. It takes hours to days, and it is resumable:

```bash
setsid nohup scripts/run-swebench-api.sh > /dev/null 2>&1 < /dev/null &
tail -f runs/swebench-api.log    # "[k/30] <instance>" per instance; "[pace] ..." while waiting for a reset
```

`run-swebench-api.sh` runs, in order:

1. `colloid swebench run --provider openrouter --out runs/swebench-api`: the 30 pre-registered
   instances, in the same order and with the same budget as the local arm (ADR 0012).
2. `scripts/export-swebench-evidence.sh api runs/swebench-api`: copies the evidence into
   `docs/results/swebench/api/`.
3. `colloid swebench probe --provider openrouter --out runs/swebench-api`: the memorisation
   probes for Qwen3.8, written to `docs/results/swebench/contamination_api.json`.
4. `stress/render_swebench.py` with both arms and both probe reports. This fills sections 7.1–7.5
   of `docs/SWEBENCH_RESULTS.md`, including the pre-registered paired comparison (exact McNemar)
   on all instances and on the instances clean for both arms.

If the container restarts, run the script again: finished instances are skipped.

When it has finished:

```bash
V=/opt/colloid/venv/bin
$V/colloid swebench ingest --provider openrouter --out runs/swebench-api   # resolved fixes + their probe verdicts -> lake
$V/colloid lake push
$V/python -m pytest -q tests && $V/ruff check . && $V/lint-imports
git add docs/results/swebench/api docs/results/swebench/contamination_api.json docs/SWEBENCH_RESULTS.md
git commit   # then push, and update PR #3's body with the API arm's result
```

Then write the API arm's narrative in `docs/SWEBENCH_RESULTS.md` §7, next to the generated
tables: what the stronger model changed, where in the funnel, and on which instances. Say whether
any difference survives on the clean instances.

## 5. Rules that carry over

- **Keys and credentials.**
  - Keys come only from environment variables. Never write them to a file, a log, a commit or
    the chat. Never use credentials found in public places.
  - The engine operator does not read the evaluator's credential files under
    `/opt/colloid/state/` (for example, the Postgres superuser password). Only the judge uses them.
- **Provenance.**
  - The engine calls OpenRouter itself. Experiment responses are never relayed through the
    assistant's own connectors.
  - The OpenRouter connector is fine for model lookup, credit checks and a smoke test on a pilot
    instance, never on a sampled one.
- **Pre-registration.** ADRs 0011 and 0012 (with their addendum and amendment) fix the sample,
  budget, judge, endpoints and verdict rule. Nothing in them changes after the fact. A bug fix
  that would change a result is reported, not silently applied.
- **Shared CPU.** Do not run CPU-heavy or cluster-touching work during a timed or scored run:
  test suites, image builds or a second arm. Stop processes by exact name (`pgrep -x`), never
  with `pkill -f` and a pattern that matches your own shell.
- **Network.**
  - Never disable TLS verification or unset `HTTPS_PROXY`.
  - Docker Hub rate-limits this host, so SWE-bench images come from Epoch AI's GHCR rebuilds
    (ADR 0011, point 10).
- **Git.** No history rewriting, orphan branches, `git clean`, `git stash` or `git rm --cached`
  on shared branches.
- **Disk.**
  - The session has a fixed allowance, about 15 GB free at the end of the local arm.
  - Images are pulled one at a time and removed after grading.
  - Do not pass `--keep-images` on a full run.

## 6. Open decisions for the owner

1. **Pay for the API arm, or use the free tier?** See section 3.
2. **The M1 re-test** (polyglot transfer, PR #2: M1b did not pass). The proposal: about 18 h, at
   least 3 seeds per arm, the fixed judge (ADR 0010) and one fixed request rate for both arms.
3. **ADR 0013 (SWE-fficiency, proposed).** It is blocked first on image access: the official
   images are on Docker Hub. Accepting it needs the three checks it lists.
4. **A contamination-resistant SWE-bench follow-up.** SWE-rebench or SWE-bench-Live issues
   created after 2026-08-14 (Qwen3.8's release). It too waits on image access.

## 7. Backlog, in the order we would take it

1. A model acceptance gate: an executed code suite on this host's backend before a run uses a
   local model ("Broken on Arrival", see `docs/RELATED_WORK.md`).
2. Repair protocol v2, for the *next* pre-registration:
   - the largest model writes the first proposal on each snippet, and small models make variants
     of candidates that passed L2 (LEVI);
   - one reproduction script per stated behaviour, with a runtime diagnosis fed into the fix
     prompt (SWE-Doctor).
3. Advice cards mined from attributed lake genes, as a third transfer channel (EvoMem).
   Candidates-to-best becomes a secondary M1 endpoint.
4. CRL seed candidates from the database literature: batching an N+1 lookup, and indexing the
   columns hot queries filter or join on. Each needs verified-gene evidence before it becomes a
   rule.
