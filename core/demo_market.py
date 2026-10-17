"""Synthetic candles for the demo account, so the review chart works offline.

The price path is a random walk pinned to every fill of the instrument: between two
fills it is a Brownian bridge, so each fill's price is hit exactly at its minute. The
noise for a segment is seeded from the instrument and the segment's start, so any
window or interval sees the same path. These candles are made up and are only used
for accounts marked as demo (the sample data).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.importer import load_specs
from core.instruments import AssetClass, default_chart_symbol, resolve
from core.market_data import BAR_COLUMNS, INTERVALS, from_utc, resample, to_utc
from core.models import Account, Execution, Trade

MINUTE_VOL = 0.00035  # ~1.3% a day


def _rng(*key: object) -> np.random.Generator:
    digest = hashlib.sha256("|".join(map(str, key)).encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "little"))


def _minute(t: datetime) -> int:
    return int(t.replace(tzinfo=UTC).timestamp()) // 60


def _walk(symbol: str, key: object, n: int, scale: float) -> np.ndarray:
    steps = _rng(symbol, key).normal(0.0, scale, n)
    return np.concatenate([[0.0], np.cumsum(steps)])


def minute_path(
    symbol: str, anchors: Sequence[tuple[datetime, float]], start: datetime, end: datetime
) -> pd.Series:
    """Price at each UTC minute in [start, end], pinned to ``anchors`` (UTC time, price)."""
    m0, m1 = _minute(start), _minute(end)
    points = sorted({_minute(t): p for t, p in anchors}.items())
    if not points:
        raise ValueError("No fills to build demo candles from")
    scale = points[0][1] * MINUTE_VOL
    minutes = np.arange(m0, m1 + 1)
    values = np.empty(len(minutes))

    first_m, first_p = points[0]
    last_m, last_p = points[-1]
    # Before the first fill: walk backwards from it. After the last: walk forwards.
    pre = minutes < first_m
    if pre.any():
        back = _walk(symbol, ("pre", first_m), first_m - int(minutes[pre][0]), scale)
        values[pre] = first_p + back[first_m - minutes[pre]]  # index = minutes before fill
    post = minutes > last_m
    if post.any():
        fwd = _walk(symbol, ("post", last_m), int(minutes[post][-1]) - last_m, scale)
        values[post] = last_p + fwd[minutes[post] - last_m]
    for (a_m, a_p), (b_m, b_p) in pairwise(points):
        seg = (minutes >= a_m) & (minutes <= b_m)
        if not seg.any():
            continue
        n = b_m - a_m
        w = _walk(symbol, ("seg", a_m), n, scale)
        k = minutes[seg] - a_m
        frac = k / n if n else 0.0
        values[seg] = a_p + (b_p - a_p) * frac + w[k] - frac * w[-1]
    for m_, p_ in points:  # exact at the fills
        if m0 <= m_ <= m1:
            values[m_ - m0] = p_
    index = pd.to_datetime(minutes * 60, unit="s")
    return pd.Series(np.maximum(values, scale), index=index)


def _in_session(times: pd.DatetimeIndex, asset_class: AssetClass) -> np.ndarray:
    if asset_class is AssetClass.CRYPTO:
        return np.ones(len(times), dtype=bool)
    if asset_class is AssetClass.STOCK:
        local = pd.DatetimeIndex([from_utc(t.to_pydatetime(), "America/New_York") for t in times])
        minutes = local.hour * 60 + local.minute
        return (local.weekday < 5) & (minutes >= 570) & (minutes < 960)
    return times.weekday < 5


def synthetic_bars(
    symbol: str,
    asset_class: AssetClass,
    anchors: Sequence[tuple[datetime, float]],
    interval: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Demo OHLC bars (UTC) for ``symbol`` between ``start`` and ``end``."""
    path = minute_path(symbol, anchors, start, end)
    path = path[_in_session(path.index, asset_class)]
    if path.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    one_minute = pd.DataFrame(
        {"time": path.index, "open": path.values, "high": path.values, "low": path.values,
         "close": path.values, "volume": np.nan}
    )  # fmt: skip
    bars = resample(one_minute, interval) if interval != "1m" else one_minute
    # Each candle opens where the previous one closed, so candles have real bodies.
    bars["open"] = bars["close"].shift(1).fillna(bars["open"])
    bars["high"] = bars[["high", "open"]].max(axis=1)
    bars["low"] = bars[["low", "open"]].min(axis=1)
    # Small wicks so candles do not look like a line chart (fills stay inside the range).
    # Seeded by bar time, so every window shows the same candles.
    epoch = ((bars["time"] - pd.Timestamp(0)) // pd.Timedelta(seconds=1)).to_numpy()
    unit = path.iloc[0] * MINUTE_VOL
    bars["high"] = bars["high"] + ((epoch * 2654435761) % 2**32) / 2**32 * unit
    bars["low"] = bars["low"] - ((epoch * 40503 + 12345) % 2**16) / 2**16 * unit
    bars["volume"] = None
    step = timedelta(seconds=INTERVALS[interval])
    return bars[bars["time"] >= start - step].reset_index(drop=True)


def demo_fetcher(session: Session) -> Callable[[str, str, datetime, datetime], pd.DataFrame]:
    """A market-data fetcher that builds demo candles from the demo accounts' fills."""
    specs = load_specs(session)
    rows = session.execute(
        select(Execution.timestamp, Execution.price, Trade.symbol, Account.timezone)
        .join(Trade, Execution.trade_id == Trade.id)
        .join(Account, Trade.account_id == Account.id)
        .where(Account.is_demo.is_(True))
    ).all()

    def fetch(ticker: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        anchors = []
        asset_class = AssetClass.STOCK
        for ts, price, symbol, tz in rows:
            spec = resolve(symbol, specs)
            if default_chart_symbol(spec) == ticker:
                anchors.append((to_utc(ts, tz), price))
                asset_class = spec.asset_class
        if not anchors:
            return pd.DataFrame(columns=BAR_COLUMNS)
        return synthetic_bars(ticker, asset_class, anchors, interval, start, end)

    fetch.source = "demo"  # type: ignore[attr-defined]
    return fetch
