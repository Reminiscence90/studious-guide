"""Trading statistics as pure functions on pandas DataFrames.

Every function takes a DataFrame of trades with (at least) these columns:

* ``net_pnl``     – P&L after fees
* ``entry_time``  – datetime the position was opened
* ``exit_time``   – datetime the position was closed (NaT while open)
* ``status``      – optional; ``"open"`` rows are excluded from all statistics

Definitions
-----------
* **Win / loss**: a trade with ``net_pnl > 0`` / ``< 0``. Break-even trades count
  towards the total but are neither wins nor losses.
* **Win rate**: wins / total closed trades.
* **Gross profit**: sum of winning trades. **Gross loss**: sum of losing trades (≤ 0).
* **Profit factor**: gross profit / |gross loss|. ``inf`` when there are wins but no
  losses; ``None`` when there are no wins and no losses.
* **Average win / average loss**: mean P&L of winners / mean |P&L| of losers.
* **Average win/loss ratio**: average win / average loss.
* **Expectancy** (per trade): win rate × average win − loss rate × average loss.
  This equals the mean net P&L per trade.
* **Max drawdown**: the largest peak-to-trough fall of cumulative net P&L, measured
  from a starting equity of 0, reported as a positive amount.
* **R-multiple**: net P&L / initial risk, where initial risk =
  |entry price − stop loss| × quantity × point value.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import pandas as pd

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# (upper bound in minutes, label); the last bucket is open-ended.
HOLDING_BUCKETS: list[tuple[float, str]] = [
    (1, "< 1 min"),
    (5, "1–5 min"),
    (15, "5–15 min"),
    (60, "15–60 min"),
    (240, "1–4 hours"),
    (24 * 60, "4–24 hours"),
    (7 * 24 * 60, "1–7 days"),
    (math.inf, "> 7 days"),
]
HOLDING_BUCKET_LABELS = [label for _, label in HOLDING_BUCKETS]


# --------------------------------------------------------------------------- basics


def closed_trades(df: pd.DataFrame) -> pd.DataFrame:
    """Drop open positions (``status == "open"`` or no exit time)."""
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if "status" in df.columns:
        mask &= df["status"].astype(str) != "open"
    if "exit_time" in df.columns:
        mask &= df["exit_time"].notna()
    return df[mask]


def _pnl(df: pd.DataFrame) -> pd.Series:
    return closed_trades(df)["net_pnl"].astype(float)


def _pnl_stats(pnl: pd.Series) -> dict[str, Any]:
    """All count/ratio statistics of a series of closed-trade P&L, in one pass."""
    values = pnl.to_numpy(dtype=float)
    n = len(values)
    wins, losses = values[values > 0], values[values < 0]
    gp, gl = float(wins.sum()), float(losses.sum())
    aw = float(wins.mean()) if len(wins) else None
    al = float(-losses.mean()) if len(losses) else None
    wr = len(wins) / n if n else None
    lr = len(losses) / n if n else None
    pf = gp / abs(gl) if gl else (math.inf if gp > 0 else None)
    if aw is None and al is None:
        ratio = None
    elif al is None:
        ratio = math.inf
    elif aw is None:
        ratio = 0.0
    else:
        ratio = aw / al
    exp = None if wr is None or lr is None else wr * (aw or 0.0) - lr * (al or 0.0)
    return {
        "total_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "net_pnl": float(values.sum()),
        "gross_profit": gp,
        "gross_loss": gl,
        "win_rate": wr,
        "loss_rate": lr,
        "profit_factor": pf,
        "avg_win": aw,
        "avg_loss": al,
        "avg_win_loss_ratio": ratio,
        "expectancy": exp,
    }


def _stat(df: pd.DataFrame, name: str) -> Any:
    return _pnl_stats(_pnl(df))[name]


def total_trades(df: pd.DataFrame) -> int:
    return len(closed_trades(df))


def net_pnl(df: pd.DataFrame) -> float:
    return float(_pnl(df).sum())


def gross_profit(df: pd.DataFrame) -> float:
    return _stat(df, "gross_profit")


def gross_loss(df: pd.DataFrame) -> float:
    """Sum of losing trades (a negative number, or 0)."""
    return _stat(df, "gross_loss")


def win_count(df: pd.DataFrame) -> int:
    return _stat(df, "wins")


def loss_count(df: pd.DataFrame) -> int:
    return _stat(df, "losses")


def win_rate(df: pd.DataFrame) -> float | None:
    return _stat(df, "win_rate")


def loss_rate(df: pd.DataFrame) -> float | None:
    return _stat(df, "loss_rate")


def profit_factor(df: pd.DataFrame) -> float | None:
    return _stat(df, "profit_factor")


def avg_win(df: pd.DataFrame) -> float | None:
    return _stat(df, "avg_win")


def avg_loss(df: pd.DataFrame) -> float | None:
    """Average losing trade as a positive amount."""
    return _stat(df, "avg_loss")


def avg_win_loss_ratio(df: pd.DataFrame) -> float | None:
    return _stat(df, "avg_win_loss_ratio")


def expectancy(df: pd.DataFrame) -> float | None:
    """Expected P&L per trade: win rate × avg win − loss rate × avg loss."""
    return _stat(df, "expectancy")


def trades_closed_on(df: pd.DataFrame, day: date) -> pd.DataFrame:
    """Closed trades whose exit falls on ``day``."""
    closed = closed_trades(df)
    return closed if closed.empty else closed[closed["exit_time"].dt.date == day]


# --------------------------------------------------------------------------- time series


def equity_curve(df: pd.DataFrame) -> pd.DataFrame:
    """Cumulative net P&L per closed trade, ordered by exit time.

    Columns: ``exit_time``, ``net_pnl``, ``cumulative_pnl``, ``peak``, ``drawdown``
    (drawdown ≤ 0, relative to the running peak with a starting equity of 0).
    """
    closed = closed_trades(df)
    if closed.empty:
        return pd.DataFrame(columns=["exit_time", "net_pnl", "cumulative_pnl", "peak", "drawdown"])
    curve = closed[["exit_time", "net_pnl"]].sort_values("exit_time", kind="stable")
    curve = curve.reset_index(drop=True)
    curve["net_pnl"] = curve["net_pnl"].astype(float)
    curve["cumulative_pnl"] = curve["net_pnl"].cumsum()
    curve["peak"] = curve["cumulative_pnl"].cummax().clip(lower=0.0)
    curve["drawdown"] = curve["cumulative_pnl"] - curve["peak"]
    return curve


def max_drawdown(df: pd.DataFrame) -> float:
    """Largest peak-to-trough decline of cumulative P&L, as a positive number."""
    curve = equity_curve(df)
    if curve.empty:
        return 0.0
    return float(-curve["drawdown"].min())


def daily_pnl(df: pd.DataFrame) -> pd.DataFrame:
    """Net P&L per day (by exit date).

    Columns: ``date``, ``net_pnl``, ``trades``, ``wins``, ``cumulative_pnl``.
    """
    closed = closed_trades(df)
    cols = ["date", "net_pnl", "trades", "wins", "cumulative_pnl"]
    if closed.empty:
        return pd.DataFrame(columns=cols)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(closed["exit_time"]).dt.date,
            "net_pnl": closed["net_pnl"].astype(float),
            "win": closed["net_pnl"].astype(float) > 0,
        }
    )
    daily = (
        frame.groupby("date")
        .agg(net_pnl=("net_pnl", "sum"), trades=("net_pnl", "size"), wins=("win", "sum"))
        .reset_index()
        .sort_values("date")
    )
    daily["wins"] = daily["wins"].astype(int)
    daily["cumulative_pnl"] = daily["net_pnl"].cumsum()
    return daily[cols].reset_index(drop=True)


def weekly_totals(daily: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a ``daily_pnl`` frame to ISO weeks (week starting Monday)."""
    if daily.empty:
        return pd.DataFrame(columns=["week_start", "net_pnl", "trades"])
    dates = pd.to_datetime(daily["date"])
    week_start = (dates - pd.to_timedelta(dates.dt.weekday, unit="D")).dt.date
    return (
        daily.assign(week_start=week_start)
        .groupby("week_start")
        .agg(net_pnl=("net_pnl", "sum"), trades=("trades", "sum"))
        .reset_index()
    )


# --------------------------------------------------------------------------- per-trade


def r_multiple(
    pnl: float,
    entry_price: float,
    stop_loss: float | None,
    quantity: float,
    point_value: float = 1.0,
) -> float | None:
    """P&L expressed in units of initial risk. None without a (valid) stop loss.

    ``point_value`` is the USD value of a 1.0 price move per unit (contract
    multiplier, forex lot size, ...), so risk is in the same USD terms as P&L.
    """
    if stop_loss is None or pd.isna(stop_loss) or quantity <= 0:
        return None
    risk = abs(entry_price - stop_loss) * quantity * point_value
    if risk == 0:
        return None
    return pnl / risk


def r_multiples(df: pd.DataFrame) -> pd.Series:
    """Vectorised :func:`r_multiple` for a DataFrame with stop_loss/avg_entry_price/quantity
    (and optionally point_value, default 1)."""
    if df.empty:
        return pd.Series(dtype=float)
    pv = df["point_value"] if "point_value" in df.columns else 1.0
    risk = (df["avg_entry_price"] - df["stop_loss"]).abs() * df["quantity"] * pv
    risk = risk.where(risk > 0)
    return (df["net_pnl"] / risk).astype(float)


def holding_minutes(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    delta = pd.to_datetime(df["exit_time"]) - pd.to_datetime(df["entry_time"])
    return delta.dt.total_seconds() / 60.0


def holding_bucket(minutes: float | None) -> str | None:
    if minutes is None or pd.isna(minutes):
        return None
    for upper, label in HOLDING_BUCKETS:
        if minutes < upper:
            return label
    return HOLDING_BUCKETS[-1][1]


def add_dimensions(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived columns used by reports: weekday, hour, month, holding bucket, R."""
    out = df.copy()
    if out.empty:
        for col in ("weekday", "hour", "month", "holding_minutes", "holding_bucket"):
            out[col] = pd.Series(dtype=object)
        return out
    entry = pd.to_datetime(out["entry_time"])
    out["weekday"] = pd.Categorical(entry.dt.day_name(), categories=WEEKDAYS, ordered=True)
    out["hour"] = entry.dt.hour
    out["month"] = pd.to_datetime(out["exit_time"]).dt.to_period("M").astype(str)
    out["holding_minutes"] = holding_minutes(out)
    out["holding_bucket"] = pd.Categorical(
        out["holding_minutes"].map(holding_bucket),
        categories=HOLDING_BUCKET_LABELS,
        ordered=True,
    )
    if {"stop_loss", "avg_entry_price", "quantity"} <= set(out.columns):
        out["r_multiple"] = r_multiples(out)
    return out


# --------------------------------------------------------------------------- summary


@dataclass(frozen=True)
class Stats:
    total_trades: int
    wins: int
    losses: int
    net_pnl: float
    gross_profit: float
    gross_loss: float
    win_rate: float | None
    profit_factor: float | None
    avg_win: float | None
    avg_loss: float | None
    avg_win_loss_ratio: float | None
    expectancy: float | None
    max_drawdown: float

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


def summary_stats(df: pd.DataFrame) -> Stats:
    closed = closed_trades(df)
    stats = _pnl_stats(closed["net_pnl"].astype(float) if not closed.empty else pd.Series())
    fields = {k: v for k, v in stats.items() if k != "loss_rate"}
    return Stats(**fields, max_drawdown=max_drawdown(closed))


# --------------------------------------------------------------------------- journal score

# Each component is scaled to 0–100 and the total is the weighted sum.
SCORE_WEIGHTS: dict[str, float] = {
    "Win rate": 0.20,
    "Profit factor": 0.25,
    "Avg win/loss": 0.20,
    "Consistency": 0.15,
    "Max drawdown": 0.20,
}
WIN_RATE_TARGET = 0.60  # win rate that earns full marks
PROFIT_FACTOR_TARGET = 2.5
WIN_LOSS_RATIO_TARGET = 2.0


def consistency(df: pd.DataFrame) -> float:
    """1 − (best day's profit / sum of all green days' profit), in [0, 1].

    Measures how evenly profits are spread across days: 0 when a single day
    produced all the profit (or there are no green days), approaching 1 when
    many days contribute similar amounts.
    """
    daily = daily_pnl(df)
    green = daily.loc[daily["net_pnl"] > 0, "net_pnl"] if not daily.empty else pd.Series()
    if green.empty:
        return 0.0
    return float(1 - green.max() / green.sum())


def _scaled(value: float | None, target: float) -> float:
    if value is None:
        return 0.0
    if math.isinf(value):
        return 100.0
    return max(0.0, min(value / target, 1.0)) * 100


@dataclass(frozen=True)
class JournalScore:
    total: float
    components: dict[str, float]


def journal_score(df: pd.DataFrame) -> JournalScore:
    """Composite 0–100 score. See the README for the full formula."""
    closed = closed_trades(df)
    if closed.empty:
        return JournalScore(0.0, dict.fromkeys(SCORE_WEIGHTS, 0.0))
    stats = summary_stats(closed)
    gp, dd = stats.gross_profit, stats.max_drawdown
    dd_score = 0.0 if gp <= 0 else (1 - min(dd / gp, 1.0)) * 100
    components = {
        "Win rate": _scaled(stats.win_rate, WIN_RATE_TARGET),
        "Profit factor": _scaled(stats.profit_factor, PROFIT_FACTOR_TARGET),
        "Avg win/loss": _scaled(stats.avg_win_loss_ratio, WIN_LOSS_RATIO_TARGET),
        "Consistency": consistency(closed) * 100,
        "Max drawdown": dd_score,
    }
    total = sum(components[k] * w for k, w in SCORE_WEIGHTS.items())
    return JournalScore(round(total, 1), {k: round(v, 1) for k, v in components.items()})


# --------------------------------------------------------------------------- breakdowns

BREAKDOWN_COLUMNS = [
    "trades",
    "wins",
    "losses",
    "win_rate",
    "net_pnl",
    "avg_pnl",
    "gross_profit",
    "gross_loss",
    "profit_factor",
]


def _group_stats(pnl: pd.Series) -> dict[str, float | int | None]:
    stats = _pnl_stats(pnl)
    stats["trades"] = stats.pop("total_trades")
    stats["avg_pnl"] = stats["expectancy"]
    return {k: stats[k] for k in BREAKDOWN_COLUMNS}


def breakdown(
    df: pd.DataFrame,
    by: str | Callable[[pd.DataFrame], pd.Series],
    *,
    explode: bool = False,
    sort_by: str | None = None,
    ascending: bool = False,
) -> pd.DataFrame:
    """Per-group stats (trades, win rate, net P&L, ...) of closed trades.

    ``by`` is a column name or a function returning a Series of group keys. With
    ``explode=True`` the column holds lists (e.g. tags) and a trade counts towards
    every group it belongs to. Categorical keys keep their category order unless
    ``sort_by`` is given. Columns are :data:`BREAKDOWN_COLUMNS` plus ``group``.
    """
    closed = closed_trades(df)
    if closed.empty:
        return pd.DataFrame(columns=["group", *BREAKDOWN_COLUMNS])
    keys = closed[by] if isinstance(by, str) else by(closed)
    frame = closed.assign(_group=keys.values)
    if explode:
        frame = frame.explode("_group")
    frame = frame[frame["_group"].notna()]
    if frame.empty:
        return pd.DataFrame(columns=["group", *BREAKDOWN_COLUMNS])
    pnl = frame["net_pnl"].astype(float)
    rows = [
        {"group": key, **_group_stats(group)}
        for key, group in pnl.groupby(frame["_group"], observed=True, sort=True)
    ]
    result = pd.DataFrame(rows, columns=["group", *BREAKDOWN_COLUMNS])
    if sort_by:
        result = result.sort_values(sort_by, ascending=ascending, kind="stable")
    return result.reset_index(drop=True)


def rule_adherence(df: pd.DataFrame) -> pd.Series:
    """Label each trade by playbook checklist adherence.

    Needs ``checklist_total`` and ``checklist_met`` columns. Trades without a
    checklist get None (and drop out of a breakdown).
    """
    if df.empty:
        return pd.Series(dtype=object)
    total, met = df["checklist_total"], df["checklist_met"]
    labels = pd.Series(None, index=df.index, dtype=object)
    labels[(total > 0) & (met == total)] = "All criteria met"
    labels[(total > 0) & (met < total)] = "Some criteria missed"
    return labels


def playbook_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Per-playbook breakdown (trades taken, win rate, net P&L, expectancy, ...)."""
    return breakdown(df, "playbook", sort_by="net_pnl")


# --------------------------------------------------------------------------- calendar


def month_grid(year: int, month: int) -> list[list[date | None]]:
    """Weeks (Monday-first) of a month; days outside the month are None."""
    return [
        [d if d.month == month else None for d in week]
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    ]
