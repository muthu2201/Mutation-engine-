"""Multi-objective fitness.

A program's fitness is a vector of **log-ratios versus the baseline**, one per objective,
each with a confidence interval (blueprint A6). Hard gates (build, correctness oracles,
sandbox policy, licence policy) are never traded off: a program that fails any gate has no
fitness at all.

The scalar used for final ranking is "$ per unit of work at a fixed SLO": the cost
objective (CPU-seconds and memory-GB-seconds priced by the cost model, per request) with a
large penalty when the latency SLO is violated. The Pareto set is kept for humans.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from colloid.core.models import Evaluation, ObjectiveEstimate, ObjectiveSpec


@dataclass(frozen=True)
class Fitness:
    """Objective log-ratios versus baseline (higher is better) with CIs."""

    value: Mapping[str, float]
    lo: Mapping[str, float]
    hi: Mapping[str, float]
    p: Mapping[str, float] = field(default_factory=dict)

    @staticmethod
    def from_estimates(estimates: Iterable[ObjectiveEstimate], objectives: Iterable[str]) -> Fitness | None:
        wanted = list(objectives)
        by_name = {e.objective: e for e in estimates if e.reference == "baseline"}
        if any(name not in by_name for name in wanted):
            return None
        value, lo, hi, p = {}, {}, {}, {}
        for name in wanted:
            est = by_name[name]
            if not math.isfinite(est.log_ratio):
                return None
            value[name] = est.log_ratio
            lo[name] = est.ci_lo if math.isfinite(est.ci_lo) else est.log_ratio - 10.0
            hi[name] = est.ci_hi if math.isfinite(est.ci_hi) else est.log_ratio + 10.0
            p[name] = est.p_value
        return Fitness(value, lo, hi, p)

    @staticmethod
    def from_evaluation(ev: Evaluation, objectives: Iterable[str]) -> Fitness | None:
        return Fitness.from_estimates(ev.objectives, objectives)

    @staticmethod
    def zero(objectives: Iterable[str]) -> Fitness:
        names = list(objectives)
        return Fitness({n: 0.0 for n in names}, {n: 0.0 for n in names}, {n: 0.0 for n in names}, {n: 1.0 for n in names})

    def names(self) -> list[str]:
        return list(self.value.keys())

    def vector(self, names: Iterable[str] | None = None) -> list[float]:
        return [self.value[n] for n in (names or self.names())]


def gain_percent(log_ratio: float) -> float:
    """Convert a log-ratio (positive = better) into "percent less of the metric".
    0.0953 → 9.1% less; -0.0953 → 10% more."""
    return (1.0 - math.exp(-log_ratio)) * 100.0


def scalar_score(
    fitness: Fitness,
    *,
    cost_objective: str,
    slo_objective: str | None,
    slo_max_regression: float = 0.10,
    gene_count: int = 0,
    parsimony: float = 0.002,
) -> float:
    """Final-ranking scalar: cost log-ratio, minus a steep penalty for SLO violation and a
    tiny parsimony term so that, all else equal, smaller genomes rank higher.

    The SLO is "latency may not regress by more than ``slo_max_regression``" judged on the
    *pessimistic* end of the interval (the CI lower bound), i.e. we only claim SLO
    compliance when we are confident about it.
    """
    score = fitness.value.get(cost_objective, 0.0)
    if slo_objective is not None and slo_objective in fitness.lo:
        limit = math.log(1.0 / (1.0 + slo_max_regression))  # e.g. -0.0953 for 10%
        worst = fitness.lo[slo_objective]
        if worst < limit:
            score -= 1.0 + (limit - worst) * 10.0
    return score - parsimony * gene_count


def objective_names(specs: Iterable[ObjectiveSpec]) -> list[str]:
    return [s.name for s in specs]
