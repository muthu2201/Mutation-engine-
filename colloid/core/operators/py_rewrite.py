"""A catalogue of semantics-preserving peephole rewrites for Python functions.

This is Colloid's deterministic, zero-cost code operator - the Python analogue of a
superoptimiser's peephole rules or an LLVM InstCombine pass. Each rule recognises one
common inefficiency pattern *by AST shape* and rewrites only the matched source span, so
formatting and comments elsewhere are untouched and the diff is minimal.

Rules (each guarded by conservative side-conditions):

``membership_set``      ``x in [a, b, c]``  →  ``x in {a, b, c}`` (≥3 hashable literals)
``sorted_first``        ``sorted(xs, key=k)[0]`` → ``min(xs, key=k)``; ``reverse=True`` → ``max``
``append_to_listcomp``  ``r = []; for t in it: [if c:] r.append(e)`` → ``r = [e for t in it if c]``
``dedupe_seen_set``     ``if x not in lst: lst.append(x)`` inside a loop → add a shadow ``set``
                        so the membership test is O(1) while preserving list order
``accumulate_sum``      ``t = 0; for v in it: t += e`` → ``t = sum((e for v in it), 0)``
``dict_get``            ``if k in d: v = d[k] else: v = dflt`` → ``v = d.get(k, dflt)``

"Semantics-preserving" is the intent, not a proof: e.g. ``sum`` of floats uses compensated
summation since Python 3.12 and can differ from a naive loop in the last bit, and
``set`` membership raises on unhashable values where a list would not. That is why every
rewrite still goes through the full evaluator cascade - the oracles are the judge.

Sources handed to this module are dedented function definitions (the code adapter
re-indents methods when it applies a gene).
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass

Edit = tuple[int, int, str]  # (start char offset, end char offset, replacement)


@dataclass(frozen=True)
class RewriteSite:
    rule: str
    description: str
    apply: Callable[[], str]


class _Src:
    """Maps AST (line, utf-8 byte column) positions to character offsets."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.line_start = [0]
        for line in self.lines:
            self.line_start.append(self.line_start[-1] + len(line))

    def offset(self, lineno: int, col_byte: int) -> int:
        line = self.lines[lineno - 1]
        prefix = line.encode("utf-8")[:col_byte].decode("utf-8", errors="ignore")
        return self.line_start[lineno - 1] + len(prefix)

    def span(self, node: ast.AST) -> tuple[int, int]:
        return (
            self.offset(node.lineno, node.col_offset),  # type: ignore[attr-defined]
            self.offset(node.end_lineno, node.end_col_offset),  # type: ignore[attr-defined]
        )

    def text(self, node: ast.AST) -> str:
        a, b = self.span(node)
        return self.source[a:b]

    def line_span(self, first: ast.stmt, last: ast.stmt) -> tuple[int, int]:
        """Whole lines covering statements ``first``..``last`` (including the newline)."""
        start = self.line_start[first.lineno - 1]
        end = self.line_start[last.end_lineno] if last.end_lineno is not None else len(self.source)
        return start, end

    def indent_of(self, node: ast.stmt) -> str:
        line = self.lines[node.lineno - 1]
        return line[: len(line) - len(line.lstrip())]


def apply_edits(source: str, edits: list[Edit]) -> str:
    out = source
    for start, end, rep in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + rep + out[end:]
    return out


def _bodies(tree: ast.AST) -> list[list[ast.stmt]]:
    out = []
    for node in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            val = getattr(node, attr, None)
            if isinstance(val, list) and val and isinstance(val[0], ast.stmt):
                out.append(val)
        if isinstance(node, ast.Try):
            for h in node.handlers:
                out.append(h.body)
    return out


def _names_loaded(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _target_names(target: ast.AST) -> set[str] | None:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Tuple) and all(isinstance(e, ast.Name) for e in target.elts):
        return {e.id for e in target.elts}  # type: ignore[attr-defined]
    return None


def _used_outside(func: ast.AST, names: set[str], inside: ast.AST) -> bool:
    inner = {id(n) for n in ast.walk(inside)}
    for n in ast.walk(func):
        if isinstance(n, ast.Name) and n.id in names and id(n) not in inner:
            return True
    return False


def _is_hashable_literal(e: ast.expr) -> bool:
    return isinstance(e, ast.Constant) and isinstance(e.value, (str, int, bytes)) and not isinstance(e.value, bool)


# ---------------------------------------------------------------------------- rules


def _membership_set(tree: ast.AST, src: _Src) -> list[tuple[str, list[Edit]]]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comp in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, (ast.In, ast.NotIn)) and isinstance(comp, (ast.List, ast.Tuple)):
                if len(comp.elts) >= 3 and all(_is_hashable_literal(e) for e in comp.elts):
                    a, b = src.span(comp)
                    rep = "{" + ", ".join(src.text(e) for e in comp.elts) + "}"
                    out.append((f"membership test against literal at line {node.lineno} uses a set", [(a, b, rep)]))
    return out


def _sorted_first(tree: ast.AST, src: _Src) -> list[tuple[str, list[Edit]]]:
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == 0):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "sorted" and len(call.args) == 1):
            continue
        kws = {k.arg: k for k in call.keywords}
        if set(kws) - {"key", "reverse"} or None in kws:
            continue
        fn = "min"
        if "reverse" in kws:
            rv = kws["reverse"].value
            if not (isinstance(rv, ast.Constant) and isinstance(rv.value, bool)):
                continue
            fn = "max" if rv.value else "min"
        parts = [src.text(call.args[0])]
        if "key" in kws:
            parts.append("key=" + src.text(kws["key"].value))
        a, b = src.span(node)
        out.append((f"sorted(...)[0] at line {node.lineno} becomes {fn}()", [(a, b, f"{fn}({', '.join(parts)})")]))
    return out


def _loop_pattern(body: list[ast.stmt], i: int) -> tuple[ast.Assign, ast.For, ast.expr | None, ast.stmt] | None:
    s, nxt = body[i], body[i + 1]
    if not (isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)):
        return None
    if not (isinstance(nxt, ast.For) and not nxt.orelse and len(nxt.body) == 1):
        return None
    inner = nxt.body[0]
    cond = None
    if isinstance(inner, ast.If) and not inner.orelse and len(inner.body) == 1:
        cond, inner = inner.test, inner.body[0]
    return s, nxt, cond, inner


def _append_to_listcomp(func: ast.AST, src: _Src) -> list[tuple[str, list[Edit]]]:
    out = []
    for body in _bodies(func):
        for i in range(len(body) - 1):
            pat = _loop_pattern(body, i)
            if pat is None:
                continue
            s, loop, cond, inner = pat
            name = s.targets[0].id  # type: ignore[attr-defined]
            if not (isinstance(s.value, ast.List) and not s.value.elts):
                continue
            if not (isinstance(inner, ast.Expr) and isinstance(inner.value, ast.Call)):
                continue
            call = inner.value
            f = call.func
            if not (isinstance(f, ast.Attribute) and f.attr == "append" and isinstance(f.value, ast.Name) and f.value.id == name):
                continue
            if len(call.args) != 1 or call.keywords:
                continue
            tnames = _target_names(loop.target)
            if tnames is None or _used_outside(func, tnames, loop):
                continue
            refs = _names_loaded(call.args[0]) | _names_loaded(loop.iter) | (_names_loaded(cond) if cond is not None else set())
            if name in refs:
                continue
            comp = f"[{src.text(call.args[0])} for {src.text(loop.target)} in {src.text(loop.iter)}"
            if cond is not None:
                comp += f" if {src.text(cond)}"
            comp += "]"
            a, b = src.line_span(s, loop)
            out.append((f"append loop at line {loop.lineno} becomes a list comprehension", [(a, b, f"{src.indent_of(s)}{name} = {comp}\n")]))
    return out


def _accumulate_sum(func: ast.AST, src: _Src) -> list[tuple[str, list[Edit]]]:
    out = []
    for body in _bodies(func):
        for i in range(len(body) - 1):
            pat = _loop_pattern(body, i)
            if pat is None:
                continue
            s, loop, cond, inner = pat
            name = s.targets[0].id  # type: ignore[attr-defined]
            if not (isinstance(s.value, ast.Constant) and type(s.value.value) in (int, float) and s.value.value == 0):
                continue
            if not (isinstance(inner, ast.AugAssign) and isinstance(inner.op, ast.Add) and isinstance(inner.target, ast.Name) and inner.target.id == name):
                continue
            tnames = _target_names(loop.target)
            if tnames is None or _used_outside(func, tnames, loop):
                continue
            if name in _names_loaded(inner.value) | _names_loaded(loop.iter):
                continue
            gen = f"({src.text(inner.value)} for {src.text(loop.target)} in {src.text(loop.iter)}"
            if cond is not None:
                gen += f" if {src.text(cond)}"
            gen += ")"
            a, b = src.line_span(s, loop)
            rep = f"{src.indent_of(s)}{name} = sum({gen}, {src.text(s.value)})\n"
            out.append((f"accumulation loop at line {loop.lineno} becomes sum()", [(a, b, rep)]))
    return out


_MUTATORS = {"append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse", "__setitem__", "__delitem__"}


def _dedupe_seen_set(func: ast.AST, src: _Src) -> list[tuple[str, list[Edit]]]:
    out = []
    # Locate "LST = []" definitions.
    defs: dict[str, ast.Assign] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.List) and not node.value.elts:
                defs.setdefault(node.targets[0].id, node)
    for node in ast.walk(func):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        for stmt in ast.walk(node):
            if not (isinstance(stmt, ast.If) and not stmt.orelse and len(stmt.body) == 1):
                continue
            t = stmt.test
            if not (isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], ast.NotIn) and isinstance(t.comparators[0], ast.Name)):
                continue
            lst = t.comparators[0].id
            app = stmt.body[0]
            if not (isinstance(app, ast.Expr) and isinstance(app.value, ast.Call)):
                continue
            f = app.value.func
            if not (isinstance(f, ast.Attribute) and f.attr == "append" and isinstance(f.value, ast.Name) and f.value.id == lst):
                continue
            if len(app.value.args) != 1 or src.text(app.value.args[0]) != src.text(t.left) or lst not in defs:
                continue
            if app.lineno <= (t.end_lineno or t.lineno):
                continue  # single-line "if ...: lst.append(...)" - keep edits non-overlapping
            # LST must not be mutated anywhere else.
            mutations = 0
            for n in ast.walk(func):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == lst and n.func.attr in _MUTATORS:
                    mutations += 1
                if isinstance(n, (ast.Subscript,)) and isinstance(n.value, ast.Name) and n.value.id == lst and isinstance(n.ctx, (ast.Store, ast.Del)):
                    mutations += 2
                if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == lst:
                    mutations += 2
            if mutations != 1:
                continue
            d = defs[lst]
            assigns = sum(
                1
                for n in ast.walk(func)
                if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(isinstance(tg, ast.Name) and tg.id == lst for tg in getattr(n, "targets", [getattr(n, "target", None)]))
            )
            if assigns != 1 or d.lineno >= node.lineno:
                continue
            seen = f"_seen_{lst}"
            if seen in {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}:
                continue
            ins_at = src.line_span(d, d)[1]
            ca, cb = src.span(t.comparators[0])
            la, lb = src.line_span(app, app)
            ind = src.indent_of(app)
            edits: list[Edit] = [
                (ins_at, ins_at, f"{src.indent_of(d)}{seen} = set()\n"),
                (ca, cb, seen),
                (la, lb, f"{ind}{seen}.add({src.text(t.left)})\n" + src.source[la:lb]),
            ]
            out.append((f"list-membership dedupe of '{lst}' at line {stmt.lineno} uses a shadow set", edits))
    return out


def _dict_get(func: ast.AST, src: _Src) -> list[tuple[str, list[Edit]]]:
    out = []
    for node in ast.walk(func):
        if not (isinstance(node, ast.If) and len(node.body) == 1 and len(node.orelse) == 1):
            continue
        t = node.test
        if not (isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], ast.In)):
            continue
        a1, a2 = node.body[0], node.orelse[0]
        if not (isinstance(a1, ast.Assign) and isinstance(a2, ast.Assign) and len(a1.targets) == 1 and len(a2.targets) == 1):
            continue
        if src.text(a1.targets[0]) != src.text(a2.targets[0]) or not isinstance(a1.targets[0], ast.Name):
            continue
        sub = a1.value
        if not (isinstance(sub, ast.Subscript) and src.text(sub.value) == src.text(t.comparators[0]) and src.text(sub.slice) == src.text(t.left)):
            continue
        a, b = src.line_span(node, node)
        rep = f"{src.indent_of(node)}{src.text(a1.targets[0])} = {src.text(sub.value)}.get({src.text(t.left)}, {src.text(a2.value)})\n"
        out.append((f"if/else lookup at line {node.lineno} becomes dict.get", [(a, b, rep)]))
    return out


RULES: dict[str, Callable[[ast.AST, _Src], list[tuple[str, list[Edit]]]]] = {
    "membership_set": _membership_set,
    "sorted_first": _sorted_first,
    "append_to_listcomp": _append_to_listcomp,
    "dedupe_seen_set": _dedupe_seen_set,
    "accumulate_sum": _accumulate_sum,
    "dict_get": _dict_get,
}


def find_rewrites(source: str) -> list[RewriteSite]:
    """All applicable rewrite sites in a function's source. Each site's ``apply()``
    returns the rewritten source; results that no longer parse are filtered out."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    src = _Src(source)
    sites: list[RewriteSite] = []
    for rule, fn in RULES.items():
        for desc, edits in fn(tree, src):
            new = apply_edits(source, edits)
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            if new != source:
                sites.append(RewriteSite(rule, desc, (lambda s=new: s)))
    return sites
