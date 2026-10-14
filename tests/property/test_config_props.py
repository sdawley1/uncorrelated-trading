from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given, settings
from hypothesis import strategies as st

from uncorrelated_trading.config import AppConfig

TICKERS = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=5)


@settings(max_examples=50, deadline=None)
@given(symbols=st.lists(TICKERS, min_size=2, max_size=200, unique=True))
def test_symbols_round_trip_in_order(symbols):
    quoted = ",".join(f'"{s}"' for s in symbols)
    text = f"[universe]\nsymbols = [{quoted}]\nexcluded_patterns = []\n"
    with TemporaryDirectory() as d:
        p = Path(d) / "c.toml"
        p.write_text(text)
        cfg = AppConfig.load(p)
    assert cfg.universe.symbols == tuple(symbols)
    assert cfg.universe.excluded_patterns == ()
