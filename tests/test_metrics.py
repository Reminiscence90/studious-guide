from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from core import metrics as m


def make_trades(pnls: list[float], start: datetime | None = None, **extra: list) -> pd.DataFrame:
    """One closed trade per P&L value, one per day, held 30 minutes."""
    start = start or datetime(2026, 6, 1, 9, 30)
    entries = [start + timedelta(days=i) for i in range(len(pnls))]
    df = pd.DataFrame(
        {
            "net_pnl": pnls,
            "entry_time": entries,
            "exit_time": [e + timedelta(minutes=30) for e in entries],
            "status": ["closed"] * len(pnls),
        }
    )
    for col, values in extra.items():
        df[col] = values
    return df


EMPTY = make_trades([])


# --------------------------------------------------------------------------- headline stats


def test_basic_stats() -> None:
    df = make_trades([100, -50, 200, -25, 0])
    assert m.total_trades(df) == 5
    assert m.net_pnl(df) == 225
    assert m.gross_profit(df) == 300
    assert m.gross_loss(df) == -75
    assert m.win_rate(df) == pytest.approx(0.4)
    assert m.loss_rate(df) == pytest.approx(0.4)
    assert m.profit_factor(df) == pytest.approx(4.0)
    assert m.avg_win(df) == pytest.approx(150)
    assert m.avg_loss(df) == pytest.approx(37.5)
    assert m.avg_win_loss_ratio(df) == pytest.approx(4.0)


def test_expectancy_matches_definition_and_mean_pnl() -> None:
    df = make_trades([100, -50, 200, -25, 0])
    expected = 0.4 * 150 - 0.4 * 37.5
    assert m.expectancy(df) == pytest.approx(expected)
    assert m.expectancy(df) == pytest.approx(df["net_pnl"].mean())


def test_no_trades() -> None:
    stats = m.summary_stats(EMPTY)
    assert stats.total_trades == 0
    assert stats.net_pnl == 0
    assert stats.win_rate is None
    assert stats.profit_factor is None
    assert stats.avg_win is None
    assert stats.avg_loss is None
    assert stats.avg_win_loss_ratio is None
    assert stats.expectancy is None
    assert stats.max_drawdown == 0
    assert m.equity_curve(EMPTY).empty
    assert m.daily_pnl(EMPTY).empty


def test_no_losses_gives_infinite_profit_factor() -> None:
    df = make_trades([10, 20])
    assert m.profit_factor(df) == math.inf
    assert m.avg_loss(df) is None
    assert m.avg_win_loss_ratio(df) == math.inf
    assert m.expectancy(df) == pytest.approx(15)


def test_no_wins() -> None:
    df = make_trades([-10, -30])
    assert m.profit_factor(df) == 0
    assert m.win_rate(df) == 0
    assert m.avg_win_loss_ratio(df) == 0
    assert m.expectancy(df) == pytest.approx(-20)


def test_only_breakeven_trades() -> None:
    df = make_trades([0, 0])
    assert m.profit_factor(df) is None
    assert m.avg_win_loss_ratio(df) is None
    assert m.win_rate(df) == 0
    assert m.expectancy(df) == 0


def test_open_positions_are_excluded() -> None:
    df = make_trades([100, -40, 999])
    df.loc[2, "status"] = "open"
    df.loc[2, "exit_time"] = pd.NaT
    assert m.total_trades(df) == 2
    assert m.net_pnl(df) == 60
    assert m.summary_stats(df).total_trades == 2
    assert len(m.equity_curve(df)) == 2


def test_rows_without_exit_time_are_treated_as_open() -> None:
    df = make_trades([5, 7]).drop(columns="status")
    df.loc[1, "exit_time"] = pd.NaT
    assert m.net_pnl(df) == 5


# --------------------------------------------------------------------------- equity & drawdown


def test_equity_curve_and_drawdown() -> None:
    df = make_trades([100, -30, -50, 120, -200, 50])
    curve = m.equity_curve(df)
    assert curve["cumulative_pnl"].tolist() == [100, 70, 20, 140, -60, -10]
    assert curve["drawdown"].tolist() == [0, -30, -80, 0, -200, -150]
    assert m.max_drawdown(df) == 200


def test_drawdown_measured_from_zero_when_first_trade_loses() -> None:
    assert m.max_drawdown(make_trades([-100, 50])) == 100


def test_drawdown_zero_when_only_winning() -> None:
    assert m.max_drawdown(make_trades([1, 2, 3])) == 0


def test_equity_curve_sorted_by_exit_time() -> None:
    df = make_trades([1, 2, 3]).iloc[::-1]
    assert m.equity_curve(df)["net_pnl"].tolist() == [1, 2, 3]


def test_daily_pnl_groups_by_exit_date() -> None:
    df = make_trades([10, -5, 20])
    df.loc[1, "exit_time"] = df.loc[0, "exit_time"] + timedelta(hours=1)
    daily = m.daily_pnl(df)
    assert daily["date"].tolist() == [date(2026, 6, 1), date(2026, 6, 3)]
    assert daily["net_pnl"].tolist() == [5, 20]
    assert daily["trades"].tolist() == [2, 1]
    assert daily["wins"].tolist() == [1, 1]
    assert daily["cumulative_pnl"].tolist() == [5, 25]


def test_weekly_totals() -> None:
    # 2026-06-01 is a Monday; 8 consecutive days span two weeks.
    daily = m.daily_pnl(make_trades([1] * 8))
    weekly = m.weekly_totals(daily)
    assert weekly["week_start"].tolist() == [date(2026, 6, 1), date(2026, 6, 8)]
    assert weekly["net_pnl"].tolist() == [7, 1]


# --------------------------------------------------------------------------- per trade


def test_r_multiple() -> None:
    assert m.r_multiple(200, entry_price=10, stop_loss=9, quantity=100) == pytest.approx(2)
    assert m.r_multiple(-50, entry_price=10, stop_loss=10.5, quantity=100) == pytest.approx(-1)
    assert m.r_multiple(10, 10, None, 100) is None
    assert m.r_multiple(10, 10, 10, 100) is None


def test_r_multiples_vectorised() -> None:
    df = pd.DataFrame(
        {
            "net_pnl": [200.0, 50.0, 10.0],
            "avg_entry_price": [10.0, 10.0, 10.0],
            "stop_loss": [9.0, None, 10.0],
            "quantity": [100.0, 100.0, 100.0],
        }
    )
    r = m.r_multiples(df)
    assert r.iloc[0] == pytest.approx(2)
    assert pd.isna(r.iloc[1]) and pd.isna(r.iloc[2])


@pytest.mark.parametrize(
    ("minutes", "label"),
    [(0.5, "< 1 min"), (1, "1–5 min"), (30, "15–60 min"), (60 * 5, "4–24 hours"),
     (60 * 24 * 3, "1–7 days"), (60 * 24 * 30, "> 7 days"), (None, None)],
)  # fmt: skip
def test_holding_bucket(minutes: float | None, label: str | None) -> None:
    assert m.holding_bucket(minutes) == label


def test_add_dimensions() -> None:
    df = make_trades([1, 2], start=datetime(2026, 6, 5, 14, 45))  # Friday
    out = m.add_dimensions(df)
    assert out["weekday"].tolist() == ["Friday", "Saturday"]
    assert out["hour"].tolist() == [14, 14]
    assert out["month"].tolist() == ["2026-06", "2026-06"]
    assert out["holding_bucket"].tolist() == ["15–60 min"] * 2


def test_add_dimensions_empty() -> None:
    assert "weekday" in m.add_dimensions(EMPTY).columns


# --------------------------------------------------------------------------- journal score


def test_consistency() -> None:
    assert m.consistency(make_trades([100])) == 0
    assert m.consistency(make_trades([100, 100, 100, 100])) == pytest.approx(0.75)
    assert m.consistency(make_trades([-10, -20])) == 0


def test_journal_score_empty() -> None:
    score = m.journal_score(EMPTY)
    assert score.total == 0
    assert set(score.components) == set(m.SCORE_WEIGHTS)


def test_journal_score_perfect_trader() -> None:
    # Many equal green days, no losses -> every component at or near 100.
    score = m.journal_score(make_trades([100] * 20))
    assert score.components["Win rate"] == 100
    assert score.components["Profit factor"] == 100
    assert score.components["Avg win/loss"] == 100
    assert score.components["Max drawdown"] == 100
    assert score.components["Consistency"] == pytest.approx(95)
    assert score.total == pytest.approx(99.2)


def test_journal_score_formula() -> None:
    df = make_trades([100, -50, 200, -25, 0])
    s = m.journal_score(df)
    # win rate 40% / 60% target; PF 4 (capped); ratio 4 (capped); consistency 1 - 200/300;
    # drawdown: max dd 50 over gross profit 300.
    expected = {
        "Win rate": 40 / 60 * 100,
        "Profit factor": 100,
        "Avg win/loss": 100,
        "Consistency": (1 - 200 / 300) * 100,
        "Max drawdown": (1 - 50 / 300) * 100,
    }
    for k, v in expected.items():
        assert s.components[k] == pytest.approx(v, abs=0.1)
    total = sum(expected[k] * w for k, w in m.SCORE_WEIGHTS.items())
    assert s.total == pytest.approx(total, abs=0.1)


def test_journal_score_weights_sum_to_one() -> None:
    assert sum(m.SCORE_WEIGHTS.values()) == pytest.approx(1)


def test_journal_score_all_losses_is_zero() -> None:
    assert m.journal_score(make_trades([-1, -2])).total == 0


# --------------------------------------------------------------------------- breakdowns


def test_breakdown_by_column() -> None:
    df = make_trades([100, -50, 30, -10], symbol=["A", "A", "B", "B"])
    out = m.breakdown(df, "symbol").set_index("group")
    assert out.loc["A", "trades"] == 2
    assert out.loc["A", "net_pnl"] == 50
    assert out.loc["A", "win_rate"] == 0.5
    assert out.loc["B", "profit_factor"] == pytest.approx(3)


def test_breakdown_explodes_list_columns() -> None:
    df = make_trades([100, -50, -30], tags=[["Breakout", "Calm"], ["Breakout"], []])
    out = m.breakdown(df, "tags", explode=True, sort_by="net_pnl").set_index("group")
    assert out.loc["Breakout", "trades"] == 2
    assert out.loc["Breakout", "net_pnl"] == 50
    assert out.loc["Calm", "net_pnl"] == 100
    assert list(out.index) == ["Calm", "Breakout"]


def test_breakdown_keeps_category_order() -> None:
    df = m.add_dimensions(make_trades([1, 2, 3], start=datetime(2026, 6, 3, 10)))
    out = m.breakdown(df, "weekday")
    assert out["group"].tolist() == ["Wednesday", "Thursday", "Friday"]


def test_breakdown_empty_and_excludes_open() -> None:
    assert m.breakdown(EMPTY, "symbol" if "symbol" in EMPTY else "status").empty
    df = make_trades([5, 10], symbol=["A", "A"])
    df.loc[1, "status"] = "open"
    assert m.breakdown(df, "symbol")["trades"].tolist() == [1]


def test_breakdown_with_function_key() -> None:
    df = make_trades([5, -10, 20])
    out = m.breakdown(df, lambda d: d["net_pnl"].gt(0).map({True: "win", False: "loss"}))
    assert set(out["group"]) == {"win", "loss"}


def test_month_grid() -> None:
    grid = m.month_grid(2026, 6)
    assert grid[0][0] == date(2026, 6, 1)  # June 2026 starts on a Monday
    assert all(len(week) == 7 for week in grid)
    days = [d for week in grid for d in week if d]
    assert len(days) == 30
