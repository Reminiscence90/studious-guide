"""Trade review: excursions (MFE / MAE), planned R:R and the chart payload.

All functions are pure. Bars use naive UTC open times; trade and execution times are
naive local times in the account's time zone (as imported from the broker).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from core.market_data import INTERVALS, floor_time, from_utc, to_utc

LONG, SHORT = "long", "short"


@dataclass(frozen=True)
class ExecutionPoint:
    time: datetime  # local (account time zone)
    side: str  # "buy" / "sell"
    quantity: float
    price: float


@dataclass(frozen=True)
class Excursions:
    """How far price moved for (MFE) and against (MAE) the position while it was open.

    Distances are positive price amounts measured from the average entry; ``*_usd`` use
    the trade's quantity and point value, ``*_r`` the initial risk (if a stop is set).
    ``capture`` is the share of the favourable move that a winning exit kept (None for
    losing or open trades).
    """

    mfe_price: float
    mae_price: float
    mfe_usd: float
    mae_usd: float
    mfe_r: float | None
    mae_r: float | None
    capture: float | None
    best_price: float
    worst_price: float


def excursions(
    bars: pd.DataFrame,
    *,
    side: str,
    entry_utc: datetime,
    exit_utc: datetime | None,
    interval: str,
    avg_entry: float,
    avg_exit: float | None,
    quantity: float,
    point_value: float = 1.0,
    stop_loss: float | None = None,
) -> Excursions | None:
    """MFE / MAE from the bars that overlap the holding period (None without bars).

    Bar data only has highs and lows, so the entry and exit bars count in full: the
    figures are the most the trade could have seen, at bar resolution.
    """
    if bars.empty:
        return None
    end = exit_utc or bars["time"].iloc[-1]
    during = bars[(bars["time"] >= floor_time(entry_utc, interval)) & (bars["time"] <= end)]
    if during.empty:
        return None
    high, low = float(during["high"].max()), float(during["low"].min())
    if side == LONG:
        mfe, mae, best, worst = high - avg_entry, avg_entry - low, high, low
        realised = (avg_exit - avg_entry) if avg_exit is not None else None
    else:
        mfe, mae, best, worst = avg_entry - low, high - avg_entry, low, high
        realised = (avg_entry - avg_exit) if avg_exit is not None else None
    mfe, mae = max(mfe, 0.0), max(mae, 0.0)
    risk = abs(avg_entry - stop_loss) if stop_loss else None
    usd = quantity * point_value
    return Excursions(
        mfe_price=mfe,
        mae_price=mae,
        mfe_usd=mfe * usd,
        mae_usd=mae * usd,
        mfe_r=mfe / risk if risk else None,
        mae_r=mae / risk if risk else None,
        capture=(realised / mfe) if realised is not None and realised > 0 and mfe > 0 else None,
        best_price=best,
        worst_price=worst,
    )


def planned_rr(side: str, entry: float, stop: float | None, target: float | None) -> float | None:
    """Reward-to-risk of the plan: |target − entry| / |entry − stop|."""
    if stop is None or target is None or entry == stop:
        return None
    reward = (target - entry) if side == LONG else (entry - target)
    return reward / abs(entry - stop)


def price_precision(price: float) -> int:
    """Decimals to show: 5 for FX majors, 3 for JPY pairs/low-priced, 2 otherwise."""
    if price < 10:
        return 5
    if price < 200:
        return 3
    return 2


def _display_epoch(utc: datetime, tz_name: str | None) -> int:
    """Seconds for the chart's time axis. Lightweight Charts shows times as UTC, so we
    pass the local wall-clock time to make the axis read in the account's time zone."""
    return int(from_utc(utc, tz_name).replace(tzinfo=UTC).timestamp())


def chart_payload(
    bars: pd.DataFrame,
    *,
    interval: str,
    tz_name: str | None,
    side: str,
    entry_time: datetime,
    exit_time: datetime | None,
    executions: list[ExecutionPoint],
    avg_entry: float,
    avg_exit: float | None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict[str, Any]:
    """Data for the Lightweight Charts component: candles, volume, fill markers, price
    lines and the visible range around the trade."""
    candles = [
        {
            "time": _display_epoch(r.time.to_pydatetime(), tz_name),
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
        }
        for r in bars.itertuples()
    ]
    volume = [
        {
            "time": c["time"],
            "value": float(v),
            "color": "rgba(27,175,122,0.35)" if c["close"] >= c["open"] else "rgba(227,73,72,0.35)",
        }
        for c, v in zip(candles, bars["volume"], strict=True)
        if v is not None and not pd.isna(v)
    ]
    if not any(v["value"] > 0 for v in volume):
        volume = []

    bar_times = {c["time"] for c in candles}
    entry_side = "buy" if side == LONG else "sell"
    markers = []
    for ex in sorted(executions, key=lambda e: e.time):
        bar_utc = floor_time(to_utc(ex.time, tz_name), interval)
        t = _display_epoch(bar_utc, tz_name)
        if t not in bar_times:
            continue  # fill outside the loaded candles
        is_buy = ex.side == "buy"
        markers.append(
            {
                "time": t,
                "position": "belowBar" if is_buy else "aboveBar",
                "shape": "arrowUp" if is_buy else "arrowDown",
                "color": "#2a78d6" if is_buy else "#eb6834",
                "text": f"{'Entry' if ex.side == entry_side else 'Exit'} "
                f"{'B' if is_buy else 'S'} {ex.quantity:g} @ {ex.price:g}",
            }
        )

    lines = [{"price": avg_entry, "color": "#2a78d6", "title": "Entry", "style": 0}]
    if avg_exit is not None:
        lines.append({"price": avg_exit, "color": "#7a7a7a", "title": "Exit", "style": 2})
    if stop_loss:
        lines.append({"price": stop_loss, "color": "#e34948", "title": "Stop", "style": 2})
    if take_profit:
        lines.append({"price": take_profit, "color": "#1baf7a", "title": "Target", "style": 2})

    step = timedelta(seconds=INTERVALS[interval])
    entry_utc = to_utc(entry_time, tz_name)
    exit_utc = to_utc(exit_time, tz_name) if exit_time else None
    span = (exit_utc or entry_utc) - entry_utc
    pad = max(step * 25, span * 0.35)
    visible = {
        "from": _display_epoch(entry_utc - pad, tz_name),
        "to": _display_epoch((exit_utc or entry_utc) + pad, tz_name),
    }
    decimals = price_precision(avg_entry)
    return {
        "candles": candles,
        "volume": volume,
        "markers": markers,
        "lines": lines,
        "visible": visible,
        "precision": decimals,
        "minMove": 10**-decimals,
        "intraday": INTERVALS[interval] < 86400,
    }
