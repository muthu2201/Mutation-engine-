"""CodeRepresentation adapter for Python, built on the standard-library ``ast`` module.

Why ``ast`` and not tree-sitter (the blueprint's suggestion)? For Python, ``ast`` *is* the
reference grammar: it can never disagree with the interpreter about what a function's
boundaries are, it gives exact line/column spans (including decorators), and it ships with
the runtime. tree-sitter's advantage - one uniform API across many languages - is covered
by having one small adapter per language behind the same port (see ``c_clang.py``).

For every function and method the adapter extracts:

* exact span (first decorator line .. last body line) and indentation, so a replacement
  can be spliced back byte-exactly;
* the dedented source (what operators and LLMs see);
* static call names (``calls`` edges in the Atlas);
* SQL statements found in string literals (``queries`` edges to SQL query units), which is
  what makes a single Atlas path span the service and database layers.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Sequence
from pathlib import Path

from colloid.ports import CodeUnit

SQL_RE = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b", re.I)


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        parts = [f.attr]
        cur = f.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


class PythonAstCode:
    PORT_API = "1.0.0"
    language = "python"

    def units(self, root: Path, rel_path: str) -> Sequence[CodeUnit]:
        text = (root / rel_path).read_text()
        return self.units_from_text(text, rel_path)

    def units_from_text(self, text: str, rel_path: str) -> list[CodeUnit]:
        tree = ast.parse(text)
        lines = text.splitlines(keepends=True)
        out: list[CodeUnit] = []

        def visit(body: list[ast.stmt], prefix: str) -> None:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                    end = node.end_lineno or node.lineno
                    raw = "".join(lines[start - 1 : end])
                    indent = raw[: len(raw) - len(raw.lstrip())]
                    indent = indent.splitlines()[-1] if indent else ""
                    source = textwrap.dedent(raw)
                    calls = sorted({n for c in ast.walk(node) if isinstance(c, ast.Call) and (n := _call_name(c))})
                    sql = []
                    for c in ast.walk(node):
                        if isinstance(c, ast.Constant) and isinstance(c.value, str) and SQL_RE.match(c.value):
                            sql.append(normalize_sql(c.value))
                    qual = f"{prefix}{node.name}"
                    out.append(
                        CodeUnit(
                            symbol_path=f"py:{rel_path}::{qual}",
                            name=qual,
                            file=rel_path,
                            start_line=start,
                            end_line=end,
                            indent=indent,
                            source=source,
                            language="python",
                            calls=tuple(calls),
                            sql=tuple(dict.fromkeys(sql)),
                            is_async=isinstance(node, ast.AsyncFunctionDef),
                        )
                    )
                elif isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}{node.name}.")

        visit(tree.body, "")
        return out

    def module_names(self, text: str) -> list[str]:
        """Top-level names available to code in this module (imports, defs, constants)."""
        tree = ast.parse(text)
        names: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                names += [a.asname or a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names += [a.asname or a.name for a in node.names]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                names += [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.append(node.target.id)
        return list(dict.fromkeys(names))

    def find(self, text: str, rel_path: str, qualname: str) -> CodeUnit:
        for u in self.units_from_text(text, rel_path):
            if u.name == qualname:
                return u
        raise KeyError(f"{qualname} not found in {rel_path}")

    def replace(self, file_text: str, unit: CodeUnit, new_source: str) -> str:
        """Splice ``new_source`` (dedented) in place of ``unit``, re-indented to the unit's
        original indentation. The unit is re-located by name in ``file_text`` so earlier
        replacements in the same file (which shift line numbers) are handled."""
        current = self.find(file_text, unit.file, unit.name)
        lines = file_text.splitlines(keepends=True)
        body = textwrap.indent(textwrap.dedent(new_source).rstrip("\n") + "\n", current.indent)
        new_text = "".join(lines[: current.start_line - 1]) + body + "".join(lines[current.end_line :])
        ast.parse(new_text)  # raises SyntaxError if the splice produced invalid code
        return new_text
