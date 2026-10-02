"""Static Stack Atlas for the Go implementation of the StackZero shop contract.

The same construction as the Python implementation's builder, through the same helpers
(``add_base_units``, ``add_query_unit``, ``add_endpoints_knobs_paths``), with Go's own parser
(``GoAstCode``) in place of CPython's ``ast`` and clang:

* Go sources → MODULE and FUNCTION units; ``calls`` edges resolved by name inside the
  package (``fetch[T](...)`` → ``fetch``; ``a.dispatch(...)`` → the method ``app.dispatch``);
* SQL in string constants → QUERY units. Placeholders are normalised (``$1`` → ``%s``), so a
  query issued by both implementations is *one* query unit with one id: the database layer
  is shared, and the Atlas says so;
* the route table → ENDPOINT units; the shared OS/database knobs plus the Go runtime and
  build knobs → KNOB units; static request paths endpoint → functions → queries → tables.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from colloid.adapters.code.go_ast import GoAstCode
from colloid.adapters.target.stackzero.atlas_builder import (
    add_base_units,
    add_endpoints_knobs_paths,
    add_query_unit,
)
from colloid.core.atlas import StackAtlas
from colloid.core.ids import sha256_hex
from colloid.core.knobs import KnobSpec
from colloid.core.models import Edge, EdgeKind, Layer, Surface, Unit, UnitKind

SERVICE = "service"
GO_FILES = ("service/handlers.go", "service/search.go", "service/util.go", "service/score.go",
            "service/main.go", "service/db.go", "service/config.go")
MUTABLE_GO = {"service/handlers.go", "service/search.go", "service/util.go", "service/score.go"}
KERNEL_GO = {"service/score.go"}  # the ranking kernel: the role libshopnative plays in the Python implementation
BUILD_FILES = ("service/go.mod", "service/go.sum")

ENDPOINTS = {
    "GET /products/search": "go:service/search.go::searchProducts",
    "GET /products/{id}": "go:service/handlers.go::productDetail",
    "GET /customers/{id}/summary": "go:service/handlers.go::customerSummary",
    "GET /customers/{id}/recommendations": "go:service/handlers.go::recommendations",
    "GET /categories/{id}/top": "go:service/handlers.go::categoryTop",
    "GET /reports/daily": "go:service/handlers.go::dailyReport",
    "POST /orders": "go:service/handlers.go::createOrder",
}
COMPONENTS = {
    "os.kernel": Layer.OS,
    "compiler.go": Layer.COMPILER,
    "runtime.go": Layer.RUNTIME,
    "db.postgres": Layer.DB,
    "db.planner": Layer.DB,
    "db.storage": Layer.DB,
    "svc.shop": Layer.SVC,
}
COMPONENT_RESOURCES = {
    "os.kernel": ("cpu", "memory"),
    "compiler.go": ("cpu",),
    "runtime.go": ("cpu", "memory", "net"),
    "db.postgres": ("cpu", "memory", "io"),
    "db.planner": ("cpu",),
    "db.storage": ("io", "memory"),
    "svc.shop": ("cpu",),
}
_PLACEHOLDER = re.compile(r"\$\d+")


def normalize_placeholders(sql: str) -> str:
    """``$1``-style (pgx) placeholders → ``%s`` (psycopg), so identical statements from
    different implementations are one query unit."""
    return _PLACEHOLDER.sub("%s", sql)


def _uid(path: str) -> str:
    return Unit.make_id(path)


def build_go_atlas(root: Path, knobs: Sequence[KnobSpec], code: GoAstCode) -> StackAtlas:
    atlas = StackAtlas()
    add_base_units(atlas, COMPONENTS, COMPONENT_RESOURCES)
    queries: dict[str, str] = {}
    by_name: dict[str, str] = {}
    methods: dict[str, list[str]] = {}
    calls: dict[str, tuple[str, ...]] = {}
    for rel in GO_FILES:
        text = (root / rel).read_text()
        mod_path = f"module:{rel}"
        mutability = "allowed" if rel in MUTABLE_GO else "review-only"
        mod = atlas.add_unit(
            Unit(id=_uid(mod_path), kind=UnitKind.MODULE, layer=Layer.SVC, name=rel, symbol_path=mod_path,
                 parent_id=_uid("component:svc.shop"), content_hash=sha256_hex(text)[:16], adapter="go_ast",
                 tags={"language": "go", "license": "Apache-2.0"})
        )
        for cu in code.units(root, rel):
            u = atlas.add_unit(
                Unit(
                    id=_uid(cu.symbol_path), kind=UnitKind.FUNCTION, layer=Layer.SVC, name=cu.name, symbol_path=cu.symbol_path,
                    parent_id=mod.id, content_hash=sha256_hex(cu.source)[:16], adapter="go_ast",
                    tags={
                        "language": "go", "file": rel, "start_line": cu.start_line, "end_line": cu.end_line, "risk_class": "B",
                        "mutability": mutability, "license": "Apache-2.0", "baseline_source": cu.source,
                        "signature": cu.extra["signature"], "body_lbrace": cu.extra["body_lbrace"], "kernel": rel in KERNEL_GO,
                        "resources": ["cpu", "memory"] if rel in KERNEL_GO else ["cpu"],
                    },
                )
            )
            atlas.add_locus(u.id, Surface.CODE_REGION)
            by_name[cu.name] = u.id
            if "." in cu.name:
                methods.setdefault(cu.name.split(".", 1)[1], []).append(u.id)
            calls[u.id] = cu.calls
            for sql in cu.sql:
                add_query_unit(atlas, queries, normalize_placeholders(sql), u.id)
    for src_id, names in calls.items():
        for call in names:
            target = by_name.get(call)
            if target is None and "." in call:
                candidates = methods.get(call.rsplit(".", 1)[1], [])
                target = candidates[0] if len(candidates) == 1 else None
            if target is not None and target != src_id:
                atlas.add_edge(Edge(src=src_id, dst=target, kind=EdgeKind.CALLS))
    add_endpoints_knobs_paths(atlas, ENDPOINTS, knobs, queries)
    return atlas
