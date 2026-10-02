"""Applying CRL rules to a stack: query facts in, index proposals out (pure)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from colloid.core.rules.crl import Rule
from colloid.core.rules.sqlfacts import QueryFacts, SortKey


@dataclass(frozen=True)
class IndexProposal:
    rule: str  # rule name
    rule_id: str
    table: str
    keys: tuple[SortKey, ...]
    queries: tuple[str, ...]  # ids of the queries that produced it

    def render(self) -> str:
        return f"{self.table} ({', '.join(k.render() for k in self.keys)})"


def covers(existing: Sequence[SortKey], wanted: Sequence[SortKey]) -> bool:
    """An existing index serves ``wanted`` if its leading columns are ``wanted``'s, with the same
    directions or all of them reversed (a backward index scan)."""
    if len(existing) < len(wanted) or any(e.column != w.column for e, w in zip(existing, wanted, strict=False)):
        return False
    same = all(e.desc == w.desc for e, w in zip(existing, wanted, strict=False))
    flipped = all(e.desc != w.desc for e, w in zip(existing, wanted, strict=False))
    return same or flipped or len(wanted) == 1


def proposals(rules: Iterable[Rule], queries: Mapping[str, QueryFacts],
              existing: Mapping[str, Sequence[Sequence[SortKey]]]) -> list[IndexProposal]:
    """Every distinct index the rules propose for these queries (``existing``: table -> its
    current indexes, primary keys included)."""
    found: dict[tuple[str, str, tuple[SortKey, ...]], set[str]] = {}
    ids: dict[str, str] = {}
    for rule in rules:
        ids[rule.name] = rule.rule_id
        for qid, facts in queries.items():
            for table, column in facts.equality:
                keys = [SortKey(column)]
                if rule.sort_keys is not None:
                    sort = facts.sort_prefix(table)
                    if not sort or any(k.column == column for k in sort):
                        continue
                    keys += list(sort)
                key_tuple = tuple(keys)
                if rule.unless_covered and any(covers(idx, key_tuple) for idx in existing.get(table, ())):
                    continue
                found.setdefault((rule.name, table, key_tuple), set()).add(qid)
    return [IndexProposal(name, ids[name], table, keys, tuple(sorted(qs))) for (name, table, keys), qs in sorted(found.items(), key=lambda kv: (kv[0][0], kv[0][1], [k.render() for k in kv[0][2]]))]
