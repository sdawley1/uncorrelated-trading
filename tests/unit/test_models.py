import socket
from dataclasses import FrozenInstanceError
from datetime import date

import numpy as np
import pytest

from uncorrelated_trading.models import Bar, CorrelationMatrix, correlation_matrices_equal


def test_bar_is_frozen():
    bar = Bar("XOM", date(2026, 1, 2), 1.0, 2.0, 0.5, 1.5, 100, "csv")
    with pytest.raises(FrozenInstanceError):
        bar.close = 3.0  # type: ignore[misc]


def test_sample_bars_fixture_shape(sample_bars):
    symbols = {b.symbol for b in sample_bars}
    assert symbols == {"AAA", "BBB", "CCC"}
    assert len(sample_bars) == 900
    days = sorted({b.day for b in sample_bars})
    assert all(d.weekday() < 5 for d in days)


def test_sample_bars_fixture_is_ordered_and_positive(sample_bars):
    for sym in ("AAA", "BBB", "CCC"):
        days = [b.day for b in sample_bars if b.symbol == sym]
        assert days == sorted(days) and len(set(days)) == 300
        assert days[-1] == date(2026, 10, 13)
    assert all(b.low <= b.open <= b.high and b.low <= b.close <= b.high for b in sample_bars)
    assert all(b.close > 0 for b in sample_bars)


def _corr(values: np.ndarray, window: int = 60) -> CorrelationMatrix:
    return CorrelationMatrix(
        window=window,
        method="pearson",
        symbols=("AAA", "BBB"),
        values=values,
        n_obs=60,
        start_day=date(2026, 7, 1),
        end_day=date(2026, 10, 13),
    )


def test_correlation_matrix_is_not_value_comparable():
    a = _corr(np.array([[1.0, 0.1], [0.1, 1.0]]))
    b = _corr(np.array([[1.0, 0.1], [0.1, 1.0]]))
    assert a != b  # eq=False: identity comparison only
    assert a == a


def test_correlation_matrices_equal_tolerates_tiny_float_noise():
    a = _corr(np.array([[1.0, 0.1], [0.1, 1.0]]))
    b = _corr(np.array([[1.0, 0.1 + 1e-14], [0.1 + 1e-14, 1.0]]))
    assert correlation_matrices_equal(a, b)


def test_correlation_matrices_equal_detects_value_difference():
    a = _corr(np.array([[1.0, 0.1], [0.1, 1.0]]))
    b = _corr(np.array([[1.0, 0.2], [0.2, 1.0]]))
    assert not correlation_matrices_equal(a, b)
    assert correlation_matrices_equal(a, b, atol=0.2)


def test_correlation_matrices_equal_detects_metadata_difference():
    v = np.array([[1.0, 0.1], [0.1, 1.0]])
    assert not correlation_matrices_equal(_corr(v, window=60), _corr(v, window=120))


def test_correlation_matrices_equal_rejects_shape_mismatch():
    a = _corr(np.array([[1.0, 0.1], [0.1, 1.0]]))
    b = _corr(np.eye(3))
    assert not correlation_matrices_equal(a, b)


def test_no_network_guard_blocks_connections():
    with pytest.raises(RuntimeError, match="network access blocked"):
        socket.create_connection(("example.com", 80))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network access blocked"):
            sock.connect(("127.0.0.1", 9))
    finally:
        sock.close()
