"""CRL rules from the lake, and rules applied to stacks.

**Mining** (``colloid rules mine``). Every gene the lake knows to *carry* a verified gain
(attribution CI above zero) that creates a B-tree index is explained against the Atlas of the
target it was verified on: which query does the index serve? An index on ``T (c)`` serving a
query that filters ``T.c = ?`` is an instance of *equality-filter-index*; an index on
``T (c, s...)`` serving a query that filters ``T.c = ?`` and sorts by ``s...`` is an instance
of *equality-filter-sorted-index*. Each instance becomes an evidence line (the gene's own
measured contribution, from the program record that measured it). A verified index that no
query explains is reported and produces nothing: v0 has no construct for it, and the grammar
grows only when evidence needs it.

**Applying** (``colloid rules apply``). Rules run over a target's query units (any language:
query units are shared facts about the database layer), drop proposals an existing index
already covers (primary keys from the schema, plus indexes in the genome), and map each
remaining proposal onto the target's index loci: a knob whose index has exactly the proposed
columns, or starts with them. A proposal with no locus is reported as such ("no locus"); the
target cannot express it yet.

In the engine, rules are the ``rule_apply`` operator: one arm per rule, proposing the knob
gene of one of the rule's proposals. The bandit learns which rules pay; the evaluator decides
whether each proposal is real, exactly as for every other operator.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from colloid.adapters.target import DEFAULT_TARGET, open_target
from colloid.core.atlas import StackAtlas
from colloid.core.genome import Genome
from colloid.core.knobs import KnobSpec
from colloid.core.lake import Record, append, head, verify_chain
from colloid.core.models import UnitKind
from colloid.core.objectives import gain_percent
from colloid.core.rules import (
    Evidence,
    IndexProposal,
    QueryFacts,
    Rule,
    SortKey,
    covers,
    index_columns,
    parse,
    primary_keys,
    proposals,
    query_facts,
    render,
)
from colloid.ports import LakeStore
from colloid.services.lake import carrying_genes, utc_now

TEMPLATES = {
    "equality-filter-index": ("Index the column a hot read filters by equality", False),
    "equality-filter-sorted-index": ("Index the equality filter together with the sort order the read asks for, so the index "
                                     "returns rows already ordered", True),
}


def query_facts_of(atlas: StackAtlas) -> dict[str, QueryFacts]:
    tables = [u.name for u in atlas.units.values() if u.kind == UnitKind.TABLE]
    return {u.id: query_facts(str(u.tags.get("sql", "")), tables) for u in atlas.units.values() if u.kind == UnitKind.QUERY}


def index_knobs(knobs: Sequence[KnobSpec]) -> dict[str, tuple[str, tuple[SortKey, ...]]]:
    """knob name -> (table, keys) for every B-tree index knob."""
    out = {}
    for k in knobs:
        if k.mechanism == "index":
            cols = index_columns(str(k.extra.get("ddl", "")))
            if cols is not None:
                out[k.name] = cols
    return out


def existing_indexes(schema_sql: str, knobs: Sequence[KnobSpec], genome_values: Mapping[str, Any] | None = None) -> dict[str, list[tuple[SortKey, ...]]]:
    out: dict[str, list[tuple[SortKey, ...]]] = {t: [keys] for t, keys in primary_keys(schema_sql).items()}
    on = {name for name, v in (genome_values or {}).items() if v is True}
    for name, (table, keys) in index_knobs(knobs).items():
        if name in on:
            out.setdefault(table, []).append(keys)
    return out


# ---------------------------------------------------------------------- mining
@dataclass
class MineReport:
    rules: list[Rule] = field(default_factory=list)
    instances: list[dict[str, Any]] = field(default_factory=list)
    unexplained: list[str] = field(default_factory=list)


def _explains(table: str, keys: tuple[SortKey, ...], facts: Mapping[str, QueryFacts]) -> tuple[str, list[str]] | None:
    """Which template (and which queries) explain an index on ``table (keys)``."""
    plain, sorted_ = [], []
    for qid, f in facts.items():
        if (table, keys[0].column) not in f.equality:
            continue
        if len(keys) == 1:
            plain.append(qid)
        elif f.sort_prefix(table) and covers(keys[1:], f.sort_prefix(table)) and len(f.sort_prefix(table)) == len(keys) - 1:
            sorted_.append(qid)
    if sorted_:
        return "equality-filter-sorted-index", sorted_
    if plain:
        return "equality-filter-index", plain
    return None


def mine(lake: LakeStore, *, atlas_of: Callable[[str], tuple[StackAtlas, Sequence[KnobSpec]]] | None = None) -> MineReport:
    """Rules generalising every verified, carrying index gene in the lake (see module docstring)."""
    records, entries = lake.records(), lake.entries()
    verify_chain(entries, records)
    cache: dict[str, tuple[StackAtlas, Sequence[KnobSpec]]] = {}

    def load(target: str) -> tuple[StackAtlas, Sequence[KnobSpec]]:
        if target not in cache:
            if atlas_of is not None:
                cache[target] = atlas_of(target)
            else:
                t = open_target(target, observe_system=False)
                cache[target] = (t.atlas_seed(), t.knobs())
        return cache[target]

    rep = MineReport()
    evidence: dict[str, dict[tuple[str, str], Evidence]] = {name: {} for name in TEMPLATES}
    for e in reversed(entries):  # newest evidence first: one evidence line per (gene, target)
        r = records[e.record]
        if r.kind != "program":
            continue
        target = str(r.content.get("target") or DEFAULT_TARGET)
        carrying = set(carrying_genes(r.content))
        if not carrying:
            continue
        atlas, knobs = load(target)
        idx = index_knobs(knobs)
        facts = query_facts_of(atlas)
        best: dict[str, Any] = {}
        for a in r.content.get("attribution", []):  # the leave-one-out measurement, when there is one
            if a["gene"] in carrying and (a["gene"] not in best or a.get("method") == "leave_one_out"):
                best[a["gene"]] = a
        for gid in sorted(carrying):
            gene = records[gid].content
            name = str(gene["locus"]["unit"])
            if gene["payload_kind"] != "value" or name not in idx or gene["payload"].get("value") is not True:
                continue
            table, keys = idx[name]
            explained = _explains(table, keys, facts)
            if explained is None:
                rep.unexplained.append(f"{name} ({target}): no query filters {table}.{keys[0].column} = ? in the way the index serves")
                continue
            template, qids = explained
            a = best[gid]
            ev = Evidence(e.record, round(gain_percent(a["value"]), 2), (round(gain_percent(a["ci"][0]), 2), round(gain_percent(a["ci"][1]), 2)), target)
            if (gid, target) not in evidence[template]:
                evidence[template][(gid, target)] = ev
                rep.instances.append({"template": template, "index": name, "target": target, "record": e.record,
                                      "queries": [atlas.units[q].name for q in qids], "contribution_pct": ev.gain_pct, "ci_pct": list(ev.ci_pct)})
    for template, (doc, sorted_) in TEMPLATES.items():
        lines = sorted(evidence[template].values(), key=lambda x: (-x.gain_pct, x.record))
        if not lines:
            continue
        rep.rules.append(Rule(name=template, version=1, filter_table="table", filter_column="column", sort_table="table" if sorted_ else None,
                              sort_keys="sort" if sorted_ else None, index_table="table",
                              index_columns=("column", "sort") if sorted_ else ("column",), unless_covered=True, evidence=tuple(lines), doc=doc))
    return rep


def rule_record(rule: Rule) -> Record:
    return Record.make("rule", {"name": rule.name, "version": rule.version, "rule_id": rule.rule_id, "text": render(rule),
                                "evidence": [{"record": e.record, "gain_pct": e.gain_pct, "ci_pct": list(e.ci_pct), "target": e.target}
                                             for e in rule.evidence]})


def commit_rules(lake: LakeStore, rules: Sequence[Rule], recorded_at: str | None = None) -> tuple[int, str]:
    """Append rule records the lake does not hold yet (idempotent). Returns (new entries, head)."""
    records, entries = lake.records(), lake.entries()
    verify_chain(entries, records)
    new = [rec for rec in (rule_record(r) for r in rules) if rec.id not in records]
    added = append(entries, new, recorded_at or utc_now())
    if not added:
        return 0, head(entries)
    return len(added), lake.commit(new, added, head(entries), f"lake: +{len(added)} CRL rule record(s)")


def load_rules(lake: LakeStore) -> list[Rule]:
    """The newest version of every rule in the lake (re-parsed and re-validated from its text)."""
    records, entries = lake.records(), lake.entries()
    verify_chain(entries, records)
    latest: dict[str, Rule] = {}
    for e in entries:
        r = records[e.record]
        if r.kind == "rule":
            for rule in parse(str(r.content["text"])):
                if rule.name not in latest or rule.version >= latest[rule.name].version:
                    latest[rule.name] = rule
    return list(latest.values())


# ---------------------------------------------------------------------- applying
@dataclass(frozen=True)
class MappedProposal:
    proposal: IndexProposal
    knob: str | None  # the target's index knob implementing it (None: no locus on this target)
    exact: bool


def apply(rules: Sequence[Rule], atlas: StackAtlas, knobs: Sequence[KnobSpec], schema_sql: str,
          genome_values: Mapping[str, Any] | None = None) -> list[MappedProposal]:
    found = proposals(rules, query_facts_of(atlas), existing_indexes(schema_sql, knobs, genome_values))
    idx = index_knobs(knobs)
    out = []
    for p in found:
        exact = next((n for n, (t, k) in sorted(idx.items()) if t == p.table and tuple(k) == p.keys), None)
        wider = None if exact else next((n for n, (t, k) in sorted(idx.items(), key=lambda kv: (len(kv[1][1]), kv[0]))
                                         if t == p.table and covers(k, p.keys)), None)
        out.append(MappedProposal(p, exact or wider, exact is not None))
    return out


def rule_genome_values(genome: Genome, knob_of_locus: Mapping[str, str]) -> dict[str, Any]:
    return {knob_of_locus[g.locus_id]: g.value for g in genome if g.locus_id in knob_of_locus}
