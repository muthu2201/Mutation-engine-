"""Repair in real repositories (SWE-bench, ADR 0011): the pure parts.

- **Localisation** ranks a repository's source files, then the code snippets inside the best
  files, against an issue. It uses BM25 over identifier terms plus what the issue names
  directly: file paths, dotted module names, identifiers in backticks or calls, and traceback
  frames.
- **A snippet** is one function or method, or one run of class-level or module-level
  statements (class attributes, constants, imports). Fixes outside function bodies are
  therefore reachable.
- **A rewrite** replaces one snippet's lines, and must leave the file parseable.
- **A reproduction script** is written from the issue alone. It prints ``ISSUE REPRODUCED``,
  ``ISSUE RESOLVED`` or ``OTHER``.

Nothing here does I/O. The search loop is ``colloid.services.swebench``, and the judge is
``colloid_evaluator.swebench``.
"""

from __future__ import annotations

import ast
import math
import re
import textwrap
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from colloid.core.operators.llm_rewrite import extract_code_block

MAX_BLOCK_LINES = 60
ISSUE_CHARS = 6000
MARKERS = ("ISSUE REPRODUCED", "ISSUE RESOLVED", "OTHER")


# ----------------------------------------------------------------------------- snippets
@dataclass(frozen=True)
class Snippet:
    file: str
    name: str  # qualified: "slugify", "Field.clean", "UsernameValidator.<L12-14>", "<L1-30>"
    kind: str  # "function" | "block"
    start: int  # 1-based, inclusive
    end: int
    indent: str
    text: str  # dedented source

    @property
    def symbol_path(self) -> str:
        return f"py:{self.file}::{self.name}"

    @property
    def lines(self) -> int:
        return self.end - self.start + 1


def snippets(text: str, rel_path: str, max_block: int = MAX_BLOCK_LINES) -> list[Snippet]:
    """Every function/method, and every run of other statements, of one Python file."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    out: list[Snippet] = []

    def make(start: int, end: int, name: str, kind: str) -> Snippet:
        raw = "".join(lines[start - 1 : end])
        first = lines[start - 1]
        return Snippet(rel_path, name, kind, start, end, first[: len(first) - len(first.lstrip())], textwrap.dedent(raw))

    def flush(block: list[ast.stmt], prefix: str) -> None:
        if not block:
            return
        start, end = block[0].lineno, block[-1].end_lineno or block[-1].lineno
        for s in range(start, end + 1, max_block):
            e = min(s + max_block - 1, end)
            out.append(make(s, e, f"{prefix}<L{s}-{e}>", "block"))

    def visit(body: list[ast.stmt], prefix: str) -> None:
        block: list[ast.stmt] = []
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                flush(block, prefix)
                block = []
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}{node.name}.")
                else:
                    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                    out.append(make(start, node.end_lineno or node.lineno, f"{prefix}{node.name}", "function"))
            else:
                block.append(node)
        flush(block, prefix)

    visit(tree.body, "")
    return out


def splice(file_text: str, snip: Snippet, new_source: str) -> str:
    """Replace the snippet's lines with ``new_source`` re-indented to the snippet's indentation.
    Raises ``SyntaxError`` if the file no longer parses."""
    lines = file_text.splitlines(keepends=True)
    body = textwrap.indent(textwrap.dedent(new_source).strip("\n") + "\n", snip.indent, lambda ln: bool(ln.strip()))
    new_text = "".join(lines[: snip.start - 1]) + body + "".join(lines[snip.end :])
    ast.parse(new_text)
    return new_text


# ----------------------------------------------------------------------------- what the issue names
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_STOP = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "not", "but", "have", "has",
    "had", "you", "your", "its", "can", "will", "would", "should", "when", "what", "which", "there", "their",
    "them", "then", "than", "into", "also", "just", "like", "use", "used", "using", "get", "set", "self",
    "none", "true", "false", "def", "class", "return", "import", "print", "len", "str", "int", "list", "dict",
    "code", "issue", "error", "expected", "actual", "example",
})
_PATH = re.compile(r"[\w./-]*\w+\.py\b")
_DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")
_FRAME = re.compile(r'File "([^"]+\.py)", line \d+, in (\w+)')
_TICKED = re.compile(r"`{1,3}([^`\n]{1,80})`{1,3}")
_CALLED = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def terms(text: str) -> list[str]:
    """Lower-case identifier terms: whole identifiers and their snake/camel parts."""
    out: list[str] = []
    for w in _WORD.findall(text):
        low = w.lower()
        if len(low) >= 3 and low not in _STOP:
            out.append(low)
        parts = [p.lower() for chunk in w.split("_") for p in _CAMEL.findall(chunk)]
        if len(parts) > 1:
            out += [p for p in parts if len(p) >= 3 and p not in _STOP]
    return out


@dataclass(frozen=True)
class Mentions:
    paths: frozenset[str]  # "django/utils/text.py" or "text.py" as written
    dotted: frozenset[str]  # "django.utils.text.slugify", "models.Field"
    names: frozenset[str]  # identifiers in backticks, calls, dotted tails, CamelCase words
    frames: tuple[tuple[str, str], ...]  # (path, function) from tracebacks


def mentions(issue: str) -> Mentions:
    frames = tuple((p, fn) for p, fn in _FRAME.findall(issue))
    dotted = {d for d in _DOTTED.findall(issue) if not d.endswith(".py") and not re.fullmatch(r"[\d.]+", d)}
    names: set[str] = set()
    for t in _TICKED.findall(issue):
        names.update(w for w in _WORD.findall(t) if len(w) >= 3)
    names.update(n for n in _CALLED.findall(issue) if len(n) >= 3 and n.lower() not in _STOP)
    for d in dotted:
        names.update(p for p in d.split(".") if len(p) >= 3)
    for w in _WORD.findall(issue):  # CamelCase (ASCIIUsernameValidator, QuerySet) and snake_case identifiers
        if (sum(c.isupper() for c in w) >= 2 and any(c.islower() for c in w)) or ("_" in w.strip("_") and len(w) >= 5):
            names.add(w)
    names.update(fn for _, fn in frames)
    return Mentions(frozenset(_PATH.findall(issue)), frozenset(dotted), frozenset(n for n in names if n.lower() not in _STOP), frames)


# ----------------------------------------------------------------------------- BM25
class BM25:
    def __init__(self, docs: Sequence[Sequence[str]], k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avg = (sum(self.len) / len(docs)) if docs else 1.0
        df: Counter[str] = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: Iterable[str]) -> list[float]:
        q = Counter(query)
        out = []
        for tf, ln in zip(self.tf, self.len, strict=True):
            s = 0.0
            for t, qn in q.items():
                f = tf.get(t, 0)
                if f:
                    s += qn * self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            out.append(s)
        return out


def _normalise(xs: list[float]) -> list[float]:
    hi = max(xs, default=0.0)
    return [x / hi if hi > 0 else 0.0 for x in xs]


def module_of(path: str) -> str:
    mod = path[:-3] if path.endswith(".py") else path
    mod = mod.replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def rank_files(issue: str, files: dict[str, str], top: int = 8) -> list[tuple[str, float]]:
    """Source files (path → text) ranked against the issue."""
    m = mentions(issue)
    paths = sorted(files)
    bm = _normalise(BM25([terms(files[p]) for p in paths]).scores(terms(issue)))
    scored = []
    for p, s in zip(paths, bm, strict=True):
        mod = module_of(p)
        if any(p.endswith(q.lstrip("./")) for q in m.paths) or any(d == mod or d.startswith(mod + ".") for d in m.dotted):
            s += 3.0
        if any(fp.endswith(p) for fp, _ in m.frames):
            s += 2.0
        defined = set(re.findall(r"^\s*(?:def|class)\s+(\w+)", files[p], re.M))
        s += min(1.5, 0.5 * len(defined & m.names))
        scored.append((p, s))
    scored.sort(key=lambda x: -x[1])
    return scored[:top]


def rank_snippets(issue: str, ranked_files: Sequence[tuple[str, float]], files: dict[str, str], *, top: int = 6,
                  max_lines: int = 150) -> list[tuple[Snippet, float]]:
    """Snippets of the top files ranked against the issue (at most ``max_lines`` each)."""
    m = mentions(issue)
    cands: list[tuple[Snippet, float]] = []
    for path, fscore in ranked_files:
        try:
            cands += [(s, fscore) for s in snippets(files[path], path) if s.lines <= max_lines]
        except SyntaxError:
            continue
    if not cands:
        return []
    bm = _normalise(BM25([terms(s.text) for s, _ in cands]).scores(terms(issue)))
    out = []
    top_file = max(f for _, f in cands) or 1.0
    for (s, fscore), b in zip(cands, bm, strict=True):
        parts = s.name.split(".")
        score = b + 0.5 * fscore / top_file
        if s.kind == "function" and parts[-1] in m.names:
            score += 2.0
        if len(parts) > 1 and parts[-2] in m.names:
            score += 1.0
        if any(fp.endswith(s.file) and fn == parts[-1] for fp, fn in m.frames):
            score += 2.0
        out.append((s, score))
    out.sort(key=lambda x: -x[1])
    return out[:top]


# ----------------------------------------------------------------------------- prompts
SYSTEM_FIX = "You are an expert Python developer fixing a reported bug in a large open-source project. You change as little code as possible."
SYSTEM_REPRO = "You write minimal, standalone Python scripts that reproduce bug reports."


def _issue(issue: str) -> str:
    return issue if len(issue) <= ISSUE_CHARS else issue[:ISSUE_CHARS] + "\n[...]"


def fix_prompt(issue: str, snip: Snippet, context: str, *, think: bool) -> str:
    where = f"`{snip.file}`" + (f", inside `{snip.name.rsplit('.', 1)[0]}`" if "." in snip.name else "")
    ask = ("First explain in at most three sentences what is wrong, then give the code block."
           if think else "Reply with the code block only.")
    return (f"<issue>\n{_issue(issue)}\n</issue>\n\n"
            f"Context from {snip.file}:\n```python\n{context.strip()}\n```\n\n"
            f"This code from {where} may contain the bug:\n```python\n{snip.text.rstrip()}\n```\n\n"
            "Rewrite this code so that the issue is fixed. Return the complete replacement for exactly this code in "
            "one ```python block. Keep names, signatures and behaviour unrelated to the issue unchanged. " + ask)


def repro_prompt(issue: str, repo: str) -> str:
    django = ("\nIf the code needs Django settings, call django.conf.settings.configure(...) with the apps it needs and "
              "then django.setup() before using models." if repo == "django/django" else "")
    return (f"An issue was reported in the {repo} repository. The package is installed and importable.\n\n"
            f"<issue>\n{_issue(issue)}\n</issue>\n\n"
            "Write a standalone Python script that reproduces this issue. It must print exactly one of:\n"
            "ISSUE REPRODUCED - if the behaviour described in the issue occurs,\n"
            "ISSUE RESOLVED - if the expected behaviour occurs instead,\n"
            "OTHER - if anything else happens (wrap the check in try/except).{django}\n"
            "Return only the script, in one ```python block.").replace("{django}", django)


def file_context(file_text: str, snip: Snippet, limit: int = 40) -> str:
    """The file's imports, and for a member the enclosing class's header line."""
    lines = file_text.splitlines()
    head = [ln for ln in lines[: snip.start - 1] if ln.startswith(("import ", "from "))][:limit]
    if "." in snip.name:
        cls = snip.name.split(".")[-2]
        hdr = next((ln for ln in lines[: snip.start] if re.match(rf"\s*class\s+{re.escape(cls)}\b", ln)), None)
        if hdr:
            head += ["", hdr.strip(), "    ..."]
    return "\n".join(head) or "# (no imports)"


# ----------------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class Rewrite:
    ok: bool
    file_text: str = ""
    reason: str = ""


def _norm(src: str) -> str:
    return "\n".join(ln.rstrip() for ln in textwrap.dedent(src).strip().splitlines())


def parse_fix(response: str, snip: Snippet, file_text: str) -> Rewrite:
    block = extract_code_block(response, "python")
    if block is None:
        return Rewrite(False, reason="no code block in response")
    if _norm(block) == _norm(snip.text):
        return Rewrite(False, reason="identical to the original")
    n = len(block.strip("\n").splitlines())
    if n > 2 * snip.lines + 30:
        return Rewrite(False, reason=f"replacement has {n} lines for a {snip.lines}-line snippet")
    if snip.kind == "function":
        name = snip.name.split(".")[-1]
        try:
            tree = ast.parse(textwrap.dedent(block))
        except SyntaxError as exc:
            return Rewrite(False, reason=f"syntax error: {exc.msg} (line {exc.lineno})")
        if not any(isinstance(n_, (ast.FunctionDef, ast.AsyncFunctionDef)) and n_.name == name for n_ in tree.body):
            return Rewrite(False, reason=f"response does not define {name}()")
    try:
        return Rewrite(True, splice(file_text, snip, block))
    except SyntaxError as exc:
        return Rewrite(False, reason=f"file does not parse after the splice: {exc.msg} (line {exc.lineno})")


def parse_repro(response: str) -> str | None:
    block = extract_code_block(response, "python")
    if block is None or "ISSUE REPRODUCED" not in block or "ISSUE RESOLVED" not in block:
        return None
    try:
        ast.parse(block)
    except SyntaxError:
        return None
    return block


def repro_outcome(output: str) -> str:
    """The last marker line the script printed, or ``"NONE"``."""
    for line in reversed(output.splitlines()):
        if line.strip() in MARKERS:
            return line.strip()
    return "NONE"
