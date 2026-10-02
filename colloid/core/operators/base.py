"""Shared operator types."""

from __future__ import annotations

import difflib
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from colloid.core.atlas import StackAtlas
from colloid.core.genome import Genome
from colloid.core.ids import sha256_hex
from colloid.core.knobs import KnobSpec
from colloid.core.models import Gene, PayloadKind, Provenance, Surface, Unit


@dataclass(frozen=True)
class Proposal:
    genome: Genome
    parent_id: str
    operator: str
    model: str | None = None
    template: str | None = None
    changed_loci: tuple[str, ...] = ()
    second_parent_id: str | None = None
    notes: str = ""

    @property
    def arm(self) -> tuple[str, str | None, str | None]:
        return (self.operator, self.model, self.template)


@dataclass
class OperatorContext:
    """Everything an operator may look at. All of it is data; nothing here does I/O."""

    atlas: StackAtlas
    knobs: Mapping[str, KnobSpec]  # knob name -> spec
    knob_of_locus: Mapping[str, str]  # locus id -> knob name
    region_loci: Sequence[str]  # loci this island may mutate
    opportunity: Mapping[str, float]
    rng: random.Random
    unit_source: Callable[[str, Genome], str] | None = None  # (unit id, genome) -> current source
    gene_lookup: Callable[[str], Gene | None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def effective_config(self, genome: Genome) -> dict[str, Any]:
        """Knob name -> effective value (baseline defaults overridden by the genome)."""
        cfg = {name: spec.default for name, spec in self.knobs.items()}
        for g in genome:
            name = self.knob_of_locus.get(g.locus_id)
            if name is not None:
                cfg[name] = g.value
        return cfg

    def code_loci(self) -> list[str]:
        return [lid for lid in self.region_loci if self.atlas.loci[lid].surface == Surface.CODE_REGION]

    def knob_loci(self) -> list[str]:
        return [lid for lid in self.region_loci if lid in self.knob_of_locus]


def code_gene(locus_id: str, unit: Unit, old_source: str, new_source: str, provenance: Provenance) -> Gene:
    """Build a SOURCE gene. ``base_hash`` pins the gene to the exact baseline source it was
    written against, so the target adapter can refuse to apply it after upstream drift."""
    diff = list(difflib.unified_diff(old_source.splitlines(), new_source.splitlines(), lineterm="", n=0))
    changed = sum(1 for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    payload = {
        "source": new_source,
        "base_hash": sha256_hex(unit.tags.get("baseline_source", old_source))[:16],
        "language": unit.tags.get("language", "python"),
        "diff_lines": changed,
    }
    return Gene.make(locus_id, PayloadKind.SOURCE, payload, provenance)


def diff_lines(old_source: str, new_source: str) -> int:
    diff = difflib.unified_diff(old_source.splitlines(), new_source.splitlines(), lineterm="", n=0)
    return sum(1 for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
