# `analysis/uncorrelated.py`: uncorrelated set discovery

The analysed symbols form a graph with an edge wherever the primary-window Pearson
`abs(rho) >= tau`. An uncorrelated set is an independent set of that graph: every pair of
members has `abs(rho) < tau`.

## Entry point

`search_uncorrelated_sets(pearson, spearman, long_pearson, tau, tau_spearman, max_size_exact,
cap_per_size, time_budget_s, momentum_z=None, monotonic=time.monotonic) -> SetSearchResult`

- `pairs`: every pair below `tau` (not capped).
- `sets_by_size[k]` for `k = 3..max_size_exact`: up to `cap_per_size` sets. Enumeration keeps
  the `cap_per_size` sets with the smallest `max_abs_rho`; those are then ranked by
  `max_abs_rho` ascending, `min_eigenvalue` descending, `mean_momentum_z` descending, then
  symbol names.
- `largest`: a largest uncorrelated set (None when no pair qualifies), `largest_exact` False
  when the deadline cut the search.
- `deadline_hit`: True when any phase was aborted.

One deadline (`monotonic() + time_budget_s`) is shared by all phases and polled every 1000
search-node visits, including the first. After it fires, later phases stop at their first
poll and return what they have: enumeration returns the best sets found so far, and the
largest-set search returns its greedy (or better) incumbent with `exact=False`.

## Building blocks

| Function | Purpose |
|---|---|
| `build_adjacency(pearson, tau)` | bool `(n, n)`, True where `abs(rho) >= tau`, False diagonal |
| `find_pairs(pearson, spearman, tau, tau_spearman)` | all pairs below `tau`, ascending |
| `enumerate_independent_sets(adj, size, cap, abs_rho, deadline, monotonic)` | DFS over candidate arrays with a bounded heap; prunes branches whose running max `abs(rho)` cannot beat the current cap-th best; the last level is vectorised |
| `largest_independent_set(adj, deadline, monotonic)` | greedy minimum-degree lower bound, then Bron-Kerbosch with Tomita pivoting on the complement (integer bitsets) |
| `fisher_ci_upper_abs(rho, n_obs, z=1.96)` | `tanh(atanh(abs(rho)) + z / sqrt(n_obs - 3))`, clipped to `[0, 1]` |
| `set_diagnostics(indices, pearson, spearman, long_pearson, tau, tau_spearman, momentum_z)` | builds an `UncorrelatedSet` |

## Diagnostics

- `max_abs_rho`, `mean_abs_rho`: over member pairs in the primary Pearson matrix.
- `max_abs_rho_ci_upper`: Fisher-z 95 % upper bound on `max_abs_rho`. With a 60-day window
  the standard error of rho is about 0.13, so `tau = 0.2` is only about 1.5 standard errors
  from zero; this column makes that visible.
- `min_eigenvalue`: smallest eigenvalue of the Pearson sub-matrix (`numpy.linalg.eigvalsh`).
- `spearman_max_abs_rho`, `passes_spearman` (`< tau_spearman`).
- `stable_in_long_window`: all member pairs below `tau` in `long_pearson`; False when
  `long_pearson` is None or lacks a member.
- `mean_momentum_z`: mean of `momentum_z` over members (missing members count 0.0); 0.0
  when `momentum_z` is None.
