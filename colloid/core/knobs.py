"""Typed knob specifications: validation, sampling and perturbation.

Knob genes give Colloid *breadth across every layer from day one* (blueprint A2 and the
roadmap rationale): a sysctl, a glibc malloc tunable, a Postgres GUC, a compiler flag and a
uvicorn option are all just typed values in a declared safe range. They are cheap to try,
trivially reversible and risk class A.

A :class:`KnobSpec` declares:

* ``type``     - ``int`` | ``float`` | ``enum`` | ``bool``
* ``low/high`` - inclusive safe range for numeric knobs, with ``scale`` ``linear`` or ``log``
                 (log scale for things like memory sizes, where 4MB→8MB matters as much as
                 1GB→2GB)
* ``choices``  - allowed values for ``enum``
* ``default``  - the baseline value (the gene-free program uses it)
* ``requires`` - conditions on other knobs, e.g. a jemalloc tuning knob only makes sense
                 when ``alloc.impl == jemalloc``. Genes at knobs whose requirement is unmet
                 are *inert*; operators avoid producing them and L0 rejects them.
* ``mechanism``/``key`` - how the target adapter applies it (opaque to the core).

Sampling and perturbation operate in a normalised ``[0, 1]`` coordinate so the same
Gaussian step size means "the same relative move" for every numeric knob.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

KnobType = Literal["int", "float", "enum", "bool"]


class KnobSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    layer: str
    type: KnobType
    default: Any
    mechanism: str
    key: str
    description: str = ""
    low: float | None = None
    high: float | None = None
    scale: Literal["linear", "log"] = "linear"
    choices: tuple[Any, ...] = ()
    unit: str = ""
    requires: dict[str, tuple[Any, ...]] = Field(default_factory=dict)
    mutability: Literal["allowed", "review-only", "frozen"] = "allowed"
    risk_class: Literal["A", "B", "C", "D"] = "A"
    resources: tuple[str, ...] = ()  # e.g. ("memory", "cpu") - Atlas epistasis priors
    affects: tuple[str, ...] = ()  # symbol paths of units this knob configures
    extra: dict[str, Any] = Field(default_factory=dict)  # mechanism-specific data (e.g. index DDL)

    @model_validator(mode="after")
    def _check(self) -> KnobSpec:
        if self.type in ("int", "float"):
            if self.low is None or self.high is None or self.low > self.high:
                raise ValueError(f"knob {self.name}: numeric knobs need low <= high")
            if self.scale == "log" and self.low <= 0:
                raise ValueError(f"knob {self.name}: log scale needs low > 0")
        if self.type == "enum" and not self.choices:
            raise ValueError(f"knob {self.name}: enum knobs need choices")
        problem = validate_value(self, self.default)
        if problem:
            raise ValueError(f"knob {self.name}: default invalid: {problem}")
        return self


def validate_value(spec: KnobSpec, value: Any) -> str | None:
    """Return why ``value`` is not acceptable for ``spec``, or ``None`` if it is."""
    if spec.type == "bool":
        return None if isinstance(value, bool) else f"expected bool, got {value!r}"
    if spec.type == "enum":
        return None if value in spec.choices else f"{value!r} not in {list(spec.choices)}"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"expected number, got {value!r}"
    if spec.type == "int" and int(value) != value:
        return f"expected integer, got {value!r}"
    if not math.isfinite(float(value)):
        return "non-finite value"
    assert spec.low is not None and spec.high is not None
    if not (spec.low <= value <= spec.high):
        return f"{value!r} outside safe range [{spec.low}, {spec.high}]"
    return None


def requirement_met(spec: KnobSpec, config: Mapping[str, Any]) -> bool:
    """True if every ``requires`` condition holds in ``config`` (name → effective value)."""
    return all(config.get(other) in allowed for other, allowed in spec.requires.items())


# ------------------------------------------------------------------ normalised coordinates


def to_unit(spec: KnobSpec, value: Any) -> float:
    """Map a numeric value into [0, 1] (respecting log scale)."""
    assert spec.low is not None and spec.high is not None
    lo, hi, v = float(spec.low), float(spec.high), float(value)
    if hi == lo:
        return 0.0
    if spec.scale == "log":
        return (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo))
    return (v - lo) / (hi - lo)


def from_unit(spec: KnobSpec, u: float) -> Any:
    assert spec.low is not None and spec.high is not None
    u = min(1.0, max(0.0, u))
    lo, hi = float(spec.low), float(spec.high)
    if spec.scale == "log":
        v = math.exp(math.log(lo) + u * (math.log(hi) - math.log(lo)))
    else:
        v = lo + u * (hi - lo)
    if spec.type == "int":
        return int(min(hi, max(lo, round(v))))
    return float(min(hi, max(lo, v)))


def sample_value(spec: KnobSpec, rng: random.Random) -> Any:
    """Draw a uniformly random value (uniform in normalised space)."""
    if spec.type == "bool":
        return rng.random() < 0.5
    if spec.type == "enum":
        return rng.choice(list(spec.choices))
    return from_unit(spec, rng.random())


def perturb_value(spec: KnobSpec, current: Any, rng: random.Random, sigma: float = 0.15) -> Any:
    """A local move from ``current``: Gaussian step in normalised space for numbers, a
    different choice for enums/bools. Guaranteed to differ from ``current`` when the knob
    has more than one possible value."""
    if spec.type == "bool":
        return not bool(current)
    if spec.type == "enum":
        others = [c for c in spec.choices if c != current]
        return rng.choice(others) if others else current
    u = to_unit(spec, current)
    for _ in range(16):
        candidate = from_unit(spec, u + rng.gauss(0.0, sigma))
        if candidate != current:
            return candidate
    # Range too narrow for the step: move to a neighbouring representable value.
    step = 1 if spec.type == "int" else (float(spec.high or 0) - float(spec.low or 0)) / 100.0
    up = current + step
    return up if validate_value(spec, up) is None else current - step


def knob_features(spec: KnobSpec, value: Any) -> list[float]:
    """Numeric features of a knob value for surrogate models (one-hot for enums)."""
    if spec.type == "bool":
        return [1.0 if value else 0.0]
    if spec.type == "enum":
        return [1.0 if value == c else 0.0 for c in spec.choices]
    return [to_unit(spec, value)]
