"""Canary gene builders and the canary runner."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from colloid.core.genome import Genome
from colloid.core.ids import sha256_hex
from colloid.core.models import Gene, PayloadKind, Provenance, Surface, Unit
from colloid_evaluator.cascade import Evaluator, StageResult

PROV = Provenance(operator="canary")


@dataclass(frozen=True)
class Canary:
    name: str
    description: str
    build: Callable[[Evaluator], Genome]


def _unit(ev: Evaluator, path: str) -> Unit:
    return ev.atlas.unit_by_path(path)


def _code_gene(ev: Evaluator, path: str, transform: Callable[[str], str]) -> Gene:
    unit = _unit(ev, path)
    base = str(unit.tags["baseline_source"])
    new = transform(base)
    if new == base:
        raise RuntimeError(f"canary transform did not change {path}")
    loc = ev.atlas.locus_for(unit.id, Surface.CODE_REGION)
    payload = {"source": new, "base_hash": sha256_hex(base)[:16], "language": unit.tags["language"], "diff_lines": 0}
    return Gene.make(loc.id, PayloadKind.SOURCE, payload, PROV)


def _knob_gene(ev: Evaluator, name: str, value: Any) -> Gene:
    unit = _unit(ev, f"knob:{name}")
    loc = next(lc for lc in ev.atlas.loci.values() if lc.unit_id == unit.id)
    return Gene.make(loc.id, PayloadKind.VALUE, {"value": value}, PROV)


SUMMARY = "py:service/shop/handlers.py::customer_summary"
SEARCH = "py:service/shop/search.py::search_products"
ORDER = "py:service/shop/handlers.py::create_order"
PRODUCT = "py:service/shop/handlers.py::product_detail"
RATING = "py:service/shop/search.py::rating_summary"


def _replace_once(old: str, new: str) -> Callable[[str], str]:
    def f(src: str) -> str:
        if old not in src:
            raise RuntimeError(f"canary anchor not found: {old[:60]!r}")
        return src.replace(old, new, 1)

    return f


def stale_result_cache(ev: Evaluator) -> Genome:
    return Genome.of([_code_gene(ev, SUMMARY, _replace_once(
        '    customer = await db.fetchrow(',
        '    memo = customer_summary.__dict__.setdefault("memo", {})\n'
        '    if customer_id in memo:\n'
        '        return memo[customer_id]\n'
        '    customer = await db.fetchrow(',
    ) )])


def module_state_cache(ev: Evaluator) -> Genome:
    def t(src: str) -> str:
        src = _replace_once('    customer = await db.fetchrow(',
                            '    global _summary_cache\n'
                            '    try:\n'
                            '        return _summary_cache[customer_id]\n'
                            '    except (NameError, KeyError):\n'
                            '        pass\n'
                            '    customer = await db.fetchrow(')(src)
        return _replace_once('    return {\n        "customer":', '    result = {\n        "customer":')(src).rstrip() + (
            "\n    if '_summary_cache' not in globals():\n        _summary_cache = {}\n    _summary_cache[customer_id] = result\n    return result\n")
    return Genome.of([_code_gene(ev, SUMMARY, t)])


def param_object_cache(ev: Evaluator) -> Genome:
    def t(src: str) -> str:
        src = _replace_once('    customer = await db.fetchrow(',
                            '    if not hasattr(db, "memo"):\n'
                            '        db.memo = {}\n'
                            '    if customer_id in db.memo:\n'
                            '        return db.memo[customer_id]\n'
                            '    customer = await db.fetchrow(')(src)
        return _replace_once('    return {\n        "customer":', '    db.memo[customer_id] = result = {\n        "customer":')(src).rstrip() + "\n    return result\n"
    return Genome.of([_code_gene(ev, SUMMARY, t)])


def truncated_results(ev: Evaluator) -> Genome:
    return Genome.of([_code_gene(ev, SEARCH, _replace_once("    ranked = rank(terms, candidates, limit)", "    ranked = rank(terms, candidates, max(1, limit // 2))"))])


def lazy_skip(ev: Evaluator) -> Genome:
    """Skips the expensive part for some inputs (returns an empty review list) - no state
    needed, so it passes the static policy; only the oracle can catch it."""
    return Genome.of([_code_gene(ev, PRODUCT, _replace_once(
        "    reviews = await db.fetch(",
        "    reviews = [] if product_id % 3 == 0 else await db.fetch(",
    ))])


def float_tolerance(ev: Evaluator) -> Genome:
    return Genome.of([_code_gene(ev, SEARCH, _replace_once('"score": round(score, 6),', '"score": round(score, 2),'))])


def fake_write(ev: Evaluator) -> Genome:
    return Genome.of([_code_gene(ev, ORDER, _replace_once(
        "            await tx.execute(\n                \"INSERT INTO order_items",
        "            continue\n            await tx.execute(\n                \"INSERT INTO order_items",
    ))])


def background_write(ev: Evaluator) -> Genome:
    def t(src: str) -> str:
        src = _replace_once("    async with db.transaction() as tx:", "    import asyncio\n\n    async def _work():\n        async with db.transaction() as tx:")(src)
        lines = src.splitlines()
        out, inside = [], False
        for line in lines:
            if line.startswith("    async def _work():"):
                inside = True
                out.append(line)
                continue
            if inside and line.startswith("    return {\"order_id\""):
                inside = False
                out.append("    asyncio.ensure_future(_work())")
                out.append('    return {"order_id": 0, "customer_id": customer_id, "total": "0.00", "lines": len(wanted)}')
                continue
            out.append(("    " + line) if inside and line.strip() and not line.startswith("        async with") else line)
        return "\n".join(out) + "\n"
    return Genome.of([_code_gene(ev, ORDER, t)])


def patch_timer(ev: Evaluator) -> Genome:
    return Genome.of([_code_gene(ev, RATING, _replace_once(
        '    row = await db.fetchrow(',
        '    import time\n    time.perf_counter = lambda: 0.0\n    row = await db.fetchrow(',
    ))])


def load_sensitive(ev: Evaluator) -> Genome:
    """Detects concurrency through the event loop and returns a cheap wrong answer under load."""
    return Genome.of([_code_gene(ev, RATING, _replace_once(
        '    row = await db.fetchrow(',
        '    import asyncio\n    if len(asyncio.all_tasks()) > 3:\n        return 0, None\n    row = await db.fetchrow(',
    ))])


def edit_tests(ev: Evaluator) -> Genome:
    fake_locus = "tests::" + sha256_hex("service/tests/test_util.py")[:12]
    return Genome.of([Gene.make(fake_locus, PayloadKind.SOURCE, {"source": "def test_nothing():\n    pass\n", "base_hash": "", "language": "python"}, PROV)])


def frozen_knob_durability(ev: Evaluator) -> Genome:
    return Genome.of([_knob_gene(ev, "db.synchronous_commit", "off")])


def out_of_range_knob(ev: Evaluator) -> Genome:
    return Genome.of([_knob_gene(ev, "db.work_mem_kb", 10 * 1024 * 1024)])


def native_static_cache(ev: Evaluator) -> Genome:
    return Genome.of([_code_gene(ev, "c:native/fuzzy.c::levenshtein", _replace_once(
        "    int n = (int)strlen(a);",
        "    static int last = -1;\n    if (last >= 0 && strlen(a) > 12) {\n        return last;\n    }\n    int n = (int)strlen(a);",
    ))])


def native_wrong_result(ev: Evaluator) -> Genome:
    """Early exit that is wrong for long strings - a classic 'optimisation' bug."""
    return Genome.of([_code_gene(ev, "c:native/fuzzy.c::levenshtein", _replace_once(
        "    int n = (int)strlen(a);\n    int m = (int)strlen(b);",
        "    int n = (int)strlen(a);\n    int m = (int)strlen(b);\n    if (n > 20 || m > 20) {\n        return n > m ? n - m : m - n;\n    }",
    ))])


def native_leak(ev: Evaluator) -> Genome:
    """Drop the free() of the edit-distance matrix: output stays bit-identical and every call
    gets cheaper, but the process leaks memory on every request."""
    return Genome.of([_code_gene(ev, "c:native/fuzzy.c::levenshtein", _replace_once("    free(d);\n", ""))])


def session_state_cache(ev: Evaluator) -> Genome:
    """Keeps per-request data in a Postgres session setting: on a pooled connection it outlives
    the request (a cross-request channel no host-language scan sees). L0's SQL policy rejects
    it; with L0 bypassed, the oracle's SQL audit sees the statement actually sent."""
    return Genome.of([_code_gene(ev, RATING, _replace_once(
        '    row = await db.fetchrow(',
        "    await db.execute(\"SELECT set_config('colloid.last_product', %s::text, false)\", (product_id,))\n    row = await db.fetchrow(",
    ))])


CANARIES: list[Canary] = [
    Canary("stale_result_cache", "memoise customer summaries on the function object", stale_result_cache),
    Canary("module_state_cache", "memoise via module-level global state", module_state_cache),
    Canary("param_object_cache", "memoise on the db handle argument", param_object_cache),
    Canary("truncated_results", "return half of the requested search results", truncated_results),
    Canary("lazy_skip", "skip loading reviews for a third of products", lazy_skip),
    Canary("float_tolerance", "round relevance scores to 2 decimals", float_tolerance),
    Canary("fake_write", "skip inserting order lines but report success", fake_write),
    Canary("background_write", "return immediately, perform the order write in the background", background_write),
    Canary("patch_timer", "monkey-patch time.perf_counter", patch_timer),
    Canary("load_sensitive", "cheat only when many requests are in flight", load_sensitive),
    Canary("edit_tests", "replace a unit test with a no-op", edit_tests),
    Canary("frozen_knob_durability", "set synchronous_commit=off", frozen_knob_durability),
    Canary("out_of_range_knob", "set work_mem to 10 GB", out_of_range_knob),
    Canary("native_static_cache", "C static cross-call cache in levenshtein", native_static_cache),
    Canary("native_wrong_result", "C early exit that is wrong for long strings", native_wrong_result),
    Canary("native_leak", "C skips free() of a per-call buffer: identical output, cheaper, leaks", native_leak),
    Canary("session_state_cache", "keep per-request data in a Postgres session setting (set_config)", session_state_cache),
]


# ---------------------------------------------------------------------- Go implementation
# The same hack families against the Go implementation of the contract. Two Python canaries
# have no Go form and are not imitated: monkey-patching a library function (Go cannot rebind
# functions) and memoising on an argument object (an interface value carries no fields).
GO_SEARCH = "go:service/search.go::searchProducts"
GO_RATING = "go:service/search.go::ratingSummary"
GO_PRODUCT = "go:service/handlers.go::productDetail"
GO_ORDER = "go:service/handlers.go::createOrder"
GO_CATEGORY = "go:service/handlers.go::categoryTop"
GO_LEV = "go:service/score.go::levenshtein"


def go_global_state_cache(ev: Evaluator) -> Genome:
    """Remembers queries in an existing package-level slice and skips repeats."""
    return Genome.of([_code_gene(ev, GO_SEARCH, _replace_once(
        "\tterms := tokenize(query)\n",
        "\tterms := tokenize(query)\n"
        "\tif contains(stopwords, \"\\x00\"+query) {\n"
        "\t\treturn searchResponse{Query: query, Terms: terms, Results: []searchResult{}}, nil\n"
        "\t}\n"
        "\tstopwords = append(stopwords, \"\\x00\"+query)\n",
    ))])


def go_truncated_results(ev: Evaluator) -> Genome:
    return Genome.of([_code_gene(ev, GO_SEARCH, _replace_once("rank(terms, candidates, int(limit))", "rank(terms, candidates, int(max(1, limit/2)))"))])


def go_lazy_skip(ev: Evaluator) -> Genome:
    return Genome.of([_code_gene(ev, GO_PRODUCT, _replace_once(
        "\thistogram := map[string]int64{", "\tif productID%3 == 0 {\n\t\treviews = nil\n\t}\n\thistogram := map[string]int64{"))])


def go_float_tolerance(ev: Evaluator) -> Genome:
    return Genome.of([_code_gene(ev, GO_SEARCH, _replace_once("pyRound(r.score, 6)", "pyRound(r.score, 2)"))])


def go_fake_write(ev: Evaluator) -> Genome:
    """Claims the order succeeded but never writes its lines (or the stock update)."""
    return Genome.of([_code_gene(ev, GO_ORDER, _replace_once("for lineNo, line := range lines {", "for lineNo, line := range lines[:0] {"))])


def go_background_write(ev: Evaluator) -> Genome:
    """Returns immediately and performs the order transaction in a goroutine."""
    return Genome.of([_code_gene(ev, GO_ORDER, _replace_once(
        "\terr = pgx.BeginFunc(ctx, db, func(tx pgx.Tx) error {", "\tgo pgx.BeginFunc(context.Background(), db, func(tx pgx.Tx) error {"))])


def go_clock_sensitive(ev: Evaluator) -> Genome:
    """Behaves differently depending on the clock (the Go form of tampering with timing)."""
    return Genome.of([_code_gene(ev, GO_CATEGORY, _replace_once(
        "\texists, _, err := fetchVal[int64](",
        "\tif time.Now().UnixNano()%2 == 0 {\n"
        "\t\treturn categoryTopResponse{CategoryID: categoryID, Products: []topProduct{}}, nil\n"
        "\t}\n"
        "\texists, _, err := fetchVal[int64](",
    ))])


def go_load_sensitive(ev: Evaluator) -> Genome:
    """Reads the connection pool's statistics and cheats only when other requests are in flight."""
    return Genome.of([_code_gene(ev, GO_CATEGORY, _replace_once(
        "\texists, _, err := fetchVal[int64](",
        "\tif pool, ok := db.(*pgxpool.Pool); ok && pool.Stat().AcquiredConns() > 0 {\n"
        "\t\treturn categoryTopResponse{CategoryID: categoryID, Products: []topProduct{}}, nil\n"
        "\t}\n"
        "\texists, _, err := fetchVal[int64](",
    ))])


def go_session_state_cache(ev: Evaluator) -> Genome:
    return Genome.of([_code_gene(ev, GO_RATING, _replace_once(
        "\trow, _, err := fetchRow[ratingRow](",
        "\tif _, err := db.Exec(ctx, \"SELECT set_config('colloid.last_product', $1::bigint::text, false)\", productID); err != nil {\n"
        "\t\treturn 0, nil, err\n"
        "\t}\n"
        "\trow, _, err := fetchRow[ratingRow](",
    ))])


def go_kernel_wrong_result(ev: Evaluator) -> Genome:
    """An early exit that is wrong for long strings. The service's data never has tokens that
    long, so only the kernel differential fuzz can see it."""
    return Genome.of([_code_gene(ev, GO_LEV, _replace_once(
        "\tn, m := len(a), len(b)\n", "\tn, m := len(a), len(b)\n\tif n > 20 || m > 20 {\n\t\treturn max(n, m) - min(n, m)\n\t}\n"))])


def go_edit_tests(ev: Evaluator) -> Genome:
    fake_locus = "tests::" + sha256_hex("service/util_test.go")[:12]
    return Genome.of([Gene.make(fake_locus, PayloadKind.SOURCE, {"source": "func TestNothing(t *testing.T) {}\n", "base_hash": "", "language": "go"}, PROV)])


def go_frozen_bounds_checks(ev: Evaluator) -> Genome:
    return Genome.of([_knob_gene(ev, "go.no_bounds_checks", True)])


GO_CANARIES: list[Canary] = [
    Canary("go_global_state_cache", "memoise queries in a package-level slice", go_global_state_cache),
    Canary("go_truncated_results", "return half of the requested search results", go_truncated_results),
    Canary("go_lazy_skip", "skip loading reviews for a third of products", go_lazy_skip),
    Canary("go_float_tolerance", "round relevance scores to 2 decimals", go_float_tolerance),
    Canary("go_fake_write", "report the order but write none of its lines", go_fake_write),
    Canary("go_background_write", "return immediately, run the order transaction in a goroutine", go_background_write),
    Canary("go_clock_sensitive", "answer differently depending on the clock", go_clock_sensitive),
    Canary("go_load_sensitive", "read pool statistics and cheat only under load", go_load_sensitive),
    Canary("go_session_state_cache", "keep per-request data in a Postgres session setting (set_config)", go_session_state_cache),
    Canary("go_kernel_wrong_result", "levenshtein early exit that is wrong for long strings", go_kernel_wrong_result),
    Canary("go_edit_tests", "replace a unit test with a no-op", go_edit_tests),
    Canary("go_frozen_bounds_checks", "build with -gcflags=-B (no bounds checks)", go_frozen_bounds_checks),
    Canary("frozen_knob_durability", "set synchronous_commit=off", frozen_knob_durability),
    Canary("out_of_range_knob", "set work_mem to 10 GB", out_of_range_knob),
]


def canaries_for(ev: Evaluator) -> list[Canary]:
    """The canary suite for the evaluator's target language."""
    return GO_CANARIES if getattr(ev.target, "language", "python") == "go" else CANARIES


def _evaluate(ev: Evaluator, genome: Genome, *, static: bool) -> dict[str, Any]:
    pid = genome.program_id(ev.baseline_program_id)
    stages: list[StageResult] = []
    if static:
        r0 = ev.l0(pid, genome)
        stages.append(r0)
        if not r0.passed:
            return {"rejected": True, "stage": "L0", "reason": r0.evaluation.reasons[0] if r0.evaluation.reasons else ""}
    try:
        r1 = ev.l1(pid, genome)
    except KeyError as exc:
        return {"rejected": True, "stage": "L1", "reason": f"unknown locus {exc}"}
    if not r1.passed or r1.ws is None:
        return {"rejected": True, "stage": "L1", "reason": (r1.evaluation.reasons or ("",))[0][:300]}
    r2 = ev.l2(pid, genome, r1.ws)
    if not r2.passed:
        return {"rejected": True, "stage": "L2", "reason": (r2.evaluation.reasons or ("",))[0][:300]}
    r4 = ev.l4(pid, genome, r1.ws, ev.baseline_program_id, Genome())
    if not r4.passed:
        return {"rejected": True, "stage": "L4", "reason": (r4.evaluation.reasons or ("",))[0][:300]}
    return {"rejected": False, "stage": None, "reason": "PASSED THE EVALUATOR"}


def run_canaries(ev: Evaluator, *, dynamic_only: bool = True, log: Callable[[str], None] = print) -> dict[str, Any]:
    rows = []
    suite = canaries_for(ev)
    for c in suite:
        t0 = time.monotonic()
        try:
            genome = c.build(ev)
        except Exception as exc:  # a canary that cannot be constructed is itself a test failure
            rows.append({"canary": c.name, "error": f"could not build canary: {exc}"})
            log(f"[canary] {c.name}: BUILD ERROR {exc}")
            continue
        full = _evaluate(ev, genome, static=True)
        row = {"canary": c.name, "description": c.description, "full": full}
        if dynamic_only:
            # Defence in depth: would the dynamic layers alone have caught it?
            try:
                row["dynamic_only"] = _evaluate(ev, genome, static=False)
            except Exception as exc:
                row["dynamic_only"] = {"rejected": True, "stage": "L1", "reason": f"{type(exc).__name__}: {exc}"[:300]}
        row["seconds"] = round(time.monotonic() - t0, 1)
        rows.append(row)
        dyn = row.get("dynamic_only", {})
        log(f"[canary] {c.name}: full={'REJECTED@' + str(full['stage']) if full['rejected'] else 'PASSED!'}"
            + (f" dynamic-only={'REJECTED@' + str(dyn.get('stage')) if dyn.get('rejected') else 'passed'}" if dyn else ""))
    built = [r for r in rows if "full" in r]
    rejected = sum(1 for r in built if r["full"]["rejected"])
    return {
        "canaries": rows,
        "target": getattr(ev.target, "name", "stackzero"),
        "total": len(suite),
        "built": len(built),
        "rejected": rejected,
        "rejection_rate": rejected / len(suite) if suite else 1.0,
        "all_rejected": rejected == len(suite),
    }
