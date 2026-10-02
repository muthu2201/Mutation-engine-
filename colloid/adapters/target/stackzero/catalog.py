"""Knob catalogue loading and genome → launch-configuration translation for StackZero.

The *baseline* is the stack exactly as found: a knob at its default value means "do not
touch it" (no environment variable, no GUC override, no sysctl write). Defaults for kernel
knobs are therefore read from the running system at load time, so the baseline really is
the machine's own configuration rather than what the catalogue author assumed.

:func:`launch_config` turns a genome into everything the target needs to run it:

* ``os``            - sysctl and THP values to apply (only non-default knobs)
* ``svc_cpus`` / ``db_cpus`` / ``sched`` / ``nice`` - process placement for the service and DB
* ``env``           - service environment (allocator preload + tunables, Python, app settings)
* ``uvicorn``       - uvicorn command-line flags
* ``pg_session``    - per-connection GUCs (passed as libpq ``options``)
* ``pg_postmaster`` - GUCs that require a Postgres restart
* ``indexes``       - index name → DDL
* ``cc``            - compiler and flags for libshopnative
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from colloid.core.knobs import KnobSpec

CATALOG = Path(__file__).with_name("knobs.yaml")

ALLOCATOR_LIBS = {
    "jemalloc": "/usr/lib/x86_64-linux-gnu/libjemalloc.so.2",
    "tcmalloc": "/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4",
    "mimalloc": "/usr/lib/x86_64-linux-gnu/libmimalloc.so.2",
}
CPU_LAYOUTS = {"shared": ("1-3", "1-3"), "split_1_2": ("1", "2-3"), "split_2_1": ("1-2", "3")}
LOADGEN_CPUS = "0"
THP_ROOT = Path("/sys/kernel/mm/transparent_hugepage")


def _observed_os_default(spec: dict[str, Any]) -> Any:
    try:
        if spec["mechanism"] == "sysctl":
            raw = (Path("/proc/sys") / spec["key"]).read_text().strip()
            return int(raw)
        if spec["mechanism"] == "thp":
            text = (THP_ROOT / spec["key"]).read_text()
            return text[text.index("[") + 1 : text.index("]")]
    except (OSError, ValueError):
        return None
    return None


def load_knobs(path: Path = CATALOG, observe_system: bool = True) -> list[KnobSpec]:
    data = yaml.safe_load(path.read_text())
    out = []
    for raw in data["knobs"]:
        raw = dict(raw)
        if observe_system and raw["mechanism"] in ("sysctl", "thp"):
            seen = _observed_os_default(raw)
            if seen is not None:
                if raw["type"] == "int":
                    raw["low"] = min(raw["low"], seen)
                    raw["high"] = max(raw["high"], seen)
                elif seen not in raw.get("choices", []):
                    raw["choices"] = [*raw.get("choices", []), seen]
                raw["default"] = seen
        for key in ("choices",):
            if key in raw:
                raw[key] = tuple(raw[key])
        for key in ("resources", "affects"):
            if key in raw:
                raw[key] = tuple(raw[key])
        if "requires" in raw:
            raw["requires"] = {k: tuple(v) for k, v in raw["requires"].items()}
        out.append(KnobSpec.model_validate(raw))
    return out


def effective(specs: Sequence[KnobSpec], values: Mapping[str, Any]) -> dict[str, Any]:
    cfg = {s.name: s.default for s in specs}
    cfg.update(values)
    return cfg


def launch_config(specs: Sequence[KnobSpec], values: Mapping[str, Any]) -> dict[str, Any]:
    """Translate knob values (name → value, only genes present) into a launch configuration."""
    by_name = {s.name: s for s in specs}
    cfg = effective(specs, values)
    set_knobs = {k: v for k, v in values.items() if k in by_name and v != by_name[k].default}

    os_cfg: dict[str, dict[str, Any]] = {"sysctl": {}, "thp": {}}
    env: dict[str, str] = {}
    uvicorn: dict[str, Any] = {"workers": cfg["py.workers"], "loop": cfg["py.loop"], "http": cfg["py.http"], "access_log": cfg["py.access_log"]}
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
        elif spec.mechanism == "app":
            env[spec.key] = _envval(value)

    for name, spec in by_name.items():
        if spec.mechanism == "index" and cfg[name]:
            indexes[spec.key] = str(spec.extra["ddl"])

    # Allocator layer.
    impl = cfg["alloc.impl"]
    if impl in ALLOCATOR_LIBS:
        env["LD_PRELOAD"] = ALLOCATOR_LIBS[impl]
    tunables = [f"{by_name[n].key}={v}" for n, v in sorted(set_knobs.items()) if n.startswith("alloc.glibc_")]
    if tunables and impl == "glibc":
        env["GLIBC_TUNABLES"] = ":".join(tunables)
    jem = []
    for n, v in sorted(set_knobs.items()):
        if n.startswith("alloc.jemalloc_"):
            jem.append(f"{by_name[n].key}:{str(v).lower() if isinstance(v, bool) else v}")
    if jem and impl == "jemalloc":
        env["MALLOC_CONF"] = ",".join(jem)
    if impl == "tcmalloc" and "alloc.tcmalloc_release_rate" in set_knobs:
        env["TCMALLOC_RELEASE_RATE"] = str(set_knobs["alloc.tcmalloc_release_rate"])
    if impl == "mimalloc" and "alloc.mimalloc_purge_delay" in set_knobs:
        env["MIMALLOC_PURGE_DELAY"] = str(set_knobs["alloc.mimalloc_purge_delay"])
    if cfg["alloc.python_malloc"] != "pymalloc":
        env["PYTHONMALLOC"] = str(cfg["alloc.python_malloc"])

    svc_cpus, db_cpus = CPU_LAYOUTS[cfg["os.cpu_layout"]]
    flags = ["-" + str(cfg["cc.opt"]), "-g", "-fPIC", "-shared"]
    if cfg["cc.march"] != "default":
        flags.append(f"-march={cfg['cc.march']}")
    if cfg["cc.lto"]:
        flags.append("-flto")
    if cfg["cc.unroll"]:
        flags.append("-funroll-loops")
    if cfg["cc.no_plt"]:
        flags.append("-fno-plt")
    if not cfg["cc.semantic_interposition"]:
        flags.append("-fno-semantic-interposition")
    if cfg["cc.fast_math"]:
        flags.append("-ffast-math")
    return {
        "os": os_cfg,
        "svc_cpus": svc_cpus,
        "db_cpus": db_cpus,
        "sched": cfg["os.service_sched"],
        "nice": int(cfg["os.service_nice"]),
        "env": env,
        "uvicorn": uvicorn,
        "pg_session": pg_session,
        "pg_postmaster": pg_postmaster,
        "indexes": indexes,
        "cc": {"compiler": cfg["cc.compiler"], "flags": flags},
    }


def _guc(value: Any, unit: str) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{value:.6g}"
    return f"{value}{unit}"


def _envval(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def pg_options(session: Mapping[str, str]) -> str:
    """libpq ``options`` string for per-connection GUCs."""
    return " ".join(f"-c {k}={v}" for k, v in sorted(session.items()))
