"""Property tests for uncorrelated set discovery."""

from __future__ import annotations

import itertools
import random
import time
from datetime import date

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from uncorrelated_trading.analysis.uncorrelated import (
    build_adjacency,
    enumerate_independent_sets,
    largest_independent_set,
    search_uncorrelated_sets,
)
from uncorrelated_trading.models import CorrelationMatrix


@st.composite
def corr_cases(draw: st.DrawFn) -> tuple[np.ndarray, float]:
    n = draw(st.integers(min_value=3, max_value=12))
    k = n * (n - 1) // 2
    upper = draw(
        st.lists(st.floats(min_value=-1.0, max_value=1.0, allow_nan=False), min_size=k, max_size=k)
    )
    vals = np.eye(n)
    iu = np.triu_indices(n, 1)
    vals[iu] = upper
    vals = np.triu(vals, 1) + np.triu(vals, 1).T + np.eye(n)
    tau = draw(st.floats(min_value=0.05, max_value=0.95, exclude_min=True, exclude_max=True))
    return vals, tau


def cm(values: np.ndarray, symbols: tuple[str, ...], method: str = "pearson") -> CorrelationMatrix:
    return CorrelationMatrix(
        window=60,
        method=method,  # type: ignore[arg-type]
        symbols=symbols,
        values=values,
        n_obs=60,
        start_day=date(2026, 7, 1),
        end_day=date(2026, 10, 13),
    )


def syms(n: int) -> tuple[str, ...]:
    return tuple(f"S{i:02d}" for i in range(n))


def brute_independent(vals: np.ndarray, tau: float, size: int) -> set[tuple[int, ...]]:
    n = vals.shape[0]
    return {
        c
        for c in itertools.combinations(range(n), size)
        if all(abs(vals[i, j]) < tau for i, j in itertools.combinations(c, 2))
    }


@settings(max_examples=60, deadline=None)
@given(corr_cases())
def test_search_properties(case: tuple[np.ndarray, float]) -> None:
    vals, tau = case
    n = vals.shape[0]
    s = syms(n)
    res = search_uncorrelated_sets(
        cm(vals, s), cm(vals, s, "spearman"), None, tau, 0.5, 5, 10_000, 30.0
    )
    pos = {x: i for i, x in enumerate(s)}
    n_below = sum(1 for i, j in itertools.combinations(range(n), 2) if abs(vals[i, j]) < tau)
    assert len(res.pairs) == n_below
    all_sets = list(res.pairs) + [x for v in res.sets_by_size.values() for x in v]
    if res.largest is not None:
        all_sets.append(res.largest)
    for us in all_sets:
        idx = [pos[x] for x in us.symbols]
        assert us.symbols == tuple(sorted(us.symbols))
        assert us.size == len(us.symbols)
        for i, j in itertools.combinations(idx, 2):
            assert abs(vals[i, j]) < tau
        assert us.min_eigenvalue <= 1.0 + 1e-9
        eig = np.linalg.eigvalsh(vals[np.ix_(idx, idx)])
        assert eig.max() >= 1.0 - 1e-9
    # every subset of a reported set is itself uncorrelated (and reported, for pairs)
    pair_names = {frozenset(p.symbols) for p in res.pairs}
    for v in res.sets_by_size.values():
        for us in v:
            for sub in itertools.combinations(us.symbols, 2):
                assert frozenset(sub) in pair_names
    # exact enumeration counts match brute force
    for size in range(3, 6):
        expect = brute_independent(vals, tau, size)
        got = {tuple(sorted(pos[x] for x in us.symbols)) for us in res.sets_by_size[size]}
        assert got == expect
    # largest is maximum when exact
    assert res.largest_exact
    best = max((k for k in range(1, n + 1) if brute_independent(vals, tau, k) or k == 1), default=1)
    largest_size = res.largest.size if res.largest is not None else 1
    assert largest_size == best


@settings(max_examples=60, deadline=None)
@given(corr_cases(), st.integers(min_value=1, max_value=6))
def test_enumeration_counts_match_brute_force(case: tuple[np.ndarray, float], size: int) -> None:
    vals, tau = case
    adj = build_adjacency(cm(vals, syms(vals.shape[0])), tau)
    absr = np.abs(vals)
    out = enumerate_independent_sets(adj, size, 10**6, absr, float("inf"), time.monotonic)
    expect = brute_independent(vals, tau, size)
    assert len(out) == len(expect)
    assert set(out) == expect
    keys = [
        (max((absr[i, j] for i, j in itertools.combinations(t, 2)), default=0.0), t) for t in out
    ]
    assert keys == sorted(keys)


@settings(max_examples=60, deadline=None)
@given(corr_cases(), st.integers(min_value=1, max_value=4))
def test_capped_enumeration_is_best_prefix(case: tuple[np.ndarray, float], cap: int) -> None:
    vals, tau = case
    adj = build_adjacency(cm(vals, syms(vals.shape[0])), tau)
    absr = np.abs(vals)
    full = enumerate_independent_sets(adj, 2, 10**6, absr, float("inf"), time.monotonic)
    capped = enumerate_independent_sets(adj, 2, cap, absr, float("inf"), time.monotonic)
    assert capped == full[:cap]


@settings(max_examples=40, deadline=None)
@given(corr_cases(), st.randoms(use_true_random=False))
def test_results_invariant_to_symbol_order(
    case: tuple[np.ndarray, float], rnd: random.Random
) -> None:
    vals, tau = case
    n = vals.shape[0]
    s = syms(n)
    perm = list(range(n))
    rnd.shuffle(perm)
    pv = vals[np.ix_(perm, perm)]
    ps = tuple(s[i] for i in perm)
    a = search_uncorrelated_sets(
        cm(vals, s), cm(vals, s, "spearman"), None, tau, 0.5, 4, 10**6, 30.0
    )
    b = search_uncorrelated_sets(cm(pv, ps), cm(pv, ps, "spearman"), None, tau, 0.5, 4, 10**6, 30.0)
    assert {frozenset(p.symbols) for p in a.pairs} == {frozenset(p.symbols) for p in b.pairs}
    for k in a.sets_by_size:
        assert {frozenset(x.symbols) for x in a.sets_by_size[k]} == {
            frozenset(x.symbols) for x in b.sets_by_size[k]
        }
    sa = a.largest.size if a.largest else 0
    sb = b.largest.size if b.largest else 0
    assert sa == sb


@settings(max_examples=40, deadline=None)
@given(corr_cases())
def test_largest_is_independent_and_maximal(case: tuple[np.ndarray, float]) -> None:
    vals, tau = case
    adj = build_adjacency(cm(vals, syms(vals.shape[0])), tau)
    best, exact = largest_independent_set(adj, float("inf"), time.monotonic)
    assert exact
    for i, j in itertools.combinations(best, 2):
        assert not adj[i, j]
    n = vals.shape[0]
    assert not any(brute_independent(vals, tau, k) for k in range(len(best) + 1, n + 1))
