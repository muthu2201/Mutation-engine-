"""The soak's leak test separates unbounded growth of the service from warm-up (ADR 0010)."""

import numpy as np

from colloid_evaluator.memory import growth, leaking, legacy_leaking, soak_verdict

DT = 0.2
T = np.arange(100) * DT  # a 20 s soak sampled every 0.2 s


def _noise(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 0.3, len(T))


def test_a_linear_leak_is_caught_by_both_rules():
    service = 40.0 + 0.8 * T + _noise(1)
    g = growth(service, DT)
    assert g is not None and leaking(g) and legacy_leaking(g)
    ok, info = soak_verdict(service.tolist(), (150.0 + _noise(2)).tolist(), DT)
    assert not ok and "service memory grows" in info["reason"] and info["legacy_leaking"]


def test_database_buffer_warm_up_no_longer_fails_the_soak():
    # experiment 2: a new index's pages fill shared buffers; stack PSS went from 188 to 202 MB in 20 s
    service = 40.0 + _noise(3)
    db = 148.0 + 14.0 * (1.0 - np.exp(-T / 6.0)) + _noise(4)
    ok, info = soak_verdict(service.tolist(), db.tolist(), DT)
    assert ok and info["legacy_leaking"], "the old rule rejected this; the new one must not"
    assert info["db"]["slope_mb_per_s"] > 0.5 and "reason" not in info


def test_service_warm_up_that_flattens_is_not_a_leak():
    service = 30.0 + 20.0 * (1.0 - np.exp(-T / 3.0)) + _noise(5)
    g = growth(service, DT)
    assert g is not None and g.slope_mb_per_s > 0.5 and g.late_slope_mb_per_s < 0.25
    assert not leaking(g) and legacy_leaking(g)


def test_too_few_samples_pass_and_say_so():
    ok, info = soak_verdict([40.0] * 5, [], DT)
    assert ok and info["samples"] == 5 and "db" not in info
    assert growth([1.0] * 9, DT) is None
