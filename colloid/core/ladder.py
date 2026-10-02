"""The evidence ladder: which milestones Colloid has earned, computed from evidence only.

Each rung has a gate that can fail. Nothing above a rung is built or claimed until its gate
passes (ADR 0007, ADR 0008), so the roadmap grows by findings, never ahead of them.

========  ===================================================================================
Rung      Gate (pre-registered before the measurements it judges)
========  ===================================================================================
J         *The judge holds on every implementation.* Each target with verified programs in
          the lake has a canary report with every canary rejected.
M1a       *Language neutrality.* Verified programs on at least two implementations written in
          different languages, judged by the same evaluator.
M1b       *Knowledge transfers.* A lake-primed run on implementation B against a cold run on B
          with the same budget and seed: verified-gain-per-hour of the primed run at least
          1.25x the cold run's, and its best verified gain non-inferior (at most 3 percentage
          points below). VGPH = best verified cost gain (L6 holdout point estimate) / hours from
          run start to that program passing L6. One run per arm: a single pair, reported as such.
M2        *Learned rules generalise.* A CRL rule mined from the lake proposes a gene that carries
          a verified gain (attribution CI above zero) on a target whose database schema differs
          from every target the rule's evidence came from (a held-out system, not another
          implementation of the same one).
M3        *Rules work across languages.* One rule's proposals carry verified gains on targets in
          at least two languages, with M2 passed.
========  ===================================================================================
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PASSED, OPEN, BLOCKED = "passed", "open", "blocked"
VGPH_FACTOR = 1.25
NON_INFERIORITY_PP = 3.0


@dataclass(frozen=True)
class Gate:
    rung: str
    title: str
    status: str
    evidence: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArmResult:
    """One arm of the transfer A/B, extracted from a run store."""

    run: str
    best_gain_pct: float | None  # best verified cost gain (L6 holdout point estimate)
    hours_to_best: float | None
    hours_to_first: float | None
    verified: int
    evaluated: int

    @property
    def vgph(self) -> float | None:
        if self.best_gain_pct is None or not self.hours_to_best:
            return None
        return self.best_gain_pct / self.hours_to_best


@dataclass
class LadderInput:
    """Plain data: program/rule record contents from the lake, plus what the lake cannot know."""

    programs: Sequence[Mapping[str, Any]]
    rules: Sequence[Mapping[str, Any]] = ()
    genes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # gene record id -> content
    language_of: Mapping[str, str] = field(default_factory=dict)  # target -> implementation language
    schema_of: Mapping[str, str] = field(default_factory=dict)  # target -> database schema fingerprint
    canaries: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # target -> canary report
    transfer: tuple[ArmResult, ArmResult] | None = None  # (cold, primed)


def _carrying(content: Mapping[str, Any]) -> set[str]:
    best: dict[str, tuple[int, bool]] = {}
    for a in content.get("attribution", []):
        ci = a.get("ci") or [None]
        if ci[0] is None:
            continue
        rank = 1 if a.get("method") == "leave_one_out" else 0
        if a["gene"] not in best or rank >= best[a["gene"]][0]:
            best[a["gene"]] = (rank, float(ci[0]) > 0)
    return {g for g, (_, ok) in best.items() if ok}


def judge_gate(inp: LadderInput) -> Gate:
    targets = sorted({str(p.get("target")) for p in inp.programs})
    if not targets:
        return Gate("J", "the judge holds on every implementation", OPEN, needs=("verified programs in the lake",))
    missing = [t for t in targets if not inp.canaries.get(t, {}).get("all_rejected")]
    ev = tuple(f"{t}: {inp.canaries[t].get('rejected')}/{inp.canaries[t].get('total')} canaries rejected" for t in targets if t in inp.canaries)
    if missing:
        return Gate("J", "the judge holds on every implementation", OPEN, ev, tuple(f"a passing canary report for {t}" for t in missing))
    return Gate("J", "the judge holds on every implementation", PASSED, ev)


def neutrality_gate(inp: LadderInput) -> Gate:
    by_lang: dict[str, list[str]] = {}
    for p in inp.programs:
        t = str(p.get("target"))
        lang = inp.language_of.get(t)
        if lang:
            by_lang.setdefault(lang, [])
            if t not in by_lang[lang]:
                by_lang[lang].append(t)
    ev = tuple(f"{lang}: verified programs on {', '.join(ts)}" for lang, ts in sorted(by_lang.items()))
    if len(by_lang) >= 2:
        return Gate("M1a", "language neutrality", PASSED, ev)
    return Gate("M1a", "language neutrality", OPEN, ev, ("verified programs on an implementation in a second language",))


def transfer_gate(inp: LadderInput) -> Gate:
    title = "knowledge transfers between implementations"
    if inp.transfer is None:
        return Gate("M1b", title, OPEN, needs=("a cold run and a lake-primed run on the same implementation (colloid compare)",))
    cold, primed = inp.transfer
    ev = tuple(f"{label} {a.run}: best verified {a.best_gain_pct}% after {a.hours_to_best} h -> {a.vgph and round(a.vgph, 2)} %/h; "
               f"first verified after {a.hours_to_first} h; {a.verified} verified of {a.evaluated} evaluated" for label, a in (("cold", cold), ("primed", primed)))
    if primed.vgph is None:
        return Gate("M1b", title, OPEN, ev, ("the primed run verified nothing",))
    if cold.vgph is None:
        return Gate("M1b", title, PASSED, ev + ("the cold run verified nothing within the same budget",))
    faster = primed.vgph >= VGPH_FACTOR * cold.vgph
    noninferior = (primed.best_gain_pct or 0.0) >= (cold.best_gain_pct or 0.0) - NON_INFERIORITY_PP
    verdict = (f"VGPH ratio {primed.vgph / cold.vgph:.2f} (needs >= {VGPH_FACTOR}); endpoint difference "
               f"{(primed.best_gain_pct or 0) - (cold.best_gain_pct or 0):+.2f} pp (needs >= -{NON_INFERIORITY_PP})")
    if faster and noninferior:
        return Gate("M1b", title, PASSED, ev + (verdict, "single pair per arm"))
    return Gate("M1b", title, OPEN, ev + (verdict,), ("a primed run that beats the cold run by the pre-registered margin",))


def rules_gate(inp: LadderInput) -> Gate:
    title = "learned rules generalise to a held-out system"
    if not inp.rules:
        return Gate("M2", title, OPEN, needs=("CRL rules mined from the lake (colloid rules mine --commit)",))
    ev: list[str] = []
    for rule in inp.rules:
        sources = {str(e["target"]) for e in rule.get("evidence", [])}
        source_schemas = {inp.schema_of.get(t) for t in sources}
        held_out = sorted({t for t, s in inp.schema_of.items() if s not in source_schemas})
        ev.append(f"rule {rule['name']} v{rule['version']}: evidence on {', '.join(sorted(sources))}; "
                  f"held-out targets available: {', '.join(held_out) or 'none'}")
        for p in inp.programs:
            t = str(p.get("target"))
            if inp.schema_of.get(t) in source_schemas:
                continue
            for gid in _carrying(p):
                prov = (inp.genes.get(gid) or {}).get("provenance", {})
                if prov.get("operator") == "rule_apply" and prov.get("template") == rule["name"]:
                    return Gate("M2", title, PASSED, tuple(ev) + (f"rule {rule['name']} verified on held-out {t}",))
    return Gate("M2", title, OPEN, tuple(ev), ("a target whose database schema differs from the rules' evidence, "
                                                "and a verified rule_apply gene on it",))


def language_rules_gate(inp: LadderInput, m2: Gate) -> Gate:
    title = "rules work across languages"
    if m2.status != PASSED:
        return Gate("M3", title, BLOCKED, needs=("M2",))
    langs: dict[str, set[str]] = {}
    for p in inp.programs:
        for gid in _carrying(p):
            prov = (inp.genes.get(gid) or {}).get("provenance", {})
            if prov.get("operator") == "rule_apply":
                langs.setdefault(str(prov.get("template")), set()).add(inp.language_of.get(str(p.get("target")), "?"))
    winners = {r: ls for r, ls in langs.items() if len(ls) >= 2}
    ev = tuple(f"rule {r}: verified in {', '.join(sorted(ls))}" for r, ls in sorted(langs.items()))
    if winners:
        return Gate("M3", title, PASSED, ev)
    return Gate("M3", title, OPEN, ev, ("one rule verified on implementations in two languages",))


def evaluate(inp: LadderInput) -> list[Gate]:
    m2 = rules_gate(inp)
    return [judge_gate(inp), neutrality_gate(inp), transfer_gate(inp), m2, language_rules_gate(inp, m2)]
