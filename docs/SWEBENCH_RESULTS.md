# Colloid on SWE-bench Verified: repairs in real repositories (experiment 3)

Experiments 1 and 2 optimised StackZero, a system we wrote. This experiment points the same
engine at real code written by other people:

- **the issues**: GitHub issues from Django, SymPy, Sphinx, Matplotlib, xarray, pytest, astropy
  and scikit-learn, at the commit where each was reported;
- **the success criterion**: belongs to someone else, namely the project's own tests that came
  with the real fix. The engine never sees them.

The protocol was pre-registered in [ADR 0011](adr/0011-swe-bench-the-gold-tests-are-the-holdout.md)
before any sampled instance was run. Its main points:

- **Sample.** 30 instances drawn at random from the 194 SWE-bench Verified instances labelled
  `<15 min fix`. The resolve rate is therefore not comparable to full-set leaderboard numbers.
- **What the search sees.** The issue text and the repository, nothing else.
- **Models.** Local models only (Qwen2.5-Coder, quantised, 3 CPU cores).
- **Holdout.** The official SWE-bench grader, run once on the one submitted patch.

Every number below is generated from the run's records by `stress/render_swebench.py`.

## 1. Result

<!-- RESULTS:SWE_SUMMARY -->
<!-- /RESULTS:SWE_SUMMARY -->

## 2. Where candidates are lost (the funnel)

<!-- RESULTS:SWE_FUNNEL -->
<!-- /RESULTS:SWE_FUNNEL -->

## 3. Localisation

<!-- RESULTS:SWE_LOCALISATION -->
<!-- /RESULTS:SWE_LOCALISATION -->

## 4. Which arms produced what

<!-- RESULTS:SWE_ARMS -->
<!-- /RESULTS:SWE_ARMS -->

## 5. Every instance

<!-- RESULTS:SWE_INSTANCES -->
<!-- /RESULTS:SWE_INSTANCES -->

## 6. Memorisation probes (post hoc; ADR 0011 addendum)

SWE-bench Verified is contaminated for frontier models (see
[RELATED_WORK](RELATED_WORK.md)). So after grading, each local model was asked two questions it
can answer only from memory:

- **File path:** the issue text alone, with no code. Which file holds the bug?
- **Task ID:** the instance ID alone. Write the gold patch.

Each winning patch was also compared with the gold patch. The verdict rule (`suspect`,
`path-only`, `clean`) was fixed in the ADR before any probe ran. These probes are diagnosis: the
resolve rate in section 1 stays the headline.

<!-- RESULTS:SWE_CONTAMINATION -->
<!-- /RESULTS:SWE_CONTAMINATION -->

## 7. The API arm: the same engine with a stronger model (ADR 0012)

The same 30 instances, localisation, prompts, bandit, judge, grader and budget, with
Qwen3.8 27B served by OpenRouter in place of the local models. It is pre-registered in
[ADR 0012](adr/0012-swe-bench-api-arm.md) and runs after the local arm.

### 7.1 Result

<!-- RESULTS:SWE_SUMMARY_API -->
<!-- /RESULTS:SWE_SUMMARY_API -->

### 7.2 The funnel

<!-- RESULTS:SWE_FUNNEL_API -->
<!-- /RESULTS:SWE_FUNNEL_API -->

### 7.3 Local vs API, instance by instance (the pre-registered comparison)

<!-- RESULTS:SWE_PAIRED -->
<!-- /RESULTS:SWE_PAIRED -->

### 7.4 Memorisation probes for the API model

<!-- RESULTS:SWE_CONTAMINATION_API -->
<!-- /RESULTS:SWE_CONTAMINATION_API -->

### 7.5 Local vs API on the instances clean for both arms (ADR 0012 amendment)

<!-- RESULTS:SWE_PAIRED_CLEAN -->
<!-- /RESULTS:SWE_PAIRED_CLEAN -->

## 8. Reproduce

```bash
python -m venv /opt/colloid/state/swebench/venv && /opt/colloid/state/swebench/venv/bin/pip install swebench pandas pyarrow
colloid swebench prepare                     # tasks.jsonl (search-visible), gold.jsonl (grader only), sample.json
colloid swebench run --out runs/swebench     # pre-registered sample; resumable
colloid swebench ingest --out runs/swebench  # resolved fixes -> the lake
colloid swebench probe --out runs/swebench   # post hoc: docs/results/swebench/contamination_local.json
python stress/render_swebench.py --arm local=runs/swebench --probes local=docs/results/swebench/contamination_local.json \
    --write docs/SWEBENCH_RESULTS.md
# the API arm (ADR 0012), with OPENROUTER_API_KEY in the environment: scripts/run-swebench-api.sh, i.e.
colloid swebench run --provider openrouter --out runs/swebench-api
colloid swebench probe --provider openrouter --out runs/swebench-api   # contamination_api.json
python stress/render_swebench.py --arm local=docs/results/swebench/local --arm api=docs/results/swebench/api \
    --probes local=docs/results/swebench/contamination_local.json --probes api=docs/results/swebench/contamination_api.json \
    --write docs/SWEBENCH_RESULTS.md
```
