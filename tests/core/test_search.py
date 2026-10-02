"""Selection, islands/MAP-Elites, bandit, attribution, splicing: the search machinery (T06,T08,T10,T11)."""

import random

from colloid.core.archive import Axis, Elite, Island, IslandModel, MapElitesGrid
from colloid.core.attribution import Measured, prune, shapley_exact, shapley_plan
from colloid.core.bandit import ThompsonBandit
from colloid.core.objectives import Fitness
from colloid.core.selection import confident_dominates, nsga2_select, nsga3_select
from colloid.core.splicing import (
    best_unions,
    design_subsets,
    fit_splice_model,
    hypervolume_2d,
    screening_design,
)


def fit(cost, mem, ci=0.01):
    return Fitness({"cost": cost, "mem": mem}, {"cost": cost - ci, "mem": mem - ci}, {"cost": cost + ci, "mem": mem + ci}, {"cost": 0.01, "mem": 0.01})


def test_confidence_aware_dominance_ignores_overlap():
    a = fit(0.10, 0.0, ci=0.05)
    b = fit(0.09, 0.0, ci=0.05)  # overlapping CIs -> not a confident win
    assert not confident_dominates(a, b, ["cost", "mem"])
    c = fit(0.30, 0.05, ci=0.01)  # clearly better on both
    assert confident_dominates(c, b, ["cost", "mem"])


def test_nsga2_keeps_pareto_and_spreads():
    fits = [fit(0.1, 0.0), fit(0.0, 0.1), fit(0.05, 0.05), fit(-0.1, -0.1)]
    chosen = nsga2_select(list(range(4)), fits, ["cost", "mem"], 3)
    assert len(chosen) == 3
    assert 3 not in chosen  # the dominated point is dropped


def test_nsga3_selects_requested_count():
    rng = random.Random(0)
    fits = [fit(rng.random() * 0.2, rng.random() * 0.2, ci=0.001) for _ in range(20)]
    chosen = nsga3_select(list(range(20)), fits, ["cost", "mem"], 8)
    assert len(chosen) == 8


def test_map_elites_keeps_best_per_cell_and_prefers_small():
    grid = MapElitesGrid((Axis("genes", edges=(1.5, 3.5)),))
    assert grid.try_insert(Elite("p1", ("a",), fit(0.1, 0), 0.1, {"genes": 1.0}, 0))
    assert not grid.try_insert(Elite("p2", ("b",), fit(0.05, 0), 0.05, {"genes": 1.0}, 0))  # worse, same cell
    assert grid.try_insert(Elite("p3", ("c",), fit(0.2, 0), 0.2, {"genes": 1.0}, 0))  # better
    # tie on score -> smaller genome wins
    grid2 = MapElitesGrid((Axis("genes", edges=(10,)),))
    grid2.try_insert(Elite("big", ("a", "b", "c"), fit(0.1, 0), 0.1, {"genes": 3.0}, 0))
    assert grid2.try_insert(Elite("small", ("a",), fit(0.1, 0), 0.1, {"genes": 1.0}, 0))
    assert grid2.cells[(0,)].program_id == "small"  # both land in cell (0,); tie -> smaller genome


def test_island_accepts_improvement_and_neutral_drift():
    isl = Island("x", (Axis("genes", edges=(2,)),), ("cost",))
    rng = random.Random(0)
    parent = Elite("par", ("a",), fit(0.1, 0), 0.1, {"genes": 1.0}, 0)
    isl.pool.append(parent)
    better = Elite("ch", ("a", "b"), fit(0.2, 0), 0.2, {"genes": 2.0}, 0)
    res = isl.offer(better, parent, rng)
    assert res["pool"] or res["grid"]
    # neutral: same score, not larger -> accepted into pool
    neutral = Elite("nt", ("c",), fit(0.1, 0, ci=0.2), 0.1, {"genes": 1.0}, 0)
    assert isl.offer(neutral, parent, rng)["pool"]


def test_island_stagnation_triggers_reseed_signal():
    isl = Island("x", (Axis("genes", edges=(2,)),), ("cost",), stagnation_limit=2)
    from colloid.core.novelty import MinHash

    isl.grid.try_insert(Elite("e", ("a",), fit(0.1, 0), 0.1, {"genes": 1.0}, 0, signature=MinHash.of_code("x")))
    stag = False
    for _ in range(2):
        stag = isl.end_generation()
    assert stag  # no improvement for stagnation_limit generations, grid non-empty -> reseed signal
    assert isl.basins.centres  # the converged elite's signature became a tabu basin centre


def test_migration_moves_elites():
    islands = {n: Island(n, (Axis("genes", edges=(2,)),), ("cost",)) for n in ("a", "b")}
    for n, isl in islands.items():
        isl.grid.try_insert(Elite(f"{n}1", ("x",), fit(0.1, 0), 0.1, {"genes": 1.0}, 0))
    model = IslandModel(islands, ("a", "b"), migration_interval=1)
    moves = model.migrate(1)
    assert len(moves) >= 1


def test_bandit_prefers_better_arm():
    b = ThompsonBandit(prior_mean=0.0, default_cost=1.0)
    good, bad = ("op", "g", None), ("op", "b", None)
    rng = random.Random(0)
    for _ in range(80):
        b.update((), good, 0.2)
        b.update((), bad, 0.0)
    picks = [b.select((), rng, cost_aware=False) for _ in range(200)]
    assert picks.count(good) > picks.count(bad) * 3


def test_bandit_cost_awareness():
    b = ThompsonBandit(prior_mean=0.0)
    cheap, dear = ("op", "cheap", None), ("op", "dear", None)
    rng = random.Random(1)
    for _ in range(50):
        b.update((), cheap, 0.10, cost=1.0)
        b.update((), dear, 0.12, cost=100.0)  # marginally better but 100x costlier
    picks = [b.select((), rng, cost_aware=True) for _ in range(200)]
    assert picks.count(cheap) > picks.count(dear)


def test_shapley_recovers_planted_values():
    # value(S) = 0.1*[a in S] + 0.2*[b in S] + 0.05*[c in S] + 0.1*[a and b]
    genes = ["a", "b", "c"]

    def v(S):
        return 0.1 * ("a" in S) + 0.2 * ("b" in S) + 0.05 * ("c" in S) + 0.1 * ("a" in S and "b" in S)

    subsets, perms = shapley_plan(genes, 64, random.Random(0))
    values = {s: Measured(v(s), 1e-6) for s in subsets}
    credits = shapley_exact(genes, values)
    total = sum(c.value for c in credits.values())
    assert abs(total - v(frozenset(genes))) < 1e-6  # efficiency
    assert credits["b"].value > credits["a"].value > credits["c"].value


def test_shapley_prune_drops_zero_effect():
    credits = shapley_exact(["a", "b"], {
        frozenset(): Measured(0, 1e-6), frozenset(["a"]): Measured(0.2, 1e-6),
        frozenset(["b"]): Measured(0.0, 1e-6), frozenset(["a", "b"]): Measured(0.2, 1e-6)})
    keep, drop = prune(credits)
    assert "a" in keep and "b" in drop


def test_splice_beats_additive_on_planted_synergy():
    genes = ["g0", "g1", "g2", "g3", "g4"]

    def v(S):
        base = sum({"g0": 0.05, "g1": 0.04, "g2": 0.03, "g3": 0.02, "g4": 0.01}[g] for g in S)
        syn = 0.08 if ("g0" in S and "g1" in S) else 0.0
        return base + syn

    design = screening_design(len(genes), max_runs=48)
    subsets = design_subsets(genes, design)
    values = {s: Measured(v(s), 1e-6) for s in subsets}
    prior = {("g0", "g1"): 1.0}
    model = fit_splice_model(genes, values, prior)
    inter = model.interactions()
    assert inter.get(("g0", "g1"), 0) > 0.03  # recovers the synergy term
    top = best_unions(model, 3)
    assert {"g0", "g1"} <= top[0][0]  # best predicted union includes the synergistic pair


def test_hypervolume_monotone():
    small = hypervolume_2d([(0.1, 0.1)])
    big = hypervolume_2d([(0.1, 0.1), (0.2, 0.05), (0.05, 0.2)])
    assert big > small
