"""The StackZero TargetSystem adapter.

Responsibilities (the blueprint's TargetSystem port, made concrete):

* **Describe the target** - knob catalogue, Atlas seed (static analysis), regions, objectives,
  baseline id, per-unit source and mutation context for LLM prompts.
* **Materialise a candidate** - copy the baseline tree into a fresh workspace, apply code genes
  (each pinned to the exact baseline source hash it was written against, so a gene can
  never silently apply to drifted code), and derive the launch configuration from knob
  genes. Tests are always copied from the baseline (they are not loci).
* **Build** (cascade L1) - byte-compile Python and compile libshopnative with the genome's
  compiler and flags. Builds run inside the sandbox (an LLM-written C file is untrusted
  input to the compiler too) and are content-addressed: the cache key is the hash of the C
  sources, compiler, flags and link order, so re-evaluating a parent costs nothing.
* **Run a candidate** - reconcile shared state (kernel knobs, Postgres postmaster GUCs, CPU
  placement, the index set of the evaluation database), then launch the service under
  uvicorn inside the sandbox on a Unix socket and wait until ``/healthz`` answers.

The evaluator drives these operations; the adapter never decides whether a candidate is
good - it only builds and runs what it is given.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import os
import random
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from colloid.adapters.code.c_clang import ClangCCode
from colloid.adapters.code.python_ast import PythonAstCode
from colloid.adapters.sandbox import select_sandbox
from colloid.adapters.sandbox.linux import LinuxProcess, LinuxSandbox
from colloid.adapters.target.stackzero.atlas_builder import C_FILES, PY_FILES, build_static_atlas
from colloid.adapters.target.stackzero.catalog import launch_config, load_knobs, pg_options
from colloid.adapters.target.stackzero.oslayer import OsLayer
from colloid.adapters.target.stackzero.postgres import PostgresCluster
from colloid.core.atlas import Region, StackAtlas
from colloid.core.genome import Genome
from colloid.core.ids import content_hash, sha256_hex
from colloid.core.knobs import KnobSpec
from colloid.core.models import Gene, Layer, ObjectiveSpec, PayloadKind, Surface, Unit, UnitKind
from colloid.ports import BuildResult, CodeUnit, SandboxSpec, Workspace

REPO_ROOT = Path(__file__).resolve().parents[4]
TARGET_ROOT = REPO_ROOT / "targets" / "stackzero"
# Engine state (Postgres cluster, build cache, sandbox logs). /opt/colloid/state on Linux bench
# hosts; a per-user directory elsewhere; COLLOID_STATE overrides both.
STATE = Path(os.environ.get("COLLOID_STATE") or ("/opt/colloid/state" if sys.platform.startswith("linux") else Path.home() / ".colloid" / "state"))


class GeneApplyError(RuntimeError):
    """A gene could not be applied (locus drifted, unknown unit, invalid splice)."""


@dataclass
class ServiceHandle:
    proc: LinuxProcess
    socket: Path
    run_dir: Path
    dbname: str
    launch: dict[str, Any]
    started_at: float = field(default_factory=time.monotonic)

    def cpuacct_path(self) -> str:
        return self.proc.cpuacct_path()

    def pids(self) -> list[int]:
        return self.proc.pids()

    def client(self, timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(transport=httpx.HTTPTransport(uds=str(self.socket)), base_url="http://candidate", timeout=timeout)


def regions() -> list[Region]:
    def knob_mech(u: Unit, mechs: tuple[str, ...]) -> bool:
        return u.kind == UnitKind.KNOB and u.tags.get("mechanism") in mechs

    return [
        Region("os", "kernel and scheduling knobs", lambda u, loc: u.layer == Layer.OS and u.kind == UnitKind.KNOB),
        Region("alloc", "memory allocator knobs", lambda u, loc: u.layer == Layer.ALLOC and u.kind == UnitKind.KNOB),
        Region("compiler", "libshopnative compiler flags", lambda u, loc: u.layer == Layer.COMPILER and u.kind == UnitKind.KNOB),
        Region("runtime", "Python / uvicorn runtime knobs", lambda u, loc: u.layer == Layer.RUNTIME and u.kind == UnitKind.KNOB),
        Region("db.config", "Postgres GUCs", lambda u, loc: knob_mech(u, ("guc_session", "guc_postmaster"))),
        Region("db.index", "Postgres index set", lambda u, loc: knob_mech(u, ("index",))),
        Region("svc.code", "service Python code", lambda u, loc: u.layer == Layer.SVC and loc.surface == Surface.CODE_REGION),
        Region("native.code", "libshopnative C code", lambda u, loc: u.layer == Layer.NATIVE and loc.surface == Surface.CODE_REGION),
    ]


OBJECTIVES = [
    ObjectiveSpec(name="cost", metric="usd_per_mreq", unit="USD per 1M requests", primary=True),
    ObjectiveSpec(name="cpu", metric="cpu_us_per_req", unit="CPU µs per request"),
    ObjectiveSpec(name="p50", metric="latency_p50_ms", unit="ms"),
    ObjectiveSpec(name="p95", metric="latency_p95_ms", unit="ms"),
    ObjectiveSpec(name="mem", metric="mem_pss_mb", unit="MB peak PSS"),
]


class StackZeroTarget:
    PORT_API = "1.0.0"
    name = "stackzero"
    language = "python"

    def __init__(
        self,
        sandbox: LinuxSandbox | None = None,
        *,
        root: Path = TARGET_ROOT,
        state: Path = STATE,
        python: str = sys.executable,
        observe_system: bool = True,
    ) -> None:
        self.root = root
        self.state = state
        self.python = python
        self.sandbox = sandbox or select_sandbox()
        self._knobs = load_knobs(observe_system=observe_system)
        self._knob_by_name = {k.name: k for k in self._knobs}
        self.pg = PostgresCluster(self.sandbox, root=state / "pg")
        self.os_layer = OsLayer(state / "os_journal.json")
        self.py_code = PythonAstCode()
        self.c_code = ClangCCode(include_dirs=[str(root / "native")])
        self.build_cache = state / "build-cache"
        self.build_cache.mkdir(parents=True, exist_ok=True)
        self.work = state / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self._atlas: StackAtlas | None = None

    # ------------------------------------------------------------------ description
    @staticmethod
    def catalog(observe_system: bool = False) -> list[KnobSpec]:
        """The knob catalogue without instantiating the target (cross-target knob comparison)."""
        return load_knobs(observe_system=observe_system)

    def knobs(self) -> Sequence[KnobSpec]:
        return self._knobs

    def knob(self, name: str) -> KnobSpec:
        return self._knob_by_name[name]

    def regions(self) -> Sequence[Region]:
        return regions()

    def objectives(self) -> Sequence[ObjectiveSpec]:
        return OBJECTIVES

    def atlas_seed(self) -> StackAtlas:
        if self._atlas is None:
            self._atlas = build_static_atlas(self.root, self._knobs)
        return self._atlas

    def baseline_id(self) -> str:
        h = hashlib.sha256()
        for rel in (*PY_FILES, *C_FILES, "native/shopnative.h", "db/schema.sql", "db/seed.sql"):
            h.update(rel.encode())
            h.update((self.root / rel).read_bytes())
        h.update((Path(__file__).with_name("knobs.yaml")).read_bytes())
        return "base-" + h.hexdigest()[:12]

    def knob_name_of_locus(self, atlas: StackAtlas) -> dict[str, str]:
        out = {}
        for lid, loc in atlas.loci.items():
            u = atlas.units[loc.unit_id]
            if u.kind == UnitKind.KNOB:
                out[lid] = str(u.tags["knob"])
        return out

    def unit_source(self, unit_id: str, genome: Genome) -> str:
        """Current source of a code unit under ``genome`` (gene payload if the genome
        rewrites it, else the baseline source)."""
        atlas = self.atlas_seed()
        for g in genome:
            if g.payload_kind == PayloadKind.SOURCE and atlas.loci[g.locus_id].unit_id == unit_id:
                return str(g.payload["source"])
        return str(atlas.units[unit_id].tags["baseline_source"])

    def mutation_context(self, unit_id: str) -> Mapping[str, Any]:
        atlas = self.atlas_seed()
        u = atlas.units[unit_id]
        if u.tags.get("language") == "c":
            header = (self.root / "native" / "shopnative.h").read_text()
            text = (
                "libshopnative is a shared library called from Python through ctypes. Public declarations:\n"
                f"```c\n{header}\n```\n"
                "The function is compiled with the other library sources; it may call the declared functions."
            )
            return {"target_context": text, "available_names": ()}
        schema = (self.root / "db" / "schema.sql").read_text()
        schema = "\n".join(line for line in schema.splitlines() if not line.startswith("--") and line.strip())
        text = (
            "`db` is an async database helper (psycopg 3, %s placeholders):\n"
            "- `await db.fetch(sql, params)` -> list of tuples\n"
            "- `await db.fetchrow(sql, params)` -> tuple or None\n"
            "- `await db.fetchval(sql, params)` -> first column of first row or None\n"
            "- `await db.execute(sql, params)` -> affected row count\n"
            "- `async with db.transaction() as tx:` (tx has the same methods)\n"
            "Pass a Python list for `= ANY(%s)`. Timestamps come back as timezone-aware datetimes.\n"
            "Only primary-key indexes are guaranteed to exist.\n"
            f"Schema:\n```sql\n{schema}\n```"
        )
        return {"target_context": text, "available_names": tuple(u.tags.get("module_names", ()))}

    # ------------------------------------------------------------------ lifecycle
    def prepare(self, *, rebuild_template: bool = False) -> None:
        restored = self.os_layer.restore()
        if restored:
            print(f"[stackzero] restored OS knobs left by a previous run: {restored}", file=sys.stderr)
        self.pg.init()
        self.pg.start({}, cpus=None)
        if rebuild_template or not self.pg.has_template():
            self.pg.build_template((self.root / "db" / "schema.sql").read_text(), (self.root / "db" / "seed.sql").read_text())
        for stale in self.pg.list_databases("cz_"):
            self.pg.drop(stale)

    def shutdown(self) -> None:
        self.os_layer.restore()
        with contextlib.suppress(Exception):
            for stale in self.pg.list_databases("cz_"):
                self.pg.drop(stale)
        self.pg.stop()

    # ------------------------------------------------------------------ materialise
    def knob_values(self, genome: Genome) -> dict[str, Any]:
        atlas = self.atlas_seed()
        names = self.knob_name_of_locus(atlas)
        return {names[g.locus_id]: g.value for g in genome if g.locus_id in names}

    def launch_for(self, genome: Genome) -> dict[str, Any]:
        return launch_config(self._knobs, self.knob_values(genome))

    def materialize(self, genome: Genome, workdir: Path) -> Workspace:
        if workdir.exists():
            shutil.rmtree(workdir)
        ignore = shutil.ignore_patterns("__pycache__", "build", "*.pyc", "node_modules")
        shutil.copytree(self.root / "service", workdir / "service", ignore=ignore)
        shutil.copytree(self.root / "native", workdir / "native", ignore=ignore)
        atlas = self.atlas_seed()
        applied: dict[str, str] = {}
        for gene in genome:
            if gene.payload_kind != PayloadKind.SOURCE:
                continue
            loc = atlas.loci.get(gene.locus_id)
            if loc is None:
                raise GeneApplyError(f"gene {gene.id}: unknown locus {gene.locus_id}")
            unit = atlas.units[loc.unit_id]
            rel = str(unit.tags["file"])
            base = str(unit.tags["baseline_source"])
            if gene.payload.get("base_hash") != sha256_hex(base)[:16]:
                raise GeneApplyError(f"gene {gene.id}: written against a different version of {unit.symbol_path} (rebase needed)")
            path = workdir / rel
            text = path.read_text()
            code_unit = CodeUnit(
                symbol_path=unit.symbol_path, name=unit.name, file=rel, start_line=int(unit.tags["start_line"]),
                end_line=int(unit.tags["end_line"]), indent="", source=base, language=str(unit.tags["language"]),
            )
            try:
                if unit.tags["language"] == "python":
                    new_text = self.py_code.replace(text, code_unit, str(gene.payload["source"]))
                else:
                    new_text = self.c_code.replace(text, code_unit, str(gene.payload["source"]))
            except (SyntaxError, ValueError, KeyError) as exc:
                raise GeneApplyError(f"gene {gene.id}: cannot splice into {rel}: {exc}") from exc
            path.write_text(new_text)
            applied[gene.id] = rel
        os.chmod(workdir, 0o755)
        for dirpath, dirnames, filenames in os.walk(workdir):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o755)
            for f in filenames:
                os.chmod(os.path.join(dirpath, f), 0o644)
        return Workspace(root=workdir, genome=genome, applied=applied, launch=self.launch_for(genome))

    # ------------------------------------------------------------------ build (L1)
    def build(self, ws: Workspace, *, link_seed: int = 0) -> BuildResult:
        t0 = time.monotonic()
        cc = ws.launch["cc"]
        sources = list(C_FILES)
        random.Random(link_seed).shuffle(sources)  # link-order randomisation (setup randomisation)
        h = hashlib.sha256()
        for rel in sorted(C_FILES) + ["native/shopnative.h"]:
            h.update(rel.encode())
            h.update((ws.root / rel).read_bytes())
        h.update(repr((cc["compiler"], cc["flags"], sources)).encode())
        key = h.hexdigest()[:24]
        cached = self.build_cache / key / "libshopnative.so"
        out_dir = ws.root / "native" / "build"
        out_dir.mkdir(parents=True, exist_ok=True)
        log = ""
        was_cached = cached.exists()
        if not was_cached:
            tmp = self.work / f"build-{key}-{os.getpid()}"
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True)
            for rel in (*C_FILES, "native/shopnative.h"):
                shutil.copy(ws.root / rel, tmp / Path(rel).name)
            argv = (cc["compiler"], *cc["flags"], "-Wall", "-o", "libshopnative.so", *(Path(s).name for s in sources), "-lm")
            res = self.sandbox.run(
                SandboxSpec(argv=argv, cwd=str(tmp), env={"TMPDIR": str(tmp)}, risk_class="B", memory_limit_mb=2048, pids_limit=64,
                            cpu_seconds=120, wall_seconds=120, writable_paths=(str(tmp),), label="build")
            )
            log = res.stdout + res.stderr
            built = tmp / "libshopnative.so"
            if res.returncode != 0 or not built.exists():
                shutil.rmtree(tmp, ignore_errors=True)
                return BuildResult(False, key, f"C build failed ({res.killed_reason or res.returncode}):\n{log[-4000:]}", time.monotonic() - t0)
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(built, cached.with_suffix(".tmp"))
            os.replace(cached.with_suffix(".tmp"), cached)
            shutil.rmtree(tmp, ignore_errors=True)
        shutil.copy(cached, out_dir / "libshopnative.so")
        os.chmod(out_dir / "libshopnative.so", 0o755)
        os.chmod(out_dir, 0o755)
        # Python: byte-compile everything (syntax errors surface here, not at request time).
        import py_compile

        for py in sorted((ws.root / "service").rglob("*.py")):
            try:
                py_compile.compile(str(py), doraise=True)
            except py_compile.PyCompileError as exc:
                return BuildResult(False, key, f"Python compile failed: {exc.msg}", time.monotonic() - t0)
        for d in (ws.root / "service").rglob("__pycache__"):
            os.chmod(d, 0o755)
            for f in d.iterdir():
                os.chmod(f, 0o644)
        art = sha256_hex(key + "".join(sha256_hex((ws.root / r).read_bytes()) for r in PY_FILES))[:24]
        return BuildResult(True, art, log[-2000:], time.monotonic() - t0, cached=was_cached, outputs={"lib": str(out_dir / "libshopnative.so")})

    # ------------------------------------------------------------------ shared state
    def apply_shared_state(self, launch: Mapping[str, Any], dbname: str) -> dict[str, Any]:
        """Reconcile machine-wide and database state with ``launch``. Returns what changed."""
        changed: dict[str, Any] = {}
        written = self.os_layer.apply(launch["os"])
        if written:
            changed["os"] = written
        if self.pg.ensure(launch["pg_postmaster"], launch["db_cpus"]):
            changed["postgres_restart"] = dict(launch["pg_postmaster"])
        idx = self.pg.apply_indexes(dbname, launch["indexes"])
        if idx["created"] or idx["dropped"]:
            changed["indexes"] = idx
        return changed

    def fresh_db(self, tag: str) -> str:
        name = f"cz_{tag}_{content_hash(tag, time.time_ns(), length=8)}"
        self.pg.clone(name)
        return name

    def drop_db(self, name: str) -> None:
        with contextlib.suppress(Exception):
            self.pg.drop(name)

    # ------------------------------------------------------------------ service
    def start_service(
        self,
        ws: Workspace,
        dbname: str,
        *,
        env_pad: int = 0,
        hash_seed: int = 0,
        app: str = "shop.app:app",
        extra_env: Mapping[str, str] | None = None,
        ready_timeout: float = 30.0,
    ) -> ServiceHandle:
        launch = ws.launch
        run_dir = self.work / f"run-{content_hash(str(ws.root), time.time_ns(), length=10)}"
        run_dir.mkdir(parents=True)
        os.chmod(run_dir, 0o755)
        sock = run_dir / "svc.sock"
        env = {
            "PYTHONPATH": str(ws.root / "service"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": str(hash_seed),
            "SHOP_DSN": self.pg.dsn(dbname),
            "SHOP_PG_OPTIONS": pg_options(launch["pg_session"]),
            "SHOP_NATIVE_LIB": str(ws.root / "native" / "build" / "libshopnative.so"),
            # Setup randomisation (Mytkowicz et al.): the size of the environment shifts stack
            # alignment; a real gain must survive it.
            "COLLOID_ENV_PAD": "x" * env_pad,
        }
        env.update(launch["env"])
        if extra_env:
            env.update(extra_env)
        uv = launch["uvicorn"]
        argv = [self.python, "-m", "uvicorn", app, "--uds", str(sock), "--workers", str(uv["workers"]), "--loop", uv["loop"],
                "--http", uv["http"], "--log-level", "info", "--timeout-keep-alive", "30", "--backlog", "2048"]
        if not uv["access_log"]:
            argv.append("--no-access-log")
        spec = SandboxSpec(
            argv=tuple(argv), cwd=str(run_dir), env=env, risk_class="B", network=False, memory_limit_mb=3072, pids_limit=256,
            cpu_seconds=100_000, wall_seconds=0, cpus=launch["svc_cpus"], nice=launch["nice"], sched_policy=launch["sched"],
            writable_paths=(str(run_dir),), stdout_path=str(run_dir / "service.log"), label="svc",
        )
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
                        r = c.get("/healthz")
                    if r.status_code == 200:
                        return handle
                    last_err = f"healthz {r.status_code}"
                except httpx.HTTPError as exc:
                    last_err = repr(exc)
            time.sleep(0.05)
        out, err = proc.logs()
        self.stop_service(handle)
        raise RuntimeError(f"service not ready after {ready_timeout}s ({last_err}): {(out + err)[-2000:]}")

    def stop_service(self, handle: ServiceHandle) -> tuple[str, str]:
        logs = handle.proc.logs()
        handle.proc.terminate(grace=3.0)
        shutil.rmtree(handle.run_dir, ignore_errors=True)
        return logs

    def run_unit_tests(self, ws: Workspace, timeout: float = 120.0) -> tuple[bool, str]:
        """Run the (baseline) unit tests against the candidate workspace, sandboxed."""
        tests = ws.root / "service" / "tests"
        shutil.rmtree(tests, ignore_errors=True)
        shutil.copytree(self.root / "service" / "tests", tests, ignore=shutil.ignore_patterns("__pycache__"))
        run_dir = self.work / f"tests-{content_hash(str(ws.root), time.time_ns(), length=10)}"
        run_dir.mkdir(parents=True)
        os.chmod(run_dir, 0o755)
        env = {
            "PYTHONPATH": str(ws.root / "service"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SHOP_NATIVE_LIB": str(ws.root / "native" / "build" / "libshopnative.so"),
        }
        env.update(ws.launch["env"])
        res = self.sandbox.run(
            SandboxSpec(argv=(self.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests)), cwd=str(run_dir), env=env,
                        risk_class="B", memory_limit_mb=1024, pids_limit=64, cpu_seconds=int(timeout), wall_seconds=timeout,
                        writable_paths=(str(run_dir),), label="tests")
        )
        shutil.rmtree(run_dir, ignore_errors=True)
        return res.returncode == 0, (res.stdout + res.stderr)[-3000:]

    def code_gene_unit(self, gene: Gene) -> Unit:
        atlas = self.atlas_seed()
        return atlas.units[atlas.loci[gene.locus_id].unit_id]


def link_orders() -> list[tuple[str, ...]]:
    return list(itertools.permutations(C_FILES))
