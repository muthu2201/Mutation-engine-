"""Knob catalogue and genome → launch configuration for the Go implementation.

The catalogue is the StackZero OS + database knobs (shared loci: the same kernel, Postgres
cluster and schema as the Python implementation) plus the Go runtime and build knobs of
``knobs_go.yaml``. :func:`launch_config` produces the same keys the evaluator and the
shared-state reconciliation use for every StackZero implementation (``os``, ``svc_cpus``,
``db_cpus``, ``sched``, ``nice``, ``env``, ``pg_session``, ``pg_postmaster``, ``indexes``) plus
``go_build`` (the ``go build`` environment and flags).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from colloid.adapters.target.stackzero.catalog import CATALOG as SHARED_CATALOG
from colloid.adapters.target.stackzero.catalog import CPU_LAYOUTS, _envval, _guc, load_knobs
from colloid.core.knobs import KnobSpec

GO_CATALOG = Path(__file__).with_name("knobs_go.yaml")
SHARED_LAYERS = ("os", "db")
# The shared knobs name the Python implementation's runtime components in ``affects``; on this
# implementation the same roles are played by the Go runtime and its HTTP server.
COMPONENT_MAP = {"component:runtime.python": "component:runtime.go", "component:runtime.uvicorn": "component:runtime.go"}


def load_go_knobs(observe_system: bool = True) -> list[KnobSpec]:
    shared = [k for k in load_knobs(SHARED_CATALOG, observe_system=observe_system) if k.layer in SHARED_LAYERS]
    out = []
    for k in shared:
        affects = tuple(dict.fromkeys(COMPONENT_MAP.get(a, a) for a in k.affects))
        out.append(k.model_copy(update={"affects": affects}))
    data = yaml.safe_load(GO_CATALOG.read_text())
    for raw in data["knobs"]:
        raw = dict(raw)
        for key in ("choices", "resources", "affects"):
            if key in raw:
                raw[key] = tuple(raw[key])
        if "requires" in raw:
            raw["requires"] = {k: tuple(v) for k, v in raw["requires"].items()}
        out.append(KnobSpec.model_validate(raw))
    return out


def launch_config(specs: Sequence[KnobSpec], values: Mapping[str, Any]) -> dict[str, Any]:
    by_name = {s.name: s for s in specs}
    cfg = {s.name: s.default for s in specs}
    cfg.update(values)
    set_knobs = {k: v for k, v in values.items() if k in by_name and v != by_name[k].default}
    os_cfg: dict[str, dict[str, Any]] = {"sysctl": {}, "thp": {}}
    env: dict[str, str] = {}
    pg_session: dict[str, str] = {}
    pg_postmaster: dict[str, str] = {}
    indexes: dict[str, str] = {}
    for name, value in set_knobs.items():
        spec = by_name[name]
        if spec.mechanism == "sysctl":
            os_cfg["sysctl"][spec.key] = value
        elif spec.mechanism == "thp":
            os_cfg["thp"][spec.key] = value
        elif spec.mechanism == "guc_session":
            pg_session[spec.key] = _guc(value, spec.unit)
        elif spec.mechanism == "guc_postmaster":
            pg_postmaster[spec.key] = _guc(value, spec.unit)
        elif spec.mechanism == "env" and not (spec.key == "GOMAXPROCS" and value == "auto") and not (spec.key == "GOMEMLIMIT" and value == "off"):
            env[spec.key] = _envval(value)
    for name, spec in by_name.items():
        if spec.mechanism == "index" and cfg[name]:
            indexes[spec.key] = str(spec.extra["ddl"])
    svc_cpus, db_cpus = CPU_LAYOUTS[cfg["os.cpu_layout"]]
    flags: list[str] = []
    if cfg["go.no_bounds_checks"]:
        flags.append("-gcflags=-B")
    return {
        "os": os_cfg,
        "svc_cpus": svc_cpus,
        "db_cpus": db_cpus,
        "sched": cfg["os.service_sched"],
        "nice": int(cfg["os.service_nice"]),
        "env": env,
        "pg_session": pg_session,
        "pg_postmaster": pg_postmaster,
        "indexes": indexes,
        "go_build": {"GOAMD64": str(cfg["go.amd64"]), "flags": flags},
    }
