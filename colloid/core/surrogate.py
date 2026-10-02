"""L3 surrogate ranking (blueprint A8).

Before paying for a real benchmark, a learned model predicts each candidate's gain and the
cascade keeps only the top-k per island. The surrogate is trained on the evaluation store
(every L4/L5 result is a labelled example) and is *continuously audited*:

* Every prediction is logged *before* the true result is known. When the result arrives
  the pair (prediction, truth) joins a rolling window, and the rank correlation (Spearman ρ)
  of that window is the surrogate's out-of-sample quality - a prequential evaluation that
  cannot leak training data.
* When ρ falls below ``min_spearman`` the surrogate is *distrusted* and the cascade stops
  filtering with it (everything proceeds to L4), exactly as the blueprint prescribes.

Model. Features come from a hashing vectoriser (stable when new loci appear): one presence
feature per locus, one value feature per knob (normalised to [0,1]), and one per operator.
With fewer than ``gbt_threshold`` examples the model is ridge regression (closed form,
well-behaved on tiny data); above it, gradient-boosted trees (scikit-learn's
HistGradientBoostingRegressor), which capture knob non-linearities and interactions.
"""

from __future__ import annotations

import hashlib
import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from colloid.core.stats import spearman


def _bucket(key: str, dim: int) -> int:
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=4).digest(), "little") % dim


@dataclass(frozen=True)
class GeneFeature:
    locus_id: str
    values: tuple[float, ...]  # normalised knob features or code-size features


def vectorise(genes: Sequence[GeneFeature], operator: str, dim: int = 256) -> np.ndarray:
    x = np.zeros(dim, dtype=np.float64)
    for g in genes:
        x[_bucket("p:" + g.locus_id, dim)] += 1.0
        for i, v in enumerate(g.values):
            x[_bucket(f"v:{g.locus_id}:{i}", dim)] += float(v)
    x[_bucket("op:" + operator, dim)] += 1.0
    x[-1] = float(len(genes))  # genome size
    return x


@dataclass
class Surrogate:
    dim: int = 256
    ridge_lambda: float = 1.0
    gbt_threshold: int = 80
    min_spearman: float = 0.3
    audit_window: int = 30
    min_audit: int = 8
    xs: list[np.ndarray] = field(default_factory=list)
    ys: list[float] = field(default_factory=list)
    audit: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=30))
    _model: Any = None
    _kind: str = "none"

    def add_example(self, x: np.ndarray, y: float, prediction: float | None = None) -> None:
        if not math.isfinite(y):
            return
        if prediction is not None and math.isfinite(prediction):
            self.audit.append((prediction, y))
        self.xs.append(x)
        self.ys.append(y)

    def fit(self) -> None:
        if len(self.ys) < 4:
            self._model, self._kind = None, "none"
            return
        x = np.vstack(self.xs)
        y = np.asarray(self.ys)
        if len(y) >= self.gbt_threshold:
            from sklearn.ensemble import HistGradientBoostingRegressor

            model = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=5, random_state=0)
            model.fit(x, y)
            self._model, self._kind = model, "gbt"
        else:
            mu = y.mean()
            a = x.T @ x + self.ridge_lambda * np.eye(x.shape[1])
            w = np.linalg.solve(a, x.T @ (y - mu))
            self._model, self._kind = (w, mu), "ridge"

    def predict(self, x: np.ndarray) -> float:
        if self._model is None:
            return 0.0
        if self._kind == "ridge":
            w, mu = self._model
            return float(x @ w + mu)
        return float(self._model.predict(x.reshape(1, -1))[0])

    @property
    def rho(self) -> float:
        if len(self.audit) < self.min_audit:
            return float("nan")
        preds, truths = zip(*self.audit, strict=True)
        return spearman(list(preds), list(truths))

    @property
    def trusted(self) -> bool:
        r = self.rho
        return math.isfinite(r) and r >= self.min_spearman and self._model is not None

    @property
    def kind(self) -> str:
        return self._kind

    def rank_keep(self, preds: Sequence[float], k: int) -> list[int]:
        """Indices of the top-k predictions (all indices when untrusted)."""
        idx = list(range(len(preds)))
        if not self.trusted or k >= len(preds):
            return idx
        return sorted(idx, key=lambda i: -preds[i])[:k]
