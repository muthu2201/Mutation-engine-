"""Static Stack Atlas construction for StackZero (blueprint A1, "how static analysis builds it").

Inputs and what each contributes:

* Python sources (``ast`` adapter)  → MODULE and FUNCTION units, ``calls`` edges resolved
  through each module's imports (``handlers.product_detail`` → handlers.py::product_detail)
* C sources (clang JSON AST)        → FUNCTION units of libshopnative with C→C ``calls``
* ctypes bindings                   → *cross-language* ``calls`` edges (native.score →
  shop_score_batch), found by matching ``lib.<symbol>`` attribute calls to C functions
* SQL string literals               → QUERY units with ``queries`` edges from the function
  that issues them, and ``depends_on`` edges to the TABLE units they read/write
* routes in app.py                  → ENDPOINT units with ``calls`` edges to handlers
* knobs.yaml                        → KNOB units with ``configures`` edges
* resource model                    → RESOURCE units (cpu, memory, io, net) and
  ``executes_on`` edges from components

Static request paths (endpoint → handler → callees → queries → tables) are added with equal
weights; the dynamic profiler later re-weights them with measured time shares and adds the
causal leverage tags.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path

from colloid.adapters.code.c_clang import ClangCCode
from colloid.adapters.code.python_ast import PythonAstCode, normalize_sql
from colloid.core.atlas import StackAtlas
from colloid.core.ids import content_hash, sha256_hex
from colloid.core.knobs import KnobSpec
from colloid.core.models import (
    AtlasPath,
    Edge,
    EdgeKind,
    EdgeSource,
    Layer,
    PathKind,
    Surface,
    Unit,
    UnitKind,
)

PY_FILES = ("service/shop/handlers.py", "service/shop/search.py", "service/shop/util.py", "service/shop/native.py",
            "service/shop/app.py", "service/shop/db.py", "service/shop/config.py")
C_FILES = ("native/tokenize.c", "native/fuzzy.c", "native/score.c")
MUTABLE_PY = {"service/shop/handlers.py", "service/shop/search.py", "service/shop/util.py", "service/shop/native.py"}
REVIEW_ONLY_PY = {"service/shop/app.py", "service/shop/db.py", "service/shop/config.py"}

ENDPOINTS = {
    "GET /products/search": "py:service/shop/search.py::search_products",
    "GET /products/{id}": "py:service/shop/handlers.py::product_detail",
    "GET /customers/{id}/summary": "py:service/shop/handlers.py::customer_summary",
    "GET /customers/{id}/recommendations": "py:service/shop/handlers.py::recommendations",
    "GET /categories/{id}/top": "py:service/shop/handlers.py::category_top",
    "GET /reports/daily": "py:service/shop/handlers.py::daily_report",
    "POST /orders": "py:service/shop/handlers.py::create_order",
}
TABLES = ("categories", "customers", "products", "orders", "order_items", "reviews")
COMPONENTS = {
    "os.kernel": Layer.OS,
    "alloc.malloc": Layer.ALLOC,
    "compiler.cc": Layer.COMPILER,
    "native.libshopnative": Layer.NATIVE,
    "runtime.python": Layer.RUNTIME,
    "runtime.uvicorn": Layer.RUNTIME,
    "db.postgres": Layer.DB,
    "db.planner": Layer.DB,
    "db.storage": Layer.DB,
    "svc.shop": Layer.SVC,
}
COMPONENT_RESOURCES = {
    "os.kernel": ("cpu", "memory"),
    "alloc.malloc": ("memory", "cpu"),
    "compiler.cc": ("cpu",),
    "native.libshopnative": ("cpu", "memory"),
    "runtime.python": ("cpu", "memory"),
    "runtime.uvicorn": ("cpu", "net"),
    "db.postgres": ("cpu", "memory", "io"),
    "db.planner": ("cpu",),
    "db.storage": ("io", "memory"),
    "svc.shop": ("cpu",),
}
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_]+)", re.I)


def query_unit_path(sql: str) -> str:
    return "sql:" + content_hash("sql", normalize_sql(sql), length=12)


def _uid(path: str) -> str:
    return Unit.make_id(path)


def _imports(tree: ast.Module) -> dict[str, str]:
    """alias → module file for ``from shop import x`` / ``import shop.x as y`` statements."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "shop":
            for a in node.names:
                out[a.asname or a.name] = f"service/shop/{a.name}.py"
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("shop."):
            mod = node.module.split(".", 1)[1]
            for a in node.names:
                out[a.asname or a.name] = f"service/shop/{mod}.py::{a.name}"
    return out


def add_base_units(atlas: StackAtlas, components: dict[str, Layer], component_resources: dict[str, tuple[str, ...]]) -> None:
    """Layers, resources, components (with ``executes_on`` edges) and the shop tables."""
    for layer in Layer:
        atlas.add_unit(Unit(id=_uid(f"layer:{layer.value}"), kind=UnitKind.LAYER, layer=layer, name=layer.value, symbol_path=f"layer:{layer.value}"))
    for res in ("cpu", "memory", "io", "net"):
        atlas.add_unit(Unit(id=_uid(f"resource:{res}"), kind=UnitKind.RESOURCE, layer=Layer.OS, name=res, symbol_path=f"resource:{res}"))
    for comp, layer in components.items():
        u = atlas.add_unit(
            Unit(
                id=_uid(f"component:{comp}"), kind=UnitKind.COMPONENT, layer=layer, name=comp, symbol_path=f"component:{comp}",
                parent_id=_uid(f"layer:{layer.value}"), tags={"resources": list(component_resources[comp])},
            )
        )
        for res in component_resources[comp]:
            atlas.add_edge(Edge(src=u.id, dst=_uid(f"resource:{res}"), kind=EdgeKind.EXECUTES_ON))
    for t in TABLES:
        atlas.add_unit(
            Unit(id=_uid(f"table:{t}"), kind=UnitKind.TABLE, layer=Layer.DB, name=t, symbol_path=f"table:{t}",
                 parent_id=_uid("component:db.storage"), tags={"resources": ["io", "memory"]})
        )


def add_query_unit(atlas: StackAtlas, queries: dict[str, str], sql: str, issuer_id: str) -> None:
    """A QUERY unit for ``sql`` (shared by every function issuing the same normalised SQL),
    ``depends_on`` edges to the tables it reads/writes and a ``queries`` edge from the issuer."""
    qpath = query_unit_path(sql)
    if qpath not in queries:
        q = atlas.add_unit(
            Unit(id=_uid(qpath), kind=UnitKind.QUERY, layer=Layer.DB, name=sql[:90], symbol_path=qpath,
                 parent_id=_uid("component:db.planner"), content_hash=sha256_hex(sql)[:16], adapter="sql",
                 tags={"sql": sql, "resources": ["cpu", "io"], "mutability": "frozen"})
        )
        queries[qpath] = q.id
        for table in {m.lower() for m in _TABLE_RE.findall(sql)}:
            if table in TABLES:
                atlas.add_edge(Edge(src=q.id, dst=_uid(f"table:{table}"), kind=EdgeKind.DEPENDS_ON))
    atlas.add_edge(Edge(src=issuer_id, dst=queries[qpath], kind=EdgeKind.QUERIES))


def build_static_atlas(root: Path, knobs: Sequence[KnobSpec]) -> StackAtlas:
    atlas = StackAtlas()
    py = PythonAstCode()
    cc = ClangCCode(include_dirs=[str(root / "native")])

    # Layers, components, resources.
    add_base_units(atlas, COMPONENTS, COMPONENT_RESOURCES)

    # C units first (Python → C edges need them).
    c_names: dict[str, str] = {}
    for rel in C_FILES:
        mod_path = f"module:{rel}"
        mod = atlas.add_unit(
            Unit(id=_uid(mod_path), kind=UnitKind.MODULE, layer=Layer.NATIVE, name=rel, symbol_path=mod_path,
                 parent_id=_uid("component:native.libshopnative"), content_hash=sha256_hex((root / rel).read_text())[:16],
                 adapter="c_clang", tags={"language": "c", "license": "Apache-2.0"})
        )
        for cu in cc.units(root, rel):
            u = atlas.add_unit(
                Unit(
                    id=_uid(cu.symbol_path), kind=UnitKind.FUNCTION, layer=Layer.NATIVE, name=cu.name, symbol_path=cu.symbol_path,
                    parent_id=mod.id, content_hash=sha256_hex(cu.source)[:16], adapter="c_clang",
                    tags={
                        "language": "c", "file": rel, "start_line": cu.start_line, "end_line": cu.end_line,
                        "risk_class": "B", "mutability": "allowed", "license": "Apache-2.0", "baseline_source": cu.source,
                        "resources": ["cpu", "memory"],
                    },
                )
            )
            atlas.add_locus(u.id, Surface.CODE_REGION)
            c_names[cu.name] = u.id
    for rel in C_FILES:
        for cu in cc.units(root, rel):
            for callee in cu.calls:
                if callee in c_names:
                    atlas.add_edge(Edge(src=_uid(cu.symbol_path), dst=c_names[callee], kind=EdgeKind.CALLS))

    # Python units.
    queries: dict[str, str] = {}
    py_units: dict[str, dict[str, object]] = {}
    for rel in PY_FILES:
        text = (root / rel).read_text()
        tree = ast.parse(text)
        mod_path = f"module:{rel}"
        mutability = "allowed" if rel in MUTABLE_PY else "review-only"
        mod = atlas.add_unit(
            Unit(id=_uid(mod_path), kind=UnitKind.MODULE, layer=Layer.SVC, name=rel, symbol_path=mod_path,
                 parent_id=_uid("component:svc.shop"), content_hash=sha256_hex(text)[:16], adapter="python_ast",
                 tags={"language": "python", "license": "Apache-2.0"})
        )
        names = py.module_names(text)
        imports = _imports(tree)
        for cu in py.units_from_text(text, rel):
            u = atlas.add_unit(
                Unit(
                    id=_uid(cu.symbol_path), kind=UnitKind.FUNCTION, layer=Layer.SVC, name=cu.name, symbol_path=cu.symbol_path,
                    parent_id=mod.id, content_hash=sha256_hex(cu.source)[:16], adapter="python_ast",
                    tags={
                        "language": "python", "file": rel, "start_line": cu.start_line, "end_line": cu.end_line,
                        "is_async": cu.is_async, "risk_class": "B", "mutability": mutability, "license": "Apache-2.0",
                        "baseline_source": cu.source, "module_names": names, "resources": ["cpu"],
                    },
                )
            )
            atlas.add_locus(u.id, Surface.CODE_REGION)
            py_units[cu.symbol_path] = {"unit": u, "calls": cu.calls, "sql": cu.sql, "rel": rel, "imports": imports, "locals": {x.name for x in py.units_from_text(text, rel)}}
            for sql in cu.sql:
                add_query_unit(atlas, queries, sql, u.id)

    # Resolve Python calls.
    for info in py_units.values():
        src_id = info["unit"].id  # type: ignore[attr-defined]
        rel = str(info["rel"])
        imports: dict[str, str] = info["imports"]  # type: ignore[assignment]
        local_names: set[str] = info["locals"]  # type: ignore[assignment]
        for call in info["calls"]:  # type: ignore[attr-defined]
            target = None
            parts = call.split(".")
            if len(parts) == 1 and call in local_names:
                target = f"py:{rel}::{call}"
            elif len(parts) == 1 and call in imports and "::" in imports[call]:
                f, name = imports[call].split("::")
                target = f"py:{f}::{name}"
            elif len(parts) == 2 and parts[0] in imports and "::" not in imports[parts[0]]:
                target = f"py:{imports[parts[0]]}::{parts[1]}"
            elif len(parts) == 2 and parts[0] in ("lib", "_lib") and parts[1] in c_names:
                atlas.add_edge(Edge(src=src_id, dst=c_names[parts[1]], kind=EdgeKind.CALLS))
                continue
            if target and _uid(target) in atlas.units and _uid(target) != src_id:
                atlas.add_edge(Edge(src=src_id, dst=_uid(target), kind=EdgeKind.CALLS))

    add_endpoints_knobs_paths(atlas, ENDPOINTS, knobs, queries)
    return atlas


def add_endpoints_knobs_paths(atlas: StackAtlas, endpoints: dict[str, str], knobs: Sequence[KnobSpec], queries: dict[str, str]) -> None:
    """ENDPOINT units, KNOB units with their loci and ``configures`` edges, the static request
    paths (endpoint → reachable functions → queries → tables) and the coverage demotion."""
    # Endpoints.
    for route, handler in endpoints.items():
        ep = atlas.add_unit(Unit(id=_uid(f"endpoint:{route}"), kind=UnitKind.ENDPOINT, layer=Layer.SVC, name=route,
                                 symbol_path=f"endpoint:{route}", parent_id=_uid("component:svc.shop")))
        atlas.add_edge(Edge(src=ep.id, dst=_uid(handler), kind=EdgeKind.CALLS))

    # Knobs.
    for spec in knobs:
        layer = Layer(spec.layer)
        comp = next((a.split(":", 1)[1] for a in spec.affects if a.startswith("component:")), None)
        parent = _uid(f"component:{comp}") if comp else _uid(f"layer:{layer.value}")
        if spec.mechanism == "index":
            parent = _uid("component:db.storage")
        ku = atlas.add_unit(
            Unit(
                id=_uid(f"knob:{spec.name}"), kind=UnitKind.KNOB, layer=layer, name=spec.name, symbol_path=f"knob:{spec.name}",
                parent_id=parent, adapter="knobs.yaml",
                tags={
                    "knob": spec.name, "mechanism": spec.mechanism, "risk_class": spec.risk_class, "mutability": spec.mutability,
                    "resources": list(spec.resources), "description": spec.description,
                    "knob_leverage": 0.6 if spec.mechanism in ("index", "guc_session", "cflag") else 0.4,
                },
            )
        )
        surface = Surface.INDEX_SET if spec.mechanism == "index" else Surface.COMPILER_FLAGS if spec.mechanism == "cflag" else Surface.KNOB
        atlas.add_locus(ku.id, surface)
        for a in spec.affects:
            if _uid(a) in atlas.units:
                atlas.add_edge(Edge(src=ku.id, dst=_uid(a), kind=EdgeKind.CONFIGURES))
        if spec.mechanism == "index":
            # an index configures every query that reads its table
            tables = [a for a in spec.affects if a.startswith("table:")]
            for q in queries.values():
                if any(e.dst == _uid(t) for t in tables for e in atlas.out_edges(q, EdgeKind.DEPENDS_ON)):
                    atlas.add_edge(Edge(src=ku.id, dst=q, kind=EdgeKind.CONFIGURES))

    # Static request paths: endpoint → reachable functions (calls) → queries → tables.
    for route in endpoints:
        ep_id = _uid(f"endpoint:{route}")
        reach = atlas.reachable(ep_id, [EdgeKind.CALLS, EdgeKind.QUERIES, EdgeKind.DEPENDS_ON])
        ordered = [ep_id] + sorted(u for u in reach if u != ep_id)
        atlas.paths.append(AtlasPath(id=content_hash("path", route, "static"), kind=PathKind.REQUEST, unit_ids=tuple(ordered),
                                     weight=1.0 / len(endpoints), workload_id="static"))
    # Units unreachable from any endpoint have no oracle coverage: demote to review-only.
    covered: set[str] = set()
    for p in atlas.paths:
        covered.update(p.unit_ids)
    for uid, u in list(atlas.units.items()):
        if u.kind == UnitKind.FUNCTION:
            atlas.set_dynamic(uid, "test_coverage", "endpoint" if uid in covered else "none")


def static_edge_source() -> EdgeSource:
    return EdgeSource.STATIC
