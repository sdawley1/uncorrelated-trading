import sqlite3
from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from uncorrelated_trading.models import Bar
from uncorrelated_trading.store.bars import BarStore
from uncorrelated_trading.store.db import migrate

BASE = date(2026, 1, 5)

bar_strategy = st.builds(
    Bar,
    symbol=st.sampled_from(["AAA", "BBB", "CCC", "DDD"]),
    day=st.integers(min_value=0, max_value=40).map(lambda i: BASE + timedelta(days=i)),
    open=st.floats(min_value=0.01, max_value=1e5, allow_nan=False),
    high=st.floats(min_value=0.01, max_value=1e5, allow_nan=False),
    low=st.floats(min_value=0.01, max_value=1e5, allow_nan=False),
    close=st.floats(min_value=0.01, max_value=1e5, allow_nan=False),
    volume=st.integers(min_value=0, max_value=10**12),
    source=st.sampled_from(["alpaca", "twelvedata", "csv"]),
)


def _memory_store() -> BarStore:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return BarStore(conn)


@settings(max_examples=75, deadline=None)
@given(bars=st.lists(bar_strategy, max_size=60), repeats=st.integers(min_value=1, max_value=3))
def test_upsert_idempotent_and_load_returns_unique_keys(bars: list[Bar], repeats: int):
    store = _memory_store()
    for _ in range(repeats):
        store.upsert(bars)
    last_by_key = {(b.symbol, b.day): b for b in bars}
    loaded = store.load(["AAA", "BBB", "CCC", "DDD"], BASE, BASE + timedelta(days=40))
    assert [(b.symbol, b.day) for b in loaded] == sorted(last_by_key)
    assert loaded == [last_by_key[k] for k in sorted(last_by_key)]
    store.upsert(bars)
    assert store.load(["AAA", "BBB", "CCC", "DDD"], BASE, BASE + timedelta(days=40)) == loaded
