"""Sandbox conformance (blueprint E): the escape canaries must be contained. These need root
(the adapter drops privileges itself) and cgroup/namespace access, so they are integration
tests. Run with COLLOID_INTEGRATION=1 as root."""

import os
import sys

import pytest

from tests.conftest import requires_integration

PY = sys.executable


@pytest.fixture(scope="module")
def sandbox():
    from colloid.adapters.sandbox.linux import LinuxSandbox

    return LinuxSandbox()


def _run(sandbox, code, **kw):
    from colloid.ports import SandboxSpec

    workdir = kw.pop("cwd", "/opt/colloid/state/sbxtest")
    os.makedirs(workdir, exist_ok=True)
    spec = SandboxSpec(argv=(PY, "-c", code), cwd=workdir, env={}, wall_seconds=kw.pop("wall", 10), **kw)
    return sandbox.run(spec)


@requires_integration
def test_network_egress_blocked(sandbox):
    r = _run(sandbox, "import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)")
    assert r.returncode != 0 and "Permission denied" in (r.stdout + r.stderr)


@requires_integration
def test_unix_socket_allowed(sandbox):
    r = _run(sandbox, "import socket; socket.socket(socket.AF_UNIX); print('ok')")
    assert r.returncode == 0 and "ok" in r.stdout


@requires_integration
def test_privilege_drop(sandbox):
    assert _run(sandbox, "import os; os.setuid(0)").returncode != 0
    r = _run(sandbox, "import os; print(os.getuid())")
    assert r.returncode == 0 and int(r.stdout.strip()) > 0


@requires_integration
def test_namespace_creation_blocked(sandbox):
    assert _run(sandbox, "import os; os.unshare(os.CLONE_NEWUSER)").returncode != 0


@requires_integration
def test_fork_bomb_contained(sandbox):
    r = _run(sandbox, "import os\nwhile True:\n    try: os.fork()\n    except OSError: pass", wall=4, pids_limit=64)
    assert r.timed_out or r.returncode != 0


@requires_integration
def test_memory_cap(sandbox):
    r = _run(sandbox, "x=[]\nwhile True: x.append(bytearray(50*1024*1024))", memory_limit_mb=256, wall=15)
    assert r.returncode != 0


@requires_integration
def test_wall_clock_kill(sandbox):
    r = _run(sandbox, "while True: pass", wall=2)
    assert r.timed_out


@requires_integration
def test_write_outside_workspace_blocked(sandbox):
    assert _run(sandbox, "open('/opt/colloid/escape.txt', 'w').write('x')").returncode != 0


@requires_integration
def test_fs_jail_holds_regardless_of_host_permissions(sandbox, tmp_path):
    """Regression (seen on a CI runner whose /opt/colloid was world-writable): confinement must
    not depend on directory permissions. A world-writable directory outside the workspace is
    read-only inside the jail, the declared workspace is writable, and /tmp is private: what
    one candidate leaves there is gone for the next one and never reaches the host."""
    import uuid

    base = f"/opt/colloid/state/sbxtest/jail-{uuid.uuid4().hex[:8]}"
    open_dir, ws = f"{base}/world-writable", f"{base}/ws"
    for d in (base, open_dir, ws):
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o777)
    escape = _run(sandbox, f"open('{open_dir}/x', 'w').write('x')", cwd=ws, writable_paths=(ws,))
    assert escape.returncode != 0 and "Read-only file system" in escape.stderr and not os.path.exists(f"{open_dir}/x")
    ok = _run(sandbox, "open('inside.txt', 'w').write('ok'); print(open('inside.txt').read())", cwd=ws, writable_paths=(ws,))
    assert ok.returncode == 0 and ok.stdout.strip() == "ok"
    marker = f"colloid-jail-{uuid.uuid4().hex}"
    first = _run(sandbox, f"open('/tmp/{marker}', 'w').write('cache'); print('wrote')", cwd=ws, writable_paths=(ws,))
    second = _run(sandbox, f"import os; print(os.path.exists('/tmp/{marker}'))", cwd=ws, writable_paths=(ws,))
    assert first.stdout.strip() == "wrote" and second.stdout.strip() == "False" and not os.path.exists(f"/tmp/{marker}")


@requires_integration
def test_cpu_accounting_counts_whole_tree(sandbox):
    # CPU burned in a child process is attributed to the cgroup (anti-reward-hacking).
    proc = sandbox.spawn(__import__("colloid.ports", fromlist=["SandboxSpec"]).SandboxSpec(
        argv=(PY, "-c", "import os, time\nif os.fork()==0:\n    t=time.time()+1.5\n    while time.time()<t: pass\nelse:\n    os.wait()"),
        cwd="/opt/colloid/state/sbxtest", env={}, wall_seconds=10, pids_limit=16))
    proc.wait(timeout=10)
    cpu_ns = proc.cpu_usage_ns()
    proc.kill()
    assert cpu_ns > 1e9  # >1s of CPU, which lived in the child


@requires_integration
def test_oom_flag_read_is_safe_against_concurrent_teardown(sandbox, monkeypatch):
    """Regression: on a wall-clock kill the timer thread removes the tree's cgroups while the
    caller, woken by the death, reads memory.oom_control; mid-teardown that read fails with
    ENODEV and crashed the evaluation (seen once in the suite). The interleaving is forced here:
    the cgroups are torn down first and their control files fail with ENODEV, and the
    process-level read must answer from the snapshot taken before removal."""
    import errno
    from pathlib import Path

    from colloid.ports import SandboxSpec

    os.makedirs("/opt/colloid/state/sbxtest", exist_ok=True)
    proc = sandbox.spawn(SandboxSpec(argv=(PY, "-c", "import time; time.sleep(30)"), cwd="/opt/colloid/state/sbxtest", env={}, wall_seconds=60))
    proc.kill()
    real = Path.read_text

    def dying(self, *a, **kw):
        if proc.cgroup in str(self):
            raise OSError(errno.ENODEV, "No such device")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", dying)
    assert proc.oom_killed() is False
    monkeypatch.setattr(Path, "read_text", real)
    r = _run(sandbox, "while True: pass", wall=0.3)  # and the real path end to end
    assert r.timed_out and r.killed_reason == "wall-clock limit"


@requires_integration
def test_cgroup_reads_tolerate_teardown(monkeypatch):
    import errno
    from pathlib import Path

    from colloid.adapters.sandbox.linux import CgroupSet

    cg = CgroupSet("teardown-test")
    real = Path.read_text

    def dying(self, *a, **kw):
        if self.name in ("memory.oom_control", "cgroup.procs") and cg.name in str(self):
            raise OSError(errno.ENODEV, "No such device")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", dying)
    assert cg.oom_killed() is False and cg.pids() == []
    monkeypatch.setattr(Path, "read_text", real)
    cg.remove()
    assert cg.oom_killed() is False and cg.pids() == []  # removed: ENOENT
