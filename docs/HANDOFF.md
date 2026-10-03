# Handoff: where Colloid stands, and how the next session runs the API arm

Written 2026-10-03 at the end of the local SWE-bench arm, for a new session that will have
`NVIDIA_API_KEY` in its environment. Read this first, then `docs/SWEBENCH_RESULTS.md` and
ADRs 0011–0013.

## 0. The goal, and how to work with the owner

**The long-term goal.** A proprietary stack with no bloat, machine-optimised from bare metal to
applications: our own language(s) and compilers, up to a full-stack ecosystem. It should be
mathematically verified wherever that is possible. The moat is two things together:

- that stack;
- the proprietary data Colloid accumulates. That means the lake of *verified* mutations, each
  with its evidence, and the rules distilled from them.

ADR 0007 sets out the path from verified mutations to a stack, and ADR 0009 the first rule
language (CRL). Nothing in the engine is sacred. If a session finds a flaw in the core (the
judge, the statistics, the lake, the search), it fixes it and records why in an ADR. Two caveats:

- A fix never edits a result that was already measured.
- A fix never changes a pre-registered protocol that is already running. It goes into the next
  pre-registration.

**Where we stand against that goal, honestly.**

- **Verification today is empirical, not mathematical.** It is a statistical judge (A/A noise
  floors, ABAB, holdouts), the projects' own tests, and official graders.
- **Next step towards "mathematically verified":** prove CRL rewrite rules sound before they are
  applied. Candidate techniques: SMT-checked rule soundness in the style of Alive2, e-graph
  rewriting, and translation validation. Each verified gene then carries a proof as well as a
  measurement.
- **Evidence so far:**
  - M1a passed.
  - M1b, the transfer of knowledge between runs, did not pass (PR #2).
  - On real repositories, small local models resolve few SWE-bench issues (section 2).
- Claims in the docs stay within that evidence.

**Asking the owner for research.** The owner can run deep-research prompts in the Claude web app
and paste the reports back. Use it whenever a decision needs research or verification beyond this
container: a technique, a benchmark, a provider's terms, prior art. Write one self-contained
prompt, saying:

- what to find;
- which sources count (papers, official docs, repository code);
- what form the answer should take (tables with links; verified figures marked as such).

Every claim taken from a returned report is checked against its primary source (the alphaXiv
connector reads papers and GitHub repositories) before it enters the docs. That is how the three
reports behind `docs/RELATED_WORK.md` were used.

Research prompts worth asking next:

1. *Proven-sound program rewriting in 2025–2026:*
   - Alive2-style SMT checking, e-graphs (egg/egglog), translation validation, verified
     compilers (CompCert, CakeML);
   - LLM-proposed rewrites with formal equivalence checks.

   Which of these can certify rules mined from measured mutations, in Python, Go and C, and at
   what cost?
2. *Superoptimisation and learned compilers:*
   - the state of the art in LLM-guided superoptimisation and compiler-pass ordering, with
     verified correctness;
   - which open toolchains (MLIR, LLVM, Cranelift) a new language could target first.
3. *Keeping proprietary data proprietary when using hosted models:* the data-retention and
   training terms of NVIDIA's hosted endpoints and OpenRouter's providers. What can be sent for
   public benchmark code, and what must stay on local models for proprietary code?

**Protecting the moat (the data).**

- The lake (`colloid/datalake`) and the stack branches are the proprietary asset.
- Prompts sent to a hosted model leave this machine. That is acceptable for public benchmark code
  such as SWE-bench, but not, by default, for proprietary code.
- Until question 3 is answered, proprietary targets use local models only.
- Never push lake data or keys anywhere except this repository's own branches.

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

1. **Add the NVIDIA key to the environment.** In the session's title bar, open the cloud
   environment menu, choose **Edit**, and add the key from build.nvidia.com as the environment
   variable **`NVIDIA_API_KEY`**. Then start a new session: a running session does not see a new
   variable. Never paste the key into the chat, because it is then in the transcript.
2. **Rotate the OpenRouter key** that was pasted into chat earlier, if you have not already. It is
   no longer needed for this arm.
3. **What the NVIDIA key gives the arm** (ADR 0012, amendment 2):
   - **Model:** Kimi K3 (`moonshotai/kimi-k3`), on its free endpoint.
   - **Limits:** a verified account has no daily cap, only about 40 requests a minute, so the arm
     fits in a day.
   - **Requests:** at most 16 per instance plus 2 per instance for the probes, so at most 540 in
     all.
   - **Why not Qwen3.8:** NVIDIA's catalogue does not have it. The switch was recorded before any
     API-arm instance ran.

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
- says whether `NVIDIA_API_KEY` (and `OPENROUTER_API_KEY`) are set, without printing them.

Then run the **pilot**. It runs the engine on the two pilot instances, which are outside the
sample, at reasoning efforts `high` and `low`. The pre-declared rule then fixes the arm's effort.
It takes up to about 100 minutes:

```bash
scripts/pilot-swebench-api.sh          # writes docs/results/swebench/api_pilot.json; log: runs/pilot-nvidia.log
git add docs/results/swebench/api_pilot.json && git commit -m "SWE-bench API arm: pilot fixes the reasoning effort" && git push
```

Commit the pilot's choice **before** the first sampled instance runs; that ordering is part of
the pre-registration. If the pilot reports that no effort completed without errors, the provider
call is wrong (for example, NVIDIA rejects a parameter). Fix it on the pilot instances only, and
say so in the PR.

Then start the arm in the background. It takes several hours, and it is resumable:

```bash
setsid nohup scripts/run-swebench-api.sh > /dev/null 2>&1 < /dev/null &
tail -f runs/swebench-api.log    # "[k/30] <instance>" per instance
```

`run-swebench-api.sh` runs, in order (`PROVIDER=nvidia` is the default):

1. `colloid swebench run --provider nvidia --reasoning-effort <pilot's choice> --out runs/swebench-api`:
   the 30 pre-registered instances, in the same order and with the same budget as the local arm.
2. `scripts/export-swebench-evidence.sh api runs/swebench-api`: copies the evidence into
   `docs/results/swebench/api/`.
3. `colloid swebench probe --provider nvidia ...`: the memorisation probes for Kimi K3, written to
   `docs/results/swebench/contamination_api.json`.
4. `stress/render_swebench.py` with both arms and both probe reports. This fills sections 7.1–7.5
   of `docs/SWEBENCH_RESULTS.md`, including the pre-registered paired comparison (exact McNemar)
   on all instances and on the instances clean for both arms.

If the container restarts, run the script again: finished instances are skipped.

When it has finished:

```bash
V=/opt/colloid/venv/bin
$V/colloid swebench ingest --provider nvidia --out runs/swebench-api   # resolved fixes + their probe verdicts -> lake
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
- **Provenance.** The engine calls the hosted model itself (NVIDIA, or OpenRouter). Experiment
  responses are never relayed through the assistant's own connectors. Connectors are fine for
  model lookup and credit checks only.
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

1. **A second API arm?** Qwen3.8 27B through OpenRouter stays implemented
   (`PROVIDER=openrouter`): a mid-size model next to Kimi K3's frontier scale. It needs an
   OpenRouter key, and on the free tier it takes about 11 days of daily resets (about one day with
   $10 of credits). It would be reported as its own arm.
2. **The M1 re-test** (polyglot transfer, PR #2: M1b did not pass). The proposal: about 18 h, at
   least 3 seeds per arm, the fixed judge (ADR 0010) and one fixed request rate for both arms.
3. **ADR 0013 (SWE-fficiency, proposed).** It is blocked first on image access: the official
   images are on Docker Hub. Accepting it needs the three checks it lists.
4. **A contamination-resistant SWE-bench follow-up.** SWE-rebench or SWE-bench-Live issues
   created after 2026-08-20 (Kimi K3's release). It too waits on image access.

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
