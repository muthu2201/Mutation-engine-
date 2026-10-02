"""Linux sandbox adapter: cgroups v1 + network namespace + rlimits + uid drop + seccomp.

Isolation levels (blueprint D4) map onto this adapter as follows:

* **A** (knobs)        - same as B; knob genes only change the *launch configuration* of a
                         target process, which still runs sandboxed.
* **B** (user code)    - unprivileged ``colloid-sbx`` user, empty network namespace, seccomp
                         deny-list, memory/pids/CPU quotas, wall-clock kill.
* **C** (data path)    - B plus a throwaway database cloned from a template for every
                         evaluation (handled by the target adapter), never production data.
* **D** (kernel)       - not offered by this adapter; it refuses risk class D. Kernel genes
                         need full VMs (QEMU/KVM), see docs/adr/0004.

Every sandboxed process tree lives in its own cgroups (cpuacct, memory, pids, freezer).
That gives exact CPU accounting for the *whole* tree (the anti-reward-hacking rule "measure
CPU time across the process tree" - a candidate cannot hide work in a child process or a
background thread) and lets :meth:`LinuxProcess.kill` freeze-then-kill the tree atomically,
which defeats fork races.
"""

from __future__ import annotations

import contextlib
import os
import pwd
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


class CgroupSet:
    """One cgroup per controller for a process tree."""

    def __init__(self, label: str, memory_limit_mb: int | None = None, pids_limit: int | None = None) -> None:
        self.name = f"{label}-{uuid.uuid4().hex[:8]}"
        self.dirs: dict[str, Path] = {}
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

    def pids(self) -> list[int]:
        try:
            text = (self.dirs["pids"] / "cgroup.procs").read_text()
        except FileNotFoundError:
            return []
        return [int(x) for x in text.split()]

    def cpu_usage_ns(self) -> int:
        return int((self.dirs["cpuacct"] / "cpuacct.usage").read_text())

    def memory_peak_bytes(self) -> int:
        return int((self.dirs["memory"] / "memory.max_usage_in_bytes").read_text())

    def oom_killed(self) -> bool:
        try:
            text = (self.dirs["memory"] / "memory.oom_control").read_text()
        except FileNotFoundError:
            return False
        for line in text.splitlines():
            if line.startswith("oom_kill "):
                return int(line.split()[1]) > 0
        return False

    def kill_all(self, timeout: float = 5.0) -> None:
        freezer = self.dirs["freezer"] / "freezer.state"
        deadline = time.monotonic() + timeout
        while True:
            pids = self.pids()
            if not pids:
                break
            with contextlib.suppress(OSError):
                _write(freezer, "FROZEN")
            for pid in pids:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                _write(freezer, "THAWED")
            if time.monotonic() > deadline:
                raise SandboxError(f"could not kill process tree in {self.name}: {pids}")
            time.sleep(0.02)

    def remove(self) -> None:
        self.kill_all()
        for d in self.dirs.values():
            for _ in range(50):
                try:
                    d.rmdir()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.02)

    def paths(self) -> list[str]:
        return [str(p) for p in self.dirs.values()]


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
        self._lock = threading.Lock()

    def alive(self) -> bool:
        return self.popen.poll() is None

    def pids(self) -> list[int]:
        return self.cgroups.pids()

    def cpu_usage_ns(self) -> int:
        return self.cgroups.cpu_usage_ns()

    def cpuacct_path(self) -> str:
        return str(self.cgroups.dirs["cpuacct"] / "cpuacct.usage")

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
            self.cgroups.remove()
            self._closed = True

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
        oom = proc.cgroups.oom_killed()
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
    for ctrl in CONTROLLERS:
        base = CGROUP_ROOT / ctrl / PARENT
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
