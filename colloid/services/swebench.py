"""Colloid on SWE-bench (ADR 0011): repair real issues in real repositories, judged by a holdout
the search never sees.

For each pre-registered instance:

1. Pull the instance image and start its container (network off).
2. Export the repository and localise. Rank files, then snippets (functions, and class- or
   module-level blocks), against the issue text.
3. Write reproduction scripts from the issue. Keep those that print ``ISSUE REPRODUCED`` at
   ``base_commit``.
4. Spend the budget on rewrites. A Thompson bandit chooses the (model × prompt) arm, and the
   snippets are visited in rank order. The judge (L0–L3) scores every candidate.
5. Submit the best candidate that passed L0–L2: most reproduction votes, then smallest diff.
6. Grade it with the official harness (``colloid_evaluator/swebench/grader.py``), then diagnose
   localisation against the gold patch. Both happen after the search is over.

The search reads ``tasks.jsonl`` only. ``gold.jsonl`` is passed to the grader subprocess by
path, and no line of this module opens it.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import random
import subprocess
import textwrap
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from colloid.core import repair
from colloid.core.bandit import ThompsonBandit
from colloid.core.lake import Record, append, head, verify_chain
from colloid.ports import LakeStore, LLMError
from colloid.services.lake import utc_now
from colloid_evaluator.swebench import container as ctr
from colloid_evaluator.swebench.judge import GRADER, RepairJudge, Verdict
from colloid_evaluator.swebench.policy import changed_lines, is_test_path

SKIP_DIRS = ("docs/", "doc/", "examples/", "example/", "benchmarks/", "asv_bench/", "build/", ".tox/", "extern/")


@dataclass(frozen=True)
class Budget:
    search_s: float = 1200.0
    llm_calls: int = 16
    repro_calls: int = 2
    snippets: int = 6
    snippet_max_lines: int = 150
    test_timeout_s: float = 300.0
    max_tokens_extra: int = 0  # added to every request's cap; a hosted reasoning model spends tokens before it answers


def load_tasks(path: Path) -> dict[str, dict[str, Any]]:
    tasks = {}
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            leaked = {"patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "eval_script", "hints_text"} & set(row)
            if leaked:
                raise ValueError(f"tasks file carries gold fields {sorted(leaked)}; the search must not see them")
            tasks[row["instance_id"]] = row
    return tasks


def source_files(root: Path) -> dict[str, str]:
    out = {}
    for f in root.rglob("*.py"):
        rel = f.relative_to(root).as_posix()
        if is_test_path(rel) or rel.startswith(SKIP_DIRS) or "/." in "/" + rel:
            continue
        try:
            out[rel] = f.read_text(errors="replace")
        except OSError:
            continue
    return out


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Repairer:
    """The search for one instance; ``llm.complete`` is any Colloid LLM provider."""

    def __init__(self, llm: Any, models: Sequence[str], budget: Budget, *, seed: int = 0, log: Callable[[str], None] = print) -> None:
        self.llm, self.models, self.budget, self.log = llm, list(models), budget, log
        self.rng = random.Random(seed)
        self.calls = 0
        self.tokens = [0, 0]

    def _ask(self, model: str, system: str, prompt: str, *, temperature: float, max_tokens: int) -> str:
        self.calls += 1
        c = self.llm.complete(model, system, [{"role": "user", "content": prompt}], max_tokens=max_tokens, temperature=temperature,
                              timeout_s=600.0)
        self.tokens[0] += c.tokens_in
        self.tokens[1] += c.tokens_out
        return str(c.text)

    def solve(self, task: dict[str, Any], judge: RepairJudge, root: Path) -> dict[str, Any]:
        t0 = time.monotonic()
        b = self.budget
        issue, repo = task["problem_statement"], task["repo"]
        files = source_files(root)
        ranked_files = repair.rank_files(issue, files)
        ranked = repair.rank_snippets(issue, ranked_files, files, top=b.snippets, max_lines=b.snippet_max_lines)
        rec: dict[str, Any] = {
            "files_considered": len(files),
            "ranked_files": [{"file": f, "score": round(s, 3)} for f, s in ranked_files],
            "localised": [{"file": s.file, "name": s.name, "kind": s.kind, "start": s.start, "end": s.end, "score": round(sc, 3)}
                          for s, sc in ranked],
            "repro": [], "candidates": [],
        }
        if not ranked:
            rec["stop"] = "nothing localised"
            return rec

        # reproduction scripts, validated at base_commit
        scripts: list[str] = []
        for i in range(b.repro_calls):
            model = self.models[i % len(self.models)]
            text = self._ask(model, repair.SYSTEM_REPRO, repair.repro_prompt(issue, repo), temperature=0.7, max_tokens=900 + b.max_tokens_extra)
            script = repair.parse_repro(text)
            outcome = judge.validate_repro(script) if script else "unparseable"
            rec["repro"].append({"model": model, "outcome": outcome, "script_hash": _h(script) if script else None})
            if outcome == "ISSUE REPRODUCED" and script:
                scripts.append(script)
        rec["validated_repro"] = len(scripts)

        bandit = ThompsonBandit(prior_mean=0.3, prior_sd=0.3, clip_hi=1.0, default_cost=120.0)
        arms = [("repair_rewrite", m, t) for m in self.models for t in ("fix", "fix_think")]
        for a in arms:
            bandit.add_arm(a)
        best: tuple[tuple[int, int], dict[str, Any]] | None = None
        visit = 0
        while self.calls < b.llm_calls and time.monotonic() - t0 < b.search_s:
            snip, _ = ranked[visit % len(ranked)]
            first_pass = visit < len(ranked)
            visit += 1
            arm = bandit.select(("repair",), self.rng)
            _, model, template = arm
            assert model is not None
            file_text = files[snip.file]
            prompt = repair.fix_prompt(issue, snip, repair.file_context(file_text, snip), think=template == "fix_think")
            ts = time.monotonic()
            try:
                text = self._ask(model, repair.SYSTEM_FIX, prompt, temperature=0.2 if first_pass else 0.8,
                                 max_tokens=min(2400, 40 * snip.lines + 400) + b.max_tokens_extra)
            except LLMError as exc:
                rec["candidates"].append({"snippet": snip.symbol_path, "model": model, "template": template, "stage": "llm", "reason": str(exc)[:300]})
                bandit.update(("repair",), arm, 0.0, cost=time.monotonic() - ts)
                continue
            rw = repair.parse_fix(text, snip, file_text)
            cand: dict[str, Any] = {"snippet": snip.symbol_path, "model": model, "template": template, "prompt_hash": _h(prompt),
                                    "response_hash": _h(text)}
            verdict: Verdict | None = None
            if not rw.ok:
                cand.update(stage="parse", reason=rw.reason)
            else:
                verdict = judge.evaluate(snip.file, rw.file_text, scripts)
                cand.update(stage=verdict.stage, ok=verdict.ok, reasons=verdict.reasons[:3], tests=verdict.tests,
                            regressions=len(verdict.regressions), votes=verdict.votes, repro=verdict.repro,
                            changed_lines=changed_lines(verdict.diff), judge_s=verdict.seconds)
                if verdict.ok:
                    cand.update(diff=verdict.diff, new_source=_new_snippet(file_text, rw.file_text, snip))
            reward = 0.0
            if verdict is not None and verdict.ok:
                reward = 1.0 if (verdict.votes > 0 or not scripts) else 0.3
            bandit.update(("repair",), arm, reward, cost=time.monotonic() - ts)
            rec["candidates"].append(cand)
            self.log(f"  [{self.calls:>2}] {model}/{template} {snip.name[:40]}: {cand.get('stage')} "
                     f"{'ok' if cand.get('ok') else (cand.get('reason') or (cand.get('reasons') or [''])[0])[:90]}"
                     + (f" votes {verdict.votes}/{len(scripts)}" if verdict is not None and verdict.ok else ""))
            if verdict is not None and verdict.ok:
                key = (verdict.votes, -changed_lines(verdict.diff))
                if best is None or key > best[0]:
                    best = (key, cand)
                if scripts and verdict.votes == len(scripts):
                    rec["stop"] = "every validated reproduction resolved"
                    break
        rec.setdefault("stop", "budget")
        rec["submission"] = None if best is None else {k: best[1][k] for k in ("snippet", "model", "template", "diff", "votes", "changed_lines",
                                                                               "new_source", "prompt_hash", "response_hash")}
        rec["search_s"] = round(time.monotonic() - t0, 1)
        rec["llm_calls"], rec["tokens_in"], rec["tokens_out"] = self.calls, self.tokens[0], self.tokens[1]
        rec["arms"] = {f"{m}/{t}": bandit.posterior(("repair",), (o, m, t))[0] for o, m, t in arms}
        return rec


def _new_snippet(old_text: str, new_text: str, snip: repair.Snippet) -> str:
    """The rewritten snippet as it stands in the new file: same start line, length shifted by the edit."""
    n = snip.lines + len(new_text.splitlines()) - len(old_text.splitlines())
    return textwrap.dedent("\n".join(new_text.splitlines()[snip.start - 1 : snip.start - 1 + n]) + "\n")


# ---------------------------------------------------------------------- the run
def _grade(grader_python: str, gold: Path, task: dict[str, Any], diff: str, image: str, out: Path) -> dict[str, Any]:
    patch = out / "submission.diff"
    patch.write_text(diff)
    res = subprocess.run([grader_python, str(GRADER), "grade", "--gold", str(gold), "--instance", task["instance_id"], "--patch", str(patch),
                          "--image", image, "--out", str(out / "report.json"), "--run-id", "colloid"],
                         capture_output=True, text=True, timeout=3600, check=False, errors="replace")
    if res.returncode != 0:
        return {"error": res.stderr[-1500:]}
    report = json.loads((out / "report.json").read_text()).get(task["instance_id"], {})
    status = report.get("tests_status") or {}
    return {"resolved": bool(report.get("resolved")), "applied": bool(report.get("patch_successfully_applied")),
            "fail_to_pass": {k: len(v) for k, v in (status.get("FAIL_TO_PASS") or {}).items()},
            "pass_to_pass": {k: len(v) for k, v in (status.get("PASS_TO_PASS") or {}).items()}}


def _diagnose(grader_python: str, gold: Path, record_path: Path) -> dict[str, Any]:
    res = subprocess.run([grader_python, str(GRADER), "diagnose", "--gold", str(gold), "--record", str(record_path)],
                         capture_output=True, text=True, timeout=120, check=False)
    return json.loads(res.stdout) if res.returncode == 0 else {"error": res.stderr[-500:]}


def run(sample: Sequence[str], tasks: dict[str, dict[str, Any]], out_dir: Path, llm: Any, models: Sequence[str], *, gold: Path,
        grader_python: str, budget: Budget | None = None, keep_images: bool = False, log: Callable[[str], None] = print,
        pace: Callable[[int], None] | None = None) -> list[dict[str, Any]]:
    budget = budget or Budget()
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    done = {json.loads(ln)["instance_id"] for ln in results_path.read_text().splitlines()} if results_path.exists() else set()
    results = []
    for n, iid in enumerate(sample, 1):
        if iid in done:
            continue
        task = tasks[iid]
        if pace is not None:  # a rate-limited hosted model: wait until a whole instance's budget is available
            pace(budget.llm_calls)
        inst_dir = out_dir / iid
        inst_dir.mkdir(exist_ok=True)
        log(f"[{n}/{len(sample)}] {iid}")
        rec: dict[str, Any] = {"instance_id": iid, "repo": task["repo"], "version": task["version"], "base_commit": task["base_commit"],
                               "image": task["image"], "started_at": datetime.datetime.now(datetime.UTC).isoformat()}
        t0 = time.monotonic()
        try:
            rec["image_digest"] = ctr.pull(task["image"])
            rec["pull_s"] = round(time.monotonic() - t0, 1)
            box = ctr.Container(task["image"], f"colloid-swe-{iid.lower()}")
            box.start()
            try:
                judge = RepairJudge(box, task["repo"], task["version"], grader_python=grader_python, test_timeout=budget.test_timeout_s, log=log)
                root = judge.export(inst_dir / "repo")
                rec.update(Repairer(llm, models, budget, seed=n, log=log).solve(task, judge, root))
            finally:
                box.stop()
                subprocess.run(["rm", "-rf", str(inst_dir / "repo")], check=False)
            sub = rec.get("submission")
            if sub:
                tg = time.monotonic()
                rec["grade"] = _grade(grader_python, gold, task, sub["diff"], task["image"], inst_dir)
                rec["grade_s"] = round(time.monotonic() - tg, 1)
            else:
                rec["grade"] = {"resolved": False, "submitted": False}
        except Exception as exc:  # an instance's infrastructure failure is recorded, never fatal to the run
            rec["error"] = f"{type(exc).__name__}: {exc}"[:2000]
            rec.setdefault("grade", {"resolved": False, "submitted": False})
        finally:
            if not keep_images:
                ctr.remove_image(task["image"])
        rec["wall_s"] = round(time.monotonic() - t0, 1)
        (inst_dir / "record.json").write_text(json.dumps(rec, indent=2))
        rec["diagnosis"] = _diagnose(grader_python, gold, inst_dir / "record.json")
        (inst_dir / "record.json").write_text(json.dumps(rec, indent=2))
        with results_path.open("a") as fh:
            fh.write(json.dumps({k: v for k, v in rec.items() if k not in ("candidates",)}) + "\n")
        g = rec.get("grade") or {}
        log(f"  -> {'RESOLVED' if g.get('resolved') else 'not resolved'}"
            f" ({'submitted' if rec.get('submission') else 'no submission'}; {rec.get('error', '')[:120]})")
        results.append(rec)
    return results


# ---------------------------------------------------------------------- the lake
def ingest(out_dir: Path, lake: LakeStore, *, probes: dict[str, Any] | None = None, recorded_at: str | None = None,
           log: Callable[[str], None] = print) -> int:
    """Resolved fixes become lake records: one gene (the rewritten snippet at its locus) and one
    program carrying the official grade as evidence. Unresolved submissions stay out.

    ``probes`` is a ``colloid swebench probe`` report (ADR 0011 addendum). When given, each
    program also records the memorisation verdict for the model that produced the fix, so a later
    run that reuses the gene knows whether it may have been recalled rather than found."""
    verdicts = {(p["instance_id"], p["model"]): p for p in (probes or {}).get("probes", [])}
    overlaps = {o["instance_id"]: o for o in (probes or {}).get("submissions", [])}
    existing, entries = lake.records(), lake.entries()
    verify_chain(entries, existing)
    order: list[Record] = []
    for line in (out_dir / "results.jsonl").read_text().splitlines():
        r = json.loads(line)
        sub, g = r.get("submission"), r.get("grade") or {}
        if not sub or not g.get("resolved"):
            continue
        file_, _, name = sub["snippet"][3:].partition("::")
        target = f"swebench:{r['repo']}"
        gene = Record.make("gene", {
            "target": target,
            "locus": {"unit_id": _h(f"{r['repo']}:{sub['snippet']}"), "unit": name, "symbol_path": f"py:{r['repo']}/{file_}::{name}",
                      "surface": "code_region", "layer": "app", "kind": "block" if "<L" in name else "function", "language": "python"},
            "payload_kind": "source",
            "payload": {"source": sub["new_source"], "diff": sub["diff"], "base_commit": r["base_commit"], "language": "python"},
            "explain": f"{r['instance_id']}: rewrite of {name} in {file_} that resolves the issue (official SWE-bench grader)",
            "provenance": {"operator": "repair_rewrite", "model": sub["model"], "template": sub["template"], "prompt_hash": sub["prompt_hash"],
                           "response_hash": sub["response_hash"], "notes": ""},
        })
        program = Record.make("program", {
            "target": target, "instance_id": r["instance_id"], "base_commit": r["base_commit"], "genes": [gene.id], "status": "verified",
            "effects": {"resolved": True, "fail_to_pass": g.get("fail_to_pass"), "pass_to_pass": g.get("pass_to_pass")},
            "holdout": {"grader": "swebench run_instance (official)", "image": r.get("image_digest"), "reproduction_votes": sub.get("votes")},
            "attribution": [{"gene": gene.id, "method": "official_grade", "value": 1.0, "ci": [1.0, 1.0]}],
            "platform": {"image": r.get("image_digest")}, "run": out_dir.name,
        })
        if probes is not None:
            pr, ov = verdicts.get((r["instance_id"], sub["model"])), overlaps.get(r["instance_id"], {})
            program = Record.make("program", {**program.content, "memorisation_probe": {
                "rule": "ADR 0011 addendum", "verdict": pr["verdict"] if pr else "not probed",
                **({k: pr[k] for k in ("path_hit", "path_mentioned_in_issue", "task_id_overlap", "task_id_exact_lines")} if pr else {}),
                "submission_overlap5": ov.get("overlap5"), "submission_identical_to_gold": ov.get("identical_added_lines")}})
        order += [rec for rec in (gene, program) if rec.id not in existing]
    new_entries = append(entries, order, recorded_at or utc_now())
    if new_entries:
        lake.commit(order, new_entries, head(entries), f"lake: +{len(new_entries)} records from SWE-bench run {out_dir.name}")
    log(f"lake {lake.location}: +{len(new_entries)} entries")
    return len(new_entries)
