"""CodeRepresentation adapter for Go, built on the Go toolchain's own parser.

The work happens in ``gosrc/gounits.go``, a small standard-library-only Go program
(``go/parser``, ``go/ast``, ``go/printer``): the same reasoning as ``python_ast`` using
CPython's ``ast`` and ``c_clang`` using clang's AST. The reference grammar of the language
decides where a function starts and ends, never a regular expression. The helper is
compiled once per source hash into the engine's state directory.

For every function and method the adapter extracts:

* exact span and source (the function from ``func`` to its closing brace; doc comments
  stay in the file and are not part of the locus);
* the canonical signature (receiver, name, type parameters, parameters, results), used by
  the LLM-response parser and the evaluator's confinement check;
* static call names (``calls`` edges in the Atlas), including generic instantiations such as
  ``fetch[T](...)``;
* SQL found in string constants, including constant concatenations (``"SELECT ... " +
  "FROM ..."``): the ``queries`` edges that make one Atlas path span service and database;
* the byte offset of the body's opening brace (the causal profiler injects its probe there).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from colloid.ports import CodeUnit

TOOL_SRC = Path(__file__).with_name("gosrc") / "gounits.go"


def go_binary() -> str:
    found = shutil.which("go") or "/usr/local/go/bin/go"
    if not Path(found).exists():
        raise RuntimeError("the Go toolchain is required for Go targets (go not found on PATH)")
    return found


def go_version(go: str) -> str:
    return subprocess.run([go, "env", "GOVERSION"], capture_output=True, text=True, check=True).stdout.strip()


def build_tool(src: Path, out_dir: Path, name: str) -> Path:
    """Compile a single-file, standard-library-only Go program, cached by source + toolchain."""
    go = go_binary()
    key = hashlib.sha256(src.read_bytes() + go_version(go).encode()).hexdigest()[:16]
    out = out_dir / f"{name}-{key}"
    if out.exists():
        return out
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{name}-build-") as tmp:
        shutil.copy(src, Path(tmp) / "main.go")
        (Path(tmp) / "go.mod").write_text(f"module {name}\n\ngo 1.22\n")
        env = {**os.environ, "CGO_ENABLED": "0", "GOFLAGS": "-mod=mod", "GOPROXY": "off", "GOTOOLCHAIN": "local",
               "GOCACHE": str(out_dir / "gocache"), "GOWORK": "off"}
        res = subprocess.run([go, "build", "-trimpath", "-o", str(out.with_suffix(".tmp")), "."], cwd=tmp, env=env,
                             capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"building {name} failed: {res.stderr[-2000:]}")
    os.replace(out.with_suffix(".tmp"), out)
    return out


class GoAstCode:
    PORT_API = "1.0.0"
    language = "go"

    def __init__(self, tool_dir: Path) -> None:
        self.tool_dir = Path(tool_dir)
        self._tool: Path | None = None

    def tool(self) -> Path:
        if self._tool is None:
            self._tool = build_tool(TOOL_SRC, self.tool_dir, "gounits")
        return self._tool

    def _run(self, args: Sequence[str], stdin: str | None = None) -> str:
        res = subprocess.run([str(self.tool()), *args], input=stdin, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise ValueError(res.stderr.strip()[:1000] or f"gounits {args[0]} failed")
        return res.stdout

    def raw_units(self, path: Path) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = json.loads(self._run(["units", str(path)]))
        return data

    def units(self, root: Path, rel_path: str) -> Sequence[CodeUnit]:
        return [self._code_unit(u, rel_path) for u in self.raw_units(root / rel_path)]

    def units_from_text(self, text: str, rel_path: str) -> list[CodeUnit]:
        with tempfile.NamedTemporaryFile("w", suffix=".go", delete=False) as fh:
            fh.write(text)
        try:
            return [self._code_unit(u, rel_path) for u in self.raw_units(Path(fh.name))]
        finally:
            os.unlink(fh.name)

    @staticmethod
    def _code_unit(u: dict[str, Any], rel_path: str) -> CodeUnit:
        return CodeUnit(
            symbol_path=f"go:{rel_path}::{u['name']}", name=u["name"], file=rel_path, start_line=int(u["start_line"]),
            end_line=int(u["end_line"]), indent="", source=u["source"], language="go", calls=tuple(u["calls"] or ()),
            sql=tuple(u["sql"] or ()), extra={"signature": u["signature"], "receiver": u["receiver"], "body_lbrace": u["body_lbrace"]},
        )

    def replace(self, file_text: str, unit: CodeUnit, new_source: str) -> str:
        """Splice ``new_source`` in place of the function ``unit.name``. The function is
        re-located by name, so several replacements in one file compose; the result must
        parse and define the function exactly once."""
        with tempfile.NamedTemporaryFile("w", suffix=".go", delete=False) as fh:
            fh.write(file_text)
        try:
            return self._run(["splice", fh.name, unit.name], stdin=new_source)
        finally:
            os.unlink(fh.name)

    def function(self, code: str, name: str) -> dict[str, Any]:
        """The single function ``name`` defined in ``code`` (a model answer), with its
        signature and anything else the answer declared at top level."""
        data: dict[str, Any] = json.loads(self._run(["func", name], stdin=code))
        return data
