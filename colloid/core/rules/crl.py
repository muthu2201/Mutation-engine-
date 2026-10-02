"""CRL - the Colloid Rule Language, version 0 ("grammar seeds").

A rule is a declarative, language-neutral optimisation pattern that Colloid *learned*: every
rule cites the verified lake evidence it was mined from, and the grammar contains only the
constructs that evidence has needed so far. It grows by evidence, never ahead of it (ADR 0008).

Grammar (EBNF; ``#`` starts a comment)::

    file      = { rule } ;
    rule      = "rule" NAME VERSION "{" [ doc ] when propose [ unless ] evidence { evidence } "}" ;
    doc       = "doc" STRING ;
    when      = "when" "query" "filters" VAR "." VAR "=" "?"
                [ "and" "orders" "by" VAR "." VAR ] ;
    propose   = "propose" "index" VAR "(" VAR { "," VAR } ")" ;
    unless    = "unless" "covered" ;
    evidence  = "evidence" "lake" HEX "gain" PCT "ci" "[" PCT "," PCT "]" "on" NAME ;

    NAME = letter { letter | digit | "-" | "_" } ;   VAR = "$" NAME ;   VERSION = "v" digit { digit } ;
    PCT  = number "%" ;   HEX = 12..64 hexadecimal digits ;   STRING = '"' ... '"'

Semantics (``colloid.core.rules.match``): ``when`` binds, for every query of a stack, each
column the query compares to a parameter by equality (``$table.$column``) and - with the
``orders by`` clause - the query's leading sort keys on the same table (``$table.$sort``,
a list). ``propose index`` names the index the rule asks for; ``unless covered`` drops the
proposal when an existing index (a primary key, or an index already in the genome) starts
with the same columns. A proposal is a *candidate*: it becomes a gene and goes through the
evaluator like any other.

Identity: a rule's ``rule_id`` hashes its meaning (name, version, when, propose, unless),
canonically printed; adding evidence does not change what a rule says. The lake record
holding a rule hashes everything, evidence included.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

KEYWORDS = {"rule", "doc", "when", "query", "filters", "and", "orders", "by", "propose", "index", "unless", "covered",
            "evidence", "lake", "gain", "ci", "on"}
CRL_VERSION = 0


class CRLError(ValueError):
    def __init__(self, message: str, line: int = 0, col: int = 0) -> None:
        super().__init__(f"{line}:{col}: {message}" if line else message)
        self.line, self.col = line, col


@dataclass(frozen=True)
class Tok:
    kind: str  # word | var | version | hex | pct | int | string | punct | eof
    text: str
    line: int
    col: int


_LEX = re.compile(r"""
    (?P<ws>[ \t\r]+) | (?P<nl>\n) | (?P<comment>\#[^\n]*)
  | (?P<string>"(?:[^"\\\n]|\\.)*")
  | (?P<pct>-?\d+(?:\.\d+)?%)
  | (?P<hex>[0-9a-f]{12,64}(?![A-Za-z0-9_-]))
  | (?P<int>\d+(?![A-Za-z0-9_.-]))
  | (?P<version>v\d+(?![A-Za-z0-9_-]))
  | (?P<var>\$[A-Za-z][A-Za-z0-9_-]*)
  | (?P<word>[A-Za-z][A-Za-z0-9_-]*)
  | (?P<punct>[{}()\[\],.=?])
""", re.X)


def lex(text: str) -> list[Tok]:
    out: list[Tok] = []
    pos, line, line_start = 0, 1, 0
    while pos < len(text):
        m = _LEX.match(text, pos)
        if m is None:
            raise CRLError(f"unexpected character {text[pos]!r}", line, pos - line_start + 1)
        kind = m.lastgroup or ""
        if kind == "nl":
            line, line_start = line + 1, m.end()
        elif kind not in ("ws", "comment"):
            out.append(Tok(kind, m.group(), line, m.start() - line_start + 1))
        pos = m.end()
    out.append(Tok("eof", "", line, pos - line_start + 1))
    return out


@dataclass(frozen=True)
class Evidence:
    record: str  # lake record id (hex prefix allowed, at least 12 digits)
    gain_pct: float
    ci_pct: tuple[float, float]
    target: str


@dataclass(frozen=True)
class Rule:
    name: str
    version: int
    filter_table: str  # variable names (without '$')
    filter_column: str
    sort_table: str | None
    sort_keys: str | None
    index_table: str
    index_columns: tuple[str, ...]
    unless_covered: bool
    evidence: tuple[Evidence, ...]
    doc: str = ""
    span: tuple[int, int] = field(default=(0, 0), compare=False)

    def semantic_text(self) -> str:
        """The canonical text of what the rule says (no doc, no evidence)."""
        when = f"when query filters ${self.filter_table}.${self.filter_column} = ?"
        if self.sort_keys is not None:
            when += f" and orders by ${self.sort_table}.${self.sort_keys}"
        cols = ", ".join(f"${c}" for c in self.index_columns)
        return f"rule {self.name} v{self.version} {{ {when} propose index ${self.index_table} ({cols}){' unless covered' if self.unless_covered else ''} }}"

    @property
    def rule_id(self) -> str:
        return hashlib.sha256(f"crl/{CRL_VERSION}\0{self.semantic_text()}".encode()).hexdigest()


def _fmt_pct(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") + "%"


def render(rule: Rule) -> str:
    """Canonical source of a rule (stable: printing a parsed rule reproduces it exactly)."""
    lines = [f"rule {rule.name} v{rule.version} {{"]
    if rule.doc:
        lines.append(f'  doc "{rule.doc}"')
    when = f"  when query filters ${rule.filter_table}.${rule.filter_column} = ?"
    if rule.sort_keys is not None:
        when += f" and orders by ${rule.sort_table}.${rule.sort_keys}"
    lines.append(when)
    lines.append(f"  propose index ${rule.index_table} ({', '.join('$' + c for c in rule.index_columns)})")
    if rule.unless_covered:
        lines.append("  unless covered")
    for e in rule.evidence:
        lines.append(f"  evidence lake {e.record} gain {_fmt_pct(e.gain_pct)} ci [{_fmt_pct(e.ci_pct[0])}, {_fmt_pct(e.ci_pct[1])}] on {e.target}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_file(rules: list[Rule]) -> str:
    return "\n".join(render(r) for r in rules)


class _Parser:
    def __init__(self, text: str) -> None:
        self.toks = lex(text)
        self.i = 0

    def peek(self) -> Tok:
        return self.toks[self.i]

    def take(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def error(self, message: str, tok: Tok | None = None) -> CRLError:
        t = tok or self.peek()
        found = "end of input" if t.kind == "eof" else repr(t.text)
        return CRLError(f"{message}, found {found}", t.line, t.col)

    def expect(self, text: str) -> Tok:
        t = self.peek()
        if t.text != text or t.kind not in ("word", "punct"):
            raise self.error(f"expected {text!r}")
        return self.take()

    def expect_kind(self, kind: str, what: str) -> Tok:
        t = self.peek()
        if t.kind != kind:
            raise self.error(f"expected {what}")
        return self.take()

    def name(self) -> str:
        t = self.peek()
        if t.kind != "word" or t.text in KEYWORDS:
            raise self.error("expected a name")
        return self.take().text

    def var(self) -> str:
        return self.expect_kind("var", "a $variable").text[1:]

    def pct(self) -> float:
        return float(self.expect_kind("pct", "a percentage like 30.95%").text[:-1])

    def rules(self) -> Iterator[Rule]:
        while self.peek().kind != "eof":
            yield self.rule()

    def rule(self) -> Rule:
        start = self.expect("rule")
        name = self.name()
        version = int(self.expect_kind("version", "a version like v1").text[1:])
        self.expect("{")
        doc = ""
        if self.peek().text == "doc":
            self.take()
            doc = self.expect_kind("string", "a quoted string").text[1:-1]
        self.expect("when")
        self.expect("query")
        self.expect("filters")
        ft = self.var()
        self.expect(".")
        fc = self.var()
        self.expect("=")
        self.expect("?")
        st = sk = None
        if self.peek().text == "and":
            self.take()
            self.expect("orders")
            self.expect("by")
            st = self.var()
            self.expect(".")
            sk = self.var()
        self.expect("propose")
        self.expect("index")
        it = self.var()
        self.expect("(")
        cols = [self.var()]
        while self.peek().text == ",":
            self.take()
            cols.append(self.var())
        self.expect(")")
        unless = False
        if self.peek().text == "unless":
            self.take()
            self.expect("covered")
            unless = True
        evidence = []
        while self.peek().text == "evidence":
            self.take()
            self.expect("lake")
            record = self.expect_kind("hex", "a lake record id (12-64 lowercase hex digits)").text
            self.expect("gain")
            gain = self.pct()
            self.expect("ci")
            self.expect("[")
            lo = self.pct()
            self.expect(",")
            hi = self.pct()
            self.expect("]")
            self.expect("on")
            evidence.append(Evidence(record, gain, (lo, hi), self.name()))
        end = self.expect("}")
        rule = Rule(name, version, ft, fc, st, sk, it, tuple(cols), unless, tuple(evidence), doc, (start.line, end.line))
        problems = validate(rule)
        if problems:
            raise CRLError(f"rule {name}: {problems[0]}", start.line, start.col)
        return rule


def validate(rule: Rule) -> list[str]:
    """Static meaning checks: every variable bound, the index starts at the filtered column on
    the filtered table, and the rule rests on verified evidence."""
    problems = []
    bound = {rule.filter_table, rule.filter_column} | ({rule.sort_table, rule.sort_keys} if rule.sort_keys else set())
    for v in (rule.index_table, *rule.index_columns):
        if v not in bound:
            problems.append(f"${v} is not bound by the when clause")
    if rule.index_table != rule.filter_table:
        problems.append("the index must be on the filtered table")
    if rule.sort_keys is not None and rule.sort_table != rule.filter_table:
        problems.append("v0 sorts on the filtered table only")
    if rule.index_columns[:1] != (rule.filter_column,):
        problems.append("the index must lead with the filtered column")
    if rule.sort_keys is not None and rule.sort_keys not in rule.index_columns:
        problems.append("a rule that matches a sort must use it in the index")
    if not rule.evidence:
        problems.append("a rule needs at least one evidence line (rules are learned, not invented)")
    for e in rule.evidence:
        if not e.ci_pct[0] > 0:
            problems.append(f"evidence {e.record[:12]}: CI lower bound {e.ci_pct[0]}% is not above zero (not a verified gain)")
    return problems


def parse(text: str) -> list[Rule]:
    rules = list(_Parser(text).rules())
    seen: set[tuple[str, int]] = set()
    for r in rules:
        if (r.name, r.version) in seen:
            raise CRLError(f"rule {r.name} v{r.version} is defined twice", *r.span[:1])
        seen.add((r.name, r.version))
    return rules
