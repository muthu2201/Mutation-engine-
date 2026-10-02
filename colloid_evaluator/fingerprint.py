"""Environment fingerprint (blueprint C, D3).

Every Evaluation records the conditions it was measured under: kernel, CPU model and
microcode, frequency governor, SMT, turbo, NUMA layout, hypervisor, compilers, Python and
Postgres versions, a hash of the installed Python packages, hashes of the load generator
and sandbox helper binaries, the repository commit and the port API versions. A result
is reproducible from ``(baseline commit, gene payloads, adapter versions, fingerprint)``;
results from different fingerprints are never compared with each other.
"""

from __future__ import annotations

import functools
import hashlib
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from colloid.ports import PORT_APIS


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _cmd(*argv: str) -> str | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _file_hash(path: str) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


@functools.lru_cache(maxsize=1)
def fingerprint(repo: str = str(Path(__file__).resolve().parents[1])) -> dict[str, Any]:
    cpuinfo = _read("/proc/cpuinfo") or ""
    model = next((line.split(":", 1)[1].strip() for line in cpuinfo.splitlines() if line.startswith("model name")), None)
    microcode = next((line.split(":", 1)[1].strip() for line in cpuinfo.splitlines() if line.startswith("microcode")), None)
    flags = next((line.split(":", 1)[1] for line in cpuinfo.splitlines() if line.startswith("flags")), "")
    freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, check=False).stdout
    thp = _read("/sys/kernel/mm/transparent_hugepage/enabled")
    return {
        "kernel": platform.release(),
        "cpu_model": model,
        "cpu_count": len([line for line in cpuinfo.splitlines() if line.startswith("processor")]),
        "microcode": microcode,
        "hypervisor": "hypervisor" in flags,
        "governor": _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "smt": _read("/sys/devices/system/cpu/smt/active"),
        "no_turbo": _read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
        "numa_nodes": len(list(Path("/sys/devices/system/node").glob("node[0-9]*"))) or None,
        "thp_at_start": thp,
        "gcc": _cmd("gcc", "--version"),
        "clang": _cmd("clang", "--version"),
        "python": sys.version.split()[0],
        "postgres": _cmd("/usr/lib/postgresql/16/bin/postgres", "--version"),
        "pip_freeze_sha": hashlib.sha256(freeze.encode()).hexdigest()[:16],
        "loadgen_sha": _file_hash("/opt/colloid/bin/colloid-loadgen"),
        "sbx_exec_sha": _file_hash("/opt/colloid/bin/sbx-exec"),
        "repo_commit": _cmd("git", "-C", repo, "rev-parse", "--short", "HEAD"),
        "port_apis": dict(PORT_APIS),
    }


def comparable(a: dict[str, Any], b: dict[str, Any]) -> bool:
    keys = ("kernel", "cpu_model", "microcode", "hypervisor", "cpu_count", "gcc", "python", "postgres", "loadgen_sha")
    return all(a.get(k) == b.get(k) for k in keys)
