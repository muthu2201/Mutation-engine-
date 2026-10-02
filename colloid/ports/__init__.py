"""Ports: the contracts between the pure core and the outside world (blueprint B, E).

Each port is a ``typing.Protocol`` with a semantic version (``PORT_API``). Adapters declare
which port API they implement; a major-version mismatch is refused at wiring time
(:func:`check_compat`). The core never imports an adapter, an SDK, a compiler or a
database driver - it only knows these protocols and the value objects below.

Port catalogue (blueprint table B):

=================  =========================================================================
LLMProvider        ``complete()`` a chat request; declares models, context size and price
CodeRepresentation enumerate code units of a file and splice replacement sources in/out
Build              hermetic, content-addressed builds of a workspace
Sandbox            run untrusted processes with isolation level by risk class
ProgramStore       programs, genes, evaluations, lineage, attribution, epistasis, LLM calls
Telemetry          structured events and spans
CostModel          price resource usage in USD
TargetSystem       everything target-specific: Atlas seed, knobs, regions, objectives,
                   workspace materialisation, stack lifecycle, workloads and oracles
LakeStore          the mutation data lake: append-only, hash-chained ledger of verified
                   mutations (records + ledger), read and appended atomically
=================  =========================================================================

Verification and Benchmark/Profiling are implemented by the *evaluator package*
(``colloid_evaluator``) on top of TargetSystem + Sandbox, because they are the security
boundary and must not be swappable by search-side code.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from colloid.core.atlas import Region, StackAtlas
from colloid.core.genome import Genome
from colloid.core.knobs import KnobSpec
from colloid.core.lake import LedgerEntry, Record
from colloid.core.models import (
    Alert,
    AttributionRecord,
    EpistasisRecord,
    Evaluation,
    Gene,
    LLMCallRecord,
    ObjectiveSpec,
    Program,
    ProgramStatus,
)

PORT_APIS: dict[str, str] = {
    "LLMProvider": "1.0.0",
    "CodeRepresentation": "1.0.0",
    "Build": "1.0.0",
    "Sandbox": "1.0.0",
    "ProgramStore": "1.0.0",
    "Telemetry": "1.0.0",
    "CostModel": "1.0.0",
    "TargetSystem": "1.0.0",
    "LakeStore": "1.0.0",
}


class PortVersionError(RuntimeError):
    pass


def check_compat(port: str, adapter_api: str) -> None:
    """Semver rule: same major version required; adapter minor may be newer."""
    want = PORT_APIS[port].split(".")
    have = adapter_api.split(".")
    if want[0] != have[0]:
        raise PortVersionError(f"{port}: adapter implements API {adapter_api}, engine needs {PORT_APIS[port]}")


# ============================================================================ LLM


@dataclass(frozen=True)
class ModelInfo:
    name: str
    context_tokens: int
    usd_per_mtok_in: float
    usd_per_mtok_out: float
    local: bool


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_s: float
    cost_usd: float
    finish_reason: str = ""


class LLMError(RuntimeError):
    """Raised by adapters for unrecoverable request failures (after retries)."""


@runtime_checkable
class LLMProvider(Protocol):
    PORT_API: str
    name: str

    def models(self) -> Sequence[ModelInfo]: ...

    def available(self) -> bool: ...

    def complete(
        self,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float = 300.0,
    ) -> Completion: ...


# ============================================================================ Code


@dataclass(frozen=True)
class CodeUnit:
    """A function (or region) located in a source file."""

    symbol_path: str  # e.g. "py:shop/handlers.py::customer_summary"
    name: str
    file: str  # path relative to the workspace root
    start_line: int  # 1-based, includes decorators
    end_line: int  # inclusive
    indent: str
    source: str  # dedented source of the unit
    language: str
    calls: tuple[str, ...] = ()
    sql: tuple[str, ...] = ()
    is_async: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class CodeRepresentation(Protocol):
    PORT_API: str
    language: str

    def units(self, root: Path, rel_path: str) -> Sequence[CodeUnit]: ...

    def replace(self, file_text: str, unit: CodeUnit, new_source: str) -> str: ...


# ============================================================================ Build


@dataclass(frozen=True)
class BuildResult:
    ok: bool
    artifact_hash: str
    log: str
    duration_s: float
    cached: bool = False
    outputs: Mapping[str, str] = field(default_factory=dict)


class Build(Protocol):
    PORT_API: str

    def build(self, workspace: Path, profile: Mapping[str, Any]) -> BuildResult: ...


# ============================================================================ Sandbox


@dataclass(frozen=True)
class SandboxSpec:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    risk_class: str = "B"
    network: bool = False
    memory_limit_mb: int = 2048
    pids_limit: int = 256
    cpu_seconds: int = 600
    wall_seconds: float = 600.0
    file_size_mb: int = 256
    open_files: int = 1024
    cpus: str | None = None  # cpuset list, e.g. "1-2"
    nice: int = 0
    sched_policy: str = "other"  # "other" | "batch" | "idle"
    run_as_sandbox_user: bool = True
    user: str | None = None  # run as this trusted system user instead (no seccomp), e.g. "postgres"
    writable_paths: tuple[str, ...] = ()
    stdout_path: str | None = None
    label: str = "candidate"


@dataclass(frozen=True)
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    wall_s: float
    timed_out: bool
    killed_reason: str = ""


class SandboxProcess(Protocol):
    pid: int
    cgroup: str

    def alive(self) -> bool: ...

    def pids(self) -> list[int]: ...

    def cpu_usage_ns(self) -> int: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int | None: ...


class Sandbox(Protocol):
    PORT_API: str
    isolation_levels: tuple[str, ...]

    def run(self, spec: SandboxSpec) -> SandboxResult: ...

    def spawn(self, spec: SandboxSpec) -> SandboxProcess: ...


# ============================================================================ Store


class ProgramStore(Protocol):
    PORT_API: str

    def put_gene(self, gene: Gene) -> None: ...
    def get_gene(self, gene_id: str) -> Gene | None: ...
    def put_program(self, program: Program) -> None: ...
    def get_program(self, program_id: str) -> Program | None: ...
    def set_status(self, program_id: str, status: ProgramStatus) -> None: ...
    def programs(self, *, island: str | None = None, status: ProgramStatus | None = None, limit: int = 10_000) -> list[Program]: ...
    def put_lineage(self, child_id: str, parent_id: str, op: str, delta_genes: Sequence[str]) -> None: ...
    def lineage(self, program_id: str) -> list[dict[str, Any]]: ...
    def put_evaluation(self, ev: Evaluation) -> None: ...
    def evaluations(self, program_id: str | None = None, *, stage: str | None = None, limit: int = 100_000) -> list[Evaluation]: ...
    def put_attribution(self, rec: AttributionRecord) -> None: ...
    def attributions(self, program_id: str | None = None) -> list[AttributionRecord]: ...
    def put_epistasis(self, rec: EpistasisRecord) -> None: ...
    def epistasis(self) -> list[EpistasisRecord]: ...
    def put_llm_call(self, rec: LLMCallRecord) -> None: ...
    def llm_calls(self, limit: int = 10_000) -> list[LLMCallRecord]: ...
    def put_alert(self, alert: Alert) -> None: ...
    def alerts(self, limit: int = 1000) -> list[Alert]: ...
    def kv_get(self, key: str) -> Any: ...
    def kv_set(self, key: str, value: Any) -> None: ...
    def put_signature(self, program_id: str, signature_hex: str) -> None: ...
    def signatures(self) -> list[tuple[str, str]]: ...
    def close(self) -> None: ...


# ============================================================================ Telemetry


class Telemetry(Protocol):
    PORT_API: str

    def emit(self, kind: str, /, **fields: Any) -> None: ...

    def span(self, name: str, **fields: Any) -> AbstractContextManager[dict[str, Any]]: ...


# ============================================================================ Cost


@dataclass(frozen=True)
class ResourceUsage:
    cpu_seconds: float
    memory_gb_seconds: float
    requests: int


class CostModel(Protocol):
    PORT_API: str

    def price(self, usage: ResourceUsage) -> float: ...

    def usd_per_million_requests(self, cpu_s_per_req: float, mem_gb: float, req_per_s: float) -> float: ...

    def describe(self) -> Mapping[str, Any]: ...


# ============================================================================ Target


@dataclass
class Workspace:
    root: Path
    genome: Genome
    applied: dict[str, str] = field(default_factory=dict)  # gene id -> file touched
    launch: dict[str, Any] = field(default_factory=dict)  # knob-derived launch configuration


class TargetSystem(Protocol):
    """Everything the engine and evaluator need to know about one target stack."""

    PORT_API: str
    name: str

    def atlas_seed(self) -> StackAtlas: ...
    def knobs(self) -> Sequence[KnobSpec]: ...
    def regions(self) -> Sequence[Region]: ...
    def objectives(self) -> Sequence[ObjectiveSpec]: ...
    def baseline_id(self) -> str: ...
    def unit_source(self, unit_id: str, genome: Genome) -> str: ...
    def mutation_context(self, unit_id: str) -> Mapping[str, Any]: ...
    def materialize(self, genome: Genome, workdir: Path) -> Workspace: ...
    def prepare(self) -> None: ...
    def shutdown(self) -> None: ...


def iter_ports() -> Iterator[tuple[str, str]]:
    yield from PORT_APIS.items()


@runtime_checkable
class LakeStore(Protocol):
    """Storage for the mutation data lake (``colloid.core.lake``). ``commit`` must be atomic:
    either all new records and ledger entries become visible, or none do; and it must refuse
    (``LakeConflict``) if the head moved since ``entries()`` was read (another writer won)."""

    PORT_API: str
    location: str

    def entries(self) -> list[LedgerEntry]: ...
    def records(self) -> dict[str, Record]: ...
    def commit(self, records: Sequence[Record], entries: Sequence[LedgerEntry], expected_head: str, message: str) -> str: ...


class LakeConflict(RuntimeError):
    """The lake's head moved between read and commit; re-read and retry."""
