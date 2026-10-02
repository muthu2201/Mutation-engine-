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
def test_cpu_accounting_counts_whole_tree(sandbox):
    # CPU burned in a child process is attributed to the cgroup (anti-reward-hacking).
    proc = sandbox.spawn(__import__("colloid.ports", fromlist=["SandboxSpec"]).SandboxSpec(
        argv=(PY, "-c", "import os, time\nif os.fork()==0:\n    t=time.time()+1.5\n    while time.time()<t: pass\nelse:\n    os.wait()"),
        cwd="/opt/colloid/state/sbxtest", env={}, wall_seconds=10, pids_limit=16))
    proc.wait(timeout=10)
    cpu_ns = proc.cpu_usage_ns()
    proc.kill()
    assert cpu_ns > 1e9  # >1s of CPU, which lived in the child
