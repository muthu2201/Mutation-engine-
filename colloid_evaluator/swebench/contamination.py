"""Post-hoc memorisation probes for the SWE-bench track (ADR 0011 addendum, ADR 0012).

Adapted from "The SWE-Bench Illusion" (Liang, Garg, Zilouchian Moghaddam, arXiv 2506.12286) and from
OpenAI's task-ID elicitation. Each probe asks a model something it can answer from memory but
not from the information given:

- **file path:** only the issue text and the repository name, no code. Does the model name a
  file the gold patch edits? A hit counts only when the issue text does not mention that file.
- **task ID:** only the instance ID. Does the model reproduce the gold patch's added lines?
  Scored as frequency-aware 5-gram overlap with them, plus the number of gold lines reproduced
  exactly.
- **submission overlap:** no model call. How much of our winning patch's added code is the
  gold patch's added code?

The suspect rule was declared in docs/adr/0011 (addendum) before any probe ran. This module
reads the gold patches, so it belongs to the judge side and runs only after grading.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

FILE_PATH_PROMPT = """You will be provided with an issue statement explaining a problem to resolve within a codebase. The code base is: {repo}.
<issue>
{issue}
</issue>
Given the issue description and project, provide the file path of the .py file containing the issue, relative to the repository root.
Reply with the path only, in one ``` block."""

TASK_ID_PROMPT = """SWE-bench Verified instance {instance_id} ({repo}).
Write the exact git diff of the change that resolved this instance (its gold patch). Reply with the diff only, in one ``` block."""

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|\S")
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_PATH = re.compile(r"[\w./-]+\.py\b")

# the verdict rule (declared before running; see ADR 0011, addendum)
OVERLAP_SUSPECT = 0.5  # task-ID probe: >= half of its predicted 5-grams are gold 5-grams...
MIN_GOLD_TOKENS = 12  # ...counted only when the gold patch's added code is long enough for 5-grams to mean something
MIN_LINE_TOKENS = 4  # a gold line reproduced verbatim counts only if it is not trivial (e.g. "return x")


def added_lines(diff: str) -> list[str]:
    return [ln[1:].strip() for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++") and ln[1:].strip()]


def tokens(lines: Iterable[str]) -> list[str]:
    return [t for ln in lines for t in _TOKEN.findall(ln)]


def overlap5(pred: Sequence[str], gold: Sequence[str]) -> float:
    """Share of the prediction's 5-grams that occur in the gold tokens (each gold 5-gram matched at most as often as it occurs)."""
    grams = lambda ts: [tuple(ts[i : i + 5]) for i in range(len(ts) - 4)]  # noqa: E731
    p, g = grams(list(pred)), Counter(grams(list(gold)))
    if not p:
        return 0.0
    hit = 0
    for gram in p:
        if g[gram] > 0:
            g[gram] -= 1
            hit += 1
    return hit / len(p)


def files_of(diff: str) -> list[str]:
    return sorted({m.group(1) for m in re.finditer(r"^diff --git a/(\S+)", diff, re.M)})


def code_block(text: str) -> str:
    m = _FENCE.search(text)
    return (m.group(1) if m else text).strip()


def predicted_path(text: str) -> str | None:
    m = _PATH.search(code_block(text))
    return m.group(0).lstrip("./") if m else None


def mentioned(issue: str, path: str) -> bool:
    """Whether the issue text names the file (full path or its module path)."""
    mod = path[:-3].replace("/", ".")
    return path in issue or mod in issue


@dataclass
class Probe:
    instance_id: str
    model: str
    gold_files: list[str]
    path_prediction: str | None
    path_hit: bool  # the predicted path is a gold file
    path_mentioned_in_issue: bool
    task_id_overlap: float  # 5-gram overlap of the task-ID probe's added lines with gold's
    task_id_exact_lines: int  # non-trivial gold added lines reproduced verbatim by the task-ID probe
    gold_tokens: int

    @property
    def verdict(self) -> str:
        """``suspect``: the model recalls the gold patch. ``path-only``: it names a gold file the issue does not
        mention (repository familiarity or instance memory; the weaker signal). ``clean``: neither."""
        recalled = (self.gold_tokens >= MIN_GOLD_TOKENS and self.task_id_overlap >= OVERLAP_SUSPECT) or self.task_id_exact_lines >= 1
        if recalled:
            return "suspect"
        return "path-only" if self.path_hit and not self.path_mentioned_in_issue else "clean"

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "verdict": self.verdict}


def probe(instance: dict[str, Any], gold: dict[str, Any], model: str, ask: Callable[[str, str], str]) -> Probe:
    """``ask(model, prompt) -> text``. Two model calls."""
    gfiles = files_of(gold["patch"])
    path_text = ask(model, FILE_PATH_PROMPT.format(repo=instance["repo"], issue=instance["problem_statement"][:6000]))
    path = predicted_path(path_text)
    hit_file = next((f for f in gfiles if path and (f == path or f.endswith("/" + path))), None)
    tid_text = ask(model, TASK_ID_PROMPT.format(instance_id=instance["instance_id"], repo=instance["repo"]))
    pred_lines = added_lines(code_block(tid_text)) or [ln.strip() for ln in code_block(tid_text).splitlines() if ln.strip()]
    gold_lines = added_lines(gold["patch"])
    return Probe(
        instance_id=instance["instance_id"], model=model, gold_files=gfiles, path_prediction=path, path_hit=hit_file is not None,
        path_mentioned_in_issue=bool(hit_file and mentioned(instance["problem_statement"], hit_file)),
        task_id_overlap=round(overlap5(tokens(pred_lines), tokens(gold_lines)), 3),
        task_id_exact_lines=len({ln for ln in gold_lines if len(tokens([ln])) >= MIN_LINE_TOKENS} & set(pred_lines)),
        gold_tokens=len(tokens(gold_lines)),
    )


def submission_overlap(submission_diff: str, gold_patch: str) -> dict[str, Any]:
    """No model call: how much of our winning patch's added code is the gold patch's added code."""
    sub, gold = added_lines(submission_diff), added_lines(gold_patch)
    return {"overlap5": round(overlap5(tokens(sub), tokens(gold)), 3), "identical_added_lines": sorted(set(sub) & set(gold)) == sorted(set(gold)),
            "gold_added_lines": len(gold), "submission_added_lines": len(sub)}


def load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    return {r["instance_id"]: r for r in (json.loads(ln) for ln in path.read_text().splitlines() if ln.strip())}


def run(sample: Sequence[str], tasks: dict[str, dict[str, Any]], gold: dict[str, dict[str, Any]], records: Path,
        ask: Callable[[str, str], str], models: Sequence[str], log: Callable[[str], None] = print,
        pace: Callable[[int], None] | None = None) -> dict[str, Any]:
    """Every probe for every (instance, model), and each graded submission's overlap with gold.

    ``pace(n)`` (hosted models) is called before each instance with the requests it needs."""
    out: dict[str, Any] = {"models": list(models), "rule": {"overlap": OVERLAP_SUSPECT, "min_gold_tokens": MIN_GOLD_TOKENS,
                                                           "min_line_tokens": MIN_LINE_TOKENS}, "probes": [], "submissions": []}
    for iid in sample:
        if pace:
            pace(2 * len(models))
        for m in models:
            p = probe(tasks[iid], gold[iid], m, ask)
            out["probes"].append(p.as_dict())
            log(f"[probe] {iid} {m}: path {'hit' if p.path_hit else 'miss'}{' (in issue)' if p.path_mentioned_in_issue else ''}, "
                f"task-ID overlap {p.task_id_overlap}, exact lines {p.task_id_exact_lines} -> {p.verdict}")
        rec_path = records / iid / "record.json"
        if rec_path.exists():
            rec = json.loads(rec_path.read_text())
            sub = rec.get("submission") or {}
            if sub.get("diff"):
                out["submissions"].append({"instance_id": iid, "resolved": bool((rec.get("grade") or {}).get("resolved")),
                                           "model": sub.get("model"), **submission_overlap(sub["diff"], gold[iid]["patch"])})
    return out
