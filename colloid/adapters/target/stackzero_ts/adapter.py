"""The TypeScript implementation of the StackZero contract, on two JavaScript runtimes.

``targets/stackzero-ts`` is one codebase (node: built-ins + node-postgres, no framework) run
unchanged on Node (type stripping) as ``stackzero-node`` and on Bun as ``stackzero-bun``, so
the difference between the two is the runtime alone.

These targets are *measurable, not yet mutable*. They build, run, pass the differential
oracle and take part in the language bake-off under the same benchmark protocol, but Colloid
has no TypeScript code representation yet. Their Atlas holds the shared OS and database loci
only (so database knowledge applies to them), and no code loci. A TypeScript adapter is a
later rung, added when there is a reason to mutate this implementation.

Dependencies are installed once from the committed ``package-lock.json`` (``npm ci``, a
trusted and networked step, integrity-checked by npm) into the engine's state directory.
Every workspace links to that read-only tree.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from colloid.adapters.sandbox import select_sandbox
from colloid.adapters.sandbox.linux import LinuxSandbox
from colloid.adapters.target.stackzero.adapter import (
    OBJECTIVES,
    REPO_ROOT,
    STATE,
    ServiceHandle,
    StackZeroTarget,
)
from colloid.adapters.target.stackzero.atlas_builder import add_base_units, add_endpoints_knobs_paths
from colloid.adapters.target.stackzero.catalog import CATALOG as SHARED_CATALOG
from colloid.adapters.target.stackzero.catalog import CPU_LAYOUTS, _guc, load_knobs, pg_options
from colloid.adapters.target.stackzero.oslayer import OsLayer
from colloid.adapters.target.stackzero.postgres import PostgresCluster
from colloid.core.atlas import Region, StackAtlas
from colloid.core.genome import Genome
from colloid.core.ids import content_hash
from colloid.core.knobs import KnobSpec
from colloid.core.models import Layer, ObjectiveSpec, UnitKind
from colloid.ports import BuildResult, SandboxSpec, Workspace

TARGET_ROOT = REPO_ROOT / "targets" / "stackzero-ts"
SHARED_DB = REPO_ROOT / "targets" / "stackzero" / "db"
COMPONENTS = {"os.kernel": Layer.OS, "runtime.js": Layer.RUNTIME, "db.postgres": Layer.DB, "db.planner": Layer.DB, "db.storage": Layer.DB,
              "svc.shop": Layer.SVC}
COMPONENT_RESOURCES = {"os.kernel": ("cpu", "memory"), "runtime.js": ("cpu", "memory", "net"), "db.postgres": ("cpu", "memory", "io"),
                       "db.planner": ("cpu",), "db.storage": ("io", "memory"), "svc.shop": ("cpu",)}


def load_ts_knobs(observe_system: bool = True) -> list[KnobSpec]:
    shared = [k for k in load_knobs(SHARED_CATALOG, observe_system=observe_system) if k.layer in ("os", "db")]
    remap = {"component:runtime.python": "component:runtime.js", "component:runtime.uvicorn": "component:runtime.js"}
    return [k.model_copy(update={"affects": tuple(dict.fromkeys(remap.get(a, a) for a in k.affects))}) for k in shared]


def launch_config(specs: Sequence[KnobSpec], values: Mapping[str, Any]) -> dict[str, Any]:
    by_name = {s.name: s for s in specs}
    cfg = {s.name: s.default for s in specs}
    cfg.update(values)
    set_knobs = {k: v for k, v in values.items() if k in by_name and v != by_name[k].default}
    os_cfg: dict[str, dict[str, Any]] = {"sysctl": {}, "thp": {}}
    pg_session: dict[str, str] = {}
    pg_postmaster: dict[str, str] = {}
    for name, value in set_knobs.items():
        spec = by_name[name]
        if spec.mechanism in ("sysctl", "thp"):
            os_cfg[spec.mechanism][spec.key] = value
        elif spec.mechanism == "guc_session":
            pg_session[spec.key] = _guc(value, spec.unit)
        elif spec.mechanism == "guc_postmaster":
            pg_postmaster[spec.key] = _guc(value, spec.unit)
    indexes = {spec.key: str(spec.extra["ddl"]) for name, spec in by_name.items() if spec.mechanism == "index" and cfg[name]}
    svc_cpus, db_cpus = CPU_LAYOUTS[cfg["os.cpu_layout"]]
    return {"os": os_cfg, "svc_cpus": svc_cpus, "db_cpus": db_cpus, "sched": cfg["os.service_sched"], "nice": int(cfg["os.service_nice"]),
            "env": {}, "pg_session": pg_session, "pg_postmaster": pg_postmaster, "indexes": indexes}


class StackZeroTsTarget(StackZeroTarget):
    PORT_API = "1.0.0"
    language = "typescript"
    runtime = "node"

    def __init__(self, sandbox: LinuxSandbox | None = None, *, root: Path = TARGET_ROOT, state: Path = STATE, observe_system: bool = True) -> None:
        self.root = root
        self.state = state
        self.sandbox = sandbox or select_sandbox()
        self._knobs = load_ts_knobs(observe_system=observe_system)
        self._knob_by_name = {k.name: k for k in self._knobs}
        self.pg = PostgresCluster.shared(self.sandbox, state / "pg")
        self.os_layer = OsLayer(state / "os_journal.json")
        self.ts_state = state / "ts"
        self.work = state / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self._atlas: StackAtlas | None = None

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"stackzero-{self.runtime}"

    @staticmethod
    def catalog(observe_system: bool = False) -> list[KnobSpec]:
        return load_ts_knobs(observe_system=observe_system)

    def knobs(self) -> Sequence[KnobSpec]:
        return self._knobs

    def schema_sql(self) -> str:
        return (SHARED_DB / "schema.sql").read_text()

    def regions(self) -> Sequence[Region]:
        return [
            Region("os", "kernel and scheduling knobs", lambda u, loc: u.layer == Layer.OS and u.kind == UnitKind.KNOB),
            Region("db.config", "Postgres GUCs", lambda u, loc: u.kind == UnitKind.KNOB and u.tags.get("mechanism") in ("guc_session", "guc_postmaster")),
            Region("db.index", "Postgres index set", lambda u, loc: u.kind == UnitKind.KNOB and u.tags.get("mechanism") == "index"),
        ]

    def objectives(self) -> Sequence[ObjectiveSpec]:
        return OBJECTIVES

    def atlas_seed(self) -> StackAtlas:
        if self._atlas is None:
            atlas = StackAtlas()
            add_base_units(atlas, COMPONENTS, COMPONENT_RESOURCES)
            add_endpoints_knobs_paths(atlas, {}, self._knobs, {})
            self._atlas = atlas
        return self._atlas

    def baseline_id(self) -> str:
        h = hashlib.sha256(self.runtime.encode())
        for p in sorted((self.root / "service" / "src").glob("*.ts")) + [self.root / "service" / "package.json", self.root / "service" / "package-lock.json",
                                                                         SHARED_DB / "schema.sql", SHARED_DB / "seed.sql", SHARED_CATALOG]:
            h.update(p.name.encode())
            h.update(p.read_bytes())
        return "base-" + h.hexdigest()[:12]

    def mutation_context(self, unit_id: str) -> Mapping[str, Any]:
        return {"target_context": "", "available_names": ()}

    # ------------------------------------------------------------------ runtime + dependencies
    def runtime_binary(self) -> str:
        if self.runtime == "node":
            found = shutil.which("node")
            if not found:
                raise RuntimeError("node is not installed")
            return os.path.realpath(found)
        # Bun is often installed under a home directory the sandbox user cannot traverse: keep a copy.
        src = shutil.which("bun")
        if not src:
            raise RuntimeError("bun is not installed")
        dst = self.ts_state / "bin" / "bun"
        if not dst.exists() or dst.stat().st_size != Path(os.path.realpath(src)).stat().st_size:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(os.path.realpath(src), dst.with_suffix(".tmp"))
            os.replace(dst.with_suffix(".tmp"), dst)
            os.chmod(dst, 0o755)
        return str(dst)

    def runtime_version(self) -> str:
        return subprocess.run([self.runtime_binary(), "--version"], capture_output=True, text=True, check=True).stdout.strip()

    def node_modules(self) -> Path:
        """``npm ci`` from the committed lockfile, once per lockfile hash (trusted, networked)."""
        lock = self.root / "service" / "package-lock.json"
        key = hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
        out = self.ts_state / f"deps-{key}"
        if (out / "node_modules").exists():
            return out / "node_modules"
        tmp = self.ts_state / f"deps-{key}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for f in ("package.json", "package-lock.json"):
            shutil.copy(self.root / "service" / f, tmp / f)
        res = subprocess.run(["npm", "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=tmp, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"npm ci failed: {res.stderr[-1500:]}")
        for dirpath, dirnames, filenames in os.walk(tmp):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o755)
            for f in filenames:
                os.chmod(os.path.join(dirpath, f), 0o644)
        os.replace(tmp, out)
        return out / "node_modules"

    def prepare(self, *, rebuild_template: bool = False) -> None:
        self.node_modules()
        self.runtime_binary()
        super().prepare(rebuild_template=rebuild_template)

    # ------------------------------------------------------------------ workspace
    def launch_for(self, genome: Genome) -> dict[str, Any]:
        return launch_config(self._knobs, self.knob_values(genome))

    def materialize(self, genome: Genome, workdir: Path) -> Workspace:
        if any(g.payload_kind.value == "source" for g in genome):
            raise RuntimeError("the TypeScript implementation has no code loci yet")
        if workdir.exists():
            shutil.rmtree(workdir)
        shutil.copytree(self.root / "service", workdir / "service", ignore=shutil.ignore_patterns("node_modules"))
        os.symlink(self.node_modules(), workdir / "service" / "node_modules")
        for dirpath, _dirs, filenames in os.walk(workdir):
            os.chmod(dirpath, 0o755)
            for f in filenames:
                p = os.path.join(dirpath, f)
                if not os.path.islink(p):
                    os.chmod(p, 0o644)
        return Workspace(root=workdir, genome=genome, applied={}, launch=self.launch_for(genome))

    def build(self, ws: Workspace, *, link_seed: int = 0) -> BuildResult:
        """No compile step: type stripping (Node) and Bun run the sources. The build checks that
        the runtime and the dependency tree are in place, and identifies the artifact."""
        t0 = time.monotonic()
        h = hashlib.sha256(self.runtime_version().encode())
        for p in sorted((ws.root / "service" / "src").glob("*.ts")):
            h.update(p.read_bytes())
        ok = (ws.root / "service" / "node_modules" / "pg").exists()
        return BuildResult(ok, h.hexdigest()[:24], "" if ok else "node_modules missing", time.monotonic() - t0)

    def start_service(self, ws: Workspace, dbname: str, *, env_pad: int = 0, hash_seed: int = 0, app: str = "",
                      extra_env: Mapping[str, str] | None = None, ready_timeout: float = 30.0) -> ServiceHandle:
        launch = ws.launch
        run_dir = self.work / f"run-{content_hash(str(ws.root), time.time_ns(), length=10)}"
        run_dir.mkdir(parents=True)
        os.chmod(run_dir, 0o755)
        sock = run_dir / "svc.sock"
        main = str(ws.root / "service" / "src" / "main.ts")
        argv = ((self.runtime_binary(), "--experimental-strip-types", "--disable-warning=ExperimentalWarning", main, "--uds", str(sock))
                if self.runtime == "node" else (self.runtime_binary(), main, "--uds", str(sock)))
        env = {"SHOP_DSN": self.pg.dsn(dbname), "SHOP_PG_OPTIONS": pg_options(launch["pg_session"]), "COLLOID_ENV_PAD": "x" * env_pad,
               "HOME": str(run_dir), "BUN_INSTALL_CACHE_DIR": str(run_dir / "bun-cache"), "NO_COLOR": "1"}
        env.update(launch["env"])
        if extra_env:
            env.update(extra_env)
        spec = SandboxSpec(argv=argv, cwd=str(ws.root / "service"), env=env, risk_class="B", network=False, memory_limit_mb=3072, pids_limit=256,
                           cpu_seconds=100_000, wall_seconds=0, cpus=launch["svc_cpus"], nice=launch["nice"], sched_policy=launch["sched"],
                           writable_paths=(str(run_dir),), stdout_path=str(run_dir / "service.log"), label="svc")
        proc = self.sandbox.spawn(spec)
        handle = ServiceHandle(proc, sock, run_dir, dbname, dict(launch))
        deadline = time.monotonic() + ready_timeout
        last_err = ""
        while time.monotonic() < deadline:
            if not proc.alive():
                out, err = proc.logs()
                self.stop_service(handle)
                raise RuntimeError(f"service exited during startup: {(out + err)[-3000:]}")
            if sock.exists():
                try:
                    with handle.client(timeout=2.0) as c:
                        if c.get("/healthz").status_code == 200:
                            return handle
                except httpx.HTTPError as exc:
                    last_err = repr(exc)
            time.sleep(0.02)
        out, err = proc.logs()
        self.stop_service(handle)
        raise RuntimeError(f"service not ready after {ready_timeout}s ({last_err}): {(out + err)[-2000:]}")

    def run_unit_tests(self, ws: Workspace, timeout: float = 120.0) -> tuple[bool, str]:
        return True, "no code loci: unit tests run only when code can change"


class StackZeroNodeTarget(StackZeroTsTarget):
    runtime = "node"


class StackZeroBunTarget(StackZeroTsTarget):
    runtime = "bun"


__all__ = ["CPU_LAYOUTS", "StackZeroBunTarget", "StackZeroNodeTarget", "StackZeroTsTarget"]
