"""Unit tests for uncorrelated pair and n-tuple discovery."""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable
from datetime import date

import numpy as np
import pytest

from uncorrelated_trading.analysis.uncorrelated import (
    SetSearchResult,
    build_adjacency,
    enumerate_independent_sets,
    find_pairs,
    fisher_ci_upper_abs,
    largest_independent_set,
    search_uncorrelated_sets,
    set_diagnostics,
)
from uncorrelated_trading.models import CorrelationMatrix

FAR_DEADLINE = float("inf")


def make_cm(
    values: np.ndarray,
    symbols: tuple[str, ...] | None = None,
    method: str = "pearson",
    n_obs: int = 60,
) -> CorrelationMatrix:
    n = values.shape[0]
    syms = symbols if symbols is not None else tuple(f"S{i:02d}" for i in range(n))
    return CorrelationMatrix(
        window=n_obs,
        method=method,  # type: ignore[arg-type]
        symbols=syms,
        values=values,
        n_obs=n_obs,
        start_day=date(2026, 7, 1),
        end_day=date(2026, 10, 13),
    )


def fake_monotonic(step: float = 0.01) -> tuple[Callable[[], float], list[float]]:
    state = [0.0]

    def tick() -> float:
        state[0] += step
        return state[0]

    return tick, state


# 5 symbols: A-B, B-C, C-D strongly correlated; everything else weak.
FIVE = np.array(
    [
        [1.00, 0.80, 0.05, 0.10, 0.02],
        [0.80, 1.00, 0.70, 0.03, 0.04],
        [0.05, 0.70, 1.00, 0.60, 0.01],
        [0.10, 0.03, 0.60, 1.00, 0.15],
        [0.02, 0.04, 0.01, 0.15, 1.00],
    ]
)
FIVE_SYMS = ("A", "B", "C", "D", "E")


def five() -> CorrelationMatrix:
    return make_cm(FIVE.copy(), FIVE_SYMS)


def test_build_adjacency_bool_diag_false():
    adj = build_adjacency(five(), 0.2)
    assert adj.dtype == np.bool_
    assert not adj.diagonal().any()
    assert (adj == adj.T).all()
    expected = {(0, 1), (1, 2), (2, 3)}
    got = {(i, j) for i in range(5) for j in range(i + 1, 5) if adj[i, j]}
    assert got == expected


def test_build_adjacency_threshold_is_inclusive():
    adj = build_adjacency(five(), 0.6)
    assert adj[2, 3] and adj[3, 2]


def test_known_independent_sets_of_five():
    adj = build_adjacency(five(), 0.2)
    absr = np.abs(FIVE)
    triples = enumerate_independent_sets(adj, 3, 100, absr, FAR_DEADLINE, time.monotonic)
    assert set(triples) == {(0, 2, 4), (0, 3, 4), (1, 3, 4)}
    # ordered by max |rho| ascending: (0,2,4) max .05; (1,3,4) max .15; (0,3,4) max .15
    assert triples == ((0, 2, 4), (0, 3, 4), (1, 3, 4))
    quads = enumerate_independent_sets(adj, 4, 100, absr, FAR_DEADLINE, time.monotonic)
    assert quads == ()


def test_enumeration_cap_keeps_best():
    adj = build_adjacency(five(), 0.2)
    absr = np.abs(FIVE)
    best = enumerate_independent_sets(adj, 3, 1, absr, FAR_DEADLINE, time.monotonic)
    assert best == ((0, 2, 4),)


def test_largest_set_of_five():
    adj = build_adjacency(five(), 0.2)
    best, exact = largest_independent_set(adj, FAR_DEADLINE, time.monotonic)
    assert exact is True
    assert len(best) == 3
    assert best == tuple(sorted(best))
    for i, j in itertools.combinations(best, 2):
        assert not adj[i, j]


def test_pairs_sorted_ascending():
    pairs = find_pairs(five(), five(), 0.2, 0.3)
    keys = [p.max_abs_rho for p in pairs]
    assert keys == sorted(keys)
    assert len(pairs) == 7  # 10 pairs minus 3 edges
    assert pairs[0].symbols == ("C", "E")
    assert all(p.size == 2 and p.symbols == tuple(sorted(p.symbols)) for p in pairs)


def test_subsets_of_reported_sets_are_uncorrelated():
    res = search_uncorrelated_sets(five(), five(), None, 0.2, 0.3, 4, 50, 10.0, momentum_z=None)
    pair_names = {frozenset(p.symbols) for p in res.pairs}
    for sets in res.sets_by_size.values():
        for s in sets:
            for sub in itertools.combinations(s.symbols, 2):
                assert frozenset(sub) in pair_names
    assert res.largest is not None
    for sub in itertools.combinations(res.largest.symbols, 2):
        assert frozenset(sub) in pair_names


def test_largest_set_on_empty_graph_is_everything():
    vals = np.eye(6)
    res = search_uncorrelated_sets(
        make_cm(vals), make_cm(vals, method="spearman"), None, 0.2, 0.3, 4, 50, 5.0
    )
    assert res.largest is not None
    assert res.largest.symbols == tuple(f"S{i:02d}" for i in range(6))
    assert res.largest_exact is True
    assert res.deadline_hit is False
    assert len(res.sets_by_size[3]) == math.comb(6, 3)
    assert len(res.sets_by_size[4]) == math.comb(6, 4)


def test_largest_set_on_complete_graph_is_singleton():
    adj = ~np.eye(7, dtype=bool)
    best, exact = largest_independent_set(adj, FAR_DEADLINE, time.monotonic)
    assert len(best) == 1
    assert exact is True


def test_largest_set_empty_input():
    best, exact = largest_independent_set(np.zeros((0, 0), dtype=bool), 0.0, time.monotonic)
    assert best == ()
    assert exact is True


def test_largest_set_deadline_returns_greedy_incumbent():
    rng = np.random.default_rng(3)
    n = 30
    upper = np.triu(rng.random((n, n)) < 0.5, 1)
    adj = upper | upper.T
    tick, _ = fake_monotonic()
    best, exact = largest_independent_set(adj, 0.0, tick)  # already expired
    assert exact is False
    assert len(best) >= 1
    for i, j in itertools.combinations(best, 2):
        assert not adj[i, j]


def test_enumeration_deadline_aborts():
    adj = np.zeros((40, 40), dtype=bool)
    absr = np.zeros((40, 40))
    tick, _ = fake_monotonic()
    out = enumerate_independent_sets(adj, 3, 10, absr, 0.0, tick)
    assert len(out) <= 10


def test_fisher_ci_upper_abs():
    r, n = 0.2, 60
    expected = math.tanh(math.atanh(0.2) + 1.96 / math.sqrt(57))
    assert fisher_ci_upper_abs(r, n) == pytest.approx(expected)
    assert fisher_ci_upper_abs(-0.2, n) == pytest.approx(expected)
    assert fisher_ci_upper_abs(1.0, n) == 1.0
    assert fisher_ci_upper_abs(0.1, 3) == 1.0
    assert 0.0 <= fisher_ci_upper_abs(0.0, 1000) <= 1.0


def test_set_diagnostics_values():
    p = five()
    sp_vals = FIVE.copy()
    sp_vals[0, 4] = sp_vals[4, 0] = 0.35
    sp = make_cm(sp_vals, FIVE_SYMS, method="spearman")
    s = set_diagnostics([4, 2, 0], p, sp, None, 0.2, 0.3, None)
    assert s.symbols == ("A", "C", "E")
    assert s.size == 3
    assert s.max_abs_rho == pytest.approx(0.05)
    assert s.mean_abs_rho == pytest.approx((0.05 + 0.02 + 0.01) / 3)
    assert s.max_abs_rho_ci_upper == pytest.approx(fisher_ci_upper_abs(0.05, 60))
    sub = FIVE[np.ix_([0, 2, 4], [0, 2, 4])]
    assert s.min_eigenvalue == pytest.approx(float(np.linalg.eigvalsh(sub).min()))
    assert s.spearman_max_abs_rho == pytest.approx(0.35)
    assert s.passes_spearman is False
    assert s.stable_in_long_window is False
    assert s.mean_momentum_z == 0.0


def test_set_diagnostics_long_window_and_momentum():
    p = five()
    long_ok = make_cm(np.eye(5), FIVE_SYMS, n_obs=250)
    long_bad_vals = np.eye(5)
    long_bad_vals[0, 2] = long_bad_vals[2, 0] = 0.5
    long_bad = make_cm(long_bad_vals, FIVE_SYMS, n_obs=250)
    long_missing = make_cm(np.eye(4), ("A", "B", "D", "E"), n_obs=250)
    mz = {"A": 1.0, "C": 2.0, "E": 0.0}
    ok = set_diagnostics([0, 2, 4], p, p, long_ok, 0.2, 0.3, mz)
    assert ok.stable_in_long_window is True
    assert ok.mean_momentum_z == pytest.approx(1.0)
    assert ok.passes_spearman is True
    assert set_diagnostics([0, 2, 4], p, p, long_bad, 0.2, 0.3).stable_in_long_window is False
    missing = set_diagnostics([0, 2, 4], p, p, long_missing, 0.2, 0.3)
    assert missing.stable_in_long_window is False


def _deadline_matrix(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vals = rng.uniform(-0.3, 0.3, size=(n, n))
    vals = np.triu(vals, 1)
    vals = vals + vals.T
    # a handful of near-perfectly correlated pairs so the graph is not empty
    for i in range(0, 10, 2):
        vals[i, i + 1] = vals[i + 1, i] = 0.995
    np.fill_diagonal(vals, 1.0)
    return vals


def test_deadline_caps_runtime():
    n = 80
    vals = _deadline_matrix(n, 11)
    pearson = make_cm(vals)
    spearman = make_cm(vals, method="spearman")
    # Each poll happens once per 1000 search-node visits, which costs roughly 5-10 ms of
    # real time here; advancing the fake clock 50 ms per poll keeps the simulation honest.
    tick, state = fake_monotonic(0.05)
    budget = 0.5
    wall0 = time.perf_counter()
    res = search_uncorrelated_sets(
        pearson, spearman, None, 0.99, 0.99, 4, 50, budget, monotonic=tick
    )
    wall = time.perf_counter() - wall0
    assert isinstance(res, SetSearchResult)
    assert res.deadline_hit is True
    assert res.largest_exact is False
    assert state[0] <= 2 * budget  # simulated time consumed stays under 2x budget
    assert wall < 5.0
    assert len(res.pairs) == math.comb(n, 2) - 5
    for size, sets in res.sets_by_size.items():
        assert len(sets) <= 50
        for s in sets:
            assert s.size == size == len(s.symbols)
            assert s.max_abs_rho < 0.99
    assert res.largest is not None
    idx = [pearson.symbols.index(x) for x in res.largest.symbols]
    for i, j in itertools.combinations(idx, 2):
        assert abs(vals[i, j]) < 0.99


def test_order_invariance():
    rng = np.random.default_rng(5)
    n = 9
    vals = rng.uniform(-0.5, 0.5, size=(n, n))
    vals = np.triu(vals, 1)
    vals = vals + vals.T
    np.fill_diagonal(vals, 1.0)
    syms = tuple(f"S{i:02d}" for i in range(n))
    perm = rng.permutation(n)
    pvals = vals[np.ix_(perm, perm)]
    psyms = tuple(syms[i] for i in perm)

    def run(v: np.ndarray, s: tuple[str, ...]) -> SetSearchResult:
        return search_uncorrelated_sets(
            make_cm(v, s), make_cm(v, s, "spearman"), None, 0.3, 0.4, 4, 10_000, 10.0
        )

    a, b = run(vals, syms), run(pvals, psyms)
    assert {frozenset(p.symbols) for p in a.pairs} == {frozenset(p.symbols) for p in b.pairs}
    assert a.sets_by_size.keys() == b.sets_by_size.keys()
    for k in a.sets_by_size:
        sa = {frozenset(s.symbols) for s in a.sets_by_size[k]}
        sb = {frozenset(s.symbols) for s in b.sets_by_size[k]}
        assert sa == sb
    assert a.largest is not None and b.largest is not None
    assert a.largest.size == b.largest.size
    assert [p.symbols for p in a.pairs] == [p.symbols for p in b.pairs]


def test_ranking_uses_eigenvalue_then_momentum():
    vals = np.eye(4)
    res = search_uncorrelated_sets(
        make_cm(vals),
        make_cm(vals, method="spearman"),
        None,
        0.2,
        0.3,
        3,
        50,
        5.0,
        momentum_z={"S00": -1.0, "S01": 0.0, "S02": 1.0, "S03": 2.0},
    )
    triples = res.sets_by_size[3]
    zs = [t.mean_momentum_z for t in triples]
    assert zs == sorted(zs, reverse=True)
    assert triples[0].symbols == ("S01", "S02", "S03")


def test_largest_none_when_no_pair_qualifies():
    vals = np.full((3, 3), 0.9)
    np.fill_diagonal(vals, 1.0)
    res = search_uncorrelated_sets(
        make_cm(vals), make_cm(vals, method="spearman"), None, 0.2, 0.3, 4, 50, 5.0
    )
    assert res.pairs == ()
    assert res.largest is None
    assert res.sets_by_size == {3: (), 4: ()}
