from __future__ import annotations

import socket
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from uncorrelated_trading.models import Bar


@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 10, 13, 21, 30, tzinfo=UTC)


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def make_bars(
    symbols: list[str], n_days: int, seed: int = 0, end: date = date(2026, 10, 13)
) -> list[Bar]:
    rng = np.random.default_rng(seed)
    days: list[date] = []
    d = end
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    bars: list[Bar] = []
    for s in symbols:
        rets = rng.normal(0.0003, 0.015, size=n_days)
        closes = 100.0 * np.exp(np.cumsum(rets))
        for day, c in zip(days, closes, strict=True):
            bars.append(
                Bar(
                    s,
                    day,
                    float(c * 0.995),
                    float(c * 1.01),
                    float(c * 0.99),
                    float(c),
                    1_000_000,
                    "csv",
                )
            )
    return bars


@pytest.fixture
def sample_bars() -> list[Bar]:
    return make_bars(["AAA", "BBB", "CCC"], 300)


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("integration"):
        return

    def guard(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network access blocked in non-integration tests")

    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
