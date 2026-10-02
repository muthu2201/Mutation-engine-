"""CodeRepresentation adapter for C, built on clang's JSON AST dump.

``clang -Xclang -ast-dump=json -fsyntax-only file.c`` gives the real compiler's view of the
translation unit. We keep the ``FunctionDecl`` nodes that (a) are defined in the file
itself (not pulled in from headers) and (b) have a body, and record their byte ranges.
Clang's JSON output elides repeated ``line`` fields, so spans are computed from byte
``offset`` values, which are always present, and converted to lines locally.

Calls between functions come from ``CallExpr → DeclRefExpr`` nodes and become ``calls``
edges in the Atlas (e.g. ``shop_score_batch → fuzzy_similarity → levenshtein``).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from colloid.ports import CodeUnit


def _walk(node: dict[str, Any]):  # type: ignore[no-untyped-def]
    yield node
    for child in node.get("inner", []) or []:
        yield from _walk(child)


class ClangCCode:
    PORT_API = "1.0.0"
    language = "c"

    def __init__(self, clang: str = "clang", include_dirs: Sequence[str] = ()) -> None:
        self.clang = clang
        self.include_dirs = tuple(include_dirs)

    def _ast(self, path: Path) -> dict[str, Any]:
        args = [self.clang, "-Xclang", "-ast-dump=json", "-fsyntax-only", *(f"-I{d}" for d in self.include_dirs), str(path)]
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"clang failed on {path}: {proc.stderr[:2000]}")
        return json.loads(proc.stdout)

    def units(self, root: Path, rel_path: str) -> Sequence[CodeUnit]:
        path = root / rel_path
        text = path.read_text()
        data = path.read_bytes()
        tree = self._ast(path)
        out: list[CodeUnit] = []
        for node in tree.get("inner", []):
            if node.get("kind") != "FunctionDecl" or node.get("isImplicit"):
                continue
            rng = node.get("range", {})
            begin, end = rng.get("begin", {}), rng.get("end", {})
            # Only definitions located in this file (header declarations carry includedFrom).
            if "includedFrom" in begin or "includedFrom" in node.get("loc", {}):
                continue
            if not any(c.get("kind") == "CompoundStmt" for c in node.get("inner", [])):
                continue
            b_off, e_off = begin.get("offset"), end.get("offset")
            if b_off is None or e_off is None:
                continue
            e_off += int(end.get("tokLen", 1))
            start_line = data[:b_off].count(b"\n") + 1
            end_line = data[:e_off].count(b"\n") + 1
            lines = text.splitlines(keepends=True)
            source = "".join(lines[start_line - 1 : end_line])
            calls = set()
            for n in _walk(node):
                if n.get("kind") == "CallExpr":
                    for m in _walk(n):
                        ref = m.get("referencedDecl") or {}
                        if m.get("kind") == "DeclRefExpr" and ref.get("kind") == "FunctionDecl":
                            calls.add(ref.get("name"))
                            break
            name = node["name"]
            out.append(
                CodeUnit(
                    symbol_path=f"c:{rel_path}::{name}",
                    name=name,
                    file=rel_path,
                    start_line=start_line,
                    end_line=end_line,
                    indent="",
                    source=source,
                    language="c",
                    calls=tuple(sorted(c for c in calls if c)),
                    extra={"storage": node.get("storageClass", ""), "type": node.get("type", {}).get("qualType", "")},
                )
            )
        return out

    def replace(self, file_text: str, unit: CodeUnit, new_source: str) -> str:
        """Replace the unit's line span. Spans are re-derived by locating the original
        source text, so multiple replacements in one file compose."""
        idx = file_text.find(unit.source)
        if idx < 0:
            raise ValueError(f"original source of {unit.name} not found (file drifted)")
        return file_text[:idx] + new_source.rstrip("\n") + "\n" + file_text[idx + len(unit.source) :]
