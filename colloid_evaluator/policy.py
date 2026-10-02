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

Rejections carry a precise reason; the engine feeds it back to the LLM as a failed-attempt
summary so the next proposal does not repeat it.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
C_FORBIDDEN = re.compile(
    r"\b(system|popen|exec[lv]p?e?|fork|vfork|clone|socket|connect|fopen|open|openat|dlopen|dlsym|mmap|mprotect|syscall|"
    r"pthread_create|signal|sigaction|kill|ptrace|getenv|setenv|abort|exit|_exit|longjmp|setjmp)\s*\("
)


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
        reasons += [f"gene {gene.id[:8]}: {r}" for r in check_gene(gene, atlas, knobs, knob_of_locus, cfg, warnings)]
    return PolicyVerdict(not reasons, tuple(reasons), tuple(warnings))


def check_gene(
    gene: Gene,
    atlas: StackAtlas,
    knobs: Mapping[str, KnobSpec],
    knob_of_locus: Mapping[str, str],
    cfg: Mapping[str, Any],
    warnings: list[str],
) -> list[str]:
    loc = atlas.loci.get(gene.locus_id)
    if loc is None:
        return ["locus does not exist in the Atlas (tests, oracles and evaluator files are not mutable loci)"]
    unit = atlas.units[loc.unit_id]
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
        already = set(scan_python(base, base))
        return [r for r in scan_python(src, base) if r not in already]
    if unit.tags.get("language") == "c":
        already = set(scan_c(base, base, unit.name))
        return [r for r in scan_c(src, base, unit.name) if r not in already]
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
