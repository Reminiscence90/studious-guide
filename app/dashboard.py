"""Dashboard: headline stats, equity curve, daily P&L and the journal score."""

from __future__ import annotations

import streamlit as st

from app.charts import daily_pnl_figure, equity_curve_figure, journal_score_radar
from app.common import (
    fmt_pct,
    fmt_ratio,
    fmt_tile_money,
    get_filters,
    load_filtered_trades,
    stat_tile,
)
from core import metrics as m

filters = get_filters()
cur = filters.currency
df = load_filtered_trades(filters)
stats = m.summary_stats(df)
score = m.journal_score(df)
open_count = int((df["status"] == "open").sum()) if not df.empty else 0

st.title("Dashboard")
st.caption(
    f"{filters.label} · {stats.total_trades} closed trades · amounts in {cur}"
    + (f" · {open_count} open positions excluded from stats" if open_count else "")
)

if stats.total_trades == 0:
    st.info("No closed trades in this range. Import trades or widen the date filter.")

row1 = st.columns(5)
with row1[0]:
    stat_tile("Net P&L", fmt_tile_money(stats.net_pnl), "Sum of net P&L (after fees)")
with row1[1]:
    stat_tile("Win rate", fmt_pct(stats.win_rate), "Winning trades / closed trades")
with row1[2]:
    stat_tile("Profit factor", fmt_ratio(stats.profit_factor), "Gross profit / |gross loss|")
with row1[3]:
    stat_tile(
        "Expectancy",
        fmt_tile_money(stats.expectancy),
        "Win rate × avg win − loss rate × avg loss (per trade)",
    )
with row1[4]:
    stat_tile("Total trades", f"{stats.total_trades}", f"{stats.wins} W / {stats.losses} L")

row2 = st.columns(5)
with row2[0]:
    stat_tile("Avg win", fmt_tile_money(stats.avg_win, signed=False))
with row2[1]:
    stat_tile("Avg loss", fmt_tile_money(stats.avg_loss, signed=False))
with row2[2]:
    stat_tile("Avg win / loss", fmt_ratio(stats.avg_win_loss_ratio), "Avg win ÷ avg loss")
with row2[3]:
    stat_tile(
        "Max drawdown",
        fmt_tile_money(-stats.max_drawdown if stats.max_drawdown else 0.0),
        "Largest peak-to-trough fall of cumulative P&L",
    )
with row2[4]:
    stat_tile("Journal score", f"{score.total:.0f} / 100", "See the radar chart and the README")

left, right = st.columns([2, 1])
with left:
    st.subheader("Cumulative P&L")
    st.plotly_chart(equity_curve_figure(m.equity_curve(df), cur), width="stretch")
with right:
    st.subheader(f"Journal score · {score.total:.0f}")
    st.plotly_chart(journal_score_radar(score), width="stretch")

st.subheader("Daily P&L")
daily = m.daily_pnl(df)
st.plotly_chart(daily_pnl_figure(daily, cur), width="stretch")

with st.expander("Journal score breakdown"):
    st.dataframe(
        {
            "Component": list(score.components),
            "Score (0–100)": list(score.components.values()),
            "Weight": [f"{w:.0%}" for w in m.SCORE_WEIGHTS.values()],
        },
        hide_index=True,
    )
