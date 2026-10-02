"""An instance's container: the repository at ``base_commit`` in its test environment.

SWE-bench images keep the repository at ``/testbed`` and its environment in conda's
``testbed``. The container runs with the network off, CPUs 1–3 and a memory cap. Each command
is wrapped in ``timeout``, so a hung test is killed inside the container, not just abandoned.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

WORKDIR = "/testbed"
ENV = "source /opt/miniconda3/bin/activate && conda activate testbed && "


@dataclass(frozen=True)
class Exec:
    code: int
    output: str
    seconds: float
    timed_out: bool


def docker(*args: str, input: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], input=input, capture_output=True, text=True, timeout=timeout, check=False,
                          errors="replace")


def pull(image: str, timeout: float = 3600) -> str:
    """Pull ``image`` and return its repository digest."""
    res = docker("pull", "-q", image, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"docker pull {image} failed: {res.stderr.strip()[-500:]}")
    dig = docker("image", "inspect", "--format", "{{index .RepoDigests 0}}", image, timeout=60)
    return dig.stdout.strip()


def remove_image(image: str) -> None:
    docker("rmi", "-f", image, timeout=300)
    docker("image", "prune", "-f", timeout=300)


class Container:
    def __init__(self, image: str, name: str, *, cpuset: str = "1-3", memory: str = "8g") -> None:
        self.image, self.name, self.cpuset, self.memory = image, name, cpuset, memory

    def start(self) -> None:
        docker("rm", "-f", self.name, timeout=60)
        res = docker("run", "-d", "--name", self.name, "--network", "none", "--cpuset-cpus", self.cpuset, "--memory", self.memory,
                     "--pids-limit", "4096", self.image, "tail", "-f", "/dev/null", timeout=300)
        if res.returncode != 0:
            raise RuntimeError(f"docker run {self.image} failed: {res.stderr.strip()[-500:]}")

    def stop(self) -> None:
        docker("rm", "-f", self.name, timeout=120)

    def run(self, script: str, *, timeout: float = 300, env: bool = True) -> Exec:
        cmd = f"cd {WORKDIR} && " + (ENV if env else "") + script
        t0 = time.monotonic()
        try:
            res = docker("exec", self.name, "timeout", "-k", "10", str(int(timeout)), "bash", "-c", cmd, timeout=timeout + 60)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout if isinstance(exc.stdout, str) else ""
            return Exec(124, out, time.monotonic() - t0, True)
        return Exec(res.returncode, res.stdout + res.stderr, time.monotonic() - t0, res.returncode in (124, 137))

    def write(self, path: str, text: str) -> None:
        res = docker("exec", "-i", self.name, "bash", "-c", f"cat > '{path}'", input=text, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(f"writing {path} failed: {res.stderr.strip()[-300:]}")

    def reset(self) -> None:
        self.run("git checkout -q -- . && git clean -fdq", env=False, timeout=120)

    def diff(self) -> str:
        return self.run("git -c core.fileMode=false diff", env=False, timeout=60).output

    def export(self, dest: Path) -> None:
        """Copy the repository (without ``.git``) to ``dest`` for localisation."""
        dest.mkdir(parents=True, exist_ok=True)
        src = subprocess.Popen(["docker", "exec", self.name, "tar", "-C", WORKDIR, "--exclude=.git", "-cf", "-", "."], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-xf", "-", "-C", str(dest)], stdin=src.stdout, check=True, timeout=600)
        src.wait(timeout=60)
