"""Correctness oracles (blueprint D1) - cascade stage L2 and the deep part of L6.

1. **Differential oracle** (all classes). The baseline service and the candidate run side
   by side, each on its own throwaway clone of the template database. The oracle sends
   the same request sequence to both, one request at a time and in lock-step, so both
   databases evolve identically, and requires every response to match: same status code,
   same JSON value. Integers, strings, booleans and nulls must be identical; floats must
   agree within an *evaluator-owned* tolerance (relative 1e-9) - the candidate cannot
   widen it, which closes the circle-packing "atol" loophole from the blueprint.

2. **Unit tests** (the baseline copy, never the candidate's) - run by the target adapter.

3. **Native differential fuzzing** for genes that touch C code or C compiler flags: the
   baseline and candidate library are linked into one driver (``native_fuzz.c``) and
   compared on thousands of random inputs; the deep (L6) pass adds AddressSanitizer and
   UndefinedBehaviorSanitizer.

4. **Spot checks under load**: response bodies recorded by the load generator for a hidden
   random subset of read requests must equal the reference arm's bodies for the same
   requests (see :func:`compare_spot_checks`).

5. **SQL audit** (every language): after the differential run, the statements the candidate
   actually sent (``pg_stat_statements`` of its database, role ``shop``) are checked with the
   same SQL policy as L0 (:func:`policy.sql_violations`). Anything the reference did not also
   do is a failure. This catches SQL assembled at run time, which no static scan can see.

6. **Go**: the kernel differential fuzz (``gofuzz/driver.go``, the counterpart of
   ``native_fuzz.c``) for genes in pure-function files, and in L6 the differential oracle
   against the candidate's race-detector build.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from colloid.adapters.sandbox.linux import LinuxSandbox
from colloid.adapters.target.stackzero.adapter import StackZeroTarget
from colloid.adapters.target.stackzero.atlas_builder import C_FILES
from colloid.ports import SandboxSpec, Workspace
from colloid_evaluator import policy
from colloid_evaluator.workloads import Request, Universe, oracle_sequence

FLOAT_REL_TOL = 1e-9
FLOAT_ABS_TOL = 1e-12
FUZZ_SRC = Path(__file__).with_name("native_fuzz.c")
PUBLIC_C_SYMBOLS = ("levenshtein", "fuzzy_similarity", "shop_tokenize", "shop_free_tokens", "shop_score_batch")


def json_equal(a: Any, b: Any, path: str = "$") -> str | None:
    """Return the JSON path of the first difference, or None if equal under tolerance."""
    if isinstance(a, bool) or isinstance(b, bool):
        return None if (type(a) is type(b) and a == b) else path
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            return None if a == b else path
        fa, fb = float(a), float(b)
        if math.isnan(fa) or math.isnan(fb):
            return path
        if fa == fb or abs(fa - fb) <= max(FLOAT_ABS_TOL, FLOAT_REL_TOL * max(abs(fa), abs(fb))):
            return None
        return path
    if type(a) is not type(b):
        return path
    if isinstance(a, dict):
        if set(a) != set(b):
            return f"{path} keys {sorted(set(a) ^ set(b))[:5]}"
        for k in a:
            d = json_equal(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path} length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            d = json_equal(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else path


def response_diff(status_a: int, body_a: bytes, status_b: int, body_b: bytes) -> str | None:
    if status_a != status_b:
        return f"status {status_b} (reference {status_a})"
    try:
        ja, jb = json.loads(body_a), json.loads(body_b)
    except ValueError:
        return None if body_a == body_b else "non-JSON bodies differ"
    return json_equal(ja, jb)


# A candidate may be this many times slower than the reference on one request (or take
# HUNG_FLOOR_S, whichever is longer) before it is declared hung. The oracle checks correctness;
# a 50x slowdown on a single request is not a performance question any more.
HUNG_FACTOR = 50.0
HUNG_FLOOR_S = 10.0


def send(client: httpx.Client, req: Request, timeout: float | None = None) -> tuple[int, bytes]:
    try:
        kw = {} if timeout is None else {"timeout": timeout}
        if req.method == "POST":
            r = client.post(req.path, content=req.body.encode(), headers={"content-type": "application/json"}, **kw)
        else:
            r = client.get(req.path, **kw)
        return r.status_code, r.content
    except httpx.TimeoutException:
        return -1, b"timeout"
    except httpx.HTTPError as exc:
        return -2, repr(exc).encode()


@dataclass
class OracleResult:
    ok: bool
    requests: int
    mismatches: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    log: str = ""


class DifferentialOracle:
    def __init__(self, target: StackZeroTarget, universe: Universe, baseline_ws: Workspace) -> None:
        self.target = target
        self.universe = universe
        self.baseline_ws = baseline_ws

    def run(self, cand_ws: Workspace, seed: int, size: str = "quick", max_mismatches: int = 5, *, race: bool = False) -> OracleResult:
        import random

        t0 = time.monotonic()
        seq = oracle_sequence(self.universe, random.Random(seed), size=size)
        ref_db = self.target.fresh_db("oref")
        cand_db = self.target.fresh_db("ocand")
        ref = cand = None
        mismatches: list[str] = []
        log = ""
        try:
            self.target.apply_shared_state(cand_ws.launch, cand_db)
            ref = self.target.start_service(self.baseline_ws, ref_db, hash_seed=seed % 1000)
            try:
                kw = {"race": True} if race else {}
                cand = self.target.start_service(cand_ws, cand_db, hash_seed=(seed + 1) % 1000, **kw)
            except RuntimeError as exc:
                return OracleResult(False, 0, [f"candidate failed to start: {str(exc)[:500]}"], time.monotonic() - t0)
            with ref.client(timeout=30.0) as rc, cand.client(timeout=30.0) as cc:
                for i, req in enumerate(seq):
                    t_ref = time.monotonic()
                    sa, ba = send(rc, req)
                    t_ref = time.monotonic() - t_ref
                    if sa < 0:
                        raise RuntimeError(f"reference service failed on {req.method} {req.path}: {ba[:200]!r}")
                    budget = max(HUNG_FLOOR_S, HUNG_FACTOR * t_ref)
                    sb, bb = send(cc, req, timeout=budget)
                    if sb == -1:
                        # A server that cannot answer within the budget is wedged (e.g. a spinning
                        # event loop); every later request would time out too, so stop now.
                        mismatches.append(f"#{i} {req.method} {req.path} [{req.kind}]: candidate hung "
                                          f"(no response in {budget:.0f}s; reference answered in {t_ref * 1000:.0f} ms)")
                        break
                    diff = response_diff(sa, ba, sb, bb)
                    if diff:
                        mismatches.append(f"#{i} {req.method} {req.path} [{req.kind}]: {diff}")
                        if len(mismatches) >= max_mismatches:
                            break
            if cand is not None and not cand.proc.alive():
                mismatches.append("candidate process died during the oracle run")
        finally:
            if cand is not None:
                out, err = self.target.stop_service(cand)
                log = (out + err)[-4000:]
                if race and "WARNING: DATA RACE" in out + err:
                    i = (out + err).index("WARNING: DATA RACE")
                    mismatches.insert(0, "race detector: " + " ".join((out + err)[i : i + 600].split())[:500])
            if ref is not None:
                self.target.stop_service(ref)
            if cand is not None and not mismatches:
                mismatches += sql_audit(self.target.pg, ref_db, cand_db)
            self.target.drop_db(ref_db)
            self.target.drop_db(cand_db)
        return OracleResult(not mismatches, len(seq), mismatches, time.monotonic() - t0, log)


def sent_statements(pg: Any, dbname: str) -> list[str]:
    """Statements the unprivileged ``shop`` role ran in ``dbname`` (pg_stat_statements)."""
    with pg.superuser(dbname) as conn:
        rows = conn.execute(
            "SELECT s.query FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid JOIN pg_roles r ON r.oid = s.userid "
            "WHERE d.datname = %s AND r.rolname = 'shop'", (dbname,)).fetchall()
    return [str(r[0]) for r in rows]


def sql_audit(pg: Any, ref_db: str, cand_db: str) -> list[str]:
    """SQL-policy violations in what the candidate sent that the reference did not also commit
    (drivers issue their own transaction control; that is not the candidate's doing)."""
    try:
        reference = {r for q in sent_statements(pg, ref_db) for r in policy.sql_violations(q)}
        found: list[str] = []
        for q in sent_statements(pg, cand_db):
            for r in policy.sql_violations(q):
                if r not in reference:
                    found.append(f"SQL audit: {r}: {' '.join(q.split())[:160]}")
        return sorted(set(found))[:5]
    except Exception:  # the audit is defence in depth; an infrastructure hiccup must not fail a candidate
        return []


GOFUZZ_DIR = Path(__file__).with_name("gofuzz")


def go_kernel_fuzz(target: Any, baseline_ws: Workspace, cand_ws: Workspace, *, seed: int, iterations: int) -> tuple[bool, str]:
    """Differentially fuzz the candidate's pure Go functions against the baseline's: both
    packages are copied under new package names into one module with the driver, built and
    run in the sandbox (see ``gofuzz/driver.go``)."""
    import re as _re

    export = (GOFUZZ_DIR / "export.go.txt").read_text()
    files: dict[str, bytes] = {
        "go.mod": (baseline_ws.root / "service" / "go.mod").read_bytes(),
        "go.sum": (baseline_ws.root / "service" / "go.sum").read_bytes(),
        "fuzz/main.go": (GOFUZZ_DIR / "driver.go").read_bytes(),
    }
    for pkg, ws in (("base", baseline_ws), ("cand", cand_ws)):
        for src in sorted((ws.root / "service").glob("*.go")):
            if src.name.endswith("_test.go"):
                continue
            text = _re.sub(r"^package main\b", f"package {pkg}", src.read_text(), count=1, flags=_re.M)
            files[f"{pkg}/{src.name}"] = text.encode()
        files[f"{pkg}/zz_export.go"] = export.replace("package PKG", f"package {pkg}").encode()
    rc, out = target.run_go_module(files, [["go", "build", "-trimpath", "-o", "fuzzdriver", "./fuzz"], ["fuzzdriver", str(seed), str(iterations)]],
                                   label="gofuzz")
    if rc != 0:
        if "MISMATCH" in out:
            return False, "Go kernel differential fuzz: " + out[out.index("MISMATCH"):][:1200]
        return False, f"Go kernel differential fuzz failed ({rc}): {out[-1200:]}"
    return True, out


def compare_spot_checks(reference: dict[int, bytes], candidate: dict[int, bytes]) -> list[str]:
    """Bodies recorded under load for the same request indices must be identical (JSON-equal)."""
    problems = []
    for idx, body in candidate.items():
        ref = reference.get(idx)
        if ref is None:
            continue
        diff = response_diff(200, ref, 200, body)
        if diff:
            problems.append(f"request #{idx} under load: {diff}")
    return problems


# Output of a sanitizer *runtime* that failed to do its job (not a finding about the candidate).
# These make the run an evaluator-infrastructure ERROR, never a candidate FAIL.
SANITIZER_INFRA_MARKERS = (
    "Sanitizer has encountered a fatal error",
    "Sanitizer CHECK failed",
    "AddressSanitizer failed to allocate",
    "Shadow memory range interleaves",
    "failed to allocate 0x",
)


def sanitizer_infrastructure_failure(output: str) -> bool:
    return any(m in output for m in SANITIZER_INFRA_MARKERS)


def native_fuzz(
    sandbox: LinuxSandbox,
    baseline_ws: Workspace,
    cand_ws: Workspace,
    work: Path,
    *,
    seed: int,
    iterations: int,
    sanitize: bool,
) -> tuple[bool, str]:
    """Differentially fuzz candidate C code (and/or compiler flags) against the baseline."""
    d = work / f"fuzz-{seed}-{os.getpid()}-{time.time_ns()}"
    d.mkdir(parents=True)
    os.chmod(d, 0o755)
    try:
        shutil.copy(FUZZ_SRC, d / "native_fuzz.c")
        shutil.copy(baseline_ws.root / "native" / "shopnative.h", d / "shopnative.h")
        for rel in C_FILES:
            name = Path(rel).name
            shutil.copy(baseline_ws.root / rel, d / f"base_{name}")
            shutil.copy(cand_ws.root / rel, d / f"cand_{name}")
        cc = cand_ws.launch["cc"]
        compiler = cc["compiler"]
        cand_flags = [f for f in cc["flags"] if f not in ("-shared", "-fPIC")]
        common = ["-g", "-I."]
        if sanitize:
            compiler = "gcc"  # clang's sanitizer runtimes are not installed; gcc's are
            common += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-fno-omit-frame-pointer"]
        renames = [f"-D{s}=base_{s}" for s in PUBLIC_C_SYMBOLS]
        script = []
        for rel in C_FILES:
            name = Path(rel).name
            script.append([compiler, "-O2", *common, *renames, "-c", f"base_{name}", "-o", f"base_{name}.o"])
            script.append([compiler, *cand_flags, *common, "-c", f"cand_{name}", "-o", f"cand_{name}.o"])
        script.append([compiler, "-O1", *common, "-c", "native_fuzz.c", "-o", "native_fuzz.o"])
        objs = [f"base_{Path(r).name}.o" for r in C_FILES] + [f"cand_{Path(r).name}.o" for r in C_FILES]
        link_flags = [f for f in cand_flags if f == "-flto"]
        script.append([compiler, *common, *link_flags, "-o", "native_fuzz", "native_fuzz.o", *objs, "-lm"])
        sh = " && ".join(" ".join(part for part in cmd) for cmd in script)
        build = sandbox.run(
            SandboxSpec(argv=("/bin/sh", "-c", sh), cwd=str(d), env={"TMPDIR": str(d)}, memory_limit_mb=2048, pids_limit=64,
                        cpu_seconds=300, wall_seconds=300, writable_paths=(str(d),), label="fuzzbuild")
        )
        if build.returncode != 0:
            return False, f"fuzz build failed: {(build.stdout + build.stderr)[-3000:]}"
        # LeakSanitizer is off on purpose: its end-of-process scan stops the world through a
        # ptrace-attaching tracer thread, which the sandbox's seccomp policy denies, so it can only
        # ever abort with "LeakSanitizer has encountered a fatal error". The driver checks leaks
        # itself, ptrace-free, from the allocator's in-use byte count around every candidate call
        # (native_fuzz.c: heap_in_use), in the quick (L2) pass as well as the sanitized (L6) one.
        # tcache off: mallinfo2() does not walk it, so parked frees would read as in-use heap.
        env = {"ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0:exitcode=3", "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1",
               "GLIBC_TUNABLES": "glibc.malloc.tcache_count=0"}
        res = sandbox.run(
            SandboxSpec(argv=("./native_fuzz", str(seed), str(iterations)), cwd=str(d), env=env, memory_limit_mb=4096 if sanitize else 1024,
                        pids_limit=16, cpu_seconds=600, wall_seconds=600, writable_paths=(str(d),), label="fuzz")
        )
        out = (res.stdout + res.stderr)[-3000:]
        if res.returncode != 0:
            reason = res.killed_reason or ("memory leak" if res.returncode == 4 else f"exit {res.returncode}")
            return False, f"native differential fuzz failed ({reason}): {out}"
        return True, out
    finally:
        shutil.rmtree(d, ignore_errors=True)
