from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from core.market_data import BAR_COLUMNS
from core.review import ExecutionPoint, chart_payload, excursions, planned_rr, price_precision

T0 = datetime(2026, 9, 17, 13, 50)  # UTC


def bars_from(closes: list[float], start: datetime = T0, highs=None, lows=None) -> pd.DataFrame:
    times = [start + timedelta(minutes=i) for i in range(len(closes))]
    return pd.DataFrame(
        {"time": pd.to_datetime(times), "open": closes,
         "high": highs or [c + 0.5 for c in closes], "low": lows or [c - 0.5 for c in closes],
         "close": closes, "volume": [None] * len(closes)},
        columns=BAR_COLUMNS,
    )  # fmt: skip


def test_excursions_long() -> None:
    bars = bars_from([100, 101, 104, 99, 102, 110])
    ex = excursions(
        bars, side="long", entry_utc=T0 + timedelta(minutes=1),
        exit_utc=T0 + timedelta(minutes=4), interval="1m", avg_entry=101, avg_exit=102,
        quantity=10, point_value=2, stop_loss=99,
    )  # fmt: skip
    assert ex is not None
    # bars 1-4 overlap the trade: high 104.5, low 98.5 (the 110 bar is after the exit)
    assert ex.mfe_price == pytest.approx(3.5)
    assert ex.mae_price == pytest.approx(2.5)
    assert ex.mfe_usd == pytest.approx(70)
    assert ex.mfe_r == pytest.approx(1.75)
    assert ex.mae_r == pytest.approx(1.25)
    assert ex.capture == pytest.approx(1 / 3.5)


def test_excursions_short_and_without_stop() -> None:
    bars = bars_from([100, 98, 95, 97])
    ex = excursions(
        bars, side="short", entry_utc=T0, exit_utc=T0 + timedelta(minutes=3), interval="1m",
        avg_entry=100, avg_exit=97, quantity=1,
    )  # fmt: skip
    assert ex is not None
    assert (ex.mfe_price, ex.mae_price) == (pytest.approx(5.5), pytest.approx(0.5))
    assert ex.mfe_r is None and ex.mae_r is None
    assert ex.best_price == 94.5


def test_excursions_without_bars() -> None:
    empty = pd.DataFrame(columns=BAR_COLUMNS)
    assert excursions(empty, side="long", entry_utc=T0, exit_utc=None, interval="1m",
                      avg_entry=1, avg_exit=None, quantity=1) is None  # fmt: skip


def test_planned_rr() -> None:
    assert planned_rr("long", 100, 98, 106) == pytest.approx(3)
    assert planned_rr("short", 100, 101, 98) == pytest.approx(2)
    assert planned_rr("long", 100, None, 106) is None
    assert planned_rr("long", 100, 98, None) is None


@pytest.mark.parametrize(("price", "decimals"), [(1.16523, 5), (147.512, 3), (4350.2, 2)])
def test_price_precision(price: float, decimals: int) -> None:
    assert price_precision(price) == decimals


def test_chart_payload_markers_lines_and_local_time_axis() -> None:
    bars = bars_from([100, 101, 102, 103, 104])
    tz = "America/New_York"  # T0 13:50 UTC = 09:50 EDT
    fills = [
        ExecutionPoint(datetime(2026, 9, 17, 9, 51, 20), "buy", 10, 101.2),  # local time
        ExecutionPoint(datetime(2026, 9, 17, 9, 53, 5), "sell", 10, 103.1),
        ExecutionPoint(datetime(2026, 9, 18, 9, 0), "sell", 1, 1),  # outside the bars
    ]
    payload = chart_payload(
        bars, interval="1m", tz_name=tz, side="long", entry_time=fills[0].time,
        exit_time=fills[1].time, executions=fills, avg_entry=101.2, avg_exit=103.1,
        stop_loss=99.5, take_profit=106,
    )  # fmt: skip
    first = payload["candles"][0]["time"]
    assert first == int(pd.Timestamp("2026-09-17 09:50", tz="UTC").timestamp())  # local wall
    assert [m["time"] - first for m in payload["markers"]] == [60, 180]
    assert payload["markers"][0]["shape"] == "arrowUp"
    assert payload["markers"][0]["text"].startswith("Entry B 10")
    assert payload["markers"][1]["text"].startswith("Exit S 10")
    assert [line["title"] for line in payload["lines"]] == ["Entry", "Exit", "Stop", "Target"]
    assert payload["volume"] == []  # no volume data
    assert payload["precision"] == 3
    assert payload["visible"]["from"] < first + 60 < payload["visible"]["to"]


def test_capture_only_for_winners() -> None:
    bars = bars_from([100, 102, 99])
    ex = excursions(
        bars, side="long", entry_utc=T0, exit_utc=T0 + timedelta(minutes=2), interval="1m",
        avg_entry=100, avg_exit=99.5, quantity=1,
    )  # fmt: skip
    assert ex is not None and ex.capture is None
