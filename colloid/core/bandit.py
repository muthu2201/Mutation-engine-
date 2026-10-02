"""Adaptive operator selection with contextual Thompson sampling (blueprint A3, T08).

An *arm* is ``(operator, model, template)``, for example ``("llm_rewrite", "qwen2.5-coder-3b",
"sql_batching")`` or ``("knob_perturb", None, None)``. The *context* is a small tuple of
locus tags (layer, risk class). The *reward* of one proposal is the lineage gain it
produced on the island's primary objective, clipped to ``[0, clip_hi]``: a proposal that
fails a gate or does not improve earns 0, a proposal that makes the child 10% cheaper than
its parent earns ≈0.095. The bandit therefore learns "expected improvement per proposal"
for every arm in every context.

Posterior model. Rewards are treated as Gaussian with unknown mean. For each
``(context, arm)`` we keep sufficient statistics (n, Σr, Σr²). The posterior mean blends
three sources with pseudo-counts:

    prior (optimistic mean, ``prior_n`` pseudo-observations)
  + the arm's global statistics across contexts (weight ``share``)
  + the context-specific statistics

so a new context borrows strength from other contexts, and a new arm (a freshly added LLM)
starts optimistic and earns budget by measured credit - exactly how model swaps happen at
runtime (blueprint E).

Cost awareness. Arms also track their mean cost (wall-clock seconds per proposal through
the cascade). ``select`` maximises *sampled reward per unit cost* so a cheap knob operator
with small gains can beat an expensive LLM call with slightly larger gains.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

Arm = tuple[str, str | None, str | None]
Context = tuple[Hashable, ...]


@dataclass
class _Stats:
    n: float = 0.0
    s: float = 0.0
    ss: float = 0.0
    cost_n: float = 0.0
    cost_s: float = 0.0

    def add(self, r: float, cost: float | None, weight: float = 1.0) -> None:
        self.n += weight
        self.s += weight * r
        self.ss += weight * r * r
        if cost is not None and math.isfinite(cost) and cost > 0:
            self.cost_n += 1
            self.cost_s += cost


@dataclass
class ThompsonBandit:
    prior_mean: float = 0.03
    prior_n: float = 2.0
    prior_sd: float = 0.05
    share: float = 0.3
    clip_hi: float = 0.5
    default_cost: float = 10.0
    arms: list[Arm] = field(default_factory=list)
    _ctx: dict[tuple[Context, Arm], _Stats] = field(default_factory=lambda: defaultdict(_Stats))
    _global: dict[Arm, _Stats] = field(default_factory=lambda: defaultdict(_Stats))

    def add_arm(self, arm: Arm) -> None:
        if arm not in self.arms:
            self.arms.append(arm)

    @staticmethod
    def reward_from_gain(log_ratio_vs_parent: float | None, passed: bool, clip_hi: float = 0.5) -> float:
        if not passed or log_ratio_vs_parent is None or not math.isfinite(log_ratio_vs_parent):
            return 0.0
        return min(max(log_ratio_vs_parent, 0.0), clip_hi)

    def update(self, context: Context, arm: Arm, reward: float, cost: float | None = None) -> None:
        self.add_arm(arm)
        r = min(max(reward, 0.0), self.clip_hi)
        self._ctx[(context, arm)].add(r, cost)
        self._global[arm].add(r, cost)

    def seed(self, arm: Arm, rewards: Sequence[float], weight: float = 0.5) -> None:
        """Pseudo-observations from earlier runs (the mutation data lake): each past reward
        counts as ``weight`` of a real observation, in the arm's global statistics only, so
        evidence carried over shifts the prior and measured credit in *this* run still
        dominates after a few pulls. Seeding never adds cost observations."""
        self.add_arm(arm)
        for r in rewards:
            self._global[arm].add(min(max(r, 0.0), self.clip_hi), None, weight)

    def posterior(self, context: Context, arm: Arm) -> tuple[float, float]:
        """Posterior mean and standard deviation of the arm's mean reward in context."""
        c = self._ctx.get((context, arm), _Stats())
        g = self._global.get(arm, _Stats())
        gn = max(g.n - c.n, 0.0) * self.share
        gs = (g.s - c.s) * self.share
        gss = (g.ss - c.ss) * self.share
        n = self.prior_n + gn + c.n
        s = self.prior_mean * self.prior_n + gs + c.s
        ss = (self.prior_sd**2 + self.prior_mean**2) * self.prior_n + gss + c.ss
        mean = s / n
        var = max(ss / n - mean * mean, 1e-6)
        return mean, math.sqrt(var / n)

    def mean_cost(self, arm: Arm) -> float:
        g = self._global.get(arm)
        if g is None or g.cost_n == 0:
            return self.default_cost
        return g.cost_s / g.cost_n

    def select(self, context: Context, rng: random.Random, available: Iterable[Arm] | None = None, cost_aware: bool = True) -> Arm:
        arms = list(available) if available is not None else list(self.arms)
        if not arms:
            raise ValueError("no arms available")
        best_arm, best_val = arms[0], -math.inf
        for arm in arms:
            mean, sd = self.posterior(context, arm)
            draw = rng.gauss(mean, sd)
            val = draw / self.mean_cost(arm) if cost_aware else draw
            if val > best_val:
                best_arm, best_val = arm, val
        return best_arm

    def allocate(self, context: Context, rng: random.Random, n: int, available: Sequence[Arm] | None = None) -> list[Arm]:
        """Draw ``n`` arms independently (Thompson sampling naturally spreads the budget)."""
        return [self.select(context, rng, available) for _ in range(n)]

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "arms": [list(a) for a in self.arms],
            "ctx": [[list(k[0]), list(k[1]), vars(v)] for k, v in self._ctx.items()],
            "global": [[list(k), vars(v)] for k, v in self._global.items()],
        }

    @staticmethod
    def from_dict(data: dict[str, Any], **kwargs: Any) -> ThompsonBandit:
        b = ThompsonBandit(**kwargs)
        b.arms = [tuple(a) for a in data.get("arms", [])]
        for ctx, arm, st in data.get("ctx", []):
            b._ctx[(tuple(ctx), tuple(arm))] = _Stats(**st)
        for arm, st in data.get("global", []):
            b._global[tuple(arm)] = _Stats(**st)
        return b

    def table(self, context: Context | None = None) -> list[dict[str, Any]]:
        rows = []
        for arm in self.arms:
            g = self._global.get(arm, _Stats())
            mean, sd = self.posterior(context or (), arm)
            rows.append(
                {
                    "operator": arm[0],
                    "model": arm[1],
                    "template": arm[2],
                    "pulls": int(g.n),
                    "mean_reward": (g.s / g.n) if g.n else None,
                    "posterior_mean": mean,
                    "posterior_sd": sd,
                    "mean_cost_s": self.mean_cost(arm),
                }
            )
        return rows
