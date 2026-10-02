"""Target registry: every system Colloid can optimise, by name.

A target is one implementation of a system behind the TargetSystem port. The StackZero shop
contract has one implementation per language; they share the database, the kernel, the
workloads, the oracles and the benchmark protocol, so the *same judge* evaluates all of them
(that is what makes their results comparable, and their database-layer knowledge
transferable).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from colloid.adapters.target.stackzero.adapter import StackZeroTarget

TARGETS: dict[str, tuple[str, str]] = {
    "stackzero": ("colloid.adapters.target.stackzero.adapter", "StackZeroTarget"),
    "stackzero-go": ("colloid.adapters.target.stackzero_go.adapter", "StackZeroGoTarget"),
}
DEFAULT_TARGET = "stackzero"


def target_class(name: str) -> type[StackZeroTarget]:
    if name not in TARGETS:
        raise SystemExit(f"unknown target {name!r}; known: {', '.join(sorted(TARGETS))}")
    module, cls = TARGETS[name]
    found: type[StackZeroTarget] = getattr(importlib.import_module(module), cls)
    return found


def open_target(name: str = DEFAULT_TARGET, **kwargs: Any) -> StackZeroTarget:
    return target_class(name)(**kwargs)


def run_target(store: Any) -> str:
    """The target a stored run was made against (runs predating the registry: StackZero)."""
    cfg = store.kv_get("config") or {}
    return str(cfg.get("target") or DEFAULT_TARGET)
