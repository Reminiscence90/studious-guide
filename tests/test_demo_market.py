from datetime import datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from core.demo_market import demo_fetcher, synthetic_bars
from core.instruments import AssetClass
from core.market_data import get_bars
from core.seed import seed

FILLS = [(datetime(2026, 9, 17, 14, 0, 30), 430.0), (datetime(2026, 9, 17, 15, 10), 436.2)]


def test_fills_are_inside_their_candles() -> None:
    for interval in ("1m", "5m", "1h"):
        bars = synthetic_bars("TSLA", AssetClass.STOCK, FILLS, interval,
                              datetime(2026, 9, 17, 12), datetime(2026, 9, 17, 21))  # fmt: skip
        for t, price in FILLS:
            bar = bars[bars["time"] <= t].iloc[-1]
            assert bar.low <= price <= bar.high


def test_same_candles_in_any_window() -> None:
    wide = synthetic_bars("TSLA", AssetClass.STOCK, FILLS, "1m",
                          datetime(2026, 9, 17, 12), datetime(2026, 9, 17, 21))  # fmt: skip
    narrow = synthetic_bars("TSLA", AssetClass.STOCK, FILLS, "1m",
                            datetime(2026, 9, 17, 14), datetime(2026, 9, 17, 16))  # fmt: skip
    merged = wide.merge(narrow, on="time")
    assert len(merged) > 100
    assert (merged.close_x - merged.close_y).abs().max() == 0


def test_stocks_follow_regular_trading_hours() -> None:
    bars = synthetic_bars("TSLA", AssetClass.STOCK, FILLS, "1m",
                          datetime(2026, 9, 17, 0), datetime(2026, 9, 18, 0))  # fmt: skip
    assert bars["time"].min() == datetime(2026, 9, 17, 13, 30)  # 09:30 New York
    assert bars["time"].max() == datetime(2026, 9, 17, 19, 59)
    crypto = synthetic_bars("BTCUSD", AssetClass.CRYPTO, FILLS, "1h",
                            datetime(2026, 9, 19), datetime(2026, 9, 20))  # Saturday  # fmt: skip
    assert len(crypto) == 25


def test_demo_fetcher_uses_seeded_fills(engine: Engine) -> None:
    seed(engine)
    with Session(engine) as s:
        fetch = demo_fetcher(s)
        bars, source = get_bars(s, "TSLA", "5m", datetime(2026, 9, 17), datetime(2026, 9, 18),
                                fetch=fetch)  # fmt: skip
    assert source == "demo"
    assert not bars.empty
