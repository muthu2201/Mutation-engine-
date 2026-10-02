"""Portable process sandbox (isolation level C) for Linux, macOS and Windows.

**This is not a security boundary.** It cannot stop a process from opening sockets, reading
files the user can read or making arbitrary syscalls. Only the Linux sandbox (seccomp,
namespaces, cgroups) can, and on macOS and Windows that means running the evaluator inside
the Colloid Linux container (``docker/``). This sandbox therefore **refuses to run untrusted
candidate code** (``run_as_sandbox_user=True``, the flag every candidate spec carries) unless
the operator opts in with ``allow_untrusted=True`` or ``COLLOID_ALLOW_UNSANDBOXED=1`` for
deterministic-operator-only development on a workstation. LLM- and red-team-generated code
must never run under it.

What it does provide, identically on every OS:

* **resource governance**: wall-clock limit, a watchdog that kills the tree when its total
  RSS exceeds ``memory_limit_mb`` (reported as OOM) or its process count exceeds
  ``pids_limit``, plus POSIX rlimits (CPU seconds, file size, open files) where they exist;
* **whole-tree cleanup**: the candidate starts in its own session (POSIX) or process group
  (Windows), and every descendant is killed on exit or timeout;
* **whole-tree CPU accounting**: a :class:`~colloid.adapters.platform.CpuCounterFile` per
  process, so the benchmark measures it exactly as it measures a cgroup.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from colloid.adapters.platform import CpuCounterFile, kill_tree, pin_cpus, process_tree, psutil
from colloid.ports import SandboxResult, SandboxSpec


class UnsafeIsolation(RuntimeError):
    """Untrusted candidate code was about to run without a security boundary."""


def _parse_cpus(spec: str | None) -> list[int]:
    if not spec:
        return []
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


class PortableProcess:
    def __init__(self, popen: subprocess.Popen[bytes], spec: SandboxSpec, log_paths: tuple[Path, Path], counter: CpuCounterFile) -> None:
        self.popen = popen
        self.spec = spec
        self.pid = popen.pid
        self.cgroup = f"portable-{spec.label}-{self.pid}"  # no cgroup; a stable label for logs
        self.log_paths = log_paths
        self.counter = counter
        self.started = time.monotonic()
        self.killed_reason = ""
        self._oom = False
        self._closed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watch = threading.Thread(target=self._watchdog, daemon=True)
        self._watch.start()

    # ------------------------------------------------------------------ SandboxProcess port
    def alive(self) -> bool:
        return self.popen.poll() is None

    def pids(self) -> list[int]:
        return process_tree(self.pid) if self.alive() else []

    def cpu_usage_ns(self) -> int:
        return self.counter.sample()

    def cpuacct_path(self) -> str:
        return str(self.counter.path)

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def terminate(self, grace: float = 3.0) -> None:
        if psutil is not None:
            for pid in self.pids():
                with contextlib.suppress(psutil.Error):
                    psutil.Process(pid).terminate()
        deadline = time.monotonic() + grace
        while self.alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.kill()

    def kill(self, reason: str = "") -> None:
        with self._lock:
            if self._closed:
                return
            if reason and not self.killed_reason:
                self.killed_reason = reason
            kill_tree(self.pid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.popen.wait(timeout=5)
            self._stop.set()
            self.counter.stop()
            self._closed = True

    def oom_killed(self) -> bool:
        return self._oom

    def logs(self, limit: int = 20_000) -> tuple[str, str]:
        out = []
        for p in self.log_paths:
            try:
                out.append(p.read_bytes()[-limit:].decode("utf-8", "replace"))
            except FileNotFoundError:
                out.append("")
        return out[0], out[1]

    # ------------------------------------------------------------------ governance
    def _watchdog(self) -> None:
        limit_bytes = self.spec.memory_limit_mb * 1024 * 1024
        deadline = self.started + self.spec.wall_seconds if self.spec.wall_seconds > 0 else float("inf")
        while not self._stop.wait(0.05):
            if not self.alive():
                return
            if time.monotonic() > deadline:
                self.kill("wall-clock limit")
                return
            if psutil is None:
                continue
            pids = self.pids()
            rss = 0
            for pid in pids:
                with contextlib.suppress(psutil.Error):
                    rss += psutil.Process(pid).memory_info().rss
            if rss > limit_bytes:
                self._oom = True
                self.kill("memory limit (OOM)")
                return
            if len(pids) > self.spec.pids_limit:
                self.kill("pids limit")
                return


class ProcessSandbox:
    """The Sandbox port at isolation level C (see the module docstring for what that means)."""

    PORT_API = "1.0.0"
    isolation_levels = ("C",)

    def __init__(self, log_dir: Path | None = None, *, allow_untrusted: bool | None = None) -> None:
        self.log_dir = Path(log_dir or Path(os.environ.get("COLLOID_STATE") or Path.home() / ".colloid" / "state") / "sandbox-logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.allow_untrusted = os.environ.get("COLLOID_ALLOW_UNSANDBOXED") == "1" if allow_untrusted is None else allow_untrusted

    def _preexec(self, spec: SandboxSpec) -> Any:
        if os.name != "posix":
            return None
        import resource  # POSIX-only

        def limits() -> None:
            os.setsid()
            for lim, val in ((resource.RLIMIT_CPU, spec.cpu_seconds), (resource.RLIMIT_FSIZE, spec.file_size_mb * 1024 * 1024),
                             (resource.RLIMIT_NOFILE, spec.open_files)):
                with contextlib.suppress(ValueError, OSError):
                    soft, hard = resource.getrlimit(lim)
                    cap = val if hard == resource.RLIM_INFINITY else min(val, hard)
                    resource.setrlimit(lim, (cap, hard))
            if spec.nice:
                with contextlib.suppress(OSError):
                    os.nice(spec.nice)

        return limits

    def spawn(self, spec: SandboxSpec) -> PortableProcess:
        if spec.run_as_sandbox_user and not self.allow_untrusted:
            raise UnsafeIsolation(
                "refusing to run untrusted candidate code at isolation level C (no security boundary on this host). "
                "Run the evaluator on Linux as root, or inside the Colloid Linux container (docker/README.md). "
                "Set COLLOID_ALLOW_UNSANDBOXED=1 only for trusted, deterministic operators on a development machine.")
        label = f"{spec.label}-{uuid.uuid4().hex[:8]}"
        out_path = Path(spec.stdout_path) if spec.stdout_path else self.log_dir / f"{label}.out"
        err_path = out_path.with_suffix(".err")
        env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "HOME": spec.cwd, **(
            {"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if sys.platform == "win32" else {}), **spec.env}
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["preexec_fn"] = self._preexec(spec)
        else:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            popen = subprocess.Popen(list(spec.argv), cwd=spec.cwd, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                     close_fds=True, **kwargs)
        if spec.cpus:
            pin_cpus(_parse_cpus(spec.cpus), popen.pid)
        counter = CpuCounterFile(popen.pid, self.log_dir / f"{label}.cpu_ns").start()
        return PortableProcess(popen, spec, (out_path, err_path), counter)

    def run(self, spec: SandboxSpec) -> SandboxResult:
        start = time.monotonic()
        proc = self.spawn(spec)
        rc = proc.wait(timeout=spec.wall_seconds + 2.0 if spec.wall_seconds > 0 else None)
        timed_out = proc.killed_reason == "wall-clock limit" or rc is None
        proc.kill("wall-clock limit" if rc is None else "")
        if rc is None:
            rc = proc.popen.returncode if proc.popen.returncode is not None else -9
        stdout, stderr = proc.logs()
        for p in proc.log_paths:
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
        with contextlib.suppress(FileNotFoundError):
            proc.counter.path.unlink()
        return SandboxResult(rc, stdout, stderr, time.monotonic() - start, timed_out, proc.killed_reason)
