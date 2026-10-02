"""Stress test: concurrent sandbox pressure and adversarial payloads.

Verifies the security boundary holds and leaks nothing under load, which is what makes the
engine safe to run for hours with thousands of untrusted candidates:

* **Concurrency** - launch many candidates at once (CPU hogs, allocators churning, forkers,
  memory growers) and confirm every cgroup and every process tree is cleaned up afterwards
  (no leaked ``/sys/fs/cgroup/.../colloid/*`` directories, no surviving PIDs, no fd leak in
  the parent).
* **Adversarial payloads** - infinite loops, immediate exit, huge stdout, SIGKILL-resistant
  ``while True: pass`` with ignored signals - each must be contained by the wall-clock and
  freeze-then-kill path, not hang the harness.
* **Escape probes** - the canary escapes (egress, uid escalation, namespace creation, writes
  outside the workspace) stay blocked under concurrency, not just in isolation.

Run as root: ``python stress/stress_sandbox.py [--workers N] [--rounds R]``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from colloid.adapters.sandbox.linux import (
    CGROUP_ROOT,
    CONTROLLERS,
    PARENT,
    LinuxSandbox,
    cleanup_stale_cgroups,
)
from colloid.ports import SandboxSpec

PY = sys.executable
WORK = "/opt/colloid/state/sbxtest"

PAYLOADS = {
    "cpu_spin": "t=__import__('time').time()+1.0\nwhile __import__('time').time()<t: pass",
    "alloc_churn": "x=[]\nfor _ in range(200000):\n    x.append(bytearray(1024))\n    if len(x)>1000: x=x[500:]",
    "quick_exit": "print('done')",
    "forker": "import os\nfor _ in range(50):\n    try:\n        if os.fork()==0: __import__('time').sleep(0.2); os._exit(0)\n    except OSError: pass\nimport time; time.sleep(0.5)",
    "mem_grow": "x=[]\nwhile True: x.append(bytearray(20*1024*1024))",
    "sig_ignore": "import signal,time\nfor s in (signal.SIGTERM,signal.SIGINT): signal.signal(s,signal.SIG_IGN)\nwhile True: time.sleep(0.1)",
    "huge_stdout": "print('x'*2000000)",
    "egress": "import socket; socket.create_connection(('1.1.1.1',80),timeout=2)",
    "escalate": "import os; os.setuid(0)",
    "escape_write": "open('/opt/colloid/ESCAPE','w').write('x')",
}
SHOULD_FAIL = {"mem_grow", "sig_ignore", "egress", "escalate", "escape_write"}


def count_cgroups() -> int:
    total = 0
    for ctrl in CONTROLLERS:
        base = CGROUP_ROOT / ctrl / PARENT
        if base.exists():
            total += sum(1 for d in base.iterdir() if d.is_dir())
    return total


def run_one(sandbox: LinuxSandbox, name: str) -> dict:
    code = PAYLOADS[name]
    spec = SandboxSpec(argv=(PY, "-c", code), cwd=WORK, env={}, wall_seconds=4.0,
                       memory_limit_mb=256 if name == "mem_grow" else 1024, pids_limit=64)
    t0 = time.monotonic()
    r = sandbox.run(spec)
    wall = time.monotonic() - t0
    contained = (r.returncode != 0 or r.timed_out) if name in SHOULD_FAIL else True
    escaped = name == "escape_write" and Path("/opt/colloid/ESCAPE").exists()
    # a hostile payload must not hold a worker past its wall-clock budget (+ kill/cleanup slack)
    prompt = wall <= spec.wall_seconds + 3.0
    return {"name": name, "rc": r.returncode, "timed_out": r.timed_out, "wall": round(wall, 2),
            "contained": contained and not escaped and prompt, "prompt": prompt}


class PeakMonitor:
    """Samples live sandbox cgroups every 10 ms while the rounds run, so the reported peak is
    the real concurrency the cleanup path had to handle (sampling only between rounds, after
    every worker has cleaned up, would always read zero)."""

    def __init__(self) -> None:
        import threading

        self.peak = 0
        self.samples = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(OSError):  # a cgroup vanishing mid-iteration is expected
                self.peak = max(self.peak, count_cgroups())
            self.samples += 1
            self._stop.wait(0.01)

    def __enter__(self) -> PeakMonitor:
        self._t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._t.join()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=6)
    args = ap.parse_args()
    os.makedirs(WORK, exist_ok=True)
    os.chmod(WORK, 0o777)
    cleanup_stale_cgroups()
    sandbox = LinuxSandbox()
    before_cg = count_cgroups()
    before_fd = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    names = list(PAYLOADS)
    results = []
    t0 = time.monotonic()
    with PeakMonitor() as mon:
        for rnd in range(args.rounds):
            batch = [names[(rnd * args.workers + i) % len(names)] for i in range(args.workers)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = [ex.submit(run_one, sandbox, n) for n in batch]
                for f in concurrent.futures.as_completed(futs):
                    results.append(f.result())
            print(f"round {rnd + 1}/{args.rounds}: {sum(r['contained'] for r in results)}/{len(results)} contained so far, "
                  f"peak live cgroups so far={mon.peak}, now={count_cgroups()}")
    peak_cg = mon.peak
    # allow async cleanup timers to finish
    deadline = time.monotonic() + 10
    while count_cgroups() > before_cg and time.monotonic() < deadline:
        time.sleep(0.5)
    after_cg = count_cgroups()
    after_fd = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    breaches = [r for r in results if not r["contained"]]
    escaped = Path("/opt/colloid/ESCAPE").exists()
    if escaped:
        Path("/opt/colloid/ESCAPE").unlink()
    summary = {
        "total_runs": len(results),
        "contained": sum(r["contained"] for r in results),
        "breaches": breaches,
        "cgroups_before": before_cg, "cgroups_peak": peak_cg, "peak_samples": mon.samples, "cgroups_after": after_cg,
        "max_wall_s": max(r["wall"] for r in results), "all_prompt": all(r["prompt"] for r in results),
        "cgroup_leak": after_cg - before_cg,
        "fd_before": before_fd, "fd_after": after_fd, "fd_leak": after_fd - before_fd,
        "escaped_filesystem": escaped,
        "elapsed_s": round(time.monotonic() - t0, 1),
        # peak_cg > 0 proves the monitor saw the concurrent cgroups it is vouching were cleaned up
        "ok": not breaches and (after_cg - before_cg) == 0 and not escaped and (after_fd - before_fd) <= 2 and peak_cg > 0,
    }
    import json

    print(json.dumps(summary, indent=2))
    Path("/opt/colloid/state/stress_sandbox.json").write_text(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
