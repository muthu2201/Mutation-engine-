"""Cascade stage L0: static validity and security policy (blueprint A8 L0, D2.4).

The evaluator re-checks every gene itself - it never trusts the operator that produced it.
Checks, cheapest first:

* **Locus validity** - the locus exists in the Atlas and is not frozen (tests, oracles and
  frozen knobs such as ``fsync``/``synchronous_commit`` are not mutable loci, so "edit the
  tests" is impossible by construction and "turn off durability" is rejected here).
* **Knob genes** - value type and safe range; requirements met (no inert genes).
* **Code genes** - locus confinement (exactly one function, same name, same signature, same
  decorators, same sync/async), a diff-size cap, licence policy, and an AST policy scan:

  - no imports outside an allow-list (no ``os``/``sys``/``time``/``subprocess``/``socket``/
    ``threading``/``ctypes``/``gc``/``importlib``/``psycopg``...)
  - no ``open``/``exec``/``eval``/``compile``/``__import__``/``globals``/``vars``/``setattr``/
    ``delattr``/``breakpoint``
  - no introspection escape hatches (``__dict__``, ``__globals__``, ``__code__``,
    ``__builtins__``, ``__subclasses__``, frame attributes ...)
  - no ``global``/``nonlocal``, no attribute assignment, no mutation of anything that is not
    a local variable created in this call - i.e. **no state that outlives a request**, the
    root of result-caching hacks
  - no caching decorators/helpers (``lru_cache``, ``cache``), no background execution
    (``create_task``, ``ensure_future``, ``run_in_executor``, ``call_later``...), no event-loop
    introspection (``all_tasks``, ``current_task``) that could detect "am I being timed?"
  - no string constants naming evaluator or system paths

  For C genes: no calls to process/file/network/dynamic-loading APIs, no inline assembly,
  no constructors, no static mutable state.

  For Go genes (``gopolicy/gopolicy.go``, the Go toolchain's own parser): the same confinement
  and the same "no state that outlives a request" rule - no goroutines, channels, timers or
  clocks, no forbidden packages, no writes to package-level variables or through arguments,
  no introspection of the connection pool.

* **SQL in genes** (every language): every string constant of a gene (constant
  concatenations folded) is checked by :func:`sql_violations`. A statement must be plain DML
  (SELECT/INSERT/UPDATE/DELETE/WITH/VALUES), one statement, no DDL, temporary tables or
  transaction control, no ``SELECT ... INTO``, and no server functions that keep or reveal
  state (``set_config``, ``current_setting``, ``pg_*`` such as ``pg_sleep`` or advisory locks).
  Per-session settings and temp tables on a *pooled* connection outlive the request: they
  are a cross-request cache that no Python or Go scan of the host code can see. The
  evaluator also audits, dynamically, the statements a candidate actually sent
  (``oracles.sql_audit``), which catches SQL assembled at run time.

Rejections carry a precise reason; the engine feeds it back to the LLM as a failed-attempt
summary so the next proposal does not repeat it.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from colloid.core.atlas import StackAtlas
from colloid.core.genome import Genome, LocusConflict
from colloid.core.ids import sha256_hex
from colloid.core.knobs import KnobSpec, requirement_met, validate_value
from colloid.core.models import Gene, Mutability, PayloadKind, UnitKind

ALLOWED_IMPORTS = {
    "re", "math", "datetime", "json", "collections", "itertools", "operator", "heapq", "bisect", "decimal",
    "statistics", "string", "functools", "shop", "shop.util", "shop.native", "shop.search", "shop.handlers",
}
FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "__import__", "globals", "vars", "setattr", "delattr", "breakpoint", "input", "memoryview"}
FORBIDDEN_ATTRS = {
    "__dict__", "__globals__", "__code__", "__builtins__", "__subclasses__", "__getattribute__", "__closure__", "__class__",
    "__bases__", "__mro__", "__module__", "__defaults__", "__kwdefaults__", "f_back", "f_globals", "f_locals", "gi_frame",
    "cr_frame", "tb_frame", "__reduce__", "__setattr__", "__delattr__",
    "lru_cache", "cache", "cached_property", "create_task", "ensure_future", "run_in_executor", "call_later", "call_soon",
    "call_at", "all_tasks", "current_task", "get_running_loop", "get_event_loop", "to_thread", "shield",
}
MUTATING_METHODS = {"append", "extend", "insert", "update", "setdefault", "pop", "popitem", "clear", "add", "discard",
                    "remove", "sort", "reverse", "__setitem__", "__delitem__", "__iadd__"}
FORBIDDEN_STRINGS = re.compile(r"(/opt/colloid|colloid_evaluator|/proc/|/sys/|/etc/|\.ssh|superuser\.pw)")
ALLOWED_LICENSES = {"Apache-2.0", "MIT", "BSD-3-Clause"}
MAX_DIFF_LINES = 160
# The judge: everything that decides whether a candidate is correct, how fast it is, and what
# Colloid remembers as verified. None of it is ever a mutable locus, for any target, and that
# includes a future target that is Colloid's own search code. An optimiser that can edit its
# grader will, so this list lives here, on the evaluator side, beyond the search side's reach.
JUDGE_PATHS = (
    "colloid_evaluator/",            # cascade, oracles, policy, benchmark protocol, canaries, workloads
    "tests/", "stress/",              # the checks on the checks
    "colloid/core/stats.py",          # every CI, p-value and the A/A noise floor
    "colloid/core/lake.py",           # the verified-knowledge ledger (forged evidence)
    "colloid/adapters/sandbox/",      # the security boundary
    "colloid/adapters/bench/",        # the load generator (the stopwatch)
    "colloid/adapters/platform.py",   # CPU accounting / pinning fallbacks
)


def is_judge_path(symbol_path: str) -> bool:
    """``py:colloid_evaluator/policy.py::scan_python`` -> True."""
    file = symbol_path.split(":", 1)[-1].split("::", 1)[0]
    return any(file == p or file.startswith(p) for p in JUDGE_PATHS)


def judge_violations(atlas: StackAtlas) -> list[str]:
    """Atlas units inside the judge that are not frozen (a target adapter bug: refuse to run)."""
    out = []
    for loc in atlas.loci.values():
        unit = atlas.units[loc.unit_id]
        if is_judge_path(unit.symbol_path) and loc.mutability != Mutability.FROZEN:
            out.append(f"{unit.symbol_path} ({loc.surface.value}) is part of the judge but mutable")
    return out
C_FORBIDDEN = re.compile(
    r"\b(system|popen|exec[lv]p?e?|fork|vfork|clone|socket|connect|fopen|open|openat|dlopen|dlsym|mmap|mprotect|syscall|"
    r"pthread_create|signal|sigaction|kill|ptrace|getenv|setenv|abort|exit|_exit|longjmp|setjmp)\s*\("
)


# ---------------------------------------------------------------------------- SQL
SQL_FIRST_WORD = re.compile(r"^[\s(]*([A-Za-z]+)(?=\s|;|$)")  # a keyword followed by whitespace, not "create_order" or "listen: %v"
SQL_STATEMENT_WORDS = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "VALUES", "TABLE", "MERGE", "CREATE", "DROP", "ALTER", "TRUNCATE", "SET", "RESET",
    "SHOW", "DO", "COPY", "LOCK", "PREPARE", "EXECUTE", "DEALLOCATE", "DISCARD", "LISTEN", "NOTIFY", "UNLISTEN", "BEGIN", "COMMIT",
    "ROLLBACK", "SAVEPOINT", "RELEASE", "START", "END", "ABORT", "VACUUM", "ANALYZE", "CALL", "GRANT", "REVOKE", "EXPLAIN",
    "DECLARE", "FETCH", "MOVE", "CLOSE", "REFRESH", "CLUSTER", "REINDEX", "COMMENT", "SECURITY", "IMPORT", "LOAD", "CHECKPOINT",
}
SQL_ALLOWED_FIRST = {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "VALUES"}
SQL_STATE_FUNCTIONS = re.compile(r"\b(set_config|current_setting|pg_[a-z0-9_]+|dblink[a-z_]*|lo_[a-z_]+)\s*\(", re.I)
SQL_FORBIDDEN_WORDS = re.compile(r"\b(CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|COPY|LISTEN|NOTIFY|UNLISTEN|PREPARE|DEALLOCATE|DISCARD|"
                                 r"VACUUM|CLUSTER|REINDEX|REFRESH|TEMP|TEMPORARY|UNLOGGED|SAVEPOINT)\b", re.I)


def sql_violations(text: str) -> list[str]:
    """Policy violations of one string constant (or one statement a candidate sent)."""
    reasons = []
    m = SQL_STATE_FUNCTIONS.search(text)
    if m:
        reasons.append(f"sql: server function {m.group(1)}() is not allowed (session state, timing or server introspection)")
    first = SQL_FIRST_WORD.match(text)
    word = first.group(1).upper() if first else ""
    if word not in SQL_STATEMENT_WORDS:
        return reasons
    if word not in SQL_ALLOWED_FIRST:
        reasons.append(f"sql: {word} statements are not allowed (only SELECT/INSERT/UPDATE/DELETE/WITH/VALUES)")
    if ";" in text.rstrip().rstrip(";"):
        reasons.append("sql: one statement per query (no ';')")
    bad = SQL_FORBIDDEN_WORDS.search(text)
    if bad:
        reasons.append(f"sql: {bad.group(1).upper()} is not allowed in a gene's SQL")
    into = re.search(r"\bINTO\b", text, re.I) is not None
    if (word == "SELECT" and into) or (word == "WITH" and into and not re.search(r"\bINSERT\s+INTO\b", text, re.I)):
        reasons.append("sql: SELECT ... INTO creates a table and is not allowed")
    return reasons


def python_strings(source: str) -> list[str]:
    """Every string constant of a Python function, with constant ``+`` concatenations folded."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return []

    def fold(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            return left + right if left is not None and right is not None else None
        return None

    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp):
            folded = fold(node)
            if folded is not None:
                out.append(folded)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def scan_sql(strings: Sequence[str]) -> list[str]:
    reasons: list[str] = []
    for text in strings:
        reasons += sql_violations(text)
    return sorted(set(reasons))


# ---------------------------------------------------------------------------- Go
GOPOLICY_SRC = Path(__file__).with_name("gopolicy") / "gopolicy.go"
_GOPOLICY: Path | None = None


def gopolicy_tool() -> Path:
    """Build the Go policy scanner from this package's own source (cached by source hash)."""
    global _GOPOLICY
    if _GOPOLICY is not None and _GOPOLICY.exists():
        return _GOPOLICY
    go = shutil.which("go") or "/usr/local/go/bin/go"
    version = subprocess.run([go, "env", "GOVERSION"], capture_output=True, text=True, check=True).stdout.strip()
    key = hashlib.sha256(GOPOLICY_SRC.read_bytes() + version.encode()).hexdigest()[:16]
    state = Path(os.environ.get("COLLOID_STATE") or ("/opt/colloid/state" if os.name == "posix" and os.path.isdir("/opt/colloid") else
                                                      Path.home() / ".colloid" / "state"))
    out = state / "go" / "judge" / f"gopolicy-{key}"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="gopolicy-") as tmp:
            shutil.copy(GOPOLICY_SRC, Path(tmp) / "main.go")
            (Path(tmp) / "go.mod").write_text("module gopolicy\n\ngo 1.22\n")
            env = {**os.environ, "CGO_ENABLED": "0", "GOPROXY": "off", "GOTOOLCHAIN": "local", "GOWORK": "off", "GOFLAGS": "-mod=mod",
                   "GOCACHE": str(out.parent / "gocache")}
            res = subprocess.run([go, "build", "-trimpath", "-o", str(out.with_suffix(".tmp")), "."], cwd=tmp, env=env,
                                 capture_output=True, text=True, check=False)
            if res.returncode != 0:
                raise RuntimeError(f"building the Go policy scanner failed: {res.stderr[-1500:]}")
        os.replace(out.with_suffix(".tmp"), out)
    _GOPOLICY = out
    return out


def scan_go(new_source: str, baseline_source: str, name: str, file: Path) -> list[str]:
    res = subprocess.run([str(gopolicy_tool()), str(file.parent), str(file), name], input=json.dumps({"source": new_source, "baseline": baseline_source}),
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        return [f"policy scanner error: {res.stderr.strip()[:300]}"]
    data = json.loads(res.stdout)
    return sorted(set(data["reasons"] or ()) | set(scan_sql(data["strings"] or ())))


@dataclass(frozen=True)
class PolicyVerdict:
    ok: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return ast.dump(fn.args, include_attributes=False) + "|" + "|".join(ast.dump(d, include_attributes=False) for d in fn.decorator_list)


def _local_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names created inside the function (assignment targets, loop/with/except/comprehension
    targets, nested defs). Parameters are *not* local for mutation purposes: mutating an
    argument object would leak state to the caller."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and node is not fn:
            if not isinstance(node, ast.Lambda):
                names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def scan_python(new_source: str, baseline_source: str) -> list[str]:
    reasons: list[str] = []
    try:
        tree = ast.parse(textwrap.dedent(new_source))
        base = ast.parse(textwrap.dedent(baseline_source))
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    others = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    base_fn = base.body[0]
    assert isinstance(base_fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    if len(defs) != 1 or others:
        return ["locus confinement: payload must contain exactly one function definition and nothing else"]
    fn = defs[0]
    if fn.name != base_fn.name:
        reasons.append(f"locus confinement: function renamed {base_fn.name} -> {fn.name}")
    if type(fn) is not type(base_fn):
        reasons.append("signature: sync/async nature changed")
    if _signature(fn) != _signature(base_fn):
        reasons.append("signature: parameters, defaults or decorators changed")
    local = _local_names(fn)
    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                if m not in ALLOWED_IMPORTS and m.split(".")[0] not in ALLOWED_IMPORTS - {"shop"}:
                    reasons.append(f"policy: import of '{m}' is not allowed")
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name in FORBIDDEN_ATTRS or a.name == "*":
                        reasons.append(f"policy: importing '{a.name}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            reasons.append("policy: global/nonlocal state is not allowed (state must not outlive a request)")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in FORBIDDEN_CALLS:
                reasons.append(f"policy: call to {f.id}() is not allowed")
            if isinstance(f, ast.Attribute) and f.attr in MUTATING_METHODS:
                root = _root_name(f.value)
                if root is None or root not in local or isinstance(f.value, ast.Attribute):
                    reasons.append(f"policy: mutating '{ast.unparse(f.value)}.{f.attr}()' - only objects created in this call may be mutated")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                reasons.append(f"policy: attribute '{node.attr}' is not allowed")
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                reasons.append(f"policy: attribute assignment '{ast.unparse(node)}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_ATTRS | {"__builtins__"}:
            reasons.append(f"policy: name '{node.id}' is not allowed")
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = _root_name(node.value)
            if root is None or root not in local:
                reasons.append(f"policy: item assignment on '{ast.unparse(node.value)}' - only objects created in this call may be mutated")
        elif isinstance(node, ast.AugAssign) and not isinstance(node.target, ast.Name):
            root = _root_name(node.target)
            if root is None or root not in local:
                reasons.append(f"policy: in-place update of '{ast.unparse(node.target)}' is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and FORBIDDEN_STRINGS.search(node.value):
            reasons.append("policy: string constant references evaluator or system paths")
    return sorted(set(reasons))


def scan_c(new_source: str, baseline_source: str, name: str) -> list[str]:
    reasons = []
    body = re.sub(r"/\*.*?\*/|//[^\n]*", " ", new_source, flags=re.S)
    if "#" in body:
        reasons.append("policy: preprocessor directives are not allowed in a function gene")
    if re.search(r"\b(asm|__asm__|__attribute__)\b", body):
        reasons.append("policy: inline assembly / attributes are not allowed")
    m = C_FORBIDDEN.search(body)
    if m:
        reasons.append(f"policy: call to {m.group(1)}() is not allowed in libshopnative")
    if re.search(r"\bstatic\b(?![^;{]*\bconst\b)", body.split("{", 1)[1] if "{" in body else ""):
        reasons.append("policy: static (cross-call) mutable state is not allowed")
    if re.search(r"\b(goto)\b", body):
        reasons.append("policy: goto is not allowed")
    head_new = " ".join(body.split("{", 1)[0].split())
    base_body = re.sub(r"/\*.*?\*/|//[^\n]*", " ", baseline_source, flags=re.S)
    head_base = " ".join(base_body.split("{", 1)[0].split())
    if head_new != head_base:
        reasons.append("signature: C function signature changed")
    depth = 0
    for ch in body:
        depth += ch == "{"
        depth -= ch == "}"
        if depth < 0:
            break
    if depth != 0:
        reasons.append("syntax: unbalanced braces")
    if FORBIDDEN_STRINGS.search(new_source):
        reasons.append("policy: string constant references evaluator or system paths")
    return reasons


def diff_size(a: str, b: str) -> int:
    import difflib

    return sum(1 for line in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0) if line[:1] in "+-" and line[:3] not in ("+++", "---"))


def check_genome(
    genome: Genome,
    atlas: StackAtlas,
    knobs: Mapping[str, KnobSpec],
    knob_of_locus: Mapping[str, str],
    *,
    source_root: Path | None = None,
) -> PolicyVerdict:
    reasons: list[str] = []
    warnings: list[str] = []
    try:
        genome.check(atlas)
    except LocusConflict as exc:
        reasons.append(f"locus conflict: {exc.why}")
    except KeyError as exc:
        reasons.append(f"unknown locus {exc}")
    values = {knob_of_locus[g.locus_id]: g.value for g in genome if g.locus_id in knob_of_locus}
    cfg = {n: s.default for n, s in knobs.items()}
    cfg.update(values)
    for gene in genome:
        reasons += [f"gene {gene.id[:8]}: {r}" for r in check_gene(gene, atlas, knobs, knob_of_locus, cfg, warnings, source_root=source_root)]
    return PolicyVerdict(not reasons, tuple(reasons), tuple(warnings))


def check_gene(
    gene: Gene,
    atlas: StackAtlas,
    knobs: Mapping[str, KnobSpec],
    knob_of_locus: Mapping[str, str],
    cfg: Mapping[str, Any],
    warnings: list[str],
    *,
    source_root: Path | None = None,
) -> list[str]:
    loc = atlas.loci.get(gene.locus_id)
    if loc is None:
        return ["locus does not exist in the Atlas (tests, oracles and evaluator files are not mutable loci)"]
    unit = atlas.units[loc.unit_id]
    if is_judge_path(unit.symbol_path):
        return [f"locus {unit.symbol_path} is part of the judge (evaluator, tests, statistics, sandbox, benchmark, lake) and is never mutable"]
    if loc.mutability == Mutability.FROZEN:
        return [f"locus {unit.symbol_path} is frozen"]
    if loc.mutability == Mutability.REVIEW_ONLY:
        warnings.append(f"{unit.symbol_path} is review-only: a human must approve before promotion")
    lic = unit.tags.get("license")
    if lic is not None and lic not in ALLOWED_LICENSES:
        return [f"licence policy: unit licensed {lic} is not mutable under policy"]
    if unit.kind == UnitKind.KNOB:
        if gene.payload_kind != PayloadKind.VALUE:
            return ["knob gene must carry a value payload"]
        spec = knobs[knob_of_locus[gene.locus_id]]
        if spec.mutability == "frozen":
            return [f"knob {spec.name} is frozen"]
        problem = validate_value(spec, gene.value)
        if problem:
            return [f"knob {spec.name}: {problem}"]
        if not requirement_met(spec, cfg):
            return [f"knob {spec.name} is inert: requires {dict(spec.requires)}"]
        if gene.value == spec.default:
            return [f"knob {spec.name} set to its baseline default (not a change)"]
        return []
    if gene.payload_kind != PayloadKind.SOURCE:
        return ["code locus needs a source payload"]
    src = str(gene.payload.get("source", ""))
    base = str(unit.tags.get("baseline_source", ""))
    if gene.payload.get("base_hash") != sha256_hex(base)[:16]:
        return ["gene was written against a different baseline version of this unit"]
    if diff_size(base, src) > MAX_DIFF_LINES:
        return [f"diff too large ({diff_size(base, src)} changed lines > {MAX_DIFF_LINES})"]
    # Only *new* violations count: a construct already present in the baseline unit (e.g. the
    # app's lifespan handler storing the pool in module state) is part of the trusted code.
    if unit.tags.get("language") == "python":
        already = set(scan_python(base, base)) | set(scan_sql(python_strings(base)))
        return [r for r in sorted(set(scan_python(src, base)) | set(scan_sql(python_strings(src)))) if r not in already]
    if unit.tags.get("language") == "go":
        if source_root is None:
            return ["go gene checked without the target's source root"]
        file = source_root / str(unit.tags["file"])
        already = set(scan_go(base, base, unit.name, file))
        return [r for r in scan_go(src, base, unit.name, file) if r not in already]
    if unit.tags.get("language") == "c":
        already = set(scan_c(base, base, unit.name)) | set(scan_sql(re.findall(r'"((?:[^"\\]|\\.)*)"', base)))
        found = set(scan_c(src, base, unit.name)) | set(scan_sql(re.findall(r'"((?:[^"\\]|\\.)*)"', src)))
        return [r for r in sorted(found) if r not in already]
    return [f"unsupported language {unit.tags.get('language')}"]


def touched_endpoints(genome: Genome, atlas: StackAtlas) -> list[str] | None:
    """Endpoint kinds whose request paths include a code unit touched by the genome.
    ``None`` means "everything" (knob genes affect the whole stack)."""
    kinds = {
        "GET /products/search": "search", "GET /products/{id}": "product", "GET /customers/{id}/summary": "summary",
        "GET /customers/{id}/recommendations": "reco", "GET /categories/{id}/top": "category_top",
        "GET /reports/daily": "daily", "POST /orders": "order",
    }
    out: set[str] = set()
    for gene in genome:
        unit = atlas.units[atlas.loci[gene.locus_id].unit_id]
        if unit.kind == UnitKind.KNOB:
            return None
        for path in atlas.paths:
            if unit.id in path.unit_ids:
                route = atlas.units[path.unit_ids[0]].name
                if route in kinds:
                    out.add(kinds[route])
    return sorted(out) if out else None


def summarize(reasons: Sequence[str]) -> str:
    return "; ".join(reasons[:3]) + (f" (+{len(reasons) - 3} more)" if len(reasons) > 3 else "")
