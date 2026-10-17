from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from core import market_data as md
from core.instruments import AssetClass, InstrumentSpec, default_chart_symbol, default_specs

NOW = datetime(2026, 10, 17, 12, 0)


def yahoo_payload(start: datetime, n: int, step: int = 60, price: float = 100.0) -> dict:
    ts = [int(pd.Timestamp(start + timedelta(seconds=i * step), tz="UTC").timestamp())
          for i in range(n)]  # fmt: skip
    closes = [price + i for i in range(n)]
    return {
        "chart": {
            "result": [
                {
                    "timestamp": ts,
                    "indicators": {
                        "quote": [
                            {
                                "open": closes,
                                "high": [c + 0.5 for c in closes],
                                "low": [c - 0.5 for c in closes],
                                "close": closes,
                                "volume": [1000] * n,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


# --------------------------------------------------------------------------- symbols


@pytest.mark.parametrize(
    ("symbol", "ticker"),
    [
        ("TSLA", "TSLA"),
        ("ES", "ES=F"),
        ("MNQ", "MNQ=F"),
        ("CL", "CL=F"),
        ("GC", "GC=F"),
        ("XAUUSD", "GC=F"),
        ("USOIL", "CL=F"),
        ("EURUSD", "EURUSD=X"),
        ("USDJPY", "USDJPY=X"),
        ("BTCUSD", "BTC-USD"),
        ("ETHUSD", "ETH-USD"),
    ],
)
def test_default_chart_symbol(symbol: str, ticker: str) -> None:
    assert default_chart_symbol(default_specs()[symbol]) == ticker


def test_chart_symbol_override_wins() -> None:
    spec = InstrumentSpec("XAUUSD", AssetClass.COMMODITY, 100, chart_symbol="XAUUSD=X")
    assert default_chart_symbol(spec) == "XAUUSD=X"


# --------------------------------------------------------------------------- time


def test_time_zone_round_trip() -> None:
    local = datetime(2026, 9, 17, 9, 30)  # New York, EDT (UTC-4)
    utc = md.to_utc(local, "America/New_York")
    assert utc == datetime(2026, 9, 17, 13, 30)
    assert md.from_utc(utc, "America/New_York") == local
    assert md.to_utc(local, "Asia/Kuala_Lumpur") == datetime(2026, 9, 17, 1, 30)
    assert md.to_utc(local, "Not/AZone") == local  # unknown zones fall back to UTC


def test_floor_time() -> None:
    t = datetime(2026, 9, 17, 13, 47, 31)
    assert md.floor_time(t, "1m") == datetime(2026, 9, 17, 13, 47)
    assert md.floor_time(t, "15m") == datetime(2026, 9, 17, 13, 45)
    assert md.floor_time(t, "4h") == datetime(2026, 9, 17, 12, 0)
    assert md.floor_time(t, "1d") == datetime(2026, 9, 17)


# --------------------------------------------------------------------------- intervals


def test_auto_interval_follows_holding_time_and_history() -> None:
    entry = NOW - timedelta(days=2)
    assert md.auto_interval(entry, entry + timedelta(minutes=20), NOW) == "1m"
    assert md.auto_interval(entry, entry + timedelta(hours=10), NOW) == "5m"
    assert md.auto_interval(entry, entry + timedelta(days=3), NOW) == "30m"
    old = NOW - timedelta(days=45)  # Yahoo has no 1m bars that far back
    assert md.auto_interval(old, old + timedelta(minutes=20), NOW) == "5m"
    older = NOW - timedelta(days=120)
    assert md.auto_interval(older, older + timedelta(minutes=20), NOW) == "1h"


def test_auto_interval_without_history_limits_or_with_local_candles() -> None:
    older = NOW - timedelta(days=120)
    exit_ = older + timedelta(minutes=20)
    assert md.auto_interval(older, exit_, NOW, history_limits=False) == "1m"
    assert md.auto_interval(older, exit_, NOW, local_intervals=["5m"]) == "5m"


def test_review_window_pads_the_trade() -> None:
    entry, exit_ = NOW - timedelta(days=1), NOW - timedelta(days=1) + timedelta(minutes=30)
    start, end = md.review_window(entry, exit_, "1m", NOW)
    assert start == entry - timedelta(minutes=60)
    assert end == exit_ + timedelta(minutes=60)
    _, end_open = md.review_window(entry, None, "1m", NOW)
    assert end_open == NOW  # open trades run to now


def test_infer_interval_and_resample() -> None:
    times = pd.date_range("2026-09-17 13:30", periods=60, freq="1min")
    assert md.infer_interval(times) == "1m"
    assert md.infer_interval(pd.date_range("2026-09-17", periods=10, freq="4h")) == "4h"
    bars = pd.DataFrame(
        {"time": times, "open": range(60), "high": [x + 1 for x in range(60)],
         "low": [x - 1 for x in range(60)], "close": [x + 0.5 for x in range(60)],
         "volume": [10] * 60}
    )  # fmt: skip
    five = md.resample(bars, "5m")
    assert len(five) == 12
    first = five.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (0, 5, -1, 4.5, 50)


# --------------------------------------------------------------------------- Yahoo


def test_parse_and_fetch_yahoo() -> None:
    start = datetime(2026, 10, 15, 13, 30)
    urls: list[str] = []

    def fake(url: str) -> dict:
        urls.append(url)
        return yahoo_payload(start, 5)

    bars = md.fetch_yahoo("ES=F", "1m", start, start + timedelta(minutes=5), get_json=fake)
    assert list(bars.columns) == md.BAR_COLUMNS
    assert bars["time"].iloc[0] == start
    assert bars["close"].tolist() == [100, 101, 102, 103, 104]
    assert "ES%3DF" in urls[0] and "interval=1m" in urls[0]


def test_fetch_yahoo_splits_long_1m_ranges() -> None:
    calls: list[str] = []
    start = datetime(2026, 9, 20)
    md.fetch_yahoo("TSLA", "1m", start, start + timedelta(days=15),
                   get_json=lambda u: calls.append(u) or yahoo_payload(start, 1))  # fmt: skip
    assert len(calls) == 3  # 7-day chunks


def test_fetch_yahoo_4h_is_built_from_1h() -> None:
    start = datetime(2026, 10, 15)
    seen: list[str] = []
    bars = md.fetch_yahoo(
        "GC=F", "4h", start, start + timedelta(hours=8),
        get_json=lambda u: seen.append(u) or yahoo_payload(start, 8, step=3600),
    )  # fmt: skip
    assert "interval=60m" in seen[0]
    assert len(bars) == 2


def test_yahoo_error_is_reported() -> None:
    def fake(_url: str) -> dict:
        return {"chart": {"result": None, "error": {"code": "Not Found",
                                                     "description": "No data found"}}}  # fmt: skip

    with pytest.raises(md.MarketDataError, match="No data found"):
        md.fetch_yahoo("NOPE", "1d", NOW - timedelta(days=5), NOW, get_json=fake)


# --------------------------------------------------------------------------- CSV upload


def test_parse_tradingview_export_unix_seconds() -> None:
    df = pd.DataFrame(
        {"time": [1789392600, 1789392660], "open": [1, 2], "high": [2, 3], "low": [0.5, 1.5],
         "close": [1.5, 2.5], "Volume": [10, 20]}
    )  # fmt: skip
    bars = md.parse_ohlc_csv(df)
    assert bars["time"].iloc[0] == pd.Timestamp(1789392600, unit="s")
    assert bars["volume"].tolist() == [10, 20]


def test_parse_text_times_in_given_zone() -> None:
    df = pd.DataFrame({"Date": ["2026.09.17 21:00", "2026.09.17 21:05"], "Open": [1, 2],
                       "High": [2, 3], "Low": [0, 1], "Close": [1.5, 2.5]})  # fmt: skip
    bars = md.parse_ohlc_csv(df, "Asia/Kuala_Lumpur")
    assert bars["time"].iloc[0] == datetime(2026, 9, 17, 13, 0)
    assert bars["volume"].isna().all()


def test_parse_rejects_missing_columns() -> None:
    with pytest.raises(md.MarketDataError, match="missing: close"):
        md.parse_ohlc_csv(pd.DataFrame({"time": [1], "open": [1], "high": [1], "low": [1]}))


# --------------------------------------------------------------------------- cache


def test_get_bars_downloads_once_then_uses_cache(session: Session) -> None:
    start = datetime(2026, 10, 14, 13, 30)
    end = start + timedelta(minutes=30)
    calls: list[tuple] = []

    def fetch(ticker: str, interval: str, s: datetime, e: datetime) -> pd.DataFrame:
        calls.append((ticker, interval))
        return md.parse_yahoo_chart(yahoo_payload(s, int((e - s).total_seconds() // 60) + 1))

    bars, source = md.get_bars(session, "TSLA", "1m", start, end, fetch=fetch)
    assert (len(bars), source) == (31, "yahoo")
    again, _ = md.get_bars(session, "TSLA", "1m", start, end, fetch=fetch)
    assert len(again) == 31
    assert calls == [("TSLA", "1m")]


def test_uploaded_candles_are_used_and_resampled(session: Session) -> None:
    times = pd.date_range("2026-05-15 07:00", periods=120, freq="1min")
    epoch = (times - pd.Timestamp(0)) // pd.Timedelta(seconds=1)
    df = pd.DataFrame({"time": epoch, "open": 1.0, "high": 1.1,
                       "low": 0.9, "close": 1.0})  # fmt: skip
    interval, n = md.import_ohlc(session, "USDJPY=X", df)
    assert (interval, n) == ("1m", 120)

    def never(*_args: object) -> pd.DataFrame:
        raise AssertionError("uploaded candles must not trigger a download")

    bars, source = md.get_bars(
        session, "USDJPY=X", "15m", datetime(2026, 5, 15, 7), datetime(2026, 5, 15, 9), never
    )
    assert source == "csv"
    assert len(bars) == 8  # 120 one-minute bars → 8 fifteen-minute bars
    assert md.local_intervals(
        session, "USDJPY=X", datetime(2026, 5, 15, 7), datetime(2026, 5, 15, 8)
    ) == ["1m"]


def test_get_bars_without_fetch_returns_cache_only(session: Session) -> None:
    bars, source = md.get_bars(session, "NONE", "1h", NOW - timedelta(days=1), NOW, fetch=None)
    assert bars.empty and source is None
