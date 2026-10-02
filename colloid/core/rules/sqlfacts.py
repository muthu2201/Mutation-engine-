"""Facts about a SQL statement that rules can match on: which tables it reads, which columns it
compares to a parameter by equality, and the sort order it asks for.

This is deliberately *not* a SQL parser. It reads the plain DML that services issue (one
statement, ``FROM``/``JOIN`` with aliases, a ``WHERE`` that is a conjunction, ``ORDER BY``
plain columns) and is conservative everywhere else: a ``WHERE`` with a top-level ``OR``
yields no equality facts, a sort key that is not a plain column ends the sort prefix, and
subqueries contribute nothing. A missed fact means a rule does not fire; it never makes one
fire wrongly. Placeholders of every driver are recognised (``%s``, ``$1``, ``?``).

The facts are language-neutral: the same statement issued from Python or Go has the same
facts, which is what lets one rule apply to every implementation of a system.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<param>%s|\$\d+|\?)
  | (?P<string>'(?:[^']|'')*')
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)
  | (?P<op><>|!=|>=|<=|::|[=<>(),*+\-/;.|])
""", re.X)
_CLAUSE_END = {"WHERE", "GROUP", "ORDER", "LIMIT", "OFFSET", "HAVING", "RETURNING", "FOR", "UNION", "WINDOW", "ON"}
_NOT_ALIAS = _CLAUSE_END | {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "LATERAL", "USING", "AS", "SET", "VALUES"}


@dataclass(frozen=True)
class Token:
    kind: str
    text: str

    @property
    def upper(self) -> str:
        return self.text.upper()


@dataclass(frozen=True)
class SortKey:
    column: str
    desc: bool = False

    def render(self) -> str:
        return f"{self.column} desc" if self.desc else self.column


@dataclass(frozen=True)
class QueryFacts:
    statement: str  # SELECT | INSERT | UPDATE | DELETE | WITH | other
    tables: tuple[str, ...]  # base tables referenced, in order of appearance
    equality: tuple[tuple[str, str], ...]  # (table, column) compared to a parameter with '='
    order_by: tuple[tuple[str, tuple[SortKey, ...]], ...]  # per table: the leading sort keys that are its plain columns

    def sort_prefix(self, table: str) -> tuple[SortKey, ...]:
        """The ORDER BY keys belonging to ``table`` up to the first key that does not."""
        for t, keys in self.order_by:
            if t == table:
                return keys
        return ()


def tokenize(sql: str) -> list[Token]:
    out: list[Token] = []
    pos = 0
    while pos < len(sql):
        m = _TOKEN.match(sql, pos)
        if m is None:  # anything unexpected (e.g. a quoted identifier) is opaque, never an error
            out.append(Token("other", sql[pos]))
            pos += 1
            continue
        pos = m.end()
        kind = m.lastgroup or "other"
        if kind != "ws":
            out.append(Token(kind, m.group()))
    return out


def _depth0_split(tokens: Sequence[Token], word: str) -> list[list[Token]] | None:
    """Split on a keyword at parenthesis depth 0; None if the other boolean operator appears."""
    parts: list[list[Token]] = [[]]
    depth = 0
    other = "OR" if word == "AND" else "AND"
    for t in tokens:
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        if depth == 0 and t.kind == "ident" and t.upper == other:
            return None
        if depth == 0 and t.kind == "ident" and t.upper == word:
            parts.append([])
        else:
            parts[-1].append(t)
    return parts


def _clause(tokens: Sequence[Token], start_word: str, start_index: int = 0) -> tuple[int, list[Token]]:
    """Tokens of the clause that starts at the first depth-0 ``start_word`` (a dotted keyword
    pair like ORDER BY is handled by the caller), up to the next clause keyword."""
    depth = 0
    begin = -1
    for i in range(start_index, len(tokens)):
        t = tokens[i]
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        elif depth == 0 and t.kind == "ident" and t.upper == start_word:
            begin = i + 1
            break
    if begin < 0:
        return -1, []
    out: list[Token] = []
    depth = 0
    for t in tokens[begin:]:
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
            if depth < 0:
                break
        if depth == 0 and t.kind == "ident" and t.upper in _CLAUSE_END and not (start_word == "FROM" and t.upper == "ON"):
            break
        if depth == 0 and t.text == ";":
            break
        out.append(t)
    return begin, out


def _from_tables(tokens: Sequence[Token], known: Collection[str]) -> tuple[list[str], dict[str, str]]:
    """Tables and alias -> table map from FROM/JOIN/UPDATE/INTO positions."""
    tables: list[str] = []
    alias: dict[str, str] = {}
    depth = 0
    for i, t in enumerate(tokens):
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        if depth != 0 or t.kind != "ident" or t.upper not in ("FROM", "JOIN", "UPDATE", "INTO"):
            continue
        if i + 1 >= len(tokens) or tokens[i + 1].kind != "ident":
            continue
        name = tokens[i + 1].text.lower()
        if name not in known:
            continue
        tables.append(name)
        alias[name] = name
        j = i + 2
        if j < len(tokens) and tokens[j].kind == "ident" and tokens[j].upper == "AS":
            j += 1
        if j < len(tokens) and tokens[j].kind == "ident" and tokens[j].upper not in _NOT_ALIAS and "." not in tokens[j].text:
            alias[tokens[j].text.lower()] = name
    return list(dict.fromkeys(tables)), alias


def _column(token: Token, alias: Mapping[str, str], tables: Sequence[str]) -> tuple[str, str] | None:
    if token.kind != "ident":
        return None
    if "." in token.text:
        a, col = token.text.lower().split(".", 1)
        return (alias[a], col) if a in alias else None
    if len(tables) == 1 and token.upper not in _NOT_ALIAS:
        return tables[0], token.text.lower()
    return None


def query_facts(sql: str, known_tables: Collection[str]) -> QueryFacts:
    tokens = tokenize(sql)
    first = tokens[0].upper if tokens and tokens[0].kind == "ident" else ""
    statement = first if first in ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH") else "other"
    known = {t.lower() for t in known_tables}
    tables, alias = _from_tables(tokens, known)
    equality: list[tuple[str, str]] = []
    if statement in ("SELECT", "UPDATE", "DELETE"):
        _, where = _clause(tokens, "WHERE")
        conjuncts = _depth0_split(where, "AND") if where else []
        for c in conjuncts or []:
            if len(c) != 3 or c[1].text != "=":
                continue
            left, right = c[0], c[2]
            col = _column(left, alias, tables) if right.kind == "param" else _column(right, alias, tables) if left.kind == "param" else None
            if col is not None and col not in equality:
                equality.append(col)
    order: list[tuple[str, tuple[SortKey, ...]]] = []
    if statement == "SELECT":
        idx = next((i for i, t in enumerate(tokens) if t.kind == "ident" and t.upper == "ORDER" and i + 1 < len(tokens) and tokens[i + 1].upper == "BY"), -1)
        if idx >= 0:
            keys: list[list[Token]] = [[]]
            depth = 0
            for t in tokens[idx + 2:]:
                if t.text == "(":
                    depth += 1
                elif t.text == ")":
                    depth -= 1
                if depth == 0 and (t.text == ";" or (t.kind == "ident" and t.upper in ("LIMIT", "OFFSET", "FOR"))):
                    break
                if depth == 0 and t.text == ",":
                    keys.append([])
                else:
                    keys[-1].append(t)
            per_table: dict[str, list[SortKey]] = {}
            current: str | None = None
            for key in keys:
                direction = key[-1].upper if len(key) == 2 and key[-1].kind == "ident" else ""
                if not key or (len(key) == 2 and direction not in ("ASC", "DESC")) or len(key) > 2:
                    break
                col = _column(key[0], alias, tables)
                if col is None or (current is not None and col[0] != current):
                    break
                current = col[0]
                per_table.setdefault(col[0], []).append(SortKey(col[1], direction == "DESC"))
            order = [(t, tuple(k)) for t, k in per_table.items()]
    return QueryFacts(statement, tuple(tables), tuple(equality), tuple(order))


_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\);", re.I | re.S)
_CREATE_INDEX = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[A-Za-z_][A-Za-z0-9_]*\s+ON\s+([A-Za-z_][A-Za-z0-9_]*)"
                           r"\s*(?:USING\s+(\w+)\s*)?\((.*?)\)\s*(?:INCLUDE\s*\(.*?\))?\s*;?\s*$", re.I | re.S)


def index_columns(ddl: str) -> tuple[str, tuple[SortKey, ...]] | None:
    """``CREATE INDEX name ON t (a, b DESC)`` -> ("t", (a, b desc)); None for a non-btree index
    (GIN, GiST, ...) or anything else this reader does not understand."""
    m = _CREATE_INDEX.match(ddl.strip())
    if m is None or (m.group(2) and m.group(2).lower() != "btree"):
        return None
    keys = []
    for part in m.group(3).split(","):
        words = part.split()
        if not words or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", words[0]) or len(words) > 2:
            return None
        if len(words) == 2 and words[1].upper() not in ("ASC", "DESC"):
            return None
        keys.append(SortKey(words[0].lower(), len(words) == 2 and words[1].upper() == "DESC"))
    return m.group(1).lower(), tuple(keys)


def primary_keys(schema_sql: str) -> dict[str, tuple[SortKey, ...]]:
    """Primary-key columns of every table in a schema script (column-level or table-level)."""
    out: dict[str, tuple[SortKey, ...]] = {}
    body_without_comments = re.sub(r"--[^\n]*", "", schema_sql)
    for m in _CREATE_TABLE.finditer(body_without_comments):
        table, body = m.group(1).lower(), m.group(2)
        table_pk = re.search(r"PRIMARY\s+KEY\s*\(([^)]*)\)", body, re.I)
        if table_pk:
            out[table] = tuple(SortKey(c.strip().lower()) for c in table_pk.group(1).split(","))
            continue
        for line in body.split(","):
            words = line.split()
            if len(words) >= 2 and re.search(r"\bPRIMARY\s+KEY\b", line, re.I):
                out[table] = (SortKey(words[0].lower()),)
                break
    return out
