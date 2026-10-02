"""Linux sandbox adapter: cgroups (v1 or unified v2) + network namespace + rlimits + uid drop + seccomp.

Isolation levels (blueprint D4) map onto this adapter as follows:

* **A** (knobs)        - same as B; knob genes only change the *launch configuration* of a
                         target process, which still runs sandboxed.
* **B** (user code)    - unprivileged ``colloid-sbx`` user, empty network namespace, seccomp
                         deny-list, memory/pids/CPU quotas, wall-clock kill.
* **C** (data path)    - B plus a throwaway database cloned from a template for every
                         evaluation (handled by the target adapter), never production data.
* **D** (kernel)       - not offered by this adapter; it refuses risk class D. Kernel genes
                         need full VMs (QEMU/KVM), see docs/adr/0004.

Every sandboxed process tree lives in its own cgroups: cpuacct, memory, pids and freezer on
cgroup v1 hosts, or one unified-hierarchy cgroup with cpu, memory and pids on v2 hosts
(:class:`CgroupSet` hides the difference).
That gives exact CPU accounting for the *whole* tree (the anti-reward-hacking rule "measure
CPU time across the process tree" - a candidate cannot hide work in a child process or a
background thread) and lets :meth:`LinuxProcess.kill` freeze-then-kill the tree atomically,
which defeats fork races.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from colloid.ports import SandboxResult, SandboxSpec

CGROUP_ROOT = Path("/sys/fs/cgroup")
CONTROLLERS = ("cpuacct", "memory", "pids", "freezer")
SANDBOX_USER = "colloid-sbx"
HELPER_SRC = Path(__file__).with_name("sbx_exec.c")
DEFAULT_HELPER = Path("/opt/colloid/bin/sbx-exec")
PARENT = "colloid"


class SandboxError(RuntimeError):
    pass


def ensure_helper(path: Path = DEFAULT_HELPER) -> Path:
    """Compile the sbx-exec helper if it is missing or older than its source."""
    if path.exists() and path.stat().st_mtime >= HELPER_SRC.stat().st_mtime:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    cmd = ["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(tmp), str(HELPER_SRC)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SandboxError(f"failed to build sbx-exec: {proc.stderr}")
    tmp.chmod(0o755)
    tmp.replace(path)
    return path


def ensure_user(name: str = SANDBOX_USER) -> tuple[int, int]:
    import pwd  # POSIX-only: imported where used, so this module imports on every OS

    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        subprocess.run(
            ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", name],
            check=True,
            capture_output=True,
        )
        pw = pwd.getpwnam(name)
    return pw.pw_uid, pw.pw_gid


def _max_nofile() -> int:
    """The largest RLIMIT_NOFILE we may set: min(our hard limit, fs.nr_open)."""
    import resource

    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    try:
        nr_open = int(Path("/proc/sys/fs/nr_open").read_text())
    except OSError:
        nr_open = hard
    return min(x for x in (hard, nr_open) if x > 0)


def _write(path: Path, value: str) -> None:
    with open(path, "w") as fh:
        fh.write(value)


def _cgroup_gone(exc: OSError) -> bool:
    """A cgroup that was removed (ENOENT) or is being torn down (ENODEV on its control files)."""
    return exc.errno in (errno.ENOENT, errno.ENODEV)


def cgroup_mode() -> str:
    """``"v1"`` when the controllers we need are mounted as v1 hierarchies (including hybrid
    hosts, where the v2 mount at /sys/fs/cgroup/unified has no controllers), ``"v2"`` on a
    unified hierarchy (Ubuntu 22.04+, Fedora, Debian 12, Docker Desktop's VM, GitHub runners)."""
    if all((CGROUP_ROOT / c).is_dir() for c in CONTROLLERS):
        return "v1"
    if (CGROUP_ROOT / "cgroup.controllers").exists():
        return "v2"
    raise SandboxError("no usable cgroup hierarchy (need v1 cpuacct/memory/pids/freezer or a v2 unified mount)")


V2_CONTROLLERS = ("cpu", "memory", "pids")
_V2_READY = False
_V2_LOCK = threading.Lock()


def _v2_prepare() -> Path:
    """Create /sys/fs/cgroup/colloid with cpu, memory and pids delegated to its children.

    v2's "no internal processes" rule forbids enabling controllers for the children of a
    cgroup that itself holds processes, except at the real root. Inside a container the
    namespace root does hold processes (the container's own), so they are moved to a leaf
    ``init`` cgroup first: the same thing docker-in-docker entrypoints do."""
    global _V2_READY
    base = CGROUP_ROOT / PARENT
    with _V2_LOCK:
        if _V2_READY:
            return base
        wanted = " ".join(f"+{c}" for c in V2_CONTROLLERS)
        available = (CGROUP_ROOT / "cgroup.controllers").read_text().split()
        missing = [c for c in V2_CONTROLLERS if c not in available]
        if missing:
            raise SandboxError(f"cgroup v2 controllers {missing} are not available (delegate them to this cgroup)")
        try:
            _write(CGROUP_ROOT / "cgroup.subtree_control", wanted)
        except OSError as exc:
            if exc.errno != errno.EBUSY:
                raise
            leaf = CGROUP_ROOT / "init"
            leaf.mkdir(exist_ok=True)
            for pid in (CGROUP_ROOT / "cgroup.procs").read_text().split():
                with contextlib.suppress(OSError):  # a process may exit meanwhile
                    _write(leaf / "cgroup.procs", pid)
            _write(CGROUP_ROOT / "cgroup.subtree_control", wanted)
        base.mkdir(exist_ok=True)
        _write(base / "cgroup.subtree_control", wanted)
        _V2_READY = True
        return base


def _kv(text: str, key: str) -> int | None:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            return int(parts[1])
    return None


class CgroupSet:
    """The cgroups of one sandboxed process tree.

    On v1, one cgroup per controller (cpuacct, memory, pids, freezer); on v2, one cgroup in
    the unified hierarchy with cpu, memory and pids enabled. Both give the same guarantees:
    exact whole-tree CPU (``cpuacct.usage`` / ``cpu.stat usage_usec``), a memory limit with OOM
    reporting, a pids limit, and an atomic kill (freeze-then-kill on v1; ``cgroup.kill`` or
    ``cgroup.freeze`` on v2)."""

    def __init__(self, label: str, memory_limit_mb: int | None = None, pids_limit: int | None = None) -> None:
        self.name = f"{label}-{uuid.uuid4().hex[:8]}"
        self.mode = cgroup_mode()
        self.dirs: dict[str, Path] = {}
        if self.mode == "v1":
            for ctrl in CONTROLLERS:
                base = CGROUP_ROOT / ctrl / PARENT
                base.mkdir(exist_ok=True)
                d = base / self.name
                d.mkdir()
                self.dirs[ctrl] = d
            if memory_limit_mb:
                _write(self.dirs["memory"] / "memory.limit_in_bytes", str(memory_limit_mb * 1024 * 1024))
                with contextlib.suppress(OSError):
                    _write(self.dirs["memory"] / "memory.oom_control", "0")
            if pids_limit:
                _write(self.dirs["pids"] / "pids.max", str(pids_limit))
        else:
            d = _v2_prepare() / self.name
            d.mkdir()
            self.dirs = dict.fromkeys(CONTROLLERS, d)  # one unified directory serves every role
            if memory_limit_mb:
                _write(d / "memory.max", str(memory_limit_mb * 1024 * 1024))
                with contextlib.suppress(OSError):  # no swap escape hatch around the limit
                    _write(d / "memory.swap.max", "0")
            if pids_limit:
                _write(d / "pids.max", str(pids_limit))

    def _read(self, role: str, name: str) -> str | None:
        try:
            return (self.dirs[role] / name).read_text()
        except OSError as exc:
            if _cgroup_gone(exc):
                return None
            raise

    def pids(self) -> list[int]:
        text = self._read("pids", "cgroup.procs")
        return [int(x) for x in text.split()] if text else []

    def cpu_counter_path(self) -> Path:
        """The file the load generator reads at chunk boundaries for whole-tree CPU."""
        return self.dirs["cpuacct"] / ("cpuacct.usage" if self.mode == "v1" else "cpu.stat")

    def cpu_usage_ns(self) -> int:
        if self.mode == "v1":
            return int((self.dirs["cpuacct"] / "cpuacct.usage").read_text())
        usec = _kv((self.dirs["cpuacct"] / "cpu.stat").read_text(), "usage_usec")
        return (usec or 0) * 1000

    def memory_peak_bytes(self) -> int:
        if self.mode == "v1":
            return int((self.dirs["memory"] / "memory.max_usage_in_bytes").read_text())
        peak = self._read("memory", "memory.peak") or self._read("memory", "memory.current")  # memory.peak needs Linux 5.19+
        return int(peak or 0)

    def oom_killed(self) -> bool:
        text = self._read("memory", "memory.oom_control" if self.mode == "v1" else "memory.events")
        n = _kv(text, "oom_kill") if text else None
        return bool(n and n > 0)

    def kill_all(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        if self.mode == "v2" and (self.dirs["pids"] / "cgroup.kill").exists():
            with contextlib.suppress(OSError):
                _write(self.dirs["pids"] / "cgroup.kill", "1")  # Linux 5.14+: kernel kills the whole tree atomically
        freezer = self.dirs["freezer"] / ("freezer.state" if self.mode == "v1" else "cgroup.freeze")
        frozen, thawed = ("FROZEN", "THAWED") if self.mode == "v1" else ("1", "0")
        while True:
            pids = self.pids()
            if not pids:
                break
            with contextlib.suppress(OSError):
                _write(freezer, frozen)
            for pid in pids:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                _write(freezer, thawed)
            if time.monotonic() > deadline:
                raise SandboxError(f"could not kill process tree in {self.name}: {pids}")
            time.sleep(0.02)

    def remove(self) -> None:
        self.kill_all()
        for d in dict.fromkeys(self.dirs.values()):  # v2: the same directory appears once
            for _ in range(50):
                try:
                    d.rmdir()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.02)

    def paths(self) -> list[str]:
        return [str(p) for p in dict.fromkeys(self.dirs.values())]


def sandbox_cgroup_parents() -> list[Path]:
    """Where this adapter creates per-tree cgroups (for cleanup and leak accounting)."""
    try:
        mode = cgroup_mode()
    except SandboxError:
        return []
    return [CGROUP_ROOT / c / PARENT for c in CONTROLLERS] if mode == "v1" else [CGROUP_ROOT / PARENT]


class LinuxProcess:
    def __init__(self, popen: subprocess.Popen[bytes], cgroups: CgroupSet, spec: SandboxSpec, log_paths: tuple[Path, Path]) -> None:
        self.popen = popen
        self.cgroups = cgroups
        self.spec = spec
        self.pid = popen.pid
        self.cgroup = cgroups.name
        self.log_paths = log_paths
        self.started = time.monotonic()
        self._closed = False
        self._oom = False
        self._lock = threading.Lock()

    def alive(self) -> bool:
        return self.popen.poll() is None

    def pids(self) -> list[int]:
        return self.cgroups.pids()

    def cpu_usage_ns(self) -> int:
        return self.cgroups.cpu_usage_ns()

    def cpuacct_path(self) -> str:
        return str(self.cgroups.cpu_counter_path())

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def terminate(self, grace: float = 3.0) -> None:
        """SIGTERM the tree, then SIGKILL whatever is left after ``grace`` seconds."""
        for pid in self.pids():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while self.pids() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.kill()

    def kill(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.cgroups.kill_all()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.popen.wait(timeout=5)
            # Snapshot before the cgroups go: once removal starts, reading memory.oom_control can
            # fail with ENODEV, and the wall-clock timer thread may be the one closing us.
            self._oom = self.cgroups.oom_killed()
            self.cgroups.remove()
            self._closed = True

    def oom_killed(self) -> bool:
        """Whether the kernel OOM-killed anything in the tree; safe against a concurrent kill()."""
        with self._lock:
            return self._oom if self._closed else self.cgroups.oom_killed()

    def logs(self, limit: int = 20_000) -> tuple[str, str]:
        out = []
        for p in self.log_paths:
            try:
                data = p.read_bytes()[-limit:]
                out.append(data.decode("utf-8", "replace"))
            except FileNotFoundError:
                out.append("")
        return out[0], out[1]


class LinuxSandbox:
    """The Sandbox port implementation."""

    PORT_API = "1.0.0"
    isolation_levels = ("A", "B", "C")

    def __init__(self, helper: Path | None = None, log_dir: Path | None = None) -> None:
        if os.geteuid() != 0:
            raise SandboxError("the Linux sandbox adapter must run as root (it drops privileges per candidate)")
        self.helper = ensure_helper(helper or DEFAULT_HELPER)
        self.uid, self.gid = ensure_user()
        self.log_dir = log_dir or Path("/opt/colloid/state/sandbox-logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if cgroup_mode() == "v1":  # v2: the unified parent is prepared (with delegation) on first use
            for ctrl in CONTROLLERS:
                (CGROUP_ROOT / ctrl / PARENT).mkdir(exist_ok=True)

    def _argv(self, spec: SandboxSpec, cg: CgroupSet) -> list[str]:
        if spec.risk_class == "D":
            raise SandboxError("risk class D (kernel/privileged) requires a VM sandbox; refusing")
        args = [str(self.helper)]
        for p in cg.paths():
            args += ["--cgroup", p]
        if not spec.network:
            args.append("--netns")
        if spec.cpus:
            args += ["--cpus", spec.cpus]
        if spec.nice:
            args += ["--nice", str(spec.nice)]
        if spec.sched_policy != "other":
            args += ["--sched", spec.sched_policy]
        args += [
            "--nproc", str(spec.pids_limit * 4),
            "--fsize-mb", str(spec.file_size_mb),
            "--nofile", str(min(spec.open_files, _max_nofile())),
            "--cpu-s", str(spec.cpu_seconds),
            "--chdir", spec.cwd,
        ]
        if spec.user is not None:
            import pwd  # POSIX-only: imported where used
            pw = pwd.getpwnam(spec.user)
            args += ["--uid", str(pw.pw_uid), "--gid", str(pw.pw_gid)]
        elif spec.run_as_sandbox_user:
            args += ["--uid", str(self.uid), "--gid", str(self.gid), "--seccomp"]
        else:
            args += ["--no-drop"]
        return [*args, "--", *spec.argv]

    def spawn(self, spec: SandboxSpec) -> LinuxProcess:
        cg = CgroupSet(spec.label, spec.memory_limit_mb, spec.pids_limit)
        for wp in spec.writable_paths:
            if spec.user is not None:
                import pwd  # POSIX-only: imported where used
                pw = pwd.getpwnam(spec.user)
                os.chown(wp, pw.pw_uid, pw.pw_gid)
            elif spec.run_as_sandbox_user:
                os.chown(wp, self.uid, self.gid)
        out_path = Path(spec.stdout_path) if spec.stdout_path else self.log_dir / f"{cg.name}.out"
        err_path = out_path.with_suffix(".err")
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": spec.cwd}
        env.update(spec.env)
        try:
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                popen = subprocess.Popen(
                    self._argv(spec, cg), env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True
                )
        except Exception:
            cg.remove()
            raise
        proc = LinuxProcess(popen, cg, spec, (out_path, err_path))
        if spec.wall_seconds > 0:
            timer = threading.Timer(spec.wall_seconds, proc.kill)
            timer.daemon = True
            timer.start()
        return proc

    def run(self, spec: SandboxSpec) -> SandboxResult:
        start = time.monotonic()
        proc = self.spawn(spec)
        rc = proc.wait(timeout=spec.wall_seconds + 2.0)
        timed_out = rc is None or (time.monotonic() - start) >= spec.wall_seconds
        oom = proc.oom_killed()
        proc.kill()
        if rc is None:
            rc = proc.popen.returncode if proc.popen.returncode is not None else -9
        stdout, stderr = proc.logs()
        reason = "wall-clock limit" if timed_out else ("memory limit (OOM)" if oom else "")
        for p in proc.log_paths:
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
        return SandboxResult(rc, stdout, stderr, time.monotonic() - start, timed_out, reason)


def cleanup_stale_cgroups() -> int:
    """Remove leftover cgroups from crashed runs (kills any processes still inside)."""
    removed = 0
    for base in sandbox_cgroup_parents():
        if not base.exists():
            continue
        for d in base.iterdir():
            if not d.is_dir():
                continue
            procs = d / "cgroup.procs"
            with contextlib.suppress(OSError, ValueError):
                for pid in [int(x) for x in procs.read_text().split()]:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
            for _ in range(50):
                try:
                    d.rmdir()
                    removed += 1
                    break
                except OSError:
                    time.sleep(0.02)
    return removed


def which(names: Sequence[str]) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None
