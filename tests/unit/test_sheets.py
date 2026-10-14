"""Tests for the optional Google Sheets publisher (fake gspread client)."""

from __future__ import annotations

import builtins
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from factories import sample_brief_inputs
from uncorrelated_trading.errors import ReportError
from uncorrelated_trading.report import sheets as sheets_mod
from uncorrelated_trading.report.sheets import SheetsPublisher
from uncorrelated_trading.report.workbook import tables

SHEET_ID = "fake-sheet-id-123"
KEY_FILE = Path("fake-service-account.json")


class FakeWorksheet:
    def __init__(self, title: str, rows: int = 0, cols: int = 0) -> None:
        self.title = title
        self.rows = rows
        self.cols = cols
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    def clear(self) -> None:
        self.calls.append(("clear", None, {}))

    def update(self, values: list[list[object]], **kwargs: Any) -> None:
        self.calls.append(("update", values, kwargs))


class FakeSpreadsheet:
    def __init__(self, existing: list[str]) -> None:
        self.sheets = {t: FakeWorksheet(t) for t in existing}
        self.added: list[str] = []

    def worksheets(self) -> list[FakeWorksheet]:
        return list(self.sheets.values())

    def worksheet(self, title: str) -> FakeWorksheet:
        if title not in self.sheets:
            raise KeyError(title)
        return self.sheets[title]

    def add_worksheet(self, title: str, rows: int, cols: int) -> FakeWorksheet:
        ws = FakeWorksheet(title, rows, cols)
        self.sheets[title] = ws
        self.added.append(title)
        return ws


class FakeClient:
    def __init__(self, spreadsheet: FakeSpreadsheet) -> None:
        self.spreadsheet = spreadsheet
        self.opened: list[str] = []

    def open_by_key(self, key: str) -> FakeSpreadsheet:
        self.opened.append(key)
        return self.spreadsheet


def _publisher(spreadsheet: FakeSpreadsheet) -> tuple[SheetsPublisher, list[Path]]:
    seen: list[Path] = []

    def factory(path: Path) -> FakeClient:
        seen.append(path)
        return FakeClient(spreadsheet)

    return SheetsPublisher(KEY_FILE, SHEET_ID, client_factory=factory), seen


def test_publish_creates_missing_worksheets():
    inputs = sample_brief_inputs()
    expected = tables(inputs)
    ss = FakeSpreadsheet(existing=["Summary"])
    pub, seen = _publisher(ss)
    pub.publish(inputs)
    assert seen == [KEY_FILE]
    assert ss.added == [name for name in expected if name != "Summary"]
    for name in ss.added:
        ws = ss.sheets[name]
        assert ws.rows == max(len(expected[name]), 1)
        assert ws.cols >= max(len(expected[name][0]), 1)


def test_publish_clears_and_updates_each_table():
    inputs = sample_brief_inputs()
    expected = tables(inputs)
    ss = FakeSpreadsheet(existing=list(expected))
    pub, _ = _publisher(ss)
    pub.publish(inputs)
    assert ss.added == []
    for name, rows in expected.items():
        calls = ss.sheets[name].calls
        assert [c[0] for c in calls] == ["clear", "update"]
        values, kwargs = calls[1][1], calls[1][2]
        assert kwargs == {"value_input_option": "RAW"}
        assert values == [["" if v is None else v for v in row] for row in rows]


def test_values_are_json_safe(monkeypatch: pytest.MonkeyPatch):
    raw: dict[str, list[list[object]]] = {
        "T": [["a", "b", "c", "d"], [np.float64(0.25), None, np.int64(3), True]],
    }
    monkeypatch.setattr(sheets_mod, "tables", lambda _inputs: raw)
    ss = FakeSpreadsheet(existing=[])
    pub, _ = _publisher(ss)
    pub.publish(sample_brief_inputs())
    values = ss.sheets["T"].calls[1][1]
    assert values == [["a", "b", "c", "d"], [0.25, "", 3, True]]
    row = values[1]
    assert type(row[0]) is float
    assert type(row[2]) is int
    assert type(row[3]) is bool


def test_missing_gspread_gives_clear_error(monkeypatch: pytest.MonkeyPatch):
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "gspread" or name.startswith("gspread."):
            raise ImportError("No module named 'gspread'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    pub = SheetsPublisher(KEY_FILE, SHEET_ID)
    expected = "gspread not installed; install with `uv sync --extra sheets`"
    with pytest.raises(ReportError, match=re.escape(expected)):
        pub.publish(sample_brief_inputs())


def test_returns_url():
    ss = FakeSpreadsheet(existing=[])
    pub, _ = _publisher(ss)
    assert (
        pub.publish(sample_brief_inputs()) == f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    )


def test_client_failure_names_sheet():
    class BrokenWorksheet(FakeWorksheet):
        def update(self, values: list[list[object]], **kwargs: Any) -> None:
            raise RuntimeError("quota exceeded")

    ss = FakeSpreadsheet(existing=[])
    ss.sheets["Momentum"] = BrokenWorksheet("Momentum")
    pub, _ = _publisher(ss)
    with pytest.raises(ReportError, match="Momentum.*quota exceeded"):
        pub.publish(sample_brief_inputs())


def test_open_failure_wrapped():
    def factory(path: Path) -> Any:
        raise FileNotFoundError(str(path))

    pub = SheetsPublisher(KEY_FILE, SHEET_ID, client_factory=factory)
    with pytest.raises(ReportError, match=SHEET_ID):
        pub.publish(sample_brief_inputs())
