"""Platform capabilities and portable OS primitives (Linux, macOS, Windows).

Colloid's *search side* is pure Python and runs anywhere. Its *judge* needs four OS
services, and each has a portable fallback:

==================  ===================================  ======================================
Need                Linux (full fidelity)                Elsewhere (portable fallback)
==================  ===================================  ======================================
isolate untrusted   seccomp + namespaces + cgroups       none in-process: run the evaluator in
candidate code      (``adapters/sandbox/linux.py``)      the Linux container (``docker/``), or
                                                         ``ProcessSandbox`` for trusted code only
whole-tree CPU      cgroup ``cpuacct.usage``             :class:`CpuCounterFile`: psutil-sampled
accounting                                               tree CPU written to a counter file
memory footprint    ``/proc/<pid>/smaps_rollup`` (PSS)   psutil PSS (Linux) -> USS -> RSS
pin CPUs            ``sched_setaffinity``                psutil ``cpu_affinity`` (Windows);
                                                         macOS cannot pin -> recorded unpinned
==================  ===================================  ======================================

The fallbacks are honest about what they lose, and the evaluator records the capability set
in every environment fingerprint. Measurements from different capability sets are never
compared, and the A/A test measures whatever extra noise a fallback adds before any promotion.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:  # psutil is the portable process API; Linux full-fidelity paths do not need it
    import psutil
except ImportError:  # pragma: no cover - exercised only on hosts without psutil
    psutil = None


@dataclass(frozen=True)
class Capabilities:
    os: str  # "linux" | "darwin" | "windows"
    arch: str
    python: str
    posix: bool
    root: bool
    cgroup_v1: bool
    cgroup_v2: bool
    seccomp: bool
    smaps: bool
    cpu_affinity: str  # "sched" | "psutil" | "none"
    psutil: bool
    container_runtime: str | None  # "docker" | "podman" | None
    container_daemon: bool

    @property
    def linux_sandbox(self) -> bool:
        """Everything the full-fidelity Linux sandbox needs."""
        return self.os == "linux" and self.root and (self.cgroup_v1 or self.cgroup_v2) and self.seccomp

    def sandbox_backend(self) -> str:
        return "linux" if self.linux_sandbox else "process"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sandbox_backend"] = self.sandbox_backend()
        return d


def _os_name() -> str:
    return {"linux": "linux", "darwin": "darwin", "win32": "windows", "cygwin": "windows"}.get(sys.platform, sys.platform)


def _container() -> tuple[str | None, bool]:
    for rt in ("docker", "podman"):
        if shutil.which(rt):
            try:
                ok = subprocess.run([rt, "info"], capture_output=True, timeout=8, check=False).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            return rt, ok
    return None, False


_CAPS: Capabilities | None = None
_FORCED = "COLLOID_FORCE_PORTABLE"  # set to 1 to exercise the portable paths on a Linux host


def capabilities(refresh: bool = False) -> Capabilities:
    global _CAPS
    if _CAPS is not None and not refresh:
        return _CAPS
    name = _os_name()
    forced = os.environ.get(_FORCED) == "1"
    is_linux = name == "linux" and not forced
    seccomp = False
    if is_linux:
        with contextlib.suppress(OSError):
            seccomp = any(line.startswith("Seccomp:") for line in Path("/proc/self/status").read_text().splitlines())
    if is_linux and hasattr(os, "sched_setaffinity"):
        affinity = "sched"
    elif psutil is not None and hasattr(psutil.Process(), "cpu_affinity"):
        affinity = "psutil"
    else:
        affinity = "none"
    runtime, daemon = _container()
    _CAPS = Capabilities(
        os=name, arch=platform.machine().lower(), python=platform.python_version(), posix=os.name == "posix",
        root=hasattr(os, "geteuid") and os.geteuid() == 0,
        cgroup_v1=is_linux and Path("/sys/fs/cgroup/cpuacct").is_dir(),
        cgroup_v2=is_linux and Path("/sys/fs/cgroup/cgroup.controllers").exists() and {"cpu", "memory", "pids"} <= set(
            Path("/sys/fs/cgroup/cgroup.controllers").read_text().split()),
        seccomp=seccomp, smaps=is_linux and Path("/proc/self/smaps_rollup").exists(), cpu_affinity=affinity,
        psutil=psutil is not None, container_runtime=runtime, container_daemon=daemon,
    )
    return _CAPS


# ---------------------------------------------------------------------- CPU pinning
def pin_cpus(cpus: Iterable[int], pid: int = 0) -> bool:
    """Pin ``pid`` (0 = this process) to ``cpus``. Returns False where the OS cannot pin
    (macOS): the caller records the run as unpinned instead of failing."""
    want = sorted(set(cpus))
    available = os.cpu_count() or 1
    want = [c for c in want if c < available] or list(range(available))
    caps = capabilities()
    try:
        if caps.cpu_affinity == "sched":
            os.sched_setaffinity(pid, set(want))
            return True
        if caps.cpu_affinity == "psutil" and psutil is not None:
            psutil.Process(pid or os.getpid()).cpu_affinity(want)
            return True
    except (OSError, ValueError):
        return False
    return False


# ---------------------------------------------------------------------- process trees
def process_tree(root_pid: int) -> list[int]:
    if psutil is None:
        return [root_pid]
    try:
        root = psutil.Process(root_pid)
        return [root_pid, *(c.pid for c in root.children(recursive=True))]
    except psutil.Error:
        return []


def pss_mb(pids: Sequence[int]) -> float:
    """Proportional set size of a set of processes in MB (shared pages split between sharers).
    Linux reads ``smaps_rollup``; elsewhere psutil gives PSS where the OS has it, then USS
    (pages unique to the process), then RSS. The metric used is recorded in the fingerprint."""
    if capabilities().smaps:
        total_kb = 0
        for pid in pids:
            try:
                with open(f"/proc/{pid}/smaps_rollup") as fh:
                    for line in fh:
                        if line.startswith("Pss:"):
                            total_kb += int(line.split()[1])
                            break
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        return total_kb / 1024.0
    if psutil is None:
        return 0.0
    total = 0
    for pid in pids:
        try:
            info = psutil.Process(pid).memory_full_info()
            total += getattr(info, "pss", None) or getattr(info, "uss", None) or info.rss
        except (psutil.Error, OSError):
            continue
    return total / (1024 * 1024)


def memory_metric() -> str:
    if capabilities().smaps:
        return "pss(smaps_rollup)"
    return "pss" if capabilities().os == "linux" else "uss"


class CpuCounterFile:
    """A file holding the cumulative CPU time (ns) of a process tree: the portable stand-in
    for a cgroup's ``cpuacct.usage``. The benchmark's load generator reads it at chunk
    boundaries exactly as it reads a cgroup file.

    A sampling thread walks the tree every ``interval`` seconds and remembers, per process
    (keyed by pid + creation time, so a recycled pid is a new process), the last CPU time it
    saw. The counter is the sum of those, so it never goes backwards, and a process that exits
    keeps the CPU it had at its last sample. What is lost is at most one interval of CPU per
    exiting process. On Linux, :mod:`tests.portable` measures the error against cgroup
    accounting."""

    def __init__(self, root_pid: int, path: Path, interval: float = 0.01) -> None:
        if psutil is None:
            raise RuntimeError("portable CPU accounting needs psutil (pip install psutil)")
        self.root_pid = root_pid
        self.path = Path(path)
        self.interval = interval
        self._seen: dict[tuple[int, float], int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"cpu-counter-{root_pid}", daemon=True)
        self._write(0)

    def _write(self, ns: int) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(str(ns))
        os.replace(tmp, self.path)  # readers never see a torn number

    def sample(self) -> int:
        assert psutil is not None
        try:
            root = psutil.Process(self.root_pid)
            procs = [root, *root.children(recursive=True)]
        except psutil.Error:
            procs = []
        with self._lock:
            for p in procs:
                try:
                    with p.oneshot():
                        t = p.cpu_times()
                        key = (p.pid, p.create_time())
                    self._seen[key] = int((t.user + t.system) * 1e9)
                except psutil.Error:
                    continue
            return sum(self._seen.values())

    def _run(self) -> None:
        while not self._stop.is_set():
            self._write(self.sample())
            self._stop.wait(self.interval)

    def start(self) -> CpuCounterFile:
        self._thread.start()
        return self

    def value(self) -> int:
        return int(self.path.read_text() or 0)

    def stop(self) -> int:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        final = self.sample()
        self._write(final)
        return final


def kill_tree(root_pid: int, timeout: float = 5.0) -> None:
    """Kill a process and every descendant (portable; descendants first, then the root).

    Only the descendants are waited for here. The root is the caller's own child and must be
    reaped by the caller's ``Popen.wait()``: if psutil reaped it, ``Popen`` would get ECHILD
    and report exit code 0 for a killed process."""
    if psutil is None:
        with contextlib.suppress(OSError):
            os.kill(root_pid, 9)
        return
    try:
        root = psutil.Process(root_pid)
        descendants = root.children(recursive=True)
    except psutil.Error:
        return
    for p in [*descendants, root]:
        with contextlib.suppress(psutil.Error):
            p.kill()
    psutil.wait_procs(descendants, timeout=timeout)


def fingerprint_extras() -> dict[str, Any]:
    caps = capabilities()
    out: dict[str, Any] = {"capabilities": caps.to_dict(), "memory_metric": memory_metric(), "platform": platform.platform()}
    if psutil is not None:
        with contextlib.suppress(Exception):
            out["logical_cpus"] = psutil.cpu_count(logical=True)
            out["physical_cpus"] = psutil.cpu_count(logical=False)
            out["memory_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
    out["time_ns_resolution"] = time.get_clock_info("perf_counter").resolution
    return out
