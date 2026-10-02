"""TargetSystem adapter for the Go implementation of the StackZero shop contract.

``stackzero-go`` is the same system as ``stackzero`` everywhere except the service: the same
HTTP contract, the same Postgres cluster, schema, seed data and index candidates, the same
kernel knobs, workloads, oracles and benchmark protocol. Only the language layer differs, so
it shares everything with :class:`StackZeroTarget` (database lifecycle, shared-state
reconciliation, service handles) and overrides what is language-specific:

* **Atlas** - built with Go's own parser (``GoAstCode``); query units are shared with the
  Python implementation's by normalising placeholders.
* **Knobs** - shared OS/database knobs plus Go runtime (GOGC, GOMEMLIMIT, GOMAXPROCS, pgx pool
  and execution mode) and build (GOAMD64) knobs.
* **Materialise** - copy the baseline tree, splice source genes with ``gounits`` (each pinned
  to the baseline source hash it was written against, exactly as for Python and C).
* **Build** (cascade L1) - ``go vet`` + ``go build`` inside the sandbox, offline: modules come
  from a module cache populated once by a trusted ``go mod download`` and verified against
  ``go.sum``; the compiler cache is a per-build hard-link copy of a cache warmed by a trusted
  build of the baseline, so a candidate build compiles only what it changed. Builds are
  content-addressed (sources + go.mod/go.sum + build settings + toolchain version).
* **Run** - the static binary under the sandbox on a Unix socket; ``race=True`` runs the
  race-detector build (cascade L6).
* **Unit tests** - the baseline ``*_test.go`` files (never the candidate's) with ``go test``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from colloid.adapters.code.go_ast import GoAstCode, go_binary, go_version
from colloid.adapters.sandbox import select_sandbox
from colloid.adapters.sandbox.linux import LinuxSandbox
from colloid.adapters.target.stackzero.adapter import (
    OBJECTIVES,
    REPO_ROOT,
    STATE,
    GeneApplyError,
    ServiceHandle,
    StackZeroTarget,
)
from colloid.adapters.target.stackzero.catalog import CATALOG as SHARED_CATALOG
from colloid.adapters.target.stackzero.catalog import pg_options
from colloid.adapters.target.stackzero.oslayer import OsLayer
from colloid.adapters.target.stackzero.postgres import PostgresCluster
from colloid.adapters.target.stackzero_go.atlas_builder import BUILD_FILES, GO_FILES, build_go_atlas
from colloid.adapters.target.stackzero_go.catalog import GO_CATALOG, launch_config, load_go_knobs
from colloid.core.atlas import Region, StackAtlas
from colloid.core.genome import Genome
from colloid.core.ids import content_hash, sha256_hex
from colloid.core.knobs import KnobSpec
from colloid.core.models import Layer, ObjectiveSpec, PayloadKind, Surface, Unit, UnitKind
from colloid.core.operators.llm_rewrite import ParseResult, extract_code_block
from colloid.ports import BuildResult, CodeUnit, SandboxSpec, Workspace

TARGET_ROOT = REPO_ROOT / "targets" / "stackzero-go"
SHARED_DB = REPO_ROOT / "targets" / "stackzero" / "db"
PROBE_FILE = "zz_colloid_probe.go"
PROBE_SOURCE = '''package main

import "time"

// colloidProbe is the causal profiler's delay probe (added to throwaway profiling builds
// only): `defer colloidProbe(d)()` makes the enclosing function spin for d times its own
// running time.
func colloidProbe(d float64) func() {
	t0 := time.Now()
	return func() {
		end := time.Now().Add(time.Duration(float64(time.Since(t0)) * d))
		for time.Now().Before(end) {
		}
	}
}
'''


def regions() -> list[Region]:
    def knob_mech(u: Unit, mechs: tuple[str, ...]) -> bool:
        return u.kind == UnitKind.KNOB and u.tags.get("mechanism") in mechs

    return [
        Region("os", "kernel and scheduling knobs", lambda u, loc: u.layer == Layer.OS and u.kind == UnitKind.KNOB),
        Region("runtime", "Go runtime and pgx pool knobs", lambda u, loc: u.layer == Layer.RUNTIME and u.kind == UnitKind.KNOB),
        Region("compiler", "go build settings", lambda u, loc: u.layer == Layer.COMPILER and u.kind == UnitKind.KNOB),
        Region("db.config", "Postgres GUCs", lambda u, loc: knob_mech(u, ("guc_session", "guc_postmaster"))),
        Region("db.index", "Postgres index set", lambda u, loc: knob_mech(u, ("index",))),
        Region("svc.code", "service Go code", lambda u, loc: u.layer == Layer.SVC and loc.surface == Surface.CODE_REGION and not u.tags.get("kernel")),
        Region("kernel.code", "ranking kernel Go code (score.go)", lambda u, loc: loc.surface == Surface.CODE_REGION and bool(u.tags.get("kernel"))),
    ]


class StackZeroGoTarget(StackZeroTarget):
    PORT_API = "1.0.0"
    name = "stackzero-go"
    language = "go"

    def __init__(self, sandbox: LinuxSandbox | None = None, *, root: Path = TARGET_ROOT, state: Path = STATE,
                 observe_system: bool = True) -> None:
        self.root = root
        self.state = state
        self.sandbox = sandbox or select_sandbox()
        self._knobs = load_go_knobs(observe_system=observe_system)
        self._knob_by_name = {k.name: k for k in self._knobs}
        self.pg = PostgresCluster.shared(self.sandbox, state / "pg")
        self.os_layer = OsLayer(state / "os_journal.json")
        self.go_state = state / "go"
        self.go_code = GoAstCode(self.go_state / "tools")
        self.modcache = self.go_state / "modcache"
        self.warm_cache = self.go_state / "gocache-warm"
        self.build_cache = state / "build-cache-go"
        self.build_cache.mkdir(parents=True, exist_ok=True)
        self.work = state / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self._atlas: StackAtlas | None = None
        self._go: str | None = None
        self._goroot: str | None = None

    # ------------------------------------------------------------------ description
    @staticmethod
    def catalog(observe_system: bool = False) -> list[KnobSpec]:
        return load_go_knobs(observe_system=observe_system)

    def knobs(self) -> Sequence[KnobSpec]:
        return self._knobs

    def schema_sql(self) -> str:
        return (SHARED_DB / "schema.sql").read_text()

    def regions(self) -> Sequence[Region]:
        return regions()

    def objectives(self) -> Sequence[ObjectiveSpec]:
        return OBJECTIVES

    def atlas_seed(self) -> StackAtlas:
        if self._atlas is None:
            self._atlas = build_go_atlas(self.root, self._knobs, self.go_code)
        return self._atlas

    def baseline_id(self) -> str:
        h = hashlib.sha256()
        for rel in (*GO_FILES, *BUILD_FILES):
            h.update(rel.encode())
            h.update((self.root / rel).read_bytes())
        for path in (SHARED_DB / "schema.sql", SHARED_DB / "seed.sql", GO_CATALOG, SHARED_CATALOG):
            h.update(path.name.encode())
            h.update(path.read_bytes())
        return "base-" + h.hexdigest()[:12]

    def mutation_context(self, unit_id: str) -> Mapping[str, Any]:
        atlas = self.atlas_seed()
        u = atlas.units[unit_id]
        rel = str(u.tags["file"])
        file_text = (self.root / rel).read_text()
        types = _type_declarations(file_text)
        imports = _imports(file_text)
        schema = (SHARED_DB / "schema.sql").read_text()
        schema = "\n".join(line for line in schema.splitlines() if not line.startswith("--") and line.strip())
        names = sorted({x.name for x in atlas.units.values() if x.kind == UnitKind.FUNCTION and x.tags.get("language") == "go" and "." not in x.name})
        text = (
            "The service is one Go package (package main, Go 1.24) using pgx v5 with $1, $2 ... placeholders. "
            "`db` is a Querier (the connection pool or a transaction). Helpers:\n"
            "- `fetch[T](ctx, db, sql, args...) ([]T, error)` scans every row into struct T by column position\n"
            "- `fetchColumn[T](ctx, db, sql, args...) ([]T, error)` for single-column rows\n"
            "- `fetchRow[T](ctx, db, sql, args...) (row T, found bool, err error)` for the first row\n"
            "- `fetchVal[T](ctx, db, sql, args...) (val T, found bool, err error)` for the first column of the first row\n"
            "- transactions: `pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error { ... })`\n"
            "Pass a Go slice for `= ANY($1)` (e.g. []int64). Timestamps are time.Time in UTC. You may declare new struct "
            "types inside the function. Only primary-key indexes are guaranteed to exist.\n"
            f"Packages imported by {rel} (the only ones available): {', '.join(imports) or 'none'}.\n"
            f"Types declared in {rel}:\n```go\n{types}\n```\n"
            f"Schema:\n```sql\n{schema}\n```"
        )
        return {"target_context": text, "available_names": tuple(names)}

    def parse_response(self, text: str, original: str, unit: Unit) -> ParseResult:
        """Extract the one function ``unit.name`` from a model answer and check it is a drop-in
        replacement (same canonical signature, nothing else declared). Import lines in the
        answer are ignored: imports are file-level and stay as they are."""
        block = extract_code_block(text, "go")
        if block is None:
            return ParseResult(False, reason="no code block in response")
        try:
            info = self.go_code.function(block, unit.name)
        except ValueError as exc:
            return ParseResult(False, reason=str(exc).splitlines()[0][:300])
        if info["extra_decls"]:
            return ParseResult(False, reason=f"response defines extra top-level code ({', '.join(info['extra_decls'][:3])}) outside the locus")
        fn = info["unit"]
        if fn["signature"] != unit.tags.get("signature"):
            return ParseResult(False, reason="signature changed")
        source = str(fn["source"])
        if source.strip() == original.strip():
            return ParseResult(False, reason="response is identical to the original")
        return ParseResult(True, source=source)

    # ------------------------------------------------------------------ toolchain
    def go(self) -> str:
        if self._go is None:
            self._go = go_binary()
        return self._go

    def goroot(self) -> str:
        if self._goroot is None:
            self._goroot = subprocess.run([self.go(), "env", "GOROOT"], capture_output=True, text=True, check=True).stdout.strip()
        return self._goroot

    def _go_env(self, gocache: Path, home: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        goroot = self.goroot()
        env = {
            "PATH": f"{goroot}/bin:/usr/bin:/bin", "HOME": str(home), "GOPATH": str(home / "gopath"), "GOCACHE": str(gocache),
            "GOMODCACHE": str(self.modcache), "GOFLAGS": "-mod=readonly", "GOPROXY": "off", "GOSUMDB": "off", "GOTOOLCHAIN": "local",
            "GOWORK": "off", "CGO_ENABLED": "0", "TMPDIR": str(home),
        }
        env.update(extra or {})
        return env

    def prepare_toolchain(self) -> None:
        """Trusted, networked, once: download and verify the module graph (go.sum), then warm
        the compiler cache with a build, vet and test-compile of the baseline."""
        svc = self.root / "service"
        env = {**os.environ, "GOMODCACHE": str(self.modcache), "GOFLAGS": "-mod=readonly", "GOTOOLCHAIN": "local", "GOWORK": "off",
               "GOCACHE": str(self.warm_cache), "CGO_ENABLED": "0", "GOAMD64": "v1"}
        stamp = self.go_state / f"warm-{self._source_key(Genome(), {}, race=False)}"
        if stamp.exists():
            return
        # The same flags as the sandboxed builds (-trimpath, GOAMD64), so their compile actions hit this cache.
        for argv in (["mod", "download"], ["vet", "."], ["build", "-trimpath", "-o", os.devnull, "."],
                     ["test", "-count=1", "-vet=off", "-run", "^$", "."]):
            res = subprocess.run([self.go(), *argv], cwd=svc, env=env, capture_output=True, text=True, check=False)
            if res.returncode != 0:
                raise RuntimeError(f"go {' '.join(argv)} failed for the baseline: {res.stderr[-2000:]}")
        race_env = {**env, "CGO_ENABLED": "1"}
        res = subprocess.run([self.go(), "build", "-trimpath", "-race", "-o", os.devnull, "."], cwd=svc, env=race_env,
                             capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"go build -race failed for the baseline: {res.stderr[-2000:]}")
        for dirpath, dirnames, filenames in os.walk(self.go_state):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o755)
            for f in filenames:
                p = os.path.join(dirpath, f)
                if not os.path.islink(p):
                    os.chmod(p, os.stat(p).st_mode | 0o444)
        stamp.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))

    def prepare(self, *, rebuild_template: bool = False) -> None:
        self.prepare_toolchain()
        super().prepare(rebuild_template=rebuild_template)

    # ------------------------------------------------------------------ materialise
    def launch_for(self, genome: Genome) -> dict[str, Any]:
        return launch_config(self._knobs, self.knob_values(genome))

    def materialize(self, genome: Genome, workdir: Path) -> Workspace:
        if workdir.exists():
            shutil.rmtree(workdir)
        shutil.copytree(self.root / "service", workdir / "service", ignore=shutil.ignore_patterns("bin", "*.test"))
        atlas = self.atlas_seed()
        applied: dict[str, str] = {}
        probe = False
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
            code_unit = CodeUnit(symbol_path=unit.symbol_path, name=unit.name, file=rel, start_line=int(unit.tags["start_line"]),
                                 end_line=int(unit.tags["end_line"]), indent="", source=base, language="go")
            try:
                new_text = self.go_code.replace(path.read_text(), code_unit, str(gene.payload["source"]))
            except ValueError as exc:
                raise GeneApplyError(f"gene {gene.id}: cannot splice into {rel}: {exc}") from exc
            path.write_text(new_text)
            applied[gene.id] = rel
            probe = probe or gene.provenance.operator == "profile_delay"
        if probe:
            (workdir / "service" / PROBE_FILE).write_text(PROBE_SOURCE)
        os.chmod(workdir, 0o755)
        for dirpath, dirnames, filenames in os.walk(workdir):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o755)
            for f in filenames:
                os.chmod(os.path.join(dirpath, f), 0o644)
        return Workspace(root=workdir, genome=genome, applied=applied, launch=self.launch_for(genome))

    # ------------------------------------------------------------------ build (L1)
    def _sources(self, svc: Path, *, tests: bool = False) -> list[Path]:
        files = sorted(p for p in svc.glob("*.go") if tests or not p.name.endswith("_test.go"))
        return files + [svc / "go.mod", svc / "go.sum"]

    def _source_key(self, genome_or_ws: Genome | Workspace, go_build: Mapping[str, Any], *, race: bool, tests: bool = False) -> str:
        svc = genome_or_ws.root / "service" if isinstance(genome_or_ws, Workspace) else self.root / "service"
        h = hashlib.sha256()
        for p in self._sources(svc, tests=tests):
            h.update(p.name.encode())
            h.update(p.read_bytes())
        h.update(repr((sorted(go_build.items()), race, tests, go_version(self.go()))).encode())
        return h.hexdigest()[:24]

    def _scratch(self, tag: str) -> tuple[Path, Path]:
        """A sandbox-writable scratch dir with the service sources and a private compiler cache
        (a hard-link copy of the warm cache: instant, and the warm files stay root-owned and
        read-only to the candidate build)."""
        tmp = self.work / f"{tag}-{content_hash(tag, time.time_ns(), length=10)}"
        tmp.mkdir(parents=True)
        cache = tmp / "gocache"
        if self.warm_cache.exists():
            subprocess.run(["cp", "-al", str(self.warm_cache), str(cache)], check=True)
        else:
            cache.mkdir()
        uid, gid = (self.sandbox.uid, self.sandbox.gid) if hasattr(self.sandbox, "uid") else (os.getuid(), os.getgid())
        for dirpath, dirnames, _ in os.walk(tmp):
            for d in dirnames:
                os.chown(os.path.join(dirpath, d), uid, gid)
        return tmp, cache

    def _go_run(self, argv: Sequence[str], cwd: Path, cache: Path, tmp: Path, extra_env: Mapping[str, str], label: str,
                timeout: float = 240.0) -> tuple[int, str]:
        env = self._go_env(cache, tmp, extra_env)
        res = self.sandbox.run(SandboxSpec(argv=(self.go(), *argv), cwd=str(cwd), env=env, risk_class="B", memory_limit_mb=3072,
                                           pids_limit=256, cpu_seconds=int(timeout * 4), wall_seconds=timeout,
                                           writable_paths=(str(tmp),), label=label))
        return res.returncode, (res.stdout + res.stderr)[-4000:] + (f"\n[{res.killed_reason}]" if res.killed_reason else "")

    def build(self, ws: Workspace, *, link_seed: int = 0, race: bool = False) -> BuildResult:
        t0 = time.monotonic()
        go_build = dict(ws.launch["go_build"])
        key = self._source_key(ws, {**go_build, "flags": tuple(go_build["flags"])}, race=race)
        name = "shop-race" if race else "shop"
        cached = self.build_cache / key / name
        out_dir = ws.root / "bin"
        out_dir.mkdir(parents=True, exist_ok=True)
        was_cached = cached.exists()
        log = ""
        if not was_cached:
            tmp, cache = self._scratch("gobuild")
            try:
                src = tmp / "service"
                src.mkdir()
                for p in self._sources(ws.root / "service"):
                    shutil.copy(p, src / p.name)
                os.chown(src, *((self.sandbox.uid, self.sandbox.gid) if hasattr(self.sandbox, "uid") else (os.getuid(), os.getgid())))
                env = {"GOAMD64": str(go_build["GOAMD64"])}
                if race:
                    env["CGO_ENABLED"] = "1"
                if not race:
                    rc, log = self._go_run(("vet", "."), src, cache, tmp, env, "govet")
                    if rc != 0:
                        return BuildResult(False, key, f"go vet failed:\n{log[-3000:]}", time.monotonic() - t0)
                args = ["build", "-trimpath", *(["-race"] if race else []), *go_build["flags"], "-o", str(tmp / name), "."]
                rc, blog = self._go_run(args, src, cache, tmp, env, "gobuild")
                log += blog
                if rc != 0 or not (tmp / name).exists():
                    return BuildResult(False, key, f"go build failed ({rc}):\n{log[-3000:]}", time.monotonic() - t0)
                cached.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(tmp / name, cached.with_suffix(".tmp"))
                os.replace(cached.with_suffix(".tmp"), cached)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        shutil.copy(cached, out_dir / name)
        os.chmod(out_dir / name, 0o755)
        os.chmod(out_dir, 0o755)
        return BuildResult(True, key, log[-2000:], time.monotonic() - t0, cached=was_cached, outputs={"bin": str(out_dir / name)})

    # ------------------------------------------------------------------ service
    def start_service(self, ws: Workspace, dbname: str, *, env_pad: int = 0, hash_seed: int = 0, app: str = "",
                      extra_env: Mapping[str, str] | None = None, ready_timeout: float = 30.0, race: bool = False) -> ServiceHandle:
        launch = ws.launch
        binary = ws.root / "bin" / ("shop-race" if race else "shop")
        if not binary.exists():
            b = self.build(ws, race=race)
            if not b.ok:
                raise RuntimeError(f"service binary does not build: {b.log[-1500:]}")
        run_dir = self.work / f"run-{content_hash(str(ws.root), time.time_ns(), length=10)}"
        run_dir.mkdir(parents=True)
        os.chmod(run_dir, 0o755)
        sock = run_dir / "svc.sock"
        env = {
            "SHOP_DSN": self.pg.dsn(dbname),
            "SHOP_PG_OPTIONS": pg_options(launch["pg_session"]),
            # Setup randomisation: the environment's size shifts the initial stack layout.
            "COLLOID_ENV_PAD": "x" * env_pad,
        }
        if race:
            env["GORACE"] = "halt_on_error=1 exitcode=66"
        env.update(launch["env"])
        if extra_env:
            env.update(extra_env)
        spec = SandboxSpec(
            argv=(str(binary), "--uds", str(sock)), cwd=str(run_dir), env=env, risk_class="B", network=False,
            memory_limit_mb=3072, pids_limit=256, cpu_seconds=100_000, wall_seconds=0, cpus=launch["svc_cpus"], nice=launch["nice"],
            sched_policy=launch["sched"], writable_paths=(str(run_dir),), stdout_path=str(run_dir / "service.log"), label="svc",
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
            time.sleep(0.02)
        out, err = proc.logs()
        self.stop_service(handle)
        raise RuntimeError(f"service not ready after {ready_timeout}s ({last_err}): {(out + err)[-2000:]}")

    def run_go_module(self, files: Mapping[str, bytes], steps: Sequence[Sequence[str]], *, label: str, timeout: float = 300.0,
                      goamd64: str = "v1") -> tuple[int, str]:
        """Write a throwaway Go module (``files``: relative path -> content) and run ``steps`` in it
        inside the sandbox, offline (``go ...`` steps use the toolchain; others run a binary built
        by an earlier step). Used by the evaluator's differential fuzzing. Returns the first
        failing step's exit code and output, or 0 and the last step's output."""
        tmp, cache = self._scratch(label)
        uid_gid = (self.sandbox.uid, self.sandbox.gid) if hasattr(self.sandbox, "uid") else (os.getuid(), os.getgid())
        try:
            mod = tmp / "mod"
            for rel, data in files.items():
                path = mod / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            for dirpath, _dirs, _ in os.walk(mod):
                os.chown(dirpath, *uid_gid)
            out = ""
            for step in steps:
                if step[0] == "go":
                    rc, out = self._go_run(tuple(step[1:]), mod, cache, tmp, {"GOAMD64": goamd64}, label, timeout=timeout)
                else:
                    res = self.sandbox.run(SandboxSpec(argv=(str(mod / step[0]), *step[1:]), cwd=str(mod), env={"TMPDIR": str(tmp)}, risk_class="B",
                                                       memory_limit_mb=2048, pids_limit=64, cpu_seconds=int(timeout), wall_seconds=timeout,
                                                       writable_paths=(str(tmp),), label=label))
                    rc, out = res.returncode, (res.stdout + res.stderr)[-4000:] + (f"\n[{res.killed_reason}]" if res.killed_reason else "")
                if rc != 0:
                    return rc, out
            return 0, out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def run_unit_tests(self, ws: Workspace, timeout: float = 180.0) -> tuple[bool, str]:
        """The baseline ``*_test.go`` files against the candidate package, sandboxed and offline."""
        tmp, cache = self._scratch("gotest")
        try:
            src = tmp / "service"
            src.mkdir()
            for p in self._sources(ws.root / "service"):
                shutil.copy(p, src / p.name)
            for p in sorted((self.root / "service").glob("*_test.go")):
                shutil.copy(p, src / p.name)
            os.chown(src, *((self.sandbox.uid, self.sandbox.gid) if hasattr(self.sandbox, "uid") else (os.getuid(), os.getgid())))
            rc, out = self._go_run(("test", "-count=1", "-vet=off", "."), src, cache, tmp, {"GOAMD64": str(ws.launch["go_build"]["GOAMD64"])},
                                   "gotest", timeout=timeout)
            return rc == 0, out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def probe_source(self, unit_id: str, d: float) -> str | None:
        """The unit's baseline source with the causal profiler's delay probe armed at entry."""
        unit = self.atlas_seed().units[unit_id]
        base = str(unit.tags["baseline_source"])
        at = int(unit.tags.get("body_lbrace", -1))
        if at < 0 or base[at] != "{":
            return None
        return base[: at + 1] + f"\n\tdefer colloidProbe({d!r})()" + base[at + 1 :]

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            super().shutdown()


def _imports(text: str) -> list[str]:
    """Import paths of a Go file (single and grouped import declarations)."""
    import re

    block = re.search(r"^import \((.*?)^\)", text, flags=re.M | re.S)
    lines = block.group(1).splitlines() if block else re.findall(r'^import (.*)$', text, flags=re.M)
    return [m.group(1) for line in lines if (m := re.search(r'"([^"]+)"', line))]


def _type_declarations(text: str) -> str:
    """The ``type`` declarations of a Go file (the row and response structs a function uses)."""
    out, depth, keep = [], 0, False
    for line in text.splitlines():
        if depth == 0 and line.startswith("type "):
            keep = True
        if keep:
            out.append(line)
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                keep = False
                depth = 0
    return "\n".join(out)
