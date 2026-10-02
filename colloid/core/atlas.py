"""The Stack Atlas: a tagged, multi-resolution property graph of the target system.

The Atlas is how Colloid "dissects" a stack. It answers three questions the search needs:

1. **Where can we mutate?**  Units carry tags (layer, risk class, mutability, licence...),
   and each mutable unit exposes one or more *loci*.
2. **Where is the leverage?**  Dynamic analysis decorates units with ``hotness`` (sampling
   profiler share), ``latency_share`` (from request traces), ``causal_leverage`` (from
   delay-injection experiments) and ``dollar_share`` (cost model). Their combination, the
   *Locus Opportunity Score*, is what the budget scheduler uses to spend mutation effort.
3. **What interacts with what?**  Edges (``calls``, ``queries``, ``configures``,
   ``executes_on``) and *paths* (ordered walks such as "HTTP endpoint → handler →
   SQL query → table") tell the attribution engine which gene pairs are *likely* to be
   epistatic (they share a path or a resource) and should be tested first.

Hierarchy (``contains`` edges / ``parent_id``):

    Layer → Component → Module → Function → Region
                      ↘ Knob

Unit ids are hashes of symbolic paths, so tags survive mutation of the unit's content.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from colloid.core.models import (
    AtlasPath,
    Edge,
    EdgeKind,
    LeverageCurve,
    Locus,
    Mutability,
    RiskClass,
    Surface,
    Unit,
    UnitKind,
)


@dataclass(frozen=True)
class Region:
    """A named Atlas subgraph that gets its own island (e.g. ``db.config``, ``svc.code``).

    ``selector`` decides which loci belong to the region. Regions partition the *mutable*
    loci; a locus belongs to the first region whose selector accepts it.
    """

    name: str
    description: str
    selector: Callable[[Unit, Locus], bool]


@dataclass
class StackAtlas:
    units: dict[str, Unit] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    loci: dict[str, Locus] = field(default_factory=dict)
    paths: list[AtlasPath] = field(default_factory=list)
    leverage: dict[str, LeverageCurve] = field(default_factory=dict)
    # dynamic decorations, keyed by unit id; kept separate from the frozen Unit tags so the
    # static skeleton can be rebuilt without losing profiling results
    dynamic: dict[str, dict[str, Any]] = field(default_factory=lambda: defaultdict(dict))

    # ------------------------------------------------------------------ building
    def add_unit(self, unit: Unit) -> Unit:
        existing = self.units.get(unit.id)
        if existing is not None and existing.symbol_path != unit.symbol_path:
            raise ValueError(f"unit id collision: {existing.symbol_path} vs {unit.symbol_path}")
        self.units[unit.id] = unit
        if unit.parent_id is not None:
            self.add_edge(Edge(src=unit.parent_id, dst=unit.id, kind=EdgeKind.CONTAINS))
        return unit

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)
        self._invalidate()

    def add_locus(self, unit_id: str, surface: Surface) -> Locus:
        unit = self.units[unit_id]
        risk = RiskClass(unit.tags.get("risk_class", "A"))
        mut = Mutability(unit.tags.get("mutability", "allowed"))
        locus = Locus.make(unit_id, surface, risk, mut)
        self.loci[locus.id] = locus
        return locus

    def set_dynamic(self, unit_id: str, key: str, value: Any) -> None:
        self.dynamic[unit_id][key] = value

    def tag(self, unit_id: str, key: str, default: Any = None) -> Any:
        """Dynamic decorations take precedence over static tags."""
        dyn = self.dynamic.get(unit_id, {})
        if key in dyn:
            return dyn[key]
        return self.units[unit_id].tags.get(key, default)

    def _invalidate(self) -> None:
        self.__dict__.pop("_children_cache", None)
        self.__dict__.pop("_out_cache", None)

    # ------------------------------------------------------------------ queries
    def unit_by_path(self, symbol_path: str) -> Unit:
        uid = Unit.make_id(symbol_path)
        if uid not in self.units:
            raise KeyError(symbol_path)
        return self.units[uid]

    def locus(self, locus_id: str) -> Locus:
        return self.loci[locus_id]

    def locus_for(self, unit_id: str, surface: Surface) -> Locus:
        for loc in self.loci.values():
            if loc.unit_id == unit_id and loc.surface == surface:
                return loc
        raise KeyError(f"no {surface} locus on {unit_id}")

    def ancestors(self, unit_id: str) -> list[str]:
        out: list[str] = []
        cur = self.units[unit_id].parent_id
        guard = 0
        while cur is not None:
            out.append(cur)
            cur = self.units[cur].parent_id if cur in self.units else None
            guard += 1
            if guard > 64:
                raise ValueError(f"cycle in contains-hierarchy at {unit_id}")
        return out

    def units_related(self, unit_a: str, unit_b: str) -> bool:
        if unit_a == unit_b:
            return True
        return unit_a in self.ancestors(unit_b) or unit_b in self.ancestors(unit_a)

    def children(self, unit_id: str) -> list[str]:
        cache: dict[str, list[str]] | None = self.__dict__.get("_children_cache")
        if cache is None:
            cache = defaultdict(list)
            for u in self.units.values():
                if u.parent_id is not None:
                    cache[u.parent_id].append(u.id)
            self.__dict__["_children_cache"] = cache
        return list(cache.get(unit_id, []))

    def out_edges(self, unit_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        cache: dict[str, list[Edge]] | None = self.__dict__.get("_out_cache")
        if cache is None:
            cache = defaultdict(list)
            for e in self.edges:
                cache[e.src].append(e)
            self.__dict__["_out_cache"] = cache
        return [e for e in cache.get(unit_id, []) if kind is None or e.kind == kind]

    def in_edges(self, unit_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        return [e for e in self.edges if e.dst == unit_id and (kind is None or e.kind == kind)]

    def descendants(self, unit_id: str) -> list[str]:
        out: list[str] = []
        stack = self.children(unit_id)
        while stack:
            u = stack.pop()
            out.append(u)
            stack.extend(self.children(u))
        return out

    def reachable(self, unit_id: str, kinds: Iterable[EdgeKind]) -> set[str]:
        """Units reachable from ``unit_id`` following edges of the given kinds."""
        kinds = set(kinds)
        seen = {unit_id}
        frontier = [unit_id]
        while frontier:
            u = frontier.pop()
            for e in self.out_edges(u):
                if e.kind in kinds and e.dst not in seen:
                    seen.add(e.dst)
                    frontier.append(e.dst)
        return seen

    def paths_through(self, unit_id: str) -> list[AtlasPath]:
        return [p for p in self.paths if unit_id in p.unit_ids]

    def resources_of(self, unit_id: str) -> set[str]:
        res = set(self.tag(unit_id, "resources", ()) or ())
        for e in self.out_edges(unit_id, EdgeKind.EXECUTES_ON):
            res.add(self.units[e.dst].name if e.dst in self.units else e.dst)
        return res

    # ------------------------------------------------------------------ priors
    def epistasis_prior(self, unit_a: str, unit_b: str) -> float:
        """Prior probability-like weight that genes at these units interact.

        The blueprint's rule: genes on the same Path or sharing a Resource are *likely*
        epistatic and are tested first; genes on disjoint paths and resources are assumed
        additive until shown otherwise. We return a weight in [0, 1]:

        * 1.0  same request/hot path *and* a shared resource
        * 0.7  shared path or shared resource
        * 0.35 one configures the other (knob → unit) transitively
        * 0.1  otherwise (small but non-zero: surprises happen)
        """
        if unit_a == unit_b:
            return 1.0
        pa = {p.id for p in self.paths_through(unit_a)} | self._paths_via_configures(unit_a)
        pb = {p.id for p in self.paths_through(unit_b)} | self._paths_via_configures(unit_b)
        shared_path = bool(pa & pb)
        shared_res = bool(self.resources_of(unit_a) & self.resources_of(unit_b))
        if shared_path and shared_res:
            return 1.0
        if shared_path or shared_res:
            return 0.7
        conf = {EdgeKind.CONFIGURES, EdgeKind.CALLS, EdgeKind.QUERIES}
        if unit_b in self.reachable(unit_a, conf) or unit_a in self.reachable(unit_b, conf):
            return 0.35
        return 0.1

    def _paths_via_configures(self, unit_id: str) -> set[str]:
        ids: set[str] = set()
        for e in self.out_edges(unit_id, EdgeKind.CONFIGURES):
            ids.update(p.id for p in self.paths_through(e.dst))
        return ids

    # ------------------------------------------------------------------ opportunity
    def opportunity(self, locus_id: str) -> float:
        """Locus Opportunity Score = causal leverage × dollar share × mutability factor.

        * Code loci use their measured causal leverage (``causal_leverage`` tag, the slope
          of the leverage curve) when available, falling back to ``latency_share`` and then
          ``hotness`` — hotness is the least trustworthy signal (Coz: perf attributed 0.15%
          of SQLite runtime to functions whose optimisation gave 25.6%).
        * Knob loci inherit the opportunity of the units they configure, discounted by a
          prior ``knob_leverage`` tag (default 0.5) because a knob moves a whole component
          but rarely by much.
        * Frozen loci score 0; review-only loci are halved.
        """
        loc = self.loci[locus_id]
        if loc.mutability == Mutability.FROZEN:
            return 0.0
        mult = 0.5 if loc.mutability == Mutability.REVIEW_ONLY else 1.0
        unit = self.units[loc.unit_id]
        if unit.kind == UnitKind.KNOB:
            targets = [e.dst for e in self.out_edges(unit.id, EdgeKind.CONFIGURES)]
            base = max((self._unit_signal(t) for t in targets), default=0.0)
            base = max(base, float(self.tag(unit.id, "dollar_share", 0.0) or 0.0))
            prior = float(self.tag(unit.id, "knob_leverage", 0.5))
            return mult * prior * max(base, 0.05)
        return mult * max(self._unit_signal(unit.id), 0.0)

    def _unit_signal(self, unit_id: str) -> float:
        lev = self.tag(unit_id, "causal_leverage")
        dollar = float(self.tag(unit_id, "dollar_share", 0.0) or 0.0)
        if lev is not None and math.isfinite(float(lev)):
            signal = max(float(lev), 0.0)
        else:
            signal = float(self.tag(unit_id, "latency_share", 0.0) or 0.0)
            if signal == 0.0:
                signal = float(self.tag(unit_id, "hotness", 0.0) or 0.0)
        # dollar share scales the signal; units with no cost data keep their signal
        return signal * (0.5 + dollar) if dollar > 0 else signal

    # ------------------------------------------------------------------ regions
    def region_of(self, locus_id: str, regions: Iterable[Region]) -> str | None:
        loc = self.loci[locus_id]
        unit = self.units[loc.unit_id]
        for region in regions:
            if region.selector(unit, loc):
                return region.name
        return None

    def loci_in(self, region: Region, *, include_frozen: bool = False) -> list[Locus]:
        out = []
        for loc in self.loci.values():
            if not include_frozen and loc.mutability == Mutability.FROZEN:
                continue
            if region.selector(self.units[loc.unit_id], loc):
                out.append(loc)
        return sorted(out, key=lambda locus: locus.id)

    # ------------------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "units": [u.model_dump(mode="json") for u in self.units.values()],
            "edges": [e.model_dump(mode="json") for e in self.edges],
            "loci": [loc.model_dump(mode="json") for loc in self.loci.values()],
            "paths": [p.model_dump(mode="json") for p in self.paths],
            "leverage": [lc.model_dump(mode="json") for lc in self.leverage.values()],
            "dynamic": {k: dict(v) for k, v in self.dynamic.items() if v},
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> StackAtlas:
        atlas = StackAtlas()
        for u in data.get("units", []):
            atlas.units[u["id"]] = Unit.model_validate(u)
        atlas.edges = [Edge.model_validate(e) for e in data.get("edges", [])]
        for loc in data.get("loci", []):
            atlas.loci[loc["id"]] = Locus.model_validate(loc)
        atlas.paths = [AtlasPath.model_validate(p) for p in data.get("paths", [])]
        for lc in data.get("leverage", []):
            curve = LeverageCurve.model_validate(lc)
            atlas.leverage[curve.unit_id] = curve
        for uid, tags in data.get("dynamic", {}).items():
            atlas.dynamic[uid].update(tags)
        return atlas
