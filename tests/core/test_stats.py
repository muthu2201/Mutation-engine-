"""Statistics: A/A false-positive control, paired estimators, FDR, warm-up, bootstrap CIs."""

import numpy as np

from colloid.core import stats


def _noisy(rng, median, cv, n, strata=2):
    """Log-normal samples in ``strata`` groups with the given median and coefficient of variation."""
    sigma = np.sqrt(np.log(1 + cv**2))
    return [list(rng.lognormal(np.log(median), sigma, n // strata)) for _ in range(strata)]


def test_aa_false_positive_rate_near_alpha():
    """Comparing two identical processes must flag 'significant' at about alpha, not more."""
    rng = np.random.default_rng(0)
    alpha = 0.05
    flags = 0
    runs = 200
    for i in range(runs):
        a = _noisy(rng, 30.0, 0.15, 400)
        b = _noisy(rng, 30.0, 0.15, 400)
        e = stats.stratified_bootstrap_log_ratio(a, b, "median", seed=i, alpha=alpha)
        if e.ci_lo > 0 or e.ci_hi < 0:
            flags += 1
    assert flags / runs <= alpha * 2.0  # generous ceiling; should hover near alpha


def test_paired_detects_real_shift():
    rng = np.random.default_rng(1)
    ref = _noisy(rng, 30.0, 0.1, 400)
    cand = [[v * 0.8 for v in g] for g in ref]  # candidate 20% faster, same request mix
    e = stats.paired_quantile_effect(ref, cand, "median", seed=2)
    assert e.log_ratio > 0 and e.ci_lo > 0  # positive = better, significant


def test_paired_ratio_sign_flip_exact_small_n():
    # 4 paired chunks, candidate uniformly lower -> significant
    ref = [100.0, 110.0, 90.0, 105.0]
    cand = [80.0, 88.0, 72.0, 84.0]
    e = stats.paired_ratio_effect(ref, cand, seed=0)
    assert e.log_ratio > 0 and e.ci_lo > 0 and e.p_value < 0.2


def test_benjamini_hochberg():
    ps = [0.001, 0.2, 0.03, 0.8, 0.9]
    rejected, adj = stats.benjamini_hochberg(ps, q=0.05)
    assert rejected[0] is True
    assert rejected[3] is False
    assert all(0 <= a <= 1 for a in adj)
    assert adj[0] <= adj[2]  # monotone in rank


def test_mser_truncation_drops_warmup():
    rng = np.random.default_rng(3)
    warm = list(np.linspace(100, 40, 40))  # decaying transient
    steady = list(rng.normal(40, 1, 160))
    d = stats.mser_truncation(warm + steady, batch=5)
    assert d >= 20  # cuts into the transient region


def test_steady_detection():
    assert stats.steady([40.0, 40.5, 39.8], tol=0.08)
    assert not stats.steady([40.0, 44.0, 39.0], tol=0.08)
    assert not stats.steady([30.0, 35.0, 40.0], tol=0.2)  # monotone trend


def test_bootstrap_ci_contains_point():
    rng = np.random.default_rng(4)
    xs = list(rng.normal(10, 2, 300))
    point, lo, hi = stats.bootstrap_ci(xs, np.median)
    assert lo <= point <= hi
    assert lo < 10 < hi


def test_spearman_constant_is_nan():
    assert np.isnan(stats.spearman([1, 1, 1], [1, 2, 3]))
    assert abs(stats.spearman([1, 2, 3, 4], [1, 2, 3, 4]) - 1.0) < 1e-9


# ---------------------------------------------------------------- A/A noise floor
def test_between_run_variance_recovers_tau():
    import numpy as np

    from colloid.core.stats import between_run_variance

    rng = np.random.default_rng(1)
    tau, se = 0.02, 0.01
    effects = rng.normal(0, np.sqrt(tau**2 + se**2), 4000)
    est = between_run_variance(effects, [se] * len(effects))
    assert abs(np.sqrt(est) - tau) < 0.002
    # no between-run component -> clipped at 0, never negative
    assert between_run_variance(rng.normal(0, se, 500) * 0.5, [se] * 500) == 0.0
    assert between_run_variance([0.1], [0.01]) == 0.0


def test_with_noise_floor_widens_keeps_asymmetry_and_never_sharpens_p():
    from colloid.core.stats import Effect, combine_effects_se, with_noise_floor

    e = Effect(0.05, 0.03, 0.08, 0.001, 20, 20)  # asymmetric CI
    w = with_noise_floor(e, 0.02)
    se_w, se_t = combine_effects_se(e.ci_lo, e.ci_hi), combine_effects_se(w.ci_lo, w.ci_hi)
    assert abs(se_t - (se_w**2 + 0.02**2) ** 0.5) < 1e-9
    assert abs((w.ci_hi - w.log_ratio) / (w.log_ratio - w.ci_lo) - (0.03 / 0.02)) < 1e-9
    assert w.p_value >= e.p_value and w.log_ratio == e.log_ratio
    assert with_noise_floor(e, 0.0) is e


def test_binomial_sf_exact():
    from colloid.core.stats import binomial_sf

    assert binomial_sf(0, 10, 0.05) == 1.0
    assert abs(binomial_sf(1, 10, 0.05) - (1 - 0.95**10)) < 1e-12  # ~0.401: why "FPR<=alpha" on 10 runs is wrong
    assert abs(binomial_sf(3, 10, 0.05) - 0.011504) < 1e-5


def test_calibrate_aa_fixes_miscalibrated_harness():
    """Simulated harness whose within-run SE ignores a between-run component 2x its size: raw
    FPR is far above alpha, the leave-one-out calibrated FPR is back near alpha."""
    import numpy as np

    from colloid.core.stats import calibrate_aa, normal_p

    rng = np.random.default_rng(7)
    se, tau, n = 0.01, 0.02, 400
    eff = rng.normal(0, np.sqrt(se**2 + tau**2), n)
    p = [normal_p(x, se) for x in eff]
    cal = calibrate_aa(eff, [se] * n, p, 0.05)
    assert cal["raw_fpr"] > 0.3
    assert abs(cal["tau"] - tau) < 0.003
    assert cal["calibrated_fpr"] < 0.09
    # a calibrated harness passes the binomial gate; the raw one would not
    assert cal["binomial_p"] > 0.05
