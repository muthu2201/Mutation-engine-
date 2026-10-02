"""Red-team operator: programmatic attempts to fool the evaluator (blueprint D2.8).

The red-team island's fitness is "fool the evaluator". Its operators deliberately produce
*incorrect* variants that a careless harness would score as faster. Every one of them must
be rejected; any that survives L0-L5 is an **evaluator breach** - it raises a critical alert
and is turned into a new canary.

Each hack wraps the original function as an inner function and post-processes or
short-circuits its result:

* ``stale_cache``  - memoise results across calls on the function object (stale reads)
* ``truncate``     - return only half of every list in the result (partial work)
* ``round_floats`` - coarsen floats to one decimal (tolerance exploitation)
* ``skip_some``    - for a third of the inputs, replay the previous call's result (skipped work)

Randomised parameters (cache key coarsening, truncation ratio, rounding digits) probe the
*thresholds* of the oracles rather than only their existence.

**Ground truth for "breach".** An attack that passes the oracle is only a breach if it could
have changed an observable output. Wrapping a function whose result never carries the
tampered kind of value into a response (a startup hook returning ``None``, a config parser
returning ``bool``) is *inert*: the oracle is right to see no difference. Two defences:

* targeting (:func:`attack_reachable`): attack only functions that return a value; the
  caller also requires the unit to lie on an Atlas request path;
* liveness confirmation (``maximal=True``): the same hack on the same unit at its extreme
  setting (floats shifted instead of rounded, lists cut to one element, a cache that ignores
  its key, every call replaying the previous result). If even the maximal variant passes the
  oracle, the channel does not reach any response and the subtle variant was inert. If the
  maximal variant is caught, the subtle one slipping through is a genuine threshold breach.
"""

from __future__ import annotations

import ast
import random
import textwrap

HACKS = ("stale_cache", "truncate", "round_floats", "skip_some")


def _forward_args(args: ast.arguments) -> str:
    parts = [a.arg for a in args.posonlyargs] + [a.arg for a in args.args]
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    parts += [f"{a.arg}={a.arg}" for a in args.kwonlyargs]
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return ", ".join(parts)


def attack_reachable(source: str) -> bool:
    """The function returns a value somewhere, so a post-processing attack can reach callers."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return False
    fn = tree.body[0] if tree.body else None
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None and not (isinstance(node.value, ast.Constant) and node.value.value is None):
            return True
    return False


def infer_hack(variant_source: str) -> str | None:
    """Which hack produced a red-team variant (for re-adjudicating stored attacks)."""
    for marker, hack in (("_rt_cache", "stale_cache"), ("_rt_last", "skip_some"), ("_rt_round", "round_floats"), ("isinstance(_rt_r, list)", "truncate")):
        if marker in variant_source:
            return hack
    return None


def redteam_variant(source: str, hack: str, rng: random.Random, *, maximal: bool = False) -> str | None:
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return None
    fn = tree.body[0] if tree.body else None
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    is_async = isinstance(fn, ast.AsyncFunctionDef)
    aw = "await " if is_async else ""
    kw = "async def" if is_async else "def"
    src = textwrap.dedent(source)
    lines = src.splitlines()
    header_end = fn.body[0].lineno - 1  # index of first body line
    header = "\n".join(lines[fn.lineno - 1 : header_end])
    if fn.decorator_list:
        header = "\n".join(lines[fn.decorator_list[0].lineno - 1 : header_end])
    params = src[src.index("(", src.index(fn.name)) + 1 :]
    depth, end = 1, 0
    for i, ch in enumerate(params):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0:
            end = i
            break
    params = params[:end]
    body = textwrap.indent(textwrap.dedent("\n".join(lines[header_end:])), "        ")
    call = f"{aw}_rt_inner({_forward_args(fn.args)})"
    key = f"repr(({_forward_args(fn.args)},))"
    if hack == "stale_cache":
        coarse = "[:0]" if maximal else rng.choice(["", "[:12]", "[:24]"])
        post = (
            f"    _rt_store = {fn.name}.__dict__.setdefault('_rt_cache', {{}})\n"
            f"    _rt_key = {key}{coarse}\n"
            f"    if _rt_key in _rt_store:\n"
            f"        return _rt_store[_rt_key]\n"
            f"    _rt_r = {call}\n"
            f"    _rt_store[_rt_key] = _rt_r\n"
            f"    return _rt_r\n"
        )
    elif hack == "truncate":
        ratio = 0.0 if maximal else rng.choice([0.5, 0.8, 0.95])
        post = (
            f"    _rt_r = {call}\n"
            f"    if isinstance(_rt_r, dict):\n"
            f"        for _k, _v in list(_rt_r.items()):\n"
            f"            if isinstance(_v, list) and len(_v) > 1:\n"
            f"                _rt_r[_k] = _v[: max(1, int(len(_v) * {ratio}))]\n"
            f"    elif isinstance(_rt_r, list) and len(_rt_r) > 1:\n"
            f"        _rt_r = _rt_r[: max(1, int(len(_rt_r) * {ratio}))]\n"
            f"    return _rt_r\n"
        )
    elif hack == "round_floats":
        digits = rng.choice([1, 2, 4])
        tamper = "x + 1.0" if maximal else f"round(x, {digits})"
        post = (
            f"    _rt_r = {call}\n"
            f"    def _rt_round(x):\n"
            f"        if isinstance(x, float):\n"
            f"            return {tamper}\n"
            f"        if isinstance(x, dict):\n"
            f"            return {{k: _rt_round(v) for k, v in x.items()}}\n"
            f"        if isinstance(x, list):\n"
            f"            return [_rt_round(v) for v in x]\n"
            f"        return x\n"
            f"    return _rt_round(_rt_r)\n"
        )
    elif hack == "skip_some":
        post = (
            f"    _rt_state = {fn.name}.__dict__.setdefault('_rt_last', {{}})\n"
            f"    if 'r' in _rt_state and hash({key}) % {1 if maximal else 3} == 0:\n"
            f"        return _rt_state['r']\n"
            f"    _rt_r = {call}\n"
            f"    _rt_state['r'] = _rt_r\n"
            f"    return _rt_r\n"
        )
    else:
        raise ValueError(hack)
    out = f"{header}\n    {kw} _rt_inner({params}):\n{body}\n{post}"
    try:
        ast.parse(out)
    except SyntaxError:
        return None
    return out
