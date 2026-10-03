"""Repository knowledge the judge needs: how to run a repo's tests, and which tests exercise a module.

The test commands are per repository, not per instance (checked against every sampled
instance's eval script by ``grader.py prepare``), so the search never learns which test
module an instance is graded on.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from colloid_evaluator.swebench.policy import is_test_path


@dataclass(frozen=True)
class RepoSpec:
    test_cmd: str
    labels: str = "path"  # "path": test files as paths; "django": dotted labels under tests/


PYTEST = RepoSpec("pytest -rA")
REPOS: dict[str, RepoSpec] = {
    "django/django": RepoSpec("./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1", "django"),
    "sympy/sympy": RepoSpec("PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose"),
    "sphinx-doc/sphinx": RepoSpec("tox --current-env -epy39 -v --"),
    "mwaskom/seaborn": RepoSpec("pytest --no-header -rA"),
}


def spec(repo: str) -> RepoSpec:
    return REPOS.get(repo, PYTEST)


def test_target(repo: str, path: str) -> str:
    """The argument that runs one test file: a path, or Django's dotted label."""
    if spec(repo).labels == "django":
        rel = path[len("tests/"):] if path.startswith("tests/") else path
        return rel[:-3].replace("/", ".") if rel.endswith(".py") else rel
    return path


def test_command(repo: str, files: Iterable[str]) -> str:
    return " ".join([spec(repo).test_cmd, *(test_target(repo, f) for f in files)])


def module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def _module_patterns(paths: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for p in paths:
        mod = module_name(p)
        pkg, _, leaf = mod.rpartition(".")
        pats = [re.escape(mod)]
        if pkg:
            pats.append(rf"from\s+{re.escape(pkg)}\s+import\s+[^\n]*\b{re.escape(leaf)}\b")
        out.append((leaf, re.compile("|".join(pats))))
    return out


def importers(root: Path, edited: Iterable[str], limit: int = 20) -> list[str]:
    """Non-test source files that import an edited module (one hop of the import graph), most references first."""
    targets = _module_patterns(edited)
    edited_set = set(edited)
    scored: list[tuple[int, str]] = []
    for f in root.rglob("*.py"):
        rel = f.relative_to(root).as_posix()
        if rel in edited_set or is_test_path(rel) or "/." in "/" + rel:
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        n = sum(len(rx.findall(text)) for _, rx in targets)
        if n:
            scored.append((n, rel))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [rel for _, rel in scored[:limit]]


def select_tests(root: Path, edited: Iterable[str], limit: int = 3, transitive: bool = False) -> list[str]:
    """Existing test files most likely to exercise the edited modules, by how often they name them.

    A test file scores for each reference to an edited module, as its dotted name or as an
    import of the module from its package, and gets a bonus when it is ``test_<module>.py``.
    With ``transitive`` (protocol v2), references to modules that import an edited module score
    a quarter each, so the tests of code that *uses* the edited module are selected too.
    """
    edited = list(edited)
    targets = _module_patterns(edited)
    indirect = _module_patterns(importers(root, edited)) if transitive else []
    scored: list[tuple[float, str]] = []
    for f in root.rglob("*.py"):
        rel = f.relative_to(root).as_posix()
        if not is_test_path(rel) or not f.name.startswith("test") or "/." in "/" + rel:
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        s = 0.0
        for leaf, rx in targets:
            s += len(rx.findall(text))
            if f.name == f"test_{leaf}.py":
                s += 5.0
        for _, rx in indirect:
            s += 0.25 * len(rx.findall(text))
        if s > 0:
            scored.append((s, rel))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [rel for _, rel in scored[:limit]]
