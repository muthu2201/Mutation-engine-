"""Genetic-Improvement statement edits (Langdon & Petke style).

Classic GI evolves *patches* made of three statement-level edit types drawn from the
program itself (the "plastic surgery" hypothesis: the ingredients of a fix usually already
exist somewhere in the code):

* ``delete`` - remove a statement (replaced by ``pass`` if it was the only one in a block)
* ``copy``   - insert a copy of an existing statement before another statement
* ``swap``   - exchange two adjacent statements in the same block

Most such edits break the program and are rejected by the cascade within milliseconds (L0
parse) or seconds (L2 oracles). Occasionally one deletes redundant work - "Software is Not
Fragile" found large neutral regions in real programs. The operator is cheap, so the bandit
can afford to keep sampling it at a low rate; if it never earns credit, its budget decays.
"""

from __future__ import annotations

import ast
import random
import textwrap

from colloid.core.operators.py_rewrite import _bodies, _Src


def _stmt_text(src: _Src, stmt: ast.stmt) -> str:
    a, b = src.line_span(stmt, stmt)
    return src.source[a:b]


def gi_edit(source: str, rng: random.Random) -> tuple[str, str] | None:
    """Apply one random GI edit. Returns ``(new_source, description)`` or ``None``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    func = tree.body[0] if tree.body and isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)) else None
    if func is None:
        return None
    src = _Src(source)
    bodies = [b for b in _bodies(func) if b is not tree.body]
    stmts = [(b, i) for b in bodies for i, s in enumerate(b) if not (i == 0 and isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))]
    if not stmts:
        return None
    op = rng.choice(["delete", "copy", "swap"])
    if op == "delete":
        body, i = rng.choice(stmts)
        s = body[i]
        a, b = src.line_span(s, s)
        rep = f"{src.indent_of(s)}pass\n" if len(body) == 1 else ""
        new = source[:a] + rep + source[b:]
        desc = f"delete statement at line {s.lineno}"
    elif op == "copy":
        sb, si = rng.choice(stmts)
        db, di = rng.choice(stmts)
        s, d = sb[si], db[di]
        text = textwrap.dedent(_stmt_text(src, s))
        text = textwrap.indent(text, src.indent_of(d))
        at = src.line_span(d, d)[0]
        new = source[:at] + text + source[at:]
        desc = f"copy line {s.lineno} before line {d.lineno}"
    else:
        candidates = [(b, i) for b, i in stmts if i + 1 < len(b)]
        if not candidates:
            return None
        body, i = rng.choice(candidates)
        s1, s2 = body[i], body[i + 1]
        a1, b1 = src.line_span(s1, s1)
        a2, b2 = src.line_span(s2, s2)
        new = source[:a1] + source[a2:b2] + source[a1:b1] + source[b2:]
        desc = f"swap lines {s1.lineno} and {s2.lineno}"
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    if new == source:
        return None
    return new, desc
