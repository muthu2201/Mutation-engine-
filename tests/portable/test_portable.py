"""Portable platform layer: these run on Linux, macOS and Windows CI. On Linux the portable
paths are forced (COLLOID_FORCE_PORTABLE=1) so the fallbacks are exercised even here, and the
CPU-accounting fallback is checked against cgroup ground truth (integration, root)."""

import itertools
import os
import sys
import time

import pytest

from colloid.adapters import platform as plat
from colloid.adapters.sandbox.portable import ProcessSandbox, UnsafeIsolation
from colloid.ports import SandboxSpec
from tests.conftest import requires_integration

PY = sys.executable


@pytest.fixture
def portable(monkeypatch):
    monkeypatch.setenv("COLLOID_FORCE_PORTABLE", "1")
    caps = plat.capabilities(refresh=True)
    yield caps
    monkeypatch.delenv("COLLOID_FORCE_PORTABLE")
    plat.capabilities(refresh=True)


@pytest.fixture
def sandbox(tmp_path):
    return ProcessSandbox(tmp_path / "logs")


def spec(tmp_path, code, **kw):
    kw.setdefault("run_as_sandbox_user", False)  # trusted code: what isolation C is allowed to run
    return SandboxSpec(argv=(PY, "-c", code), cwd=str(tmp_path), env={}, **kw)


def test_capabilities_are_coherent(portable):
    assert portable.os in ("linux", "darwin", "windows")
    assert not portable.linux_sandbox and portable.sandbox_backend() == "process"  # forced portable
    assert portable.psutil and plat.memory_metric() in ("pss", "uss")
    assert "capabilities" in plat.fingerprint_extras()


def test_untrusted_code_is_refused_at_isolation_c(tmp_path, sandbox):
    with pytest.raises(UnsafeIsolation, match="isolation level C"):
        sandbox.run(spec(tmp_path, "print('hi')", run_as_sandbox_user=True))
    opted_in = ProcessSandbox(tmp_path / "l2", allow_untrusted=True)
    assert opted_in.run(spec(tmp_path, "print('hi')", run_as_sandbox_user=True)).stdout.strip() == "hi"


def test_run_captures_output_and_exit_code(tmp_path, sandbox):
    r = sandbox.run(spec(tmp_path, "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"))
    assert (r.returncode, r.stdout.strip(), r.stderr.strip(), r.timed_out) == (3, "out", "err", False)


def test_wall_clock_kills_the_whole_tree(tmp_path, sandbox):
    code = ("import subprocess, sys, time\n"
            "kids = [subprocess.Popen([sys.executable, '-c', 'import time\\nwhile True: time.sleep(0.05)']) for _ in range(3)]\n"
            "print(' '.join(str(k.pid) for k in kids), flush=True)\n"
            "while True: time.sleep(0.05)\n")
    t0 = time.monotonic()
    r = sandbox.run(spec(tmp_path, code, wall_seconds=1.5))
    assert r.timed_out and r.killed_reason == "wall-clock limit" and time.monotonic() - t0 < 10
    assert r.returncode != 0  # regression: reaping the root via psutil made a killed process report 0
    kids = [int(x) for x in r.stdout.split()]
    time.sleep(0.2)
    assert len(kids) == 3 and not any(plat.psutil.pid_exists(k) and plat.psutil.Process(k).status() != "zombie" for k in kids)


def test_memory_watchdog_kills_and_reports_oom(tmp_path, sandbox):
    r = sandbox.run(spec(tmp_path, "x = []\nwhile True: x.append(bytearray(8 * 1024 * 1024))", memory_limit_mb=128, wall_seconds=20))
    assert r.killed_reason == "memory limit (OOM)" and r.returncode != 0


def test_pids_watchdog(tmp_path, sandbox):
    code = ("import subprocess, sys, time\n"
            "ps = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']) for _ in range(12)]\n"
            "time.sleep(30)\n")
    r = sandbox.run(spec(tmp_path, code, pids_limit=5, wall_seconds=20))
    assert r.killed_reason == "pids limit"


def test_cpu_counter_tracks_short_lived_children(tmp_path, portable):
    """Five children each burn ~0.25 s of CPU and exit. The counter must keep their CPU after
    they are gone (at most one sampling interval lost per child)."""
    import subprocess

    burn = "import time\nt = time.process_time()\nwhile time.process_time() - t < 0.25: pass\n"
    parent = subprocess.Popen([PY, "-c", f"import subprocess, sys\nfor _ in range(5): subprocess.run([sys.executable, '-c', {burn!r}])\n"])
    counter = plat.CpuCounterFile(parent.pid, tmp_path / "cpu_ns", interval=0.01).start()
    values = []
    while parent.poll() is None:
        values.append(counter.value())
        time.sleep(0.05)
    final = counter.stop()
    assert all(b >= a for a, b in itertools.pairwise(values))  # monotonic
    assert 1.0e9 <= final <= 2.5e9, final  # 5 x 0.25 s of burning (+ interpreter start-up), none lost


def test_pss_and_pinning_fallbacks(portable):
    assert plat.pss_mb([os.getpid()]) > 1.0
    pinned = plat.pin_cpus({0})
    assert pinned == (portable.cpu_affinity != "none")


@requires_integration
def test_portable_cpu_accounting_matches_cgroup_ground_truth(tmp_path):
    """On Linux, run a 3-process CPU burner inside the Linux sandbox (cgroup cpuacct = ground
    truth) while the portable counter samples the same tree. They must agree within 5%."""
    from colloid.adapters.sandbox.linux import LinuxSandbox

    code = ("import multiprocessing as mp, time\n"
            "def burn():\n    t = time.process_time()\n    while time.process_time() - t < 0.6: pass\n"
            "if __name__ == '__main__':\n    ps = [mp.Process(target=burn) for _ in range(3)]\n"
            "    [p.start() for p in ps]; [p.join() for p in ps]\n")
    work = tmp_path / "w"
    work.mkdir()
    os.chmod(work, 0o777)
    (work / "burn.py").write_text(code)
    lsb = LinuxSandbox()
    proc = lsb.spawn(SandboxSpec(argv=(PY, str(work / "burn.py")), cwd=str(work), env={}, wall_seconds=30, writable_paths=(str(work),)))
    counter = plat.CpuCounterFile(proc.pid, tmp_path / "cpu_ns", interval=0.01).start()
    while proc.alive():
        cgroup_ns = proc.cpu_usage_ns()  # read while the cgroup exists
        time.sleep(0.01)
    portable_ns = counter.stop()
    proc.kill()
    assert cgroup_ns > 1.5e9
    assert abs(portable_ns - cgroup_ns) / cgroup_ns < 0.05, (portable_ns, cgroup_ns)
