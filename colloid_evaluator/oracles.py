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


def send(client: httpx.Client, req: Request) -> tuple[int, bytes]:
    try:
        if req.method == "POST":
            r = client.post(req.path, content=req.body.encode(), headers={"content-type": "application/json"})
        else:
            r = client.get(req.path)
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

    def run(self, cand_ws: Workspace, seed: int, size: str = "quick", max_mismatches: int = 5) -> OracleResult:
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
                cand = self.target.start_service(cand_ws, cand_db, hash_seed=(seed + 1) % 1000)
            except RuntimeError as exc:
                return OracleResult(False, 0, [f"candidate failed to start: {str(exc)[:500]}"], time.monotonic() - t0)
            with ref.client(timeout=30.0) as rc, cand.client(timeout=30.0) as cc:
                for i, req in enumerate(seq):
                    sa, ba = send(rc, req)
                    sb, bb = send(cc, req)
                    if sa < 0:
                        raise RuntimeError(f"reference service failed on {req.method} {req.path}: {ba[:200]!r}")
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
            if ref is not None:
                self.target.stop_service(ref)
            self.target.drop_db(ref_db)
            self.target.drop_db(cand_db)
        return OracleResult(not mismatches, len(seq), mismatches, time.monotonic() - t0, log)


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
        env = {"ASAN_OPTIONS": "detect_leaks=1:abort_on_error=0:exitcode=3", "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1"}
        res = sandbox.run(
            SandboxSpec(argv=("./native_fuzz", str(seed), str(iterations)), cwd=str(d), env=env, memory_limit_mb=4096 if sanitize else 1024,
                        pids_limit=16, cpu_seconds=600, wall_seconds=600, writable_paths=(str(d),), label="fuzz")
        )
        out = (res.stdout + res.stderr)[-3000:]
        if res.returncode != 0:
            reason = res.killed_reason or f"exit {res.returncode}"
            return False, f"native differential fuzz failed ({reason}): {out}"
        return True, out
    finally:
        shutil.rmtree(d, ignore_errors=True)
