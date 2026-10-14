from factories import sample_brief_inputs
from uncorrelated_trading.report.inputs import holdings_rows


def test_holdings_rows_join_momentum_and_largest_set():
    rows = holdings_rows(sample_brief_inputs())
    assert [r["symbol"] for r in rows] == ["AAA", "XYZ"]
    aaa, xyz = rows
    assert (
        aaa["momentum_rank"] == 2 and aaa["direction"] == "flat" and aaa["in_largest_set"] is True
    )
    assert (
        xyz["momentum_rank"] is None and xyz["direction"] == "" and xyz["in_largest_set"] is False
    )
