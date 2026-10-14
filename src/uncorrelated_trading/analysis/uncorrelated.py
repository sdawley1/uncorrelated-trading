"""Uncorrelated pair and n-tuple discovery on a correlation graph.

The analysed symbols form a graph with an edge wherever ``abs(rho) >= tau`` in the primary
Pearson matrix. An *uncorrelated set* is an independent set of that graph (a clique of its
complement). This module provides:

* :func:`build_adjacency` and :func:`find_pairs` for the graph and its non-edges;
* :func:`enumerate_independent_sets`, a depth-first extension over candidate arrays that
  keeps the ``cap`` best sets of one size ranked by their largest ``abs(rho)``;
* :func:`largest_independent_set`, a greedy minimum-degree incumbent improved by
  Bron-Kerbosch with pivoting on the complement graph;
* :func:`set_diagnostics`, which turns member indices into a
  :class:`~uncorrelated_trading.models.UncorrelatedSet`;
* :func:`search_uncorrelated_sets`, the entry point used by the pipeline.

Every search respects a shared ``deadline`` read from an injected ``monotonic`` callable,
checked once every 1000 search-node visits (and on the first visit), so pathological
configurations such as ``tau`` close to 1 terminate with partial, well-formed results.
"""

from __future__ import annotations

import heapq
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from uncorrelated_trading.models import CorrelationMatrix, UncorrelatedSet

_CHECK_EVERY = 1000

# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetSearchResult:
    """Outcome of :func:`search_uncorrelated_sets`.

    Attributes
    ----------
    pairs : tuple of UncorrelatedSet
        Every uncorrelated pair, ranked (``max_abs_rho`` ascending first).
    sets_by_size : dict of int to tuple of UncorrelatedSet
        For each size ``3..max_size_exact``, up to ``cap_per_size`` ranked sets.
    largest : UncorrelatedSet or None
        A largest uncorrelated set, or None when it has fewer than two members.
    largest_exact : bool
        True when ``largest`` is proven maximum (the deadline did not cut the search).
    deadline_hit : bool
        True when any search phase was aborted by the deadline.
    """

    pairs: tuple[UncorrelatedSet, ...]
    sets_by_size: dict[int, tuple[UncorrelatedSet, ...]]
    largest: UncorrelatedSet | None
    largest_exact: bool
    deadline_hit: bool


class _Deadline:
    """Counts search-node visits and polls ``monotonic`` every ``_CHECK_EVERY`` visits."""

    __slots__ = ("deadline", "expired", "monotonic", "next_check", "visits")

    def __init__(self, deadline: float, monotonic: Callable[[], float]) -> None:
        self.deadline = deadline
        self.monotonic = monotonic
        self.visits = 0
        self.next_check = 0
        self.expired = False

    def visit(self, count: int = 1) -> bool:
        """Record ``count`` visits; return True when the deadline has passed."""
        self.visits += count
        if self.visits >= self.next_check:
            self.next_check = self.visits + _CHECK_EVERY
            if self.monotonic() > self.deadline:
                self.expired = True
        return self.expired


# ---------------------------------------------------------------------------
# Graph construction and pairs
# ---------------------------------------------------------------------------


def build_adjacency(pearson: CorrelationMatrix, tau: float) -> np.ndarray:
    """Build the correlation graph's adjacency matrix.

    Parameters
    ----------
    pearson : CorrelationMatrix
        Symmetric correlation matrix with unit diagonal.
    tau : float
        Correlation threshold; an edge exists where ``abs(rho) >= tau``.

    Returns
    -------
    numpy.ndarray
        Boolean array of shape ``(n, n)``, symmetric, with a False diagonal.
    """
    adj = np.abs(np.asarray(pearson.values, dtype=float)) >= tau
    np.fill_diagonal(adj, False)
    return adj


def _rank_key(s: UncorrelatedSet) -> tuple[float, float, float, tuple[str, ...]]:
    """Ranking: ``max_abs_rho`` asc, ``min_eigenvalue`` desc, ``mean_momentum_z`` desc."""
    return (s.max_abs_rho, -s.min_eigenvalue, -s.mean_momentum_z, s.symbols)


def find_pairs(
    pearson: CorrelationMatrix,
    spearman: CorrelationMatrix,
    tau: float,
    tau_spearman: float,
) -> tuple[UncorrelatedSet, ...]:
    """Return every pair ``i < j`` with Pearson ``abs(rho) < tau``, ascending by ``abs(rho)``.

    Parameters
    ----------
    pearson : CorrelationMatrix
        Primary-window Pearson matrix (defines the pairs).
    spearman : CorrelationMatrix
        Same-window Spearman matrix (diagnostic only).
    tau : float
        Pearson threshold.
    tau_spearman : float
        Spearman threshold for ``passes_spearman``.

    Returns
    -------
    tuple of UncorrelatedSet
        Size-2 sets sorted by ``max_abs_rho`` ascending (ties by symbol names). The long-window
        stability flag is False and ``mean_momentum_z`` is 0.0; use
        :func:`search_uncorrelated_sets` for fully populated diagnostics.
    """
    return _pairs(pearson, spearman, None, tau, tau_spearman, None)


def _pairs(
    pearson: CorrelationMatrix,
    spearman: CorrelationMatrix,
    long_pearson: CorrelationMatrix | None,
    tau: float,
    tau_spearman: float,
    momentum_z: Mapping[str, float] | None,
) -> tuple[UncorrelatedSet, ...]:
    adj = build_adjacency(pearson, tau)
    n = adj.shape[0]
    iu, ju = np.triu_indices(n, 1)
    keep = ~adj[iu, ju]
    out = [
        set_diagnostics(
            (int(i), int(j)), pearson, spearman, long_pearson, tau, tau_spearman, momentum_z
        )
        for i, j in zip(iu[keep], ju[keep], strict=True)
    ]
    return tuple(sorted(out, key=_rank_key))


# ---------------------------------------------------------------------------
# Fixed-size enumeration
# ---------------------------------------------------------------------------


def _enumerate(
    adjacency: np.ndarray,
    size: int,
    cap: int,
    abs_rho: np.ndarray,
    clock: _Deadline,
) -> tuple[tuple[tuple[int, ...], ...], bool]:
    """Enumerate independent sets; return ``(sets, completed)``."""
    n = int(adjacency.shape[0])
    if size < 1 or cap < 1 or size > n:
        return (), True
    nonadj = ~np.asarray(adjacency, dtype=bool)
    absr = np.asarray(abs_rho, dtype=float)
    # min-heap whose root is the worst kept set: key (-max_rho, -order, indices)
    heap: list[tuple[float, int, tuple[int, ...]]] = []
    order = 0

    def offer(max_rho: float, s: tuple[int, ...]) -> None:
        nonlocal order
        order += 1  # later sets are lexicographically larger and lose ties
        item = (-max_rho, -order, s)
        if len(heap) < cap:
            heapq.heappush(heap, item)
        elif max_rho < -heap[0][0]:
            heapq.heapreplace(heap, item)

    def bound() -> float:
        return -heap[0][0] if len(heap) >= cap else math.inf

    def extend(s: list[int], cur_max: float, cands: np.ndarray) -> bool:
        """Depth-first extension; return False when the deadline aborted the search."""
        if len(s) == size - 1:
            if clock.visit(max(1, int(cands.size))):
                return False
            if cands.size == 0:
                return True
            if s:
                leaf_max = np.maximum(cur_max, absr[np.ix_(cands, s)].max(axis=1))
            else:
                leaf_max = np.zeros(cands.size)
            for v, m in zip(cands.tolist(), leaf_max.tolist(), strict=True):
                if m >= bound():
                    continue
                offer(float(m), (*s, int(v)))
            return True
        if clock.visit():
            return False
        need = size - len(s)
        for pos in range(int(cands.size)):
            if cands.size - pos < need:
                break
            v = int(cands[pos])
            new_max = cur_max if not s else max(cur_max, float(absr[v, s].max()))
            if new_max >= bound():
                continue
            rest = cands[pos + 1 :]
            child = rest[nonadj[v, rest]]
            if child.size < need - 1:
                continue
            s.append(v)
            ok = extend(s, new_max, child)
            s.pop()
            if not ok:
                return False
        return True

    completed = extend([], 0.0, np.arange(n))
    ranked = sorted(((-neg, idx) for neg, _, idx in heap))
    return tuple(idx for _, idx in ranked), completed


def enumerate_independent_sets(
    adjacency: np.ndarray,
    size: int,
    cap: int,
    abs_rho: np.ndarray,
    deadline: float,
    monotonic: Callable[[], float],
) -> tuple[tuple[int, ...], ...]:
    """Enumerate independent sets of one size, keeping the ``cap`` best.

    Parameters
    ----------
    adjacency : numpy.ndarray
        Boolean ``(n, n)`` adjacency matrix from :func:`build_adjacency`.
    size : int
        Number of members per set.
    cap : int
        Maximum number of sets returned.
    abs_rho : numpy.ndarray
        ``(n, n)`` matrix of absolute correlations used for ranking.
    deadline : float
        Abort when ``monotonic()`` exceeds this value (polled every 1000 node visits).
    monotonic : callable
        Monotonic clock returning seconds.

    Returns
    -------
    tuple of tuple of int
        Index tuples sorted ascending within each tuple, ordered by the largest ``abs_rho``
        among members ascending (ties by lexicographic indices). When the deadline does not
        fire and ``cap`` is large enough this is every independent set of ``size``; when it
        fires, the best sets found so far.
    """
    sets, _ = _enumerate(adjacency, size, cap, abs_rho, _Deadline(deadline, monotonic))
    return sets


# ---------------------------------------------------------------------------
# Maximum independent set
# ---------------------------------------------------------------------------


def _greedy_min_degree(adj: np.ndarray) -> list[int]:
    """Greedy independent set: repeatedly take a minimum-degree vertex, drop its neighbours."""
    alive = np.ones(adj.shape[0], dtype=bool)
    chosen: list[int] = []
    while alive.any():
        deg = np.where(alive, adj[:, alive].sum(axis=1), np.iinfo(np.int64).max)
        v = int(np.argmin(deg))
        chosen.append(v)
        alive[v] = False
        alive &= ~adj[v]
    return sorted(chosen)


def _bits(mask: int) -> list[int]:
    out: list[int] = []
    while mask:
        low = mask & -mask
        out.append(low.bit_length() - 1)
        mask ^= low
    return out


def _largest(adjacency: np.ndarray, clock: _Deadline) -> tuple[tuple[int, ...], bool]:
    adj = np.asarray(adjacency, dtype=bool)
    n = int(adj.shape[0])
    if n == 0:
        return (), True
    best = _greedy_min_degree(adj)
    if len(best) == n:
        return tuple(best), True
    # complement-graph neighbourhoods as integer bitsets
    comp = ~adj
    np.fill_diagonal(comp, False)
    nbr = [sum(1 << int(j) for j in np.flatnonzero(comp[i])) for i in range(n)]
    best_r: list[int] = list(best)

    def expand(r: list[int], p: int, x: int) -> bool:
        nonlocal best_r
        if clock.visit():
            return False
        if p == 0:
            if x == 0 and len(r) > len(best_r):
                best_r = list(r)
            return True
        if len(r) + p.bit_count() <= len(best_r):
            return True
        pivot = max(_bits(p | x), key=lambda u: (p & nbr[u]).bit_count())
        for v in _bits(p & ~nbr[pivot]):
            r.append(v)
            ok = expand(r, p & nbr[v], x & nbr[v])
            r.pop()
            if not ok:
                return False
            p &= ~(1 << v)
            x |= 1 << v
            if len(r) + p.bit_count() <= len(best_r):
                break
        return True

    exact = expand([], (1 << n) - 1, 0)
    return tuple(sorted(best_r)), exact


def largest_independent_set(
    adjacency: np.ndarray,
    deadline: float,
    monotonic: Callable[[], float],
) -> tuple[tuple[int, ...], bool]:
    """Find a largest independent set of the correlation graph.

    A greedy minimum-degree heuristic supplies a lower bound; Bron-Kerbosch with Tomita
    pivoting on the complement graph then searches for a larger clique until it either
    finishes or the deadline fires.

    Parameters
    ----------
    adjacency : numpy.ndarray
        Boolean ``(n, n)`` adjacency matrix from :func:`build_adjacency`.
    deadline : float
        Abort when ``monotonic()`` exceeds this value (polled every 1000 recursion calls,
        including the first).
    monotonic : callable
        Monotonic clock returning seconds.

    Returns
    -------
    indices : tuple of int
        Sorted member indices of the best set found.
    exact : bool
        True when the set is proven maximum: either the greedy set already covers every
        vertex (no edges) or Bron-Kerbosch completed before the deadline.
    """
    return _largest(adjacency, _Deadline(deadline, monotonic))


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def fisher_ci_upper_abs(rho: float, n_obs: int, z: float = 1.96) -> float:
    """Upper confidence bound on ``abs(rho)`` via the Fisher z-transform.

    Parameters
    ----------
    rho : float
        Sample correlation (its absolute value is used).
    n_obs : int
        Number of observations behind ``rho``.
    z : float
        Normal quantile (1.96 for a two-sided 95 % interval).

    Returns
    -------
    float
        ``tanh(atanh(|rho|) + z / sqrt(n_obs - 3))`` clipped to ``[0, 1]``; 1.0 when
        ``n_obs <= 3`` or ``|rho| >= 1``.
    """
    r = abs(float(rho))
    if n_obs <= 3 or r >= 1.0:
        return 1.0
    upper = math.tanh(math.atanh(r) + z / math.sqrt(n_obs - 3))
    return min(1.0, max(0.0, upper))


def _submatrix(cm: CorrelationMatrix, names: Sequence[str]) -> np.ndarray | None:
    pos = {s: i for i, s in enumerate(cm.symbols)}
    if any(s not in pos for s in names):
        return None
    idx = [pos[s] for s in names]
    return np.asarray(cm.values, dtype=float)[np.ix_(idx, idx)]


def _off_diag_abs(sub: np.ndarray) -> np.ndarray:
    iu = np.triu_indices(sub.shape[0], 1)
    return np.abs(sub[iu])


def set_diagnostics(
    indices: Sequence[int],
    pearson: CorrelationMatrix,
    spearman: CorrelationMatrix,
    long_pearson: CorrelationMatrix | None,
    tau: float,
    tau_spearman: float,
    momentum_z: Mapping[str, float] | None = None,
) -> UncorrelatedSet:
    """Compute diagnostics for a set of symbols given by indices into ``pearson.symbols``.

    Parameters
    ----------
    indices : sequence of int
        Member positions in ``pearson.symbols`` (any order).
    pearson : CorrelationMatrix
        Primary-window Pearson matrix.
    spearman : CorrelationMatrix
        Primary-window Spearman matrix; members are looked up by name.
    long_pearson : CorrelationMatrix or None
        Longest-window Pearson matrix for the stability flag.
    tau : float
        Pearson threshold (also applied to ``long_pearson``).
    tau_spearman : float
        Spearman threshold for ``passes_spearman``.
    momentum_z : mapping of str to float, optional
        Composite momentum z-score per symbol; members absent from it count as 0.0.

    Returns
    -------
    UncorrelatedSet
        Diagnostics with sorted symbols. ``stable_in_long_window`` is False when
        ``long_pearson`` is None or lacks a member; ``mean_momentum_z`` is 0.0 when
        ``momentum_z`` is None.

    Raises
    ------
    ValueError
        If ``indices`` is empty or a member is missing from ``spearman``.
    """
    if len(indices) == 0:
        raise ValueError("set_diagnostics needs at least one member")
    names = tuple(sorted(pearson.symbols[int(i)] for i in indices))
    p_sub = _submatrix(pearson, names)
    s_sub = _submatrix(spearman, names)
    if p_sub is None or s_sub is None:
        raise ValueError(f"members {names} missing from the Spearman matrix")
    p_off = _off_diag_abs(p_sub)
    s_off = _off_diag_abs(s_sub)
    max_abs = float(p_off.max()) if p_off.size else 0.0
    mean_abs = float(p_off.mean()) if p_off.size else 0.0
    sp_max = float(s_off.max()) if s_off.size else 0.0
    stable = False
    if long_pearson is not None:
        l_sub = _submatrix(long_pearson, names)
        stable = l_sub is not None and bool((_off_diag_abs(l_sub) < tau).all())
    mom = 0.0
    if momentum_z is not None:
        mom = float(np.mean([float(momentum_z.get(s, 0.0)) for s in names]))
    return UncorrelatedSet(
        symbols=names,
        size=len(names),
        max_abs_rho=max_abs,
        mean_abs_rho=mean_abs,
        max_abs_rho_ci_upper=fisher_ci_upper_abs(max_abs, pearson.n_obs),
        min_eigenvalue=float(np.linalg.eigvalsh(p_sub).min()),
        spearman_max_abs_rho=sp_max,
        passes_spearman=sp_max < tau_spearman,
        stable_in_long_window=stable,
        mean_momentum_z=mom,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def search_uncorrelated_sets(
    pearson: CorrelationMatrix,
    spearman: CorrelationMatrix,
    long_pearson: CorrelationMatrix | None,
    tau: float,
    tau_spearman: float,
    max_size_exact: int,
    cap_per_size: int,
    time_budget_s: float,
    momentum_z: Mapping[str, float] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> SetSearchResult:
    """Discover uncorrelated pairs, n-tuples and a largest uncorrelated set.

    Parameters
    ----------
    pearson : CorrelationMatrix
        Primary-window Pearson matrix (defines the graph).
    spearman : CorrelationMatrix
        Primary-window Spearman matrix (diagnostic).
    long_pearson : CorrelationMatrix or None
        Longest-window Pearson matrix for ``stable_in_long_window``.
    tau : float
        Pearson threshold; sets are pairwise ``abs(rho) < tau``.
    tau_spearman : float
        Spearman threshold for ``passes_spearman``.
    max_size_exact : int
        Largest set size enumerated exhaustively (sizes ``3..max_size_exact``).
    cap_per_size : int
        Maximum number of sets kept per size.
    time_budget_s : float
        Wall-clock budget shared by enumeration and the largest-set search.
    momentum_z : mapping of str to float, optional
        Composite momentum z-scores used for ``mean_momentum_z`` and tie-breaking.
    monotonic : callable
        Monotonic clock; injected in tests.

    Returns
    -------
    SetSearchResult
        Pairs (all, ranked), up to ``cap_per_size`` ranked sets per size, a largest set and
        flags. Ranking within a size: ``max_abs_rho`` ascending, ``min_eigenvalue``
        descending, ``mean_momentum_z`` descending, then symbol names.
    """
    clock = _Deadline(monotonic() + time_budget_s, monotonic)
    adj = build_adjacency(pearson, tau)
    abs_rho = np.abs(np.asarray(pearson.values, dtype=float))

    def diag(idx: Sequence[int]) -> UncorrelatedSet:
        return set_diagnostics(idx, pearson, spearman, long_pearson, tau, tau_spearman, momentum_z)

    pairs = _pairs(pearson, spearman, long_pearson, tau, tau_spearman, momentum_z)
    sets_by_size: dict[int, tuple[UncorrelatedSet, ...]] = {}
    for size in range(3, max_size_exact + 1):
        found, _ = _enumerate(adj, size, cap_per_size, abs_rho, clock)
        sets_by_size[size] = tuple(sorted((diag(s) for s in found), key=_rank_key))
    best, exact = _largest(adj, clock)
    largest = diag(best) if len(best) >= 2 else None
    return SetSearchResult(
        pairs=pairs,
        sets_by_size=sets_by_size,
        largest=largest,
        largest_exact=exact,
        deadline_hit=clock.expired,
    )
