"""The search-time judge for one SWE-bench instance (L0–L3, ADR 0011).

A candidate is one edited file. The judge works through these stages, then resets the
repository:

- L0: the patch policy.
- L1: the edited file byte-compiles in the instance's environment.
- L2: the existing test files that exercise the edited module do not regress. Every test that
  passed at ``base_commit`` must still pass; the baseline is measured once per test set.
- L3: each validated reproduction script is run, and the candidate gets one vote for each
  ``ISSUE RESOLVED``.

Test logs are parsed by the official SWE-bench parser for the repository (``grader.py
parse-log``, run in the grader's virtualenv).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from colloid_evaluator.swebench import policy, repos
from colloid_evaluator.swebench.container import WORKDIR, Container

GRADER = Path(__file__).with_name("grader.py")
PASSED = "PASSED"
MARKERS = ("ISSUE REPRODUCED", "ISSUE RESOLVED", "OTHER")


def _outcome(output: str) -> str:
    for line in reversed(output.splitlines()):
        if line.strip() in MARKERS:
            return line.strip()
    return "NONE"


@dataclass
class Verdict:
    stage: str  # the first stage that rejected, or "L3" when all ran
    ok: bool  # passed L0-L2
    diff: str = ""
    reasons: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    tests_passed_base: int = 0
    votes: int = 0
    repro: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class RepairJudge:
    def __init__(self, container: Container, repo: str, version: str, *, grader_python: str, test_timeout: float = 300,
                 repro_timeout: float = 90, log: Callable[[str], None] = print, protocol: str = "v1") -> None:
        self.c, self.repo, self.version = container, repo, version
        # v2 (ADR 0014): transitive test selection, six files, each run on its own; a file that times out at
        # base_commit is left out of the comparison instead of turning a partial log into false regressions
        self.protocol = protocol
        self.timed_out_at_base: set[str] = set()
        self.grader_python, self.test_timeout, self.repro_timeout, self.log = grader_python, test_timeout, repro_timeout, log
        self.root: Path | None = None
        self._base: dict[tuple[str, ...], dict[str, str]] = {}

    # ------------------------------------------------------------------ setup
    def export(self, dest: Path) -> Path:
        self.c.export(dest)
        self.root = dest
        return dest

    # ------------------------------------------------------------------ tests
    def parse(self, log: str) -> dict[str, str]:
        res = subprocess.run([self.grader_python, str(GRADER), "parse-log", "--repo", self.repo, "--version", self.version],
                             input=log, capture_output=True, text=True, timeout=120, check=False, errors="replace")
        if res.returncode != 0:
            raise RuntimeError(f"log parser failed: {res.stderr[-500:]}")
        return dict(json.loads(res.stdout))

    def run_tests(self, files: Sequence[str], *, at_base: bool = False) -> tuple[dict[str, str], str]:
        if self.protocol == "v1":
            ex = self.c.run(repos.test_command(self.repo, files), timeout=self.test_timeout)
            return self.parse(ex.output), ex.output[-3000:] + ("\n[timed out]" if ex.timed_out else "")
        merged: dict[str, str] = {}
        tail = ""
        for f in files:
            if not at_base and f in self.timed_out_at_base:
                continue
            ex = self.c.run(repos.test_command(self.repo, [f]), timeout=self.test_timeout)
            if ex.timed_out and at_base:
                self.timed_out_at_base.add(f)
                continue
            merged.update(self.parse(ex.output))
            tail = ex.output[-3000:] + ("\n[timed out]" if ex.timed_out else "")
        return merged, tail

    def baseline(self, files: Sequence[str]) -> dict[str, str]:
        key = tuple(files)
        if key not in self._base:
            self.c.reset()
            self._base[key], _ = self.run_tests(files, at_base=True)
        return self._base[key]

    def tests_for(self, edited: Sequence[str]) -> list[str]:
        assert self.root is not None
        if self.protocol == "v1":
            return repos.select_tests(self.root, edited)
        return repos.select_tests(self.root, edited, limit=6, transitive=True)

    # ------------------------------------------------------------------ reproduction scripts
    def run_repro(self, script: str) -> str:
        path = f"/tmp/repro_{hashlib.sha256(script.encode()).hexdigest()[:12]}.py"
        self.c.write(path, script)
        return _outcome(self.c.run(f"python {path}", timeout=self.repro_timeout).output)

    def validate_repro(self, script: str) -> str:
        """The script's outcome at ``base_commit`` (a script is usable only if it is REPRODUCED)."""
        self.c.reset()
        return self.run_repro(script)

    # ------------------------------------------------------------------ one candidate
    def evaluate(self, rel: str, new_text: str, scripts: Sequence[str]) -> Verdict:
        t0 = time.monotonic()
        try:
            v = self._evaluate(rel, new_text, scripts)
        finally:
            self.c.reset()
        v.seconds = round(time.monotonic() - t0, 1)
        return v

    def _evaluate(self, rel: str, new_text: str, scripts: Sequence[str]) -> Verdict:
        tests = self.tests_for([rel])
        base = self.baseline(tests) if tests else {}
        self.c.reset()
        self.c.write(f"{WORKDIR}/{rel}", new_text)
        diff = self.c.diff()
        v = Verdict("L0", False, diff=diff, tests=tests, tests_passed_base=sum(1 for s in base.values() if s == PASSED))
        v.reasons = policy.check_patch(diff)
        if v.reasons:
            return v
        comp = self.c.run(f"python -m py_compile '{rel}'", timeout=120)
        if comp.code != 0:
            v.stage, v.reasons = "L1", [comp.output.strip()[-400:]]
            return v
        if tests:
            cand, tail = self.run_tests(tests)
            v.regressions = sorted(t for t, s in base.items() if s == PASSED and cand.get(t) != PASSED)
            if v.regressions or (base and not cand):
                v.stage = "L2"
                v.reasons = ([f"{len(v.regressions)} test(s) that passed at base_commit now fail"] if v.regressions
                             else ["the test run produced no parseable results: " + tail[-300:]])
                return v
        v.stage, v.ok = "L3", True
        v.repro = [self.run_repro(s) for s in scripts]
        v.votes = sum(1 for r in v.repro if r == "ISSUE RESOLVED")
        return v
