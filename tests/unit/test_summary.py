"""Tests for the text and HTML brief renderers."""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime
from pathlib import Path

from factories import sample_brief_inputs
from uncorrelated_trading.models import PortfolioContext
from uncorrelated_trading.report import summary
from uncorrelated_trading.report.summary import (
    BriefInputs,
    holdings_rows,
    render_html,
    render_text,
    subject_line,
)

GOLDEN = Path(__file__).resolve().parent.parent / "golden"


def _with_result(inputs: BriefInputs, **changes: object) -> BriefInputs:
    return dataclasses.replace(inputs, result=dataclasses.replace(inputs.result, **changes))


def test_reexports_inputs() -> None:
    assert summary.BriefInputs is BriefInputs
    assert holdings_rows(sample_brief_inputs())[0]["symbol"] == "AAA"


def test_text_matches_golden() -> None:
    expected = (GOLDEN / "brief.txt").read_text(encoding="utf-8")
    assert render_text(sample_brief_inputs()) == expected


def test_html_matches_golden() -> None:
    expected = (GOLDEN / "brief.html").read_text(encoding="utf-8")
    assert render_html(sample_brief_inputs()) == expected


def test_subject_line_format() -> None:
    assert subject_line(sample_brief_inputs()) == (
        "[uncorrelated-trading] Brief for 2026-10-13 (data through 2026-10-13, post_close)"
    )


def test_subject_line_uses_new_york_send_day() -> None:
    # 03:00 UTC on 14 Oct is still 13 Oct in New York.
    inputs = _with_result(
        sample_brief_inputs(), created_at=datetime(2026, 10, 14, 3, 0, tzinfo=UTC)
    )
    assert "Brief for 2026-10-13 " in subject_line(inputs)
    inputs = _with_result(
        sample_brief_inputs(), created_at=datetime(2026, 10, 14, 11, 0, tzinfo=UTC)
    )
    assert "Brief for 2026-10-14 " in subject_line(inputs)


def test_html_has_no_external_urls() -> None:
    html = render_html(sample_brief_inputs())
    assert re.search(r"https?://", html) is None
    assert "<script" not in html.lower()
    assert html.count("<html") == 1
    assert "<table" in html


def test_holdings_section_shows_error_when_account_unavailable() -> None:
    inputs = dataclasses.replace(
        sample_brief_inputs(),
        portfolio=PortfolioContext(account=None, positions=(), error="401 unauthorized <key>"),
    )
    text = render_text(inputs)
    assert "Account unavailable: 401 unauthorized <key>" in text
    assert "XYZ" not in text
    html = render_html(inputs)
    assert "Account unavailable: 401 unauthorized &lt;key&gt;" in html


def test_changes_line_lists_added_and_removed_symbols() -> None:
    text = render_text(sample_brief_inputs(previous_largest=("AAA", "CCC", "DDD")))
    assert "Changes since last run: largest set added BBB; removed CCC" in text
    text = render_text(sample_brief_inputs(previous_largest=("AAA", "BBB", "DDD")))
    assert "Changes since last run: largest set unchanged" in text
    text = render_text(sample_brief_inputs(previous_largest=None))
    assert "Changes since last run: no previous run" in text


def test_text_lines_within_width() -> None:
    many = tuple(f"SYM{i:03d}" for i in range(60))
    base = sample_brief_inputs(previous_largest=many[::2])
    largest = dataclasses.replace(base.result.largest_set, symbols=many, size=len(many))
    inputs = _with_result(base, largest_set=largest)
    for text in (render_text(sample_brief_inputs()), render_text(inputs)):
        assert max(len(line) for line in text.splitlines()) <= 100


def test_provisional_flag_in_header() -> None:
    base = sample_brief_inputs()
    inputs = _with_result(base, as_of=dataclasses.replace(base.result.as_of, provisional=True))
    text = render_text(inputs)
    header = "\n".join(text.splitlines()[:6])
    assert "PROVISIONAL" in header
    assert "PROVISIONAL" in render_html(inputs)
    assert "PROVISIONAL" not in render_text(base)


def test_largest_set_time_capped_label() -> None:
    inputs = _with_result(sample_brief_inputs(), largest_set_exact=False)
    assert "(search time-capped; may not be maximal)" in render_text(inputs)
    assert "(search time-capped; may not be maximal)" in render_html(inputs)


def test_missing_and_empty_sizes_print_none_found() -> None:
    inputs = _with_result(sample_brief_inputs(), sets_by_size={})
    text = render_text(inputs)
    assert "Size 3: none found" in text
    assert "Size 4: none found" in text


def test_footer_contains_parameters_and_caveats() -> None:
    text = " ".join(render_text(sample_brief_inputs()).split())
    assert "tau=0.20" in text
    assert "SE(rho) ~ 0.13" in text
    assert "1.51 standard errors from zero" in text
    assert "dividend" in text
    assert "Advisory output, not investment advice." in text
    assert "0.1.0" in text and "0123456789abcdef0123456789abcdef" in text
