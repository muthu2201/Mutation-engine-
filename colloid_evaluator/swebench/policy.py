"""L0 for repairs: what a submitted patch may touch (ADR 0011)."""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHANGED_LINES = 200
TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*\.py$|_tests?\.py$|(^|/)conftest\.py$")


@dataclass(frozen=True)
class FileChange:
    path: str
    new: bool = False
    deleted: bool = False


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def changes(diff: str) -> list[FileChange]:
    out: list[FileChange] = []
    for block in re.split(r"^diff --git ", diff, flags=re.M)[1:]:
        head = block.split("\n", 1)[0]
        m = re.match(r"a/(\S+) b/(\S+)", head)
        if not m:
            continue
        out.append(FileChange(m.group(2), new="\nnew file mode" in block[:300], deleted="\ndeleted file mode" in block[:300]))
    return out


def changed_lines(diff: str) -> int:
    return sum(1 for ln in diff.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---")))


def check_patch(diff: str, max_lines: int = MAX_CHANGED_LINES) -> list[str]:
    """Violations of the repair patch policy; empty means the patch may be judged further."""
    files = changes(diff)
    if not files:
        return ["empty patch"]
    out = []
    for f in files:
        if not f.path.endswith(".py"):
            out.append(f"{f.path}: only Python source files may change")
        if is_test_path(f.path):
            out.append(f"{f.path}: tests are the judge's, not the candidate's")
        if f.deleted:
            out.append(f"{f.path}: deletes a file")
    if "GIT binary patch" in diff:
        out.append("binary patch")
    n = changed_lines(diff)
    if n > max_lines:
        out.append(f"{n} changed lines (cap {max_lines})")
    return out
