"""Sandbox adapters. :func:`select_sandbox` picks the strongest isolation the host offers:
the Linux sandbox (seccomp + namespaces + cgroups, isolation A/B) when running as root on
Linux, otherwise the portable process sandbox (isolation C, trusted code only); see
``colloid.adapters.platform`` for the capability matrix."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def select_sandbox(log_dir: Path | None = None) -> Any:
    from colloid.adapters.platform import capabilities

    if capabilities().linux_sandbox:
        from colloid.adapters.sandbox.linux import LinuxSandbox

        return LinuxSandbox(log_dir=log_dir) if log_dir else LinuxSandbox()
    from colloid.adapters.sandbox.portable import ProcessSandbox

    return ProcessSandbox(log_dir)
