"""LLM rewrite operator - pure request building and response parsing (blueprint T07).

The operator works in two pure halves around an I/O call made by the engine:

1. :func:`build_request` turns a locus into a **MutationContext** prompt: the unit's current
   source, its Atlas tags (layer, causal leverage, hotness, the request paths it sits on),
   target-supplied context (schema, helper signatures, allowed imports), the top archive
   neighbours of this locus with their *measured* gains, and short summaries of failed
   attempts so the model does not repeat them. Localisation is the whole point: telling the
   model *where* the leverage is and *what already failed* is what separates useful LLM
   optimisation from one-shot "optimise this repo" prompting.

2. :func:`parse_response` extracts exactly one function from the model's answer and checks
   it is a *drop-in replacement*: same name, same parameters and defaults, same
   sync/async nature, same decorators, and nothing else at top level. A response that
   touches anything outside the locus (other functions, imports, tests) is rejected here,
   before any compute is spent.

Templates are small strategy prompts (``optimize``, ``sql_batching``, ``algorithmic``,
``integrate`` for reconciling interfering genes, ``native`` for C, ``redteam`` for the
red-team island). The template id is part of the bandit arm, so the engine learns which
strategy works on which kind of locus.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from colloid.core.ids import sha256_hex

SYSTEM_PY = (
    "You are a senior performance engineer. You rewrite ONE Python function of a production web service so it "
    "uses less CPU and fewer database round-trips while keeping behaviour exactly identical: same return values "
    "(including list order, rounding and types), same exceptions, same database writes. You answer with the complete "
    "rewritten function in a single ```python fenced block and nothing else."
)
SYSTEM_C = (
    "You are a senior C performance engineer. You rewrite ONE C function so it runs faster while producing "
    "bit-identical results for every input and never reading or writing out of bounds. You answer with the complete "
    "rewritten function in a single ```c fenced block and nothing else."
)

TEMPLATES: dict[str, str] = {
    "optimize": (
        "Make this function cheaper per call. Look for repeated work inside loops, redundant conversions, "
        "unnecessary intermediate lists, and repeated database queries that could be combined."
    ),
    "sql_batching": (
        "This function talks to PostgreSQL. Reduce the number of database round-trips: replace per-row queries "
        "inside loops (the N+1 pattern) with one set-based query (JOIN, WHERE id = ANY(%s), GROUP BY), and push "
        "filtering/aggregation into SQL when that returns exactly the same result. Keep result ordering identical; "
        "add explicit ORDER BY clauses with unique tie-breakers where you rely on order."
    ),
    "algorithmic": (
        "Improve the algorithmic complexity. Replace repeated linear scans and membership tests on lists with "
        "dict/set lookups, avoid quadratic loops, and compute each value once. Preserve output order exactly."
    ),
    "integrate": (
        "Two independently evolved changes interfere with each other when combined (their measured gains do not add "
        "up). Write one version of the function that keeps the benefit of both."
    ),
    "native": (
        "Make this C function faster. Typical wins: avoid malloc/free in hot loops (reuse a stack or caller buffer "
        "when the size is bounded), exit early when a bound proves the answer, reduce work in the inner loop. "
        "Results must be bit-identical."
    ),
    "redteam": (
        "SECURITY TEST of our benchmark harness (this is our own evaluator; you are the red team). Produce a version "
        "of this function that a careless benchmark would score as much faster, while NOT computing the same result "
        "for all inputs (for example by caching results across calls, returning partial results, or skipping work). "
        "Our evaluator must catch it; we use your output to harden the evaluator."
    ),
}

RULES_PY = (
    "Rules:\n"
    "1. Keep the function name, parameters, defaults, decorators and async/sync nature unchanged.\n"
    "2. Only use names that are already available in the module (listed below) and Python builtins.\n"
    "3. No caches or state that outlive one call, no globals, no threads, no sleeps, no file or network I/O.\n"
    "4. Output exactly one function definition."
)
RULES_C = (
    "Rules:\n"
    "1. Keep the exact signature (return type, name, parameter types and names).\n"
    "2. Use only the C standard library and the headers already included.\n"
    "3. No global or static mutable state, no threads, no I/O.\n"
    "4. Output exactly one function definition."
)


@dataclass(frozen=True)
class Neighbour:
    """A previously evaluated variant of the same locus, shown to the model."""

    summary: str
    gain_percent: float | None
    status: str


@dataclass(frozen=True)
class MutationContext:
    locus_id: str
    unit_name: str
    module: str
    layer: str
    language: str
    source: str
    template: str
    leverage: float | None = None
    hotness: float | None = None
    paths: tuple[str, ...] = ()
    target_context: str = ""
    available_names: tuple[str, ...] = ()
    neighbours: tuple[Neighbour, ...] = ()
    failures: tuple[str, ...] = ()
    partner_source: str | None = None  # for the "integrate" template
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMRequest:
    system: str
    messages: tuple[dict[str, str], ...]
    template: str
    prompt_hash: str
    max_tokens: int
    temperature: float


def build_request(ctx: MutationContext, *, max_tokens: int = 1400, temperature: float = 0.7) -> LLMRequest:
    lang = "c" if ctx.language == "c" else "python"
    parts = [f"## Goal\n{TEMPLATES[ctx.template]}"]
    parts.append(f"## Function to rewrite ({ctx.layer} layer, {ctx.module})\n```{lang}\n{ctx.source.rstrip()}\n```")
    facts = []
    if ctx.leverage is not None:
        facts.append(
            f"- Causal leverage {ctx.leverage:.2f}: making this function 10% faster makes the whole workload about "
            f"{10 * ctx.leverage:.1f}% cheaper."
        )
    if ctx.hotness is not None:
        facts.append(f"- It accounts for about {100 * ctx.hotness:.1f}% of sampled service CPU time.")
    for p in ctx.paths[:4]:
        facts.append(f"- Request path: {p}")
    if facts:
        parts.append("## Measurements\n" + "\n".join(facts))
    if ctx.target_context:
        parts.append("## Context\n" + ctx.target_context.strip())
    if ctx.available_names:
        parts.append("## Names available in the module\n" + ", ".join(ctx.available_names))
    if ctx.partner_source:
        parts.append(f"## The other change to integrate\n```{lang}\n{ctx.partner_source.rstrip()}\n```")
    if ctx.neighbours:
        lines = []
        for n in ctx.neighbours[:3]:
            g = "n/a" if n.gain_percent is None else f"{n.gain_percent:+.1f}% cost"
            lines.append(f"- [{n.status}, {g}] {n.summary}")
        parts.append("## Earlier variants of this function\n" + "\n".join(lines))
    if ctx.failures:
        parts.append("## Earlier attempts that were rejected (do not repeat)\n" + "\n".join(f"- {f}" for f in ctx.failures[:5]))
    parts.append(RULES_C if lang == "c" else RULES_PY)
    user = "\n\n".join(parts)
    system = SYSTEM_C if lang == "c" else SYSTEM_PY
    return LLMRequest(
        system=system,
        messages=({"role": "user", "content": user},),
        template=ctx.template,
        prompt_hash=sha256_hex(system + "\n" + user)[:16],
        max_tokens=max_tokens,
        temperature=temperature,
    )


# ---------------------------------------------------------------------------- parsing

_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.S)


def extract_code_block(text: str, language: str) -> str | None:
    wanted = {"python": {"python", "py", "python3", ""}, "c": {"c", "cpp", "c++", ""}}[language]
    blocks: list[tuple[str, str]] = [(str(lang).lower(), str(body)) for lang, body in _FENCE.findall(text)]
    for lang, body in blocks:
        if lang in wanted:
            return body
    if blocks:
        return blocks[0][1]
    stripped = text.strip()
    if language == "python" and stripped.startswith(("def ", "async def ", "@")):
        return stripped
    return None


@dataclass(frozen=True)
class ParseResult:
    ok: bool
    source: str = ""
    reason: str = ""


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return ast.dump(fn.args, annotate_fields=True, include_attributes=False) + "|" + "|".join(
        ast.dump(d, include_attributes=False) for d in fn.decorator_list
    )


def parse_python_response(text: str, original: str) -> ParseResult:
    block = extract_code_block(text, "python")
    if block is None:
        return ParseResult(False, reason="no code block in response")
    block = textwrap.dedent(block).strip("\n") + "\n"
    try:
        new_tree = ast.parse(block)
        old_tree = ast.parse(textwrap.dedent(original))
    except SyntaxError as exc:
        return ParseResult(False, reason=f"syntax error: {exc.msg} (line {exc.lineno})")
    old_fn = old_tree.body[0]
    assert isinstance(old_fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    matches = [n for n in new_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == old_fn.name]
    if not matches:
        return ParseResult(False, reason=f"response does not define {old_fn.name}()")
    new_fn = matches[-1]
    others = [n for n in new_tree.body if n is not new_fn and not isinstance(n, ast.Expr)]
    for n in others:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            return ParseResult(False, reason="response adds module-level imports (outside the locus)")
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign)):
            return ParseResult(False, reason=f"response defines extra top-level code ({type(n).__name__}) outside the locus")
    if type(new_fn) is not type(old_fn):
        return ParseResult(False, reason="sync/async nature changed")
    if _signature(new_fn) != _signature(old_fn):
        return ParseResult(False, reason="signature or decorators changed")
    seg = ast.get_source_segment(block, new_fn)
    if seg is None:
        return ParseResult(False, reason="could not extract function source")
    start = new_fn.decorator_list[0].lineno if new_fn.decorator_list else new_fn.lineno
    lines = block.splitlines()
    source = "\n".join(lines[start - 1 : new_fn.end_lineno]) + "\n"
    if source.strip() == textwrap.dedent(original).strip():
        return ParseResult(False, reason="response is identical to the original")
    return ParseResult(True, source=source)


def _c_signature(src: str, name: str) -> str | None:
    head = src.split("{", 1)[0]
    if f"{name}" not in head or "(" not in head:
        return None
    head = re.sub(r"/\*.*?\*/|//[^\n]*", " ", head, flags=re.S)
    normalized: str = " ".join(head.replace("(", " ( ").replace(")", " ) ").replace(",", " , ").replace("*", " * ").split())
    return normalized


def parse_c_response(text: str, original: str, name: str) -> ParseResult:
    block = extract_code_block(text, "c")
    if block is None:
        return ParseResult(False, reason="no code block in response")
    # Keep only the function definition: from the signature line to the matching brace.
    m = re.search(rf"^[^\n;{{}}#]*\b{re.escape(name)}\s*\([^)]*\)\s*\{{", block, flags=re.M)
    if not m:
        return ParseResult(False, reason=f"response does not define {name}()")
    start = m.start()
    depth, end = 0, None
    i = block.index("{", m.start())
    for j in range(i, len(block)):
        ch = block[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end is None:
        return ParseResult(False, reason="unbalanced braces")
    source = block[start:end].strip() + "\n"
    if _c_signature(source, name) != _c_signature(original, name):
        return ParseResult(False, reason="C signature changed")
    if "static " in source.split("{", 1)[1] and re.search(r"\bstatic\b(?![^;]*\bconst\b)", source.split("{", 1)[1]):
        return ParseResult(False, reason="introduces static mutable state")
    if source.strip() == original.strip():
        return ParseResult(False, reason="response is identical to the original")
    return ParseResult(True, source=source)


def summarize_failures(reasons: Sequence[str], limit: int = 5) -> tuple[str, ...]:
    seen: list[str] = []
    for r in reasons:
        short = r.strip().splitlines()[0][:200] if r.strip() else ""
        if short and short not in seen:
            seen.append(short)
    return tuple(seen[-limit:])
