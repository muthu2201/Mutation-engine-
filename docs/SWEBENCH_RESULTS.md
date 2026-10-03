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

**Resolved: 2 / 30** (6.7%, Wilson 95% CI 1.8–21.3%), graded by the official SWE-bench harness. 30 of 30 pre-registered instances ran.

| | count |
|---|---|
| instances run | 30 |
| a patch was submitted (passed L0–L2) | 30 |
| resolved (all FAIL_TO_PASS and PASS_TO_PASS pass) | 2 |
| infrastructure errors | 0 |
| wall clock, all instances | 8.0 h |
| LLM calls | 450 |

<!-- /RESULTS:SWE_SUMMARY -->

## 2. Where candidates are lost (the funnel)

<!-- RESULTS:SWE_FUNNEL -->

| stage | candidates |
|---|---|
| proposals | 390 |
| parsed and spliced | 227 |
| passed L0 (patch policy) | 220 |
| passed L1 (applies, compiles) | 216 |
| passed L2 (no regressions) | 164 |

| reproduction script at base_commit | scripts |
|---|---|
| unparseable | 29 |
| NONE | 18 |
| ISSUE REPRODUCED | 10 |
| ISSUE RESOLVED | 3 |

| why a response was not a candidate | count |
|---|---|
| parse: identical to the original | 76 |
| parse: response does not define __init__() | 10 |
| parse: no code block in response | 4 |
| parse: response does not define fit() | 3 |
| parse: response does not define _eval_evalf() | 3 |
| parse: response does not define authenticate() | 2 |
| parse: syntax error: '(' was never closed (line 9) | 2 |
| parse: syntax error: '(' was never closed (line 19) | 2 |

<!-- /RESULTS:SWE_FUNNEL -->

## 3. Localisation

<!-- RESULTS:SWE_LOCALISATION -->

Computed after grading, from the gold patch, for diagnosis only:

| | instances |
|---|---|
| a localised snippet is in a file the gold patch changes | 21 / 30 |
| a localised snippet overlaps the gold patch's changed lines | 16 / 30 |
| the submission edits a file the gold patch changes | 14 / 30 |

<!-- /RESULTS:SWE_LOCALISATION -->

## 4. Which arms produced what

<!-- RESULTS:SWE_ARMS -->

| arm (model / prompt) | proposals | passed L0–L2 | resolved a reproduction | resolved instances |
|---|---|---|---|---|
| qwen2.5-coder-1.5b / fix | 153 | 65 | 0 | 0 |
| qwen2.5-coder-1.5b / fix_think | 67 | 27 | 0 | 0 |
| qwen2.5-coder-3b / fix | 44 | 14 | 0 | 0 |
| qwen2.5-coder-3b / fix_think | 38 | 15 | 0 | 0 |
| qwen2.5-coder-7b / fix | 51 | 22 | 0 | 0 |
| qwen2.5-coder-7b / fix_think | 37 | 21 | 0 | 2 |

<!-- /RESULTS:SWE_ARMS -->

## 5. Every instance

<!-- RESULTS:SWE_INSTANCES -->

| instance | localised (file / lines) | validated repro | candidates ok / proposed | submitted | resolved | wall min |
|---|---|---|---|---|---|---|
| `astropy__astropy-7336` | ✓ / ✗ | 0/2 | 3 / 14 | yes | no | 7 |
| `django__django-11099` | ✓ / ✓ | 1/2 | 8 / 14 | yes | no | 6 |
| `django__django-11451` | ✓ / ✓ | 0/2 | 5 / 14 | yes | **yes** | 16 |
| `django__django-11490` | ✗ / ✗ | 0/2 | 5 / 14 | yes | no | 9 |
| `django__django-11951` | ✓ / ✓ | 0/2 | 4 / 14 | yes | no | 16 |
| `django__django-12276` | ✓ / ✓ | 0/2 | 11 / 14 | yes | no | 5 |
| `django__django-12304` | ✗ / ✗ | 0/2 | 5 / 11 | yes | no | 23 |
| `django__django-13109` | ✓ / ✓ | 0/2 | 8 / 14 | yes | **yes** | 20 |
| `django__django-13112` | ✗ / ✗ | 0/2 | 4 / 14 | yes | no | 13 |
| `django__django-13821` | ✗ / ✗ | 1/2 | 7 / 14 | yes | no | 7 |
| `django__django-13933` | ✓ / ✓ | 0/2 | 8 / 14 | yes | no | 8 |
| `django__django-14580` | ✗ / ✗ | 0/2 | 4 / 14 | yes | no | 12 |
| `django__django-15569` | ✓ / ✓ | 0/2 | 7 / 14 | yes | no | 14 |
| `matplotlib__matplotlib-20859` | ✓ / ✗ | 1/2 | 4 / 10 | yes | no | 26 |
| `matplotlib__matplotlib-24177` | ✗ / ✗ | 0/2 | 5 / 14 | yes | no | 17 |
| `matplotlib__matplotlib-25287` | ✗ / ✗ | 0/2 | 2 / 13 | yes | no | 29 |
| `matplotlib__matplotlib-25311` | ✗ / ✗ | 1/2 | 2 / 13 | yes | no | 23 |
| `pydata__xarray-4075` | ✓ / ✗ | 1/2 | 5 / 9 | yes | no | 22 |
| `pydata__xarray-4629` | ✗ / ✗ | 2/2 | 6 / 13 | yes | no | 22 |
| `pytest-dev__pytest-7205` | ✓ / ✓ | 0/2 | 3 / 14 | yes | no | 14 |
| `pytest-dev__pytest-7982` | ✓ / ✓ | 0/2 | 8 / 14 | yes | no | 10 |
| `scikit-learn__scikit-learn-13135` | ✓ / ✓ | 0/2 | 4 / 12 | yes | no | 22 |
| `sphinx-doc__sphinx-8621` | ✓ / ✓ | 0/2 | 11 / 14 | yes | no | 15 |
| `sphinx-doc__sphinx-9281` | ✓ / ✗ | 0/2 | 8 / 14 | yes | no | 12 |
| `sphinx-doc__sphinx-9698` | ✓ / ✓ | 0/2 | 9 / 14 | yes | no | 16 |
| `sphinx-doc__sphinx-9711` | ✓ / ✓ | 0/2 | 9 / 12 | yes | no | 21 |
| `sympy__sympy-12096` | ✓ / ✓ | 1/2 | 1 / 12 | yes | no | 21 |
| `sympy__sympy-12481` | ✓ / ✓ | 1/2 | 3 / 14 | yes | no | 17 |
| `sympy__sympy-15809` | ✓ / ✗ | 1/2 | 2 / 5 | yes | no | 21 |
| `sympy__sympy-16886` | ✓ / ✓ | 0/2 | 3 / 14 | yes | no | 15 |

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

The same 30 instances, localisation, prompts, bandit, judge, grader and budget, with a hosted
model in place of the local models. That model is Kimi K3 (`moonshotai/kimi-k3`), served by
NVIDIA's hosted endpoints. It is pre-registered in [ADR 0012](adr/0012-swe-bench-api-arm.md); its
amendment 2 records the switch from Qwen3.8 on OpenRouter, made before any API-arm instance ran.
The model's reasoning effort is fixed by a pilot on two instances outside the sample
(`docs/results/swebench/api_pilot.json`). The arm runs after the local arm.

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
python -m venv /opt/colloid/state/swebench/venv && /opt/colloid/state/swebench/venv/bin/pip install swebench==5.0.2 pandas==3.0.6 pyarrow==25.0.1
colloid swebench prepare                     # tasks.jsonl (search-visible), gold.jsonl (grader only), sample.json
colloid swebench run --out runs/swebench     # pre-registered sample; resumable
colloid swebench probe --out runs/swebench   # post hoc: docs/results/swebench/contamination_local.json
colloid swebench ingest --out runs/swebench  # resolved fixes, with their probe verdicts -> the lake
python stress/render_swebench.py --arm local=runs/swebench --probes local=docs/results/swebench/contamination_local.json \
    --write docs/SWEBENCH_RESULTS.md
# the API arm (ADR 0012, amendment 2), with NVIDIA_API_KEY in the environment:
scripts/pilot-swebench-api.sh                # pilot instances only: fixes the reasoning effort (api_pilot.json)
scripts/run-swebench-api.sh                  # i.e. the following, with the pilot's effort:
colloid swebench run --provider nvidia --reasoning-effort EFFORT --out runs/swebench-api
colloid swebench probe --provider nvidia --reasoning-effort EFFORT --out runs/swebench-api   # contamination_api.json
python stress/render_swebench.py --arm local=docs/results/swebench/local --arm api=docs/results/swebench/api \
    --probes local=docs/results/swebench/contamination_local.json --probes api=docs/results/swebench/contamination_api.json \
    --write docs/SWEBENCH_RESULTS.md
```
