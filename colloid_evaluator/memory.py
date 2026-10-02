"""Memory growth under sustained load: the L6 soak's leak test (ADR 0010).

A leak grows without bound; warm-up grows and then flattens. Two things tell them apart:

- **whose memory.** The soak samples the service's process tree, which is what a gene changes,
  separately from the database cluster. The cluster's memory is bounded by its configuration
  (``shared_buffers``, and ``work_mem`` per operation: range-limited knobs). Its shared buffers
  also fill as a new index is first read, and every backend that touches a buffer page adds it
  to its PSS. The cluster's growth is recorded but does not decide the verdict.
- **whether growth persists.** A leak's slope over the second half of the soak is about the same
  as over the whole soak. A warm-up's second-half slope falls towards zero.

The rule used through experiment 2 was one least-squares slope of service plus database PSS
above 0.5 MB/s. That threshold sat inside the spread of honest programs (0.2 to 0.9 MB/s), and
it rejected programs whose holdout gains were real. :func:`legacy_leaking` keeps it, so that a
calibration can score both rules on the same samples.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

LEAK_MB_PER_S = 0.5
PERSIST_FRACTION = 0.5  # the second-half slope must keep at least this share of the threshold
MIN_SAMPLES = 10


@dataclass(frozen=True)
class Growth:
    samples: int
    start_mb: float  # mean of the first five samples
    end_mb: float  # mean of the last five samples
    slope_mb_per_s: float  # least squares over the whole soak
    late_slope_mb_per_s: float  # least squares over its second half

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _slope(y: np.ndarray, interval_s: float) -> float:
    return float(np.polyfit(np.arange(len(y)) * interval_s, y, 1)[0])


def growth(series: Sequence[float], interval_s: float) -> Growth | None:
    """Growth of one PSS series sampled every ``interval_s``; ``None`` if it is too short to fit."""
    y = np.asarray(series, dtype=float)
    if len(y) < MIN_SAMPLES:
        return None
    late = y[len(y) // 2:]
    return Growth(samples=len(y), start_mb=float(y[:5].mean()), end_mb=float(y[-5:].mean()),
                  slope_mb_per_s=_slope(y, interval_s), late_slope_mb_per_s=_slope(late, interval_s))


def leaking(g: Growth, threshold: float = LEAK_MB_PER_S) -> bool:
    """Growth above the threshold over the whole soak that is still going in its second half."""
    return g.slope_mb_per_s > threshold and g.late_slope_mb_per_s > threshold * PERSIST_FRACTION


def legacy_leaking(total: Growth, threshold: float = LEAK_MB_PER_S) -> bool:
    """The rule used through experiment 2: one slope of service plus database PSS."""
    return total.slope_mb_per_s > threshold


def soak_verdict(service: Sequence[float], db: Sequence[float], interval_s: float) -> tuple[bool, dict[str, Any]]:
    """``(ok, info)`` for one soak phase. Only the service's growth decides; the database's is recorded."""
    svc = growth(service, interval_s)
    info: dict[str, Any] = {"rule": "service PSS, persistent growth", "threshold_mb_per_s": LEAK_MB_PER_S}
    if db:
        total = [a + b for a, b in zip(service, db, strict=False)]
        dbg, tot = growth(db, interval_s), growth(total, interval_s)
        info["db"] = dbg.as_dict() if dbg else None
        info["total"] = tot.as_dict() if tot else None
        info["legacy_leaking"] = bool(tot and legacy_leaking(tot))
    if svc is None:
        info["samples"] = len(service)
        return True, info
    info["service"] = svc.as_dict()
    # coarse shape (one point a second) for later diagnosis
    step = max(1, round(1.0 / interval_s))
    info["service_series_mb"] = [round(float(v), 2) for v in list(service)[::step]]
    if leaking(svc):
        info["reason"] = (f"service memory grows {svc.slope_mb_per_s:.2f} MB/s under sustained load and is still growing "
                          f"{svc.late_slope_mb_per_s:.2f} MB/s in the second half (leak suspected)")
        return False, info
    return True, info
