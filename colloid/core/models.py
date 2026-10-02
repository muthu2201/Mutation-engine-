"""Domain data model.

These pydantic models are the vocabulary shared by every part of Colloid. They mirror the
relational schema in the blueprint (section C) one-to-one so the program database is a
straightforward projection of them.

Key ideas, in the order the engine uses them:

* A **Unit** is any addressable piece of the target stack at any resolution: a layer, a
  component, a Python function, a C function, a SQL query, a Postgres table, or a *knob*
  (a sysctl, a GUC, an allocator setting, a compiler flag...). Units form the nodes of the
  Stack Atlas.
* A **Locus** is a *mutable site*: a unit plus the surface through which it can be changed
  (``code_region`` for a function body, ``knob`` for a configuration value...).
* A **Gene** is ``(locus, payload)``: one concrete change at one locus, for example
  ``work_mem = 64MB`` or "replace function ``customer_summary`` with this source".
* A **Program** is a sparse *set* of genes applied on top of a fixed baseline. The baseline
  itself is the program with zero genes.
* An **Evaluation** is the evaluator's verdict on a program at one cascade stage, together
  with the metrics and objective estimates (log-ratios versus baseline/parent with
  confidence intervals) it measured.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from colloid.core.ids import content_hash


class Layer(StrEnum):
    """Stack layers. Order roughly follows the request path from metal to pixels."""

    OS = "os"
    ALLOC = "alloc"
    COMPILER = "compiler"
    NATIVE = "native"
    RUNTIME = "runtime"
    DB = "db"
    SVC = "svc"
    FRONTEND = "frontend"


class UnitKind(StrEnum):
    LAYER = "layer"
    COMPONENT = "component"
    MODULE = "module"
    FUNCTION = "function"
    REGION = "region"
    KNOB = "knob"
    QUERY = "query"
    TABLE = "table"
    ENDPOINT = "endpoint"
    RESOURCE = "resource"


class EdgeKind(StrEnum):
    CONTAINS = "contains"
    CALLS = "calls"
    DEPENDS_ON = "depends_on"
    DATAFLOW = "dataflow"
    CONFIGURES = "configures"
    EXECUTES_ON = "executes_on"
    QUERIES = "queries"
    RENDERED_BY = "rendered_by"


class EdgeSource(StrEnum):
    STATIC = "static"
    PROFILE = "profile"
    TRACE = "trace"
    CAUSAL = "causal"


class PathKind(StrEnum):
    HOT = "hot"
    REQUEST = "request"
    CRITICAL = "critical"


class RiskClass(StrEnum):
    """Blueprint D4: the sandbox and oracle strength scale with the risk class."""

    A = "A"  # reversible knob inside a declared safe range
    B = "B"  # user-space code
    C = "C"  # data-path code touching persistence
    D = "D"  # kernel / privileged


class Mutability(StrEnum):
    ALLOWED = "allowed"
    REVIEW_ONLY = "review-only"
    FROZEN = "frozen"


class Surface(StrEnum):
    CODE_REGION = "code_region"
    KNOB = "knob"
    COMPILER_FLAGS = "compiler_flags"
    LINK_ORDER = "link_order"
    ALLOC_POLICY = "alloc_policy"
    SCHED_POLICY = "sched_policy"
    QUERY_HINT = "query_hint"
    INDEX_SET = "index_set"
    BUNDLE_SPLIT = "bundle_split"
    PROTOCOL_PARAM = "protocol_param"


class PayloadKind(StrEnum):
    VALUE = "value"  # payload = {"value": <json scalar>}
    SOURCE = "source"  # payload = {"source": <replacement source>, "base_hash": <hash of replaced source>}


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- Atlas


class Unit(Frozen):
    id: str
    kind: UnitKind
    layer: Layer
    name: str
    symbol_path: str
    parent_id: str | None = None
    content_hash: str = ""
    adapter: str = ""
    tags: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def make_id(symbol_path: str) -> str:
        """Unit ids derive from the *symbolic path* only, so they stay stable when the unit's
        content is mutated. ``content_hash`` tracks the content separately; a gene records
        the content hash it was written against and is retired if the baseline drifts."""
        return content_hash("unit", symbol_path)


class Edge(Frozen):
    src: str
    dst: str
    kind: EdgeKind
    weight: float = 1.0
    source: EdgeSource = EdgeSource.STATIC


class AtlasPath(Frozen):
    id: str
    kind: PathKind
    unit_ids: tuple[str, ...]
    weight: float
    workload_id: str = "train"


class LeverageCurve(Frozen):
    """Causal leverage of a unit: how much the end-to-end objective moves per unit of local
    speedup. Measured by delay injection (see the profiler adapter). ``points`` holds
    ``(local_slowdown_fraction, e2e_relative_change)`` pairs; ``slope`` is the fitted
    d(e2e)/d(local) at zero, i.e. the expected end-to-end gain fraction per unit of local
    speedup fraction."""

    unit_id: str
    workload_id: str
    points: tuple[tuple[float, float], ...]
    slope: float
    slope_ci: tuple[float, float]


# --------------------------------------------------------------------------- Genome


class Locus(Frozen):
    id: str
    unit_id: str
    surface: Surface
    risk_class: RiskClass
    mutability: Mutability

    @staticmethod
    def make(unit_id: str, surface: Surface, risk_class: RiskClass, mutability: Mutability) -> Locus:
        return Locus(
            id=content_hash("locus", unit_id, surface.value),
            unit_id=unit_id,
            surface=surface,
            risk_class=risk_class,
            mutability=mutability,
        )


class Provenance(Frozen):
    """Who/what produced a gene. Logged so lineage credit can be assigned to the operator,
    model and prompt template that produced an improvement."""

    operator: str
    model: str | None = None
    template: str | None = None
    prompt_hash: str | None = None
    response_hash: str | None = None
    notes: str = ""


class Gene(Frozen):
    id: str
    locus_id: str
    payload_kind: PayloadKind
    payload: dict[str, Any]
    provenance: Provenance

    @staticmethod
    def make(locus_id: str, payload_kind: PayloadKind, payload: dict[str, Any], provenance: Provenance) -> Gene:
        # Provenance is deliberately *not* part of the id: the same change proposed by two
        # different operators is the same gene and is evaluated once.
        gid = content_hash("gene", locus_id, payload_kind.value, payload)
        return Gene(id=gid, locus_id=locus_id, payload_kind=payload_kind, payload=payload, provenance=provenance)

    @property
    def value(self) -> Any:
        return self.payload.get("value")


class ProgramStatus(StrEnum):
    PROPOSED = "proposed"
    REJECTED = "rejected"  # failed a hard gate (L0-L2) or was dominated at L4
    EVALUATED = "evaluated"  # has an L5 fitness estimate
    ELITE = "elite"  # occupies a MAP-Elites cell / Pareto front
    PROMOTED = "promoted"  # passed L6 deep assurance and holdout persistence
    VERIFIED = "verified"  # passed L6, but promotion withheld: the A/A noise-floor gate failed
    FAILED = "failed"  # infrastructure error (not the candidate's fault)


class Program(Frozen):
    id: str
    baseline_id: str
    gene_ids: tuple[str, ...]
    island: str
    generation: int
    parent_ids: tuple[str, ...] = ()
    operator: str = ""
    status: ProgramStatus = ProgramStatus.PROPOSED
    created_at: float = Field(default_factory=time.time)

    @staticmethod
    def make_id(baseline_id: str, gene_ids: tuple[str, ...] | list[str]) -> str:
        return content_hash("program", baseline_id, sorted(gene_ids))


# --------------------------------------------------------------------------- Evaluation


class Stage(StrEnum):
    L0 = "L0"  # static policy / validity
    L1 = "L1"  # hermetic build
    L2 = "L2"  # quick correctness (differential + consistency oracles, unit tests)
    L3 = "L3"  # surrogate rank
    L4 = "L4"  # micro benchmark on touched paths
    L5 = "L5"  # macro benchmark, full workload, ABAB/ABC interleaving
    L6 = "L6"  # deep assurance + holdout persistence (elites only)


STAGE_ORDER: tuple[Stage, ...] = (Stage.L0, Stage.L1, Stage.L2, Stage.L3, Stage.L4, Stage.L5, Stage.L6)


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"  # evaluator/infrastructure problem, not attributable to the candidate
    SUSPICIOUS = "suspicious"


class MetricSummary(Frozen):
    median: float
    ci_lo: float
    ci_hi: float
    n: int
    unit: str = ""


class ObjectiveEstimate(Frozen):
    """Effect of a candidate on one objective relative to a reference program.

    ``log_ratio = log(m_reference / m_candidate)`` for metrics we minimise, so a *positive*
    value always means "the candidate is better". 0.0953 ≈ 10% better; -0.0953 ≈ 10% worse.
    The interval is a bootstrap confidence interval and ``p_value`` a two-sided test of
    "no difference".
    """

    objective: str
    reference: str  # "baseline" | "parent"
    reference_program_id: str
    log_ratio: float
    ci_lo: float
    ci_hi: float
    p_value: float
    n_candidate: int
    n_reference: int

    @property
    def significant_gain(self) -> bool:
        return self.ci_lo > 0.0

    @property
    def significant_loss(self) -> bool:
        return self.ci_hi < 0.0


class Evaluation(Frozen):
    id: str
    program_id: str
    stage: Stage
    protocol_id: str
    verdict: Verdict
    reasons: tuple[str, ...] = ()
    metrics: dict[str, MetricSummary] = Field(default_factory=dict)
    objectives: tuple[ObjectiveEstimate, ...] = ()
    env_fingerprint: dict[str, Any] = Field(default_factory=dict)
    raw_ref: str | None = None
    cost_usd: float = 0.0
    duration_s: float = 0.0
    created_at: float = Field(default_factory=time.time)

    def objective(self, name: str, reference: str = "baseline") -> ObjectiveEstimate | None:
        for est in self.objectives:
            if est.objective == name and est.reference == reference:
                return est
        return None


class AttributionRecord(Frozen):
    program_id: str
    gene_id: str
    method: str  # "lineage" | "shapley" | "ablation"
    objective: str
    value: float
    ci_lo: float
    ci_hi: float


class EpistasisRecord(Frozen):
    gene_a: str
    gene_b: str
    objective: str
    epsilon: float
    ci_lo: float
    ci_hi: float
    n: int


class LLMCallRecord(Frozen):
    id: str
    provider: str
    model: str
    template: str
    params: dict[str, Any]
    prompt_hash: str
    response_hash: str
    tokens_in: int
    tokens_out: int
    latency_s: float
    cost_usd: float
    ok: bool
    error: str = ""
    created_at: float = Field(default_factory=time.time)


class Alert(Frozen):
    id: str
    kind: str  # "suspicion" | "canary" | "aa_test" | "redteam_breach" | "infra"
    severity: str  # "info" | "warning" | "critical"
    message: str
    program_id: str | None = None
    created_at: float = Field(default_factory=time.time)


class ObjectiveSpec(Frozen):
    """An optimisation objective. All Colloid objectives are minimised raw metrics; they
    are converted into maximised log-ratios versus a reference."""

    name: str
    metric: str
    unit: str
    primary: bool = False


@dataclass(frozen=True, slots=True)
class Measured:
    """A measured effect: a log-ratio gain versus a reference (positive = better) with its
    standard error. The evaluator produces these; attribution combines them. Lives in the
    core model layer (a plain dataclass, accepting positional args) so the evaluator need
    not depend on the search machinery."""

    value: float
    se: float
